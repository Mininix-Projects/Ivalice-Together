from __future__ import annotations

import mmap
import math
import os
import struct
import time
import zlib
from collections.abc import Iterable, Sequence

import runtime_paths

ROLE_ENV = "FF12_COOP_CONTROLLER_BROKER_ROLE"
ROLE_WRITER = "writer"
ROLE_READER = "reader"

PLAYER_COUNT = 3
FORMAT_VERSION = 2
MAGIC = 0x43544649  # "IFTC" in little-endian memory order.
MAX_SNAPSHOT_AGE_SECONDS = 1.0

_HEADER = struct.Struct("<IIIQQIIIII")
_CHECKSUM_HEADER = struct.Struct("<IIQQIIII")
_SEQUENCE_OFFSET = 8
_CONNECTED_MASK = (1 << PLAYER_COUNT) - 1


def configured_role() -> str:
    value = os.environ.get(ROLE_ENV, "").strip().casefold()
    return value if value in (ROLE_WRITER, ROLE_READER) else "direct"


class ControllerStateChannel:
    """Process-shared snapshot of normalized controller strengths.

    Only the movement helper opens SDL devices. The headless menu consumes the
    same per-player values from this Windows named mapping, so a HID controller
    is never polled independently by two co-op helpers.
    """

    def __init__(self, control_count: int) -> None:
        if control_count < 1:
            raise ValueError("control_count must be positive")
        self.control_count = int(control_count)
        self._payload = struct.Struct(f"<{PLAYER_COUNT * self.control_count}f")
        self.size = _HEADER.size + self._payload.size
        self._mapping: mmap.mmap | None = None
        self._sequence = 0
        # A process restart can reuse both the named mapping and sequence 2.
        # Publish a process-instance identity so readers can absorb controls
        # inherited across a writer restart instead of manufacturing edges.
        self._writer_instance_id = (
            (os.getpid() << 32) ^ time.monotonic_ns() ^ id(self)
        ) & 0xFFFFFFFFFFFFFFFF
        if self._writer_instance_id == 0:
            self._writer_instance_id = 1
        self.sample_writer_id: int | None = None
        self.sample_connected_mask = 0
        self.sample_generations = (0,) * PLAYER_COUNT

    def open(self) -> bool:
        if self._mapping is not None:
            return True
        if os.name != "nt":
            return False
        try:
            game_pid = int(os.environ.get(runtime_paths.GAME_PID_ENV, "0"))
        except (TypeError, ValueError):
            return False
        if game_pid <= 0:
            return False
        tag = (
            f"Local\\IvaliceTogetherControllerState_{game_pid}"
            f"_v{FORMAT_VERSION}"
        )
        try:
            self._mapping = mmap.mmap(
                -1,
                self.size,
                tagname=tag,
                access=mmap.ACCESS_WRITE,
            )
        except (OSError, TypeError, ValueError):
            self._mapping = None
        return self._mapping is not None

    def close(self) -> None:
        mapping, self._mapping = self._mapping, None
        self.sample_writer_id = None
        self.sample_connected_mask = 0
        self.sample_generations = (0,) * PLAYER_COUNT
        if mapping is not None:
            try:
                mapping.close()
            except (BufferError, OSError):
                pass

    def publish(
        self,
        rows: Iterable[Sequence[float]],
        *,
        connected: Sequence[bool] | None = None,
        generations: Sequence[int] | None = None,
    ) -> bool:
        if not self.open():
            return False
        normalized_rows = tuple(tuple(float(value) for value in row) for row in rows)
        if len(normalized_rows) != PLAYER_COUNT or any(
            len(row) != self.control_count for row in normalized_rows
        ):
            raise ValueError("controller snapshot shape does not match the channel")
        if connected is None:
            connected_values = (True,) * PLAYER_COUNT
        else:
            connected_values = tuple(bool(value) for value in connected)
            if len(connected_values) != PLAYER_COUNT:
                raise ValueError("controller connection shape does not match the channel")
        if generations is None:
            generation_values = (0,) * PLAYER_COUNT
        else:
            generation_values = tuple(generations)
            if len(generation_values) != PLAYER_COUNT or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 0xFFFFFFFF
                for value in generation_values
            ):
                raise ValueError("controller generation shape does not match the channel")

        values = tuple(
            min(1.0, max(0.0, value)) for row in normalized_rows for value in row
        )
        connected_mask = sum(
            1 << player
            for player, is_connected in enumerate(connected_values)
            if is_connected
        )
        self._sequence = (self._sequence + 2) & 0xFFFFFFFE
        if self._sequence == 0:
            self._sequence = 2
        writing_sequence = self._sequence | 1
        written_at = time.monotonic_ns()
        payload = self._payload.pack(*values)
        checksum = zlib.crc32(
            _CHECKSUM_HEADER.pack(
                MAGIC,
                FORMAT_VERSION,
                self._writer_instance_id,
                written_at,
                connected_mask,
                *generation_values,
            )
            + payload
        ) & 0xFFFFFFFF
        snapshot = _HEADER.pack(
            MAGIC,
            FORMAT_VERSION,
            writing_sequence,
            self._writer_instance_id,
            written_at,
            connected_mask,
            *generation_values,
            checksum,
        ) + payload

        if self._mapping is None:
            return False
        try:
            self._mapping.seek(0)
            self._mapping.write(snapshot)
            self._mapping.seek(_SEQUENCE_OFFSET)
            self._mapping.write(struct.pack("<I", self._sequence))
            return True
        except (BufferError, OSError, ValueError):
            self.close()
            return False

    def read(self) -> tuple[tuple[float, ...], ...] | None:
        if not self.open():
            return None
        if self._mapping is None:
            return None
        for _attempt in range(3):
            try:
                self._mapping.seek(0)
                snapshot = self._mapping.read(self.size)
                if len(snapshot) != self.size:
                    self.close()
                    return None
                (
                    magic,
                    version,
                    sequence,
                    writer_id,
                    written_at,
                    connected_mask,
                    *generations,
                    checksum,
                ) = _HEADER.unpack_from(snapshot)
                if sequence & 1:
                    continue
                if (
                    magic != MAGIC
                    or version != FORMAT_VERSION
                    or writer_id == 0
                    or connected_mask & ~_CONNECTED_MASK
                ):
                    self.close()
                    return None
                self._mapping.seek(_SEQUENCE_OFFSET)
                final_sequence = struct.unpack("<I", self._mapping.read(4))[0]
            except (BufferError, OSError, struct.error, ValueError):
                self.close()
                return None
            if sequence != final_sequence or final_sequence & 1:
                continue

            payload = snapshot[_HEADER.size :]
            expected_checksum = zlib.crc32(
                _CHECKSUM_HEADER.pack(
                    magic,
                    version,
                    writer_id,
                    written_at,
                    connected_mask,
                    *generations,
                )
                + payload
            ) & 0xFFFFFFFF
            if checksum != expected_checksum:
                self.close()
                return None

            age_ns = time.monotonic_ns() - written_at
            if age_ns < 0 or age_ns > int(MAX_SNAPSHOT_AGE_SECONDS * 1e9):
                # If this reader is the final owner of an abandoned or older
                # mapping, dropping the handle lets the next writer create a
                # correctly sized current-generation channel.
                self.close()
                return None
            values = self._payload.unpack(payload)
            if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
                self.close()
                return None
            self.sample_writer_id = writer_id
            self.sample_connected_mask = connected_mask
            self.sample_generations = tuple(generations)
            return tuple(
                tuple(
                    values[
                        player * self.control_count : (player + 1) * self.control_count
                    ]
                )
                for player in range(PLAYER_COUNT)
            )
        return None


__all__ = [
    "ControllerStateChannel",
    "ROLE_ENV",
    "ROLE_READER",
    "ROLE_WRITER",
    "configured_role",
]
