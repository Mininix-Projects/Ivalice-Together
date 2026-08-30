"""Execution tests for both collision-aware camera-distance caves."""

from __future__ import annotations

import faulthandler
import struct
import sys
import unittest
from pathlib import Path

try:
    from unicorn import Uc, UcError, UC_ARCH_X86, UC_HOOK_CODE, UC_MODE_64
    from unicorn.x86_const import (
        UC_X86_REG_EFLAGS,
        UC_X86_REG_R10,
        UC_X86_REG_R11,
        UC_X86_REG_R12,
        UC_X86_REG_R13,
        UC_X86_REG_R14,
        UC_X86_REG_R15,
        UC_X86_REG_R8,
        UC_X86_REG_R9,
        UC_X86_REG_RAX,
        UC_X86_REG_RBP,
        UC_X86_REG_RBX,
        UC_X86_REG_RCX,
        UC_X86_REG_RDI,
        UC_X86_REG_RDX,
        UC_X86_REG_RIP,
        UC_X86_REG_RSI,
        UC_X86_REG_RSP,
        UC_X86_REG_XMM0,
        UC_X86_REG_XMM1,
        UC_X86_REG_XMM2,
        UC_X86_REG_XMM3,
        UC_X86_REG_XMM4,
        UC_X86_REG_XMM5,
        UC_X86_REG_XMM6,
    )
except ImportError:  # pragma: no cover - exercised only in minimal dev installs
    Uc = None
    UcError = RuntimeError


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import camera_frame_sync as frame_sync  # noqa: E402
import coop_runtime as movement  # noqa: E402


CAVE_BASE = 0x5BAA0000
STACK = 0x71000000
STACK_SIZE = 0x4000
RIG_PAGE = movement.CAMERA_PRIMARY_RIG & ~0xFFF
SECONDARY_RIG = movement.CAMERA_PRIMARY_RIG + 0x1000

NATIVE_DISTANCE = 4.0
NATIVE_YAW = 0.25
NATIVE_PITCH = -0.5
SITE1_MULTIPLIER = 1.5
SITE2_MULTIPLIER = 1.25
SNAPSHOT = (11.0, 22.0, 33.0, 2.0, 0.5, 0.125, 1.0, 8.0)
POISON_SNAPSHOT = (-101.0, -202.0, -303.0, 90.0, 40.0, 20.0, 9.0, 10.0)
STACK_SENTINEL = 0xFEEDFACECAFEBEEF

GPR_INITIAL = {
    UC_X86_REG_RAX: 0x1111111122222222,
    UC_X86_REG_RBX: 0x2222222233333333,
    UC_X86_REG_RCX: 0x3333333344444444,
    UC_X86_REG_RDX: 0x4444444455555555,
    UC_X86_REG_RDI: 0x5555555566666666,
    UC_X86_REG_RBP: 0x6666666677777777,
    UC_X86_REG_R8: 0x7777777788888888,
    UC_X86_REG_R9: 0x8888888899999999,
    UC_X86_REG_R10: 0x99999999AAAAAAAA,
    UC_X86_REG_R11: 0xAAAAAAAABBBBBBBB,
    UC_X86_REG_R12: 0xBBBBBBBBCCCCCCCC,
    UC_X86_REG_R13: 0xCCCCCCCCDDDDDDDD,
    UC_X86_REG_R14: 0xDDDDDDDDEEEEEEEE,
    UC_X86_REG_R15: 0xEEEEEEEEFFFFFFFF,
}
GPRS = (*GPR_INITIAL, UC_X86_REG_RSI)
XMMS = (
    UC_X86_REG_XMM0,
    UC_X86_REG_XMM1,
    UC_X86_REG_XMM2,
    UC_X86_REG_XMM3,
    UC_X86_REG_XMM4,
    UC_X86_REG_XMM5,
    UC_X86_REG_XMM6,
)


def xmm_value(low: float, seed: int) -> int:
    return int.from_bytes(
        struct.pack("<fIII", low, seed, seed + 1, seed + 2),
        "little",
    )


XMM_INITIAL = {
    UC_X86_REG_XMM0: xmm_value(19.0, 0x10000000),
    UC_X86_REG_XMM1: xmm_value(29.0, 0x20000000),
    UC_X86_REG_XMM2: xmm_value(39.0, 0x30000000),
    UC_X86_REG_XMM3: xmm_value(SITE1_MULTIPLIER, 0x40000000),
    UC_X86_REG_XMM4: xmm_value(59.0, 0x50000000),
    UC_X86_REG_XMM5: xmm_value(69.0, 0x60000000),
    UC_X86_REG_XMM6: xmm_value(SITE2_MULTIPLIER, 0x70000000),
}


class CaptureProcess:
    def __init__(self) -> None:
        self.memory = {
            movement.CAMERA_DISTANCE_MUL1_SITE: movement.CAMERA_RIG_BUILD1_ORIGINAL,
            movement.CAMERA_DISTANCE_MUL2_SITE: movement.CAMERA_RIG_BUILD2_ORIGINAL,
        }

    def read(self, address: int, size: int) -> bytes:
        return self.memory.get(int(address), bytes(size))[:size]

    def write(self, address: int, data: bytes) -> None:
        self.memory[int(address)] = bytes(data)

    def patch_code(self, address: int, data: bytes) -> None:
        self.write(address, data)


@unittest.skipUnless(Uc is not None, "unicorn is required for native cave emulation")
class CameraZoomSnapshotExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_cave_base = movement.COMMAND_BLOCK - 0x200
        movement.set_cave_base(CAVE_BASE)
        if not movement.CAMERA_DISTANCE_YIELDS_TO_WALL:
            raise AssertionError("the production camera must yield to native walls")
        process = CaptureProcess()
        movement.install_collision_aware_camera_zoom(process)
        cls.caves = {
            1: process.memory[movement.CAMERA_DISTANCE_CAVE1],
            2: process.memory[movement.CAMERA_DISTANCE_CAVE2],
        }
        cls.helper = process.memory[movement.CAMERA_SNAPSHOT_HELPER]

    @classmethod
    def tearDownClass(cls) -> None:
        movement.set_cave_base(cls.old_cave_base)

    @staticmethod
    def site_layout(site: int) -> tuple[int, int, int, bytes]:
        if site == 1:
            return (
                movement.CAMERA_DISTANCE_CAVE1,
                movement.CAMERA_DISTANCE_MUL1_SITE,
                movement.CAMERA_RIG_BUILD1_RETURN,
                movement.CAMERA_RIG_BUILD1_ORIGINAL,
            )
        if site == 2:
            return (
                movement.CAMERA_DISTANCE_CAVE2,
                movement.CAMERA_DISTANCE_MUL2_SITE,
                movement.CAMERA_RIG_BUILD2_RETURN,
                movement.CAMERA_RIG_BUILD2_ORIGINAL,
            )
        raise ValueError(f"unknown camera-distance site: {site}")

    @staticmethod
    def write_output(
        emulator,
        *,
        generation: int,
        active: bool,
        snapshot: tuple[float, ...],
    ) -> None:
        slot = frame_sync.generation_slot(
            generation,
            frame_sync.CAMERA_OUTPUT_SLOT_OFFSETS,
        )
        emulator.mem_write(
            movement.CAMERA_FRAME_STATE + slot,
            frame_sync.CAMERA_OUTPUT_PAYLOAD.pack(*snapshot),
        )
        commit = (int(active) << 32) | (generation & 0xFFFFFFFF)
        emulator.mem_write(
            movement.CAMERA_FRAME_STATE + frame_sync.CAMERA_OUTPUT_COMMIT_OFFSET,
            struct.pack("<Q", commit),
        )

    def make_emulator(
        self,
        *,
        site: int,
        generation: int = 1,
        output_active: bool = True,
        lifecycle_active: bool = True,
        primary_rig: bool = True,
        wall: bool = False,
        snapshot: tuple[float, ...] = SNAPSHOT,
    ):
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            emulator.mem_map(CAVE_BASE, 0x10000)
            emulator.mem_map(RIG_PAGE, 0x3000)
            emulator.mem_map(STACK, STACK_SIZE)
            emulator.mem_map(movement.CAMERA_DISTANCE_MUL1_SITE & ~0xFFF, 0x1000)
        finally:
            if restore_faulthandler:
                faulthandler.enable()

        emulator.mem_write(movement.CAMERA_DISTANCE_CAVE1, self.caves[1])
        emulator.mem_write(movement.CAMERA_DISTANCE_CAVE2, self.caves[2])
        emulator.mem_write(movement.CAMERA_SNAPSHOT_HELPER, self.helper)
        emulator.mem_write(
            movement.CAMERA_FRAME_STATE + frame_sync.CAMERA_OUTPUT_HEADER_OFFSET,
            struct.pack(
                "<II",
                frame_sync.CAMERA_FRAME_MAGIC,
                frame_sync.CAMERA_FRAME_VERSION,
            ),
        )
        self.write_output(
            emulator,
            generation=generation,
            active=output_active,
            snapshot=snapshot,
        )
        emulator.mem_write(
            movement.COMMAND_BLOCK + movement.CAMERA_ACTIVE_OFFSET,
            struct.pack("<I", int(lifecycle_active)),
        )

        for rig in (movement.CAMERA_PRIMARY_RIG, SECONDARY_RIG):
            emulator.mem_write(
                rig + 0x30,
                struct.pack("<3f", NATIVE_YAW, NATIVE_PITCH, NATIVE_DISTANCE),
            )
            emulator.mem_write(rig + 0x40, struct.pack("<I", 0))
        if wall:
            emulator.mem_write(
                movement.CAMERA_PRIMARY_RIG + 0x40,
                struct.pack("<I", movement.CAMERA_BACK_ON_WALL_MASK),
            )

        stack_pointer = STACK + 0x2000
        emulator.mem_write(
            stack_pointer + 0x20,
            struct.pack("<Q", STACK_SENTINEL),
        )
        for register, value in GPR_INITIAL.items():
            emulator.reg_write(register, value)
        emulator.reg_write(
            UC_X86_REG_RSI,
            movement.CAMERA_PRIMARY_RIG if primary_rig else SECONDARY_RIG,
        )
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        for register, value in XMM_INITIAL.items():
            emulator.reg_write(register, value)
        emulator.reg_write(UC_X86_REG_EFLAGS, 0xAD7)
        initial_flags = emulator.reg_read(UC_X86_REG_EFLAGS)
        return emulator, stack_pointer, initial_flags

    def execute_cave(self, emulator, site: int) -> None:
        cave, _native_site, return_address, _original = self.site_layout(site)
        try:
            emulator.emu_start(cave, return_address, count=1024)
        except UcError as error:  # pragma: no cover - assertion adds diagnostics
            self.fail(f"camera-distance cave {site} faulted: {error}")
        self.assertEqual(emulator.reg_read(UC_X86_REG_RIP), return_address)

    def execute_original(self, emulator, site: int) -> None:
        _cave, native_site, return_address, original = self.site_layout(site)
        emulator.mem_write(native_site, original)
        try:
            emulator.emu_start(native_site, return_address, count=128)
        except UcError as error:  # pragma: no cover - assertion adds diagnostics
            self.fail(f"original camera-distance site {site} faulted: {error}")
        self.assertEqual(emulator.reg_read(UC_X86_REG_RIP), return_address)

    @staticmethod
    def low_float(emulator, register: int) -> float:
        bits = emulator.reg_read(register) & 0xFFFFFFFF
        return struct.unpack("<f", struct.pack("<I", bits))[0]

    def assert_control_state_matches_original(
        self,
        actual,
        original,
        *,
        stack_pointer: int,
    ) -> None:
        for register in GPRS:
            with self.subTest(register=register):
                self.assertEqual(
                    actual.reg_read(register),
                    original.reg_read(register),
                )
        self.assertEqual(actual.reg_read(UC_X86_REG_RSP), stack_pointer)
        self.assertEqual(
            actual.reg_read(UC_X86_REG_EFLAGS),
            original.reg_read(UC_X86_REG_EFLAGS),
        )
        self.assertEqual(
            actual.mem_read(stack_pointer + 0x20, 8),
            original.mem_read(stack_pointer + 0x20, 8),
        )

    def assert_complete_original_path(
        self,
        actual,
        original,
        *,
        stack_pointer: int,
    ) -> None:
        self.assert_control_state_matches_original(
            actual,
            original,
            stack_pointer=stack_pointer,
        )
        for register in XMMS:
            with self.subTest(xmm_register=register):
                self.assertEqual(
                    actual.reg_read(register),
                    original.reg_read(register),
                )

    def test_stable_canonical_snapshot_applies_all_three_rig_adjustments(self):
        for site, generation in ((1, 2), (2, 3)):
            with self.subTest(site=site, generation=generation):
                emulator, stack_pointer, initial_flags = self.make_emulator(
                    site=site,
                    generation=generation,
                )
                original, _, _ = self.make_emulator(site=site)
                self.execute_original(original, site)

                self.execute_cave(emulator, site)

                multiplier_register = UC_X86_REG_XMM3 if site == 1 else UC_X86_REG_XMM6
                multiplier = SITE1_MULTIPLIER if site == 1 else SITE2_MULTIPLIER
                self.assertAlmostEqual(
                    self.low_float(emulator, multiplier_register),
                    multiplier * (NATIVE_DISTANCE + SNAPSHOT[3]),
                )
                self.assertAlmostEqual(
                    self.low_float(emulator, UC_X86_REG_XMM1),
                    NATIVE_YAW + SNAPSHOT[4],
                )
                self.assertAlmostEqual(
                    self.low_float(emulator, UC_X86_REG_XMM2),
                    NATIVE_PITCH + SNAPSHOT[5],
                )
                if site == 2:
                    self.assertEqual(
                        emulator.reg_read(UC_X86_REG_XMM3),
                        emulator.reg_read(UC_X86_REG_XMM6),
                    )
                    self.assertEqual(
                        struct.unpack(
                            "<Q",
                            emulator.mem_read(stack_pointer + 0x20, 8),
                        )[0],
                        movement.CAMERA_PRIMARY_RIG + 0x10,
                    )
                self.assertEqual(
                    emulator.reg_read(UC_X86_REG_EFLAGS),
                    initial_flags,
                )
                self.assert_control_state_matches_original(
                    emulator,
                    original,
                    stack_pointer=stack_pointer,
                )

    def test_u64_commit_change_during_copy_uses_the_complete_original_path(self):
        copy_second_qword = self.helper.index(bytes.fromhex("49 8B 52 08"))
        for site in (1, 2):
            with self.subTest(site=site):
                emulator, stack_pointer, _ = self.make_emulator(
                    site=site,
                    generation=1,
                )
                original, _, _ = self.make_emulator(site=site)
                self.execute_original(original, site)
                fired = False

                def change_commit(uc, address, _size, _user_data):
                    nonlocal fired
                    if (
                        fired
                        or address
                        != movement.CAMERA_SNAPSHOT_HELPER + copy_second_qword
                    ):
                        return
                    fired = True
                    self.write_output(
                        uc,
                        generation=2,
                        active=True,
                        snapshot=POISON_SNAPSHOT,
                    )

                emulator.hook_add(UC_HOOK_CODE, change_commit)

                self.execute_cave(emulator, site)

                self.assertTrue(fired)
                self.assert_complete_original_path(
                    emulator,
                    original,
                    stack_pointer=stack_pointer,
                )

    def test_inactive_and_noncanonical_rigs_always_use_original_path(self):
        scenarios = (
            ("output inactive", {"output_active": False}),
            ("lifecycle inactive", {"lifecycle_active": False}),
            ("secondary rig", {"primary_rig": False}),
            ("native wall", {"wall": True}),
        )
        for site in (1, 2):
            for label, arguments in scenarios:
                with self.subTest(site=site, scenario=label):
                    emulator, stack_pointer, _ = self.make_emulator(
                        site=site,
                        **arguments,
                    )
                    original, _, _ = self.make_emulator(
                        site=site,
                        primary_rig=arguments.get("primary_rig", True),
                    )
                    self.execute_original(original, site)

                    self.execute_cave(emulator, site)

                    self.assert_complete_original_path(
                        emulator,
                        original,
                        stack_pointer=stack_pointer,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
