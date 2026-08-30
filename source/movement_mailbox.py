"""Atomic movement commands shared with the native movement hook.

The game and the Python worker run concurrently.  A single aligned scalar is
safe to read, but a movement command is not a scalar: actor, physics object,
headings and magnitude must all describe the same sample.  This module owns the
small seqlock record used at that boundary.  Process-memory I/O remains supplied
by the caller so the state and publication rules are independently testable.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any


SEQUENCE_OFFSET = 0x00
ACTIVE_OFFSET = 0x04
ACTOR_OFFSET = 0x08
PHYS_OFFSET = 0x10
MEMBER_ID_OFFSET = 0x18
ACTOR_GENERATION_OFFSET = 0x1C
WORLD_EPOCH_OFFSET = 0x20
HEADING_OFFSET = 0x24
VISUAL_HEADING_OFFSET = 0x28
MAGNITUDE_OFFSET = 0x2C
MAILBOX_SIZE = 0x30

# Readers that encounter an in-progress publication need the previous complete
# heading, not the unrelated value the native follower controller calculated in
# the meantime.  The producer refreshes this fallback *before* marking the main
# record odd.  A fallback-local seqlock also detects a lagging reader that
# overlaps the *next* publication; native consumers then fail open for that
# frame.  They must never spin on a publisher-owned sequence because a stopped
# helper can leave an odd value indefinitely.
FALLBACK_ACTIVE_OFFSET = 0x30
FALLBACK_SOURCE_SEQUENCE_OFFSET = 0x34
FALLBACK_ACTOR_OFFSET = 0x38
FALLBACK_PHYS_OFFSET = 0x40
FALLBACK_HEADING_OFFSET = 0x48
FALLBACK_VISUAL_HEADING_OFFSET = 0x4C
FALLBACK_PAYLOAD_SIZE = 0x20
FALLBACK_SEQUENCE_OFFSET = 0x50
FALLBACK_SIZE = 0x24
# The visual-facing helper accepts a pointer, so native code copies either the
# current or fallback scalar into this private word only after it has validated
# a complete record.  Python never writes this word.
NATIVE_SELECTED_VISUAL_OFFSET = 0x54
MAILBOX_STORAGE_SIZE = NATIVE_SELECTED_VISUAL_OFFSET + 0x04

INACTIVE_MEMBER_ID = 0xFFFFFFFF

_SEQUENCE = struct.Struct("<I")
_PAYLOAD = struct.Struct("<IQQIIIfff")
_FALLBACK = struct.Struct("<IIQQff")

assert _PAYLOAD.size == MAILBOX_SIZE - 4
assert _FALLBACK.size == FALLBACK_PAYLOAD_SIZE


@dataclass(frozen=True)
class MovementCommand:
    active: bool
    actor: int
    phys: int
    member_id: int
    actor_generation: int
    world_epoch: int
    heading: float
    visual_heading: float
    magnitude: float

    def __post_init__(self) -> None:
        for name in ("actor", "phys"):
            value = int(getattr(self, name))
            if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"{name} is outside uint64")
        for name in ("member_id", "actor_generation", "world_epoch"):
            value = int(getattr(self, name))
            if not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"{name} is outside uint32")
        for name in ("heading", "visual_heading", "magnitude"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if self.active and (not self.actor or not self.phys):
            raise ValueError("an active movement command needs actor and phys")

    @classmethod
    def inactive(
        cls,
        *,
        heading: float = 0.0,
        visual_heading: float = 0.0,
        world_epoch: int = 0,
    ) -> "MovementCommand":
        return cls(
            False,
            0,
            0,
            INACTIVE_MEMBER_ID,
            0,
            int(world_epoch) & 0xFFFFFFFF,
            float(heading),
            float(visual_heading),
            0.0,
        )

    def payload(self) -> bytes:
        return _PAYLOAD.pack(
            int(bool(self.active)),
            int(self.actor) if self.active else 0,
            int(self.phys) if self.active else 0,
            int(self.member_id) if self.active else INACTIVE_MEMBER_ID,
            int(self.actor_generation) if self.active else 0,
            int(self.world_epoch),
            float(self.heading),
            float(self.visual_heading),
            float(self.magnitude) if self.active else 0.0,
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "MovementCommand":
        (
            active,
            actor,
            phys,
            member_id,
            actor_generation,
            world_epoch,
            heading,
            visual_heading,
            magnitude,
        ) = _PAYLOAD.unpack(payload)
        return cls(
            bool(active),
            actor,
            phys,
            member_id,
            actor_generation,
            world_epoch,
            heading,
            visual_heading,
            magnitude,
        )


@dataclass(frozen=True)
class StagedMovementPublication:
    """A payload prepared for a game-thread commit.

    While a publication is staged the main sequence remains odd and native
    readers deliberately consume the previous complete command from the
    fallback record.  The game-thread leader bridge publishes ``sequence`` only
    after the stock leader setter returns, making actor ownership and native
    leadership change at one frame boundary.
    """

    command: MovementCommand
    payload: bytes
    previous_command: MovementCommand | None
    previous_payload: bytes | None
    sequence: int
    fallback_sequence: int


@dataclass(frozen=True)
class ReservedMovementPublication:
    """A command journaled elsewhere for an atomic native publication."""

    command: MovementCommand
    payload: bytes
    sequence: int


class MovementMailboxPublisher:
    """Publish complete commands and suppress byte-identical repetitions."""

    def __init__(self, process: Any, address: int):
        self.process = process
        self.address = int(address)
        self.sequence = 0
        self.fallback_sequence = 0
        self.last_command: MovementCommand | None = None
        self.last_payload: bytes | None = None
        self.staged: StagedMovementPublication | None = None
        self.reserved: ReservedMovementPublication | None = None

    @staticmethod
    def _next_even(sequence: int) -> int:
        committed = (int(sequence) + 2) & 0xFFFFFFFE
        return committed or 2

    def _publish_fallback(self) -> int:
        previous = self.last_command
        fallback_active = bool(
            previous is not None
            and previous.active
            and self.sequence
            and not self.sequence & 1
        )
        fallback_committed = self._next_even(self.fallback_sequence)
        self.process.write(
            self.address + FALLBACK_SEQUENCE_OFFSET,
            _SEQUENCE.pack(fallback_committed | 1),
        )
        self.process.write(
            self.address + FALLBACK_ACTIVE_OFFSET,
            _FALLBACK.pack(
                int(fallback_active),
                int(self.sequence) if fallback_active else 0,
                int(previous.actor) if fallback_active else 0,
                int(previous.phys) if fallback_active else 0,
                float(previous.heading) if fallback_active else 0.0,
                float(previous.visual_heading) if fallback_active else 0.0,
            ),
        )
        self.process.write(
            self.address + FALLBACK_SEQUENCE_OFFSET,
            _SEQUENCE.pack(fallback_committed),
        )
        return fallback_committed

    def stage(self, command: MovementCommand) -> StagedMovementPublication:
        """Prepare ``command`` but keep the previous command observable."""

        payload = command.payload()
        if self.staged is not None:
            if self.staged.payload == payload:
                return self.staged
            raise RuntimeError("movement mailbox already has a staged publication")

        fallback_committed = self._publish_fallback()
        committed = self._next_even(self.sequence)
        self.process.write(
            self.address + SEQUENCE_OFFSET,
            _SEQUENCE.pack(committed | 1),
        )
        self.process.write(self.address + ACTIVE_OFFSET, payload)
        self.staged = StagedMovementPublication(
            command=command,
            payload=payload,
            previous_command=self.last_command,
            previous_payload=self.last_payload,
            sequence=committed,
            fallback_sequence=fallback_committed,
        )
        return self.staged

    def reserve(self, command: MovementCommand) -> ReservedMovementPublication:
        """Freeze ordinary publication while a native journal owns the next seq."""

        payload = command.payload()
        if self.staged is not None:
            raise RuntimeError("cannot reserve a staged movement mailbox")
        if self.reserved is not None:
            if self.reserved.payload == payload:
                return self.reserved
            raise RuntimeError("movement mailbox already has a reservation")
        self.reserved = ReservedMovementPublication(
            command,
            payload,
            self._next_even(self.sequence),
        )
        return self.reserved

    def finalize_reserved(self, expected_sequence: int | None = None) -> bool:
        reserved = self.reserved
        if reserved is None:
            return False
        if expected_sequence is not None and int(expected_sequence) != reserved.sequence:
            raise RuntimeError("reserved movement sequence does not match transaction")
        committed = read_consistent(self.process, self.address, attempts=1)
        if committed is None:
            return False
        sequence, command = committed
        if sequence != reserved.sequence or command.payload() != reserved.payload:
            return False
        self.sequence = sequence
        self.last_command = command
        self.last_payload = reserved.payload
        self.reserved = None
        return True

    def cancel_reserved(self) -> bool:
        if self.reserved is None:
            return False
        self.reserved = None
        return True

    def adopt_committed(self, expected_sequence: int) -> bool:
        """Adopt a native-journal publication after a worker restart."""

        committed = read_consistent(self.process, self.address, attempts=2)
        if committed is None:
            return False
        sequence, command = committed
        if sequence != int(expected_sequence):
            return False
        self.sequence = sequence
        self.last_command = command
        self.last_payload = command.payload()
        if self.reserved is not None and self.reserved.sequence == sequence:
            self.reserved = None
        return True

    def adopt_current(self) -> bool:
        """Synchronize publisher bookkeeping without changing process memory."""

        committed = read_consistent(self.process, self.address, attempts=2)
        if committed is None:
            return False
        sequence, command = committed
        self.sequence = sequence
        self.last_command = command
        self.last_payload = command.payload()
        return True

    def finalize_staged(self, expected_sequence: int | None = None) -> bool:
        """Adopt a staged payload after the native bridge committed it."""

        staged = self.staged
        if staged is None:
            return False
        if expected_sequence is not None and int(expected_sequence) != staged.sequence:
            raise RuntimeError("staged movement sequence does not match transaction")
        visible = _SEQUENCE.unpack(
            self.process.read(self.address + SEQUENCE_OFFSET, _SEQUENCE.size)
        )[0]
        if visible != staged.sequence:
            return False
        self.sequence = staged.sequence
        self.fallback_sequence = staged.fallback_sequence
        self.last_command = staged.command
        self.last_payload = staged.payload
        self.staged = None
        return True

    def abort_staged(self) -> bool:
        """Rollback an uncommitted stage without exposing an inactive gap.

        If the game thread already published the even commit word, rollback is
        no longer safe: the setter and command became visible together.  In
        that case this method adopts the committed command idempotently.
        """

        staged = self.staged
        if staged is None:
            return False
        visible = _SEQUENCE.unpack(
            self.process.read(self.address + SEQUENCE_OFFSET, _SEQUENCE.size)
        )[0]
        if visible == staged.sequence:
            return self.finalize_staged(staged.sequence)

        previous = staged.previous_command or MovementCommand.inactive(
            world_epoch=staged.command.world_epoch
        )
        previous_payload = previous.payload()
        self.process.write(self.address + ACTIVE_OFFSET, previous_payload)
        self.process.write(
            self.address + SEQUENCE_OFFSET,
            _SEQUENCE.pack(staged.sequence),
        )
        self.sequence = staged.sequence
        self.fallback_sequence = staged.fallback_sequence
        self.last_command = previous
        self.last_payload = previous_payload
        self.staged = None
        return True

    def publish(self, command: MovementCommand) -> bool:
        payload = command.payload()
        # A native leader transaction owns the odd main record until its game-
        # thread commit or an explicit abort.  Ordinary frame publication must
        # never overwrite that prepared payload.
        if self.staged is not None or self.reserved is not None:
            return False
        if payload == self.last_payload:
            return False

        # Keep the main sequence stable while copying the previous complete
        # sample. A fallback-local seqlock covers lagging readers that overlap
        # the next refresh. Only after the fallback commit do we expose an odd
        # main sequence that can select it.
        fallback_committed = self._publish_fallback()
        committed = self._next_even(self.sequence)
        self.process.write(
            self.address + SEQUENCE_OFFSET,
            _SEQUENCE.pack(committed | 1),
        )
        self.process.write(self.address + ACTIVE_OFFSET, payload)
        self.process.write(
            self.address + SEQUENCE_OFFSET,
            _SEQUENCE.pack(committed),
        )
        self.sequence = committed
        self.fallback_sequence = fallback_committed
        self.last_command = command
        self.last_payload = payload
        return True


def publisher_for(process: Any, address: int) -> MovementMailboxPublisher:
    """Return the per-process/per-address publisher used by compatibility APIs."""

    registry = getattr(process, "_movement_mailbox_publishers", None)
    if registry is None:
        registry = {}
        setattr(process, "_movement_mailbox_publishers", registry)
    address = int(address)
    publisher = registry.get(address)
    if publisher is None:
        publisher = MovementMailboxPublisher(process, address)
        registry[address] = publisher
    return publisher


def reset_mailbox(process: Any, address: int) -> MovementMailboxPublisher:
    address = int(address)
    # A helper can restart while the native hook is still executing.  Clearing
    # the whole record with one cross-process copy would not make that copy
    # atomic, and restarting the sequence at a value the old publisher used can
    # let a reader mistake a mixed record for a stable one.  Continue from the
    # sequence already visible in the target and retire it through the same
    # odd/payload/even protocol as every other publication.
    previous_command = None
    try:
        committed = read_consistent(process, address, attempts=1)
    except (AttributeError, OSError, RuntimeError, struct.error, ValueError):
        committed = None
    if committed is not None:
        previous, previous_command = committed
    else:
        try:
            previous = _SEQUENCE.unpack(process.read(address, _SEQUENCE.size))[0]
        except (AttributeError, OSError, RuntimeError, struct.error, ValueError):
            previous = 0
    registry = getattr(process, "_movement_mailbox_publishers", None)
    if registry is not None:
        registry.pop(address, None)
    publisher = publisher_for(process, address)
    publisher.sequence = previous & 0xFFFFFFFE
    try:
        fallback_previous = _SEQUENCE.unpack(
            process.read(address + FALLBACK_SEQUENCE_OFFSET, _SEQUENCE.size)
        )[0]
    except (AttributeError, OSError, RuntimeError, struct.error, ValueError):
        fallback_previous = 0
    publisher.fallback_sequence = fallback_previous & 0xFFFFFFFE
    publisher.last_command = previous_command
    publisher.publish(MovementCommand.inactive())
    return publisher


def read_consistent(
    process: Any,
    address: int,
    *,
    attempts: int = 4,
) -> tuple[int, MovementCommand] | None:
    """Read one committed record, returning ``None`` rather than torn data."""

    for _ in range(max(1, int(attempts))):
        first = _SEQUENCE.unpack(
            process.read(int(address) + SEQUENCE_OFFSET, _SEQUENCE.size)
        )[0]
        if not first or first & 1:
            continue
        payload = process.read(int(address) + ACTIVE_OFFSET, _PAYLOAD.size)
        final = _SEQUENCE.unpack(
            process.read(int(address) + SEQUENCE_OFFSET, _SEQUENCE.size)
        )[0]
        if first == final and not final & 1:
            return final, MovementCommand.from_payload(payload)
    return None
