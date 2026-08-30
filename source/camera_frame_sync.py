"""Frame-synchronous transport for the shared camera.

The movement worker intentionally samples input independently from the game's
render cadence.  Camera goals therefore cross into the injected renderer via a
double-buffered mailbox.  The renderer resamples each complete goal once per
DXGI Present and publishes a second double-buffered snapshot for the native
camera hook.
"""

from __future__ import annotations

import ctypes as ct
import math
import os
import struct
import time
from dataclasses import dataclass
from typing import Callable, Sequence


CAMERA_FRAME_STATE_OFFSET = 0x2000
CAMERA_FRAME_STATE_SIZE = 0x100

CAMERA_FRAME_MAGIC = 0x324D4143  # "CAM2"
CAMERA_FRAME_VERSION = 2

CAMERA_GOAL_HEADER_OFFSET = 0x00
CAMERA_GOAL_GENERATION_OFFSET = 0x08
CAMERA_GOAL_ACK_OFFSET = 0x0C
# The renderer publishes the QPC timestamp for each accepted game Present into
# the slot selected by the existing Present generation at cave +0xD90.  Python
# brackets position reads with that generation, so actor velocity is measured
# on the game's real frame clock instead of an assumed 60 Hz grid.
CAMERA_PRESENT_GENERATION_CAVE_OFFSET = 0xD90
CAMERA_PRESENT_TICK_SLOT_OFFSETS = (0x10, 0x18)
CAMERA_GOAL_SLOT_OFFSETS = (0x20, 0x60)
CAMERA_GOAL_SLOT_SIZE = 0x40
CAMERA_GOAL_PAYLOAD = struct.Struct("<IIQ3f3f5fI")

CAMERA_OUTPUT_HEADER_OFFSET = 0xA0
CAMERA_OUTPUT_COMMIT_OFFSET = 0xA8
CAMERA_OUTPUT_GENERATION_OFFSET = CAMERA_OUTPUT_COMMIT_OFFSET
CAMERA_OUTPUT_ACTIVE_OFFSET = CAMERA_OUTPUT_COMMIT_OFFSET + 4
CAMERA_OUTPUT_SLOT_OFFSETS = (0xB0, 0xD0)
CAMERA_OUTPUT_SLOT_SIZE = 0x20
# The committed renderer snapshot is exactly the camera command as eight
# floats: focus XYZ, distance, yaw, pitch, and the collision interval.  The
# adjacent u64 commit packs generation in its low dword and active in its high
# dword, allowing native readers to validate both with one atomic value.
CAMERA_OUTPUT_PAYLOAD = struct.Struct("<8f")

CAMERA_BRIDGE_TIME_CONSTANT_SECONDS = 0.025
CAMERA_BRIDGE_PREDICTION_HORIZON_SECONDS = 0.050
CAMERA_BRIDGE_STALE_SECONDS = 0.250
CAMERA_BRIDGE_MIN_DELTA_SECONDS = 0.001
CAMERA_BRIDGE_MAX_DELTA_SECONDS = 0.050

assert CAMERA_GOAL_PAYLOAD.size == CAMERA_GOAL_SLOT_SIZE
assert CAMERA_OUTPUT_PAYLOAD.size == CAMERA_OUTPUT_SLOT_SIZE
assert CAMERA_OUTPUT_SLOT_OFFSETS[1] + CAMERA_OUTPUT_SLOT_SIZE <= CAMERA_FRAME_STATE_SIZE


if os.name == "nt":
    _kernel32 = ct.WinDLL("kernel32", use_last_error=True)
    _query_performance_counter = _kernel32.QueryPerformanceCounter
    _query_performance_counter.argtypes = [ct.POINTER(ct.c_longlong)]
    _query_performance_counter.restype = ct.c_int
    _query_performance_frequency = _kernel32.QueryPerformanceFrequency
    _query_performance_frequency.argtypes = [ct.POINTER(ct.c_longlong)]
    _query_performance_frequency.restype = ct.c_int
    _performance_frequency = ct.c_longlong()
    if not _query_performance_frequency(ct.byref(_performance_frequency)):
        raise OSError(ct.get_last_error(), "QueryPerformanceFrequency failed")
    _performance_frequency_value = int(_performance_frequency.value)
else:  # pragma: no cover - production is Windows; this keeps unit imports portable.
    _query_performance_counter = None
    _performance_frequency_value = 0


def monotonic_tick_us() -> int:
    """Return QPC-derived microseconds shared with the injected renderer."""

    if _query_performance_counter is not None:
        counter = ct.c_longlong()
        if not _query_performance_counter(ct.byref(counter)):
            raise OSError(ct.get_last_error(), "QueryPerformanceCounter failed")
        ticks = int(counter.value)
        frequency = _performance_frequency_value
        return (ticks // frequency) * 1_000_000 + (
            (ticks % frequency) * 1_000_000
        ) // frequency
    return time.monotonic_ns() // 1_000


def next_u32_generation(current: int) -> int:
    generation = (int(current) + 1) & 0xFFFFFFFF
    # Zero is reserved for "no committed snapshot".  On wrap, jump to two
    # rather than one: the parity must still alternate so the writer never
    # overwrites the slot named by the still-committed old generation.
    return generation or 2


def generation_slot(generation: int, offsets: tuple[int, int]) -> int:
    return offsets[int(generation) & 1]


def _finite_vector(values: Sequence[float], count: int) -> tuple[float, ...]:
    result = tuple(float(value) for value in values[:count])
    if len(result) != count or not all(math.isfinite(value) for value in result):
        raise ValueError("camera frame contains a non-finite or incomplete vector")
    return result


@dataclass(frozen=True)
class CameraGoal:
    active: bool
    reset_epoch: int
    producer_tick_us: int
    focus: tuple[float, float, float]
    velocity: tuple[float, float, float]
    extra_distance: float
    yaw_offset: float
    pitch_offset: float
    collision_exclusion: tuple[float, float]


@dataclass(frozen=True)
class CameraBridgeFrame:
    active: bool
    reset_epoch: int
    present_generation: int
    present_tick_us: int
    focus: tuple[float, float, float]
    extra_distance: float
    advanced: bool


@dataclass(frozen=True)
class CameraPresentSample:
    generation: int
    tick_us: int


class CameraGoalPublisher:
    """Publish whole camera goals without exposing a partially written frame."""

    def __init__(self, tick_us: Callable[[], int] = monotonic_tick_us) -> None:
        self._tick_us = tick_us
        self.generation = 0
        self.reset_epoch = 0
        self._source_token: tuple[int, int] | None = None
        self._initialized_address: int | None = None

    def reset(self) -> None:
        self._source_token = None
        self._initialized_address = None
        self.generation = 0

    def publish(
        self,
        process,
        state_address: int,
        focus: Sequence[float],
        velocity: Sequence[float],
        extra_distance: float,
        yaw_offset: float,
        pitch_offset: float,
        collision_exclusion: Sequence[float],
        *,
        source_reset_epoch: int,
        source_mode: int = 0,
        producer_tick_us: int | None = None,
    ) -> CameraGoal:
        focus_xyz = _finite_vector(focus, 3)
        velocity_xyz = _finite_vector(velocity, 3)
        exclusion = _finite_vector(collision_exclusion, 2)
        scalars = tuple(
            float(value)
            for value in (extra_distance, yaw_offset, pitch_offset)
        )
        if not all(math.isfinite(value) for value in scalars):
            raise ValueError("camera frame contains a non-finite scalar")

        source_token = (
            int(source_reset_epoch) & 0xFFFFFFFF,
            int(source_mode) & 0xFFFFFFFF,
        )
        if source_token != self._source_token:
            self.reset_epoch = next_u32_generation(self.reset_epoch)
            self._source_token = source_token

        state_address = int(state_address)
        if self._initialized_address != state_address:
            header = struct.pack(
                "<IIII4I",
                CAMERA_FRAME_MAGIC,
                CAMERA_FRAME_VERSION,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            process.write(state_address + CAMERA_GOAL_HEADER_OFFSET, header)
            self._initialized_address = state_address
            self.generation = 0

        tick_us = int(self._tick_us() if producer_tick_us is None else producer_tick_us)
        tick_us = max(0, tick_us) & 0xFFFFFFFFFFFFFFFF
        self.generation = next_u32_generation(self.generation)
        payload = CAMERA_GOAL_PAYLOAD.pack(
            1,
            self.reset_epoch,
            tick_us,
            *focus_xyz,
            *velocity_xyz,
            *scalars,
            *exclusion,
            0,
        )
        slot = generation_slot(self.generation, CAMERA_GOAL_SLOT_OFFSETS)
        # The inactive slot is completed first.  The four-byte generation write
        # is the only commit, so a native reader sees either old or new data.
        process.write(state_address + slot, payload)
        process.write(
            state_address + CAMERA_GOAL_GENERATION_OFFSET,
            struct.pack("<I", self.generation),
        )
        return CameraGoal(
            True,
            self.reset_epoch,
            tick_us,
            focus_xyz,  # type: ignore[arg-type]
            velocity_xyz,  # type: ignore[arg-type]
            scalars[0],
            scalars[1],
            scalars[2],
            exclusion,  # type: ignore[arg-type]
        )


def read_committed_goal(process, state_address: int) -> CameraGoal | None:
    header = process.read(state_address + CAMERA_GOAL_HEADER_OFFSET, 12)
    magic, version, generation_before = struct.unpack("<III", header)
    if (
        magic != CAMERA_FRAME_MAGIC
        or version != CAMERA_FRAME_VERSION
        or generation_before == 0
    ):
        return None
    slot = generation_slot(generation_before, CAMERA_GOAL_SLOT_OFFSETS)
    payload = process.read(state_address + slot, CAMERA_GOAL_SLOT_SIZE)
    generation_after = struct.unpack(
        "<I",
        process.read(state_address + CAMERA_GOAL_GENERATION_OFFSET, 4),
    )[0]
    if generation_after != generation_before:
        return None
    (
        active,
        reset_epoch,
        producer_tick_us,
        focus_x,
        focus_y,
        focus_z,
        velocity_x,
        velocity_y,
        velocity_z,
        extra_distance,
        yaw_offset,
        pitch_offset,
        exclusion_start,
        exclusion_end,
        _reserved,
    ) = CAMERA_GOAL_PAYLOAD.unpack(payload)
    values = (
        focus_x,
        focus_y,
        focus_z,
        velocity_x,
        velocity_y,
        velocity_z,
        extra_distance,
        yaw_offset,
        pitch_offset,
        exclusion_start,
        exclusion_end,
    )
    if active != 1 or not all(math.isfinite(value) for value in values):
        return None
    return CameraGoal(
        True,
        reset_epoch,
        producer_tick_us,
        (focus_x, focus_y, focus_z),
        (velocity_x, velocity_y, velocity_z),
        extra_distance,
        yaw_offset,
        pitch_offset,
        (exclusion_start, exclusion_end),
    )


def read_present_sample(
    process,
    cave_address: int,
    state_address: int,
) -> CameraPresentSample | None:
    """Read one renderer Present timestamp with its generation bracket."""

    generation_address = (
        int(cave_address) + CAMERA_PRESENT_GENERATION_CAVE_OFFSET
    )
    generation_before = struct.unpack(
        "<I", process.read(generation_address, 4)
    )[0]
    if generation_before == 0:
        return None
    slot = generation_slot(
        generation_before,
        CAMERA_PRESENT_TICK_SLOT_OFFSETS,
    )
    tick_us = struct.unpack(
        "<Q", process.read(int(state_address) + slot, 8)
    )[0]
    generation_after = struct.unpack(
        "<I", process.read(generation_address, 4)
    )[0]
    if generation_after != generation_before or tick_us == 0:
        return None
    return CameraPresentSample(generation_before, tick_us)


class CameraFrameResampler:
    """Executable model of the Present-side native resampler."""

    def __init__(
        self,
        *,
        time_constant: float = CAMERA_BRIDGE_TIME_CONSTANT_SECONDS,
        prediction_horizon: float = CAMERA_BRIDGE_PREDICTION_HORIZON_SECONDS,
        stale_seconds: float = CAMERA_BRIDGE_STALE_SECONDS,
    ) -> None:
        self.time_constant = float(time_constant)
        self.prediction_horizon = float(prediction_horizon)
        self.stale_seconds = float(stale_seconds)
        self.reset()

    def reset(self) -> None:
        self._focus = (0.0, 0.0, 0.0)
        self._extra_distance = 0.0
        self._reset_epoch: int | None = None
        self._last_present_generation: int | None = None
        self._last_present_tick_us: int | None = None
        self._active = False

    def advance(
        self,
        goal: CameraGoal | None,
        present_generation: int,
        present_tick_us: int,
    ) -> CameraBridgeFrame:
        generation = int(present_generation) & 0xFFFFFFFF
        tick_us = max(0, int(present_tick_us))
        if self._last_present_generation == generation:
            return CameraBridgeFrame(
                self._active,
                self._reset_epoch or 0,
                generation,
                tick_us,
                self._focus,
                self._extra_distance,
                False,
            )
        self._last_present_generation = generation

        if goal is None or not goal.active:
            self._active = False
            self._reset_epoch = None
            self._last_present_tick_us = tick_us
            return CameraBridgeFrame(
                False,
                0,
                generation,
                tick_us,
                self._focus,
                self._extra_distance,
                True,
            )

        age_us = max(0, tick_us - int(goal.producer_tick_us))
        if age_us > int(round(self.stale_seconds * 1_000_000.0)):
            self._active = False
            self._reset_epoch = None
            self._last_present_tick_us = tick_us
            return CameraBridgeFrame(
                False,
                0,
                generation,
                tick_us,
                self._focus,
                self._extra_distance,
                True,
            )

        prediction_age = min(
            age_us / 1_000_000.0,
            self.prediction_horizon,
        )
        desired = tuple(
            goal.focus[index] + goal.velocity[index] * prediction_age
            for index in range(3)
        )
        reset = not self._active or self._reset_epoch != goal.reset_epoch
        if reset:
            self._focus = desired
            self._extra_distance = goal.extra_distance
        else:
            previous_tick_us = (
                tick_us
                if self._last_present_tick_us is None
                else self._last_present_tick_us
            )
            elapsed = max(0, tick_us - int(previous_tick_us))
            dt = min(
                CAMERA_BRIDGE_MAX_DELTA_SECONDS,
                max(CAMERA_BRIDGE_MIN_DELTA_SECONDS, elapsed / 1_000_000.0),
            )
            prediction_live = age_us < int(
                round(self.prediction_horizon * 1_000_000.0)
            )
            velocity = goal.velocity if prediction_live else (0.0, 0.0, 0.0)
            predicted = tuple(
                self._focus[index] + velocity[index] * dt for index in range(3)
            )
            alpha = 1.0 - math.exp(-dt / max(0.001, self.time_constant))
            self._focus = tuple(
                predicted[index] + (desired[index] - predicted[index]) * alpha
                for index in range(3)
            )
            self._extra_distance += (
                goal.extra_distance - self._extra_distance
            ) * alpha
        self._active = True
        self._reset_epoch = goal.reset_epoch
        self._last_present_tick_us = tick_us
        return CameraBridgeFrame(
            True,
            goal.reset_epoch,
            generation,
            tick_us,
            self._focus,
            self._extra_distance,
            True,
        )


__all__ = [
    "CAMERA_BRIDGE_MAX_DELTA_SECONDS",
    "CAMERA_BRIDGE_MIN_DELTA_SECONDS",
    "CAMERA_BRIDGE_PREDICTION_HORIZON_SECONDS",
    "CAMERA_BRIDGE_STALE_SECONDS",
    "CAMERA_BRIDGE_TIME_CONSTANT_SECONDS",
    "CAMERA_FRAME_MAGIC",
    "CAMERA_FRAME_STATE_OFFSET",
    "CAMERA_FRAME_STATE_SIZE",
    "CAMERA_FRAME_VERSION",
    "CAMERA_GOAL_ACK_OFFSET",
    "CAMERA_GOAL_GENERATION_OFFSET",
    "CAMERA_GOAL_HEADER_OFFSET",
    "CAMERA_GOAL_PAYLOAD",
    "CAMERA_GOAL_SLOT_OFFSETS",
    "CAMERA_GOAL_SLOT_SIZE",
    "CAMERA_PRESENT_GENERATION_CAVE_OFFSET",
    "CAMERA_PRESENT_TICK_SLOT_OFFSETS",
    "CAMERA_OUTPUT_ACTIVE_OFFSET",
    "CAMERA_OUTPUT_COMMIT_OFFSET",
    "CAMERA_OUTPUT_GENERATION_OFFSET",
    "CAMERA_OUTPUT_HEADER_OFFSET",
    "CAMERA_OUTPUT_PAYLOAD",
    "CAMERA_OUTPUT_SLOT_OFFSETS",
    "CAMERA_OUTPUT_SLOT_SIZE",
    "CameraBridgeFrame",
    "CameraFrameResampler",
    "CameraGoal",
    "CameraGoalPublisher",
    "CameraPresentSample",
    "generation_slot",
    "monotonic_tick_us",
    "next_u32_generation",
    "read_committed_goal",
    "read_present_sample",
]
