from __future__ import annotations

import struct

import camera_frame_sync
import coop_runtime as coop


class MemoryProcess:
    def __init__(self) -> None:
        self.data: dict[int, int] = {}

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def write(self, address: int, payload: bytes) -> None:
        for offset, value in enumerate(bytes(payload)):
            self.data[address + offset] = value


def camera_command(process: MemoryProcess) -> tuple[float, ...]:
    goal = camera_frame_sync.read_committed_goal(process, coop.CAMERA_FRAME_STATE)
    assert goal is not None
    return (
        *goal.focus,
        goal.extra_distance,
        goal.yaw_offset,
        goal.pitch_offset,
        *goal.collision_exclusion,
    )


def test_camera_command_uses_live_values_without_an_external_hold() -> None:
    process = MemoryProcess()
    coop.write_camera_command(
        process,
        (1.0, 2.0, 3.0),
        4.0,
        5.0,
        6.0,
        (7.0, 8.0),
    )
    assert camera_command(process) == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)


def test_camera_command_stays_frozen_while_the_external_hold_is_armed() -> None:
    process = MemoryProcess()
    held = (11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0)
    process.write(
        coop.third_player_block() + coop.EXTERNAL_CAMERA_HOLD_OFFSET,
        struct.pack("<II8f", coop.EXTERNAL_CAMERA_HOLD_MAGIC, 1, *held),
    )
    goal = coop.write_camera_command(
        process,
        (1.0, 2.0, 3.0),
        4.0,
        5.0,
        6.0,
        (7.0, 8.0),
    )
    assert camera_command(process) == held
    assert goal.velocity == (0.0, 0.0, 0.0)
