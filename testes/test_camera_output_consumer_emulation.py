"""Execution-level regressions for the native camera-output consumer cave."""

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
DUAL_BASE = 0x5C000000
CONTROLLER = 0x70000000
STACK = 0x71000000

ORIGINAL_FOCUS = (101.0, -22.5, 303.0)
ORIGINAL_W = 0.25
ORIGINAL_Y_BITS = struct.unpack("<I", struct.pack("<f", ORIGINAL_FOCUS[1]))[0]
XMM0_VALUE = (0xFEDCBA987654321001234567 << 32) | ORIGINAL_Y_BITS

PRESERVED_REGISTERS = {
    UC_X86_REG_RAX: 0x1111111122222222,
    UC_X86_REG_RBX: 0x2222222233333333,
    UC_X86_REG_RCX: 0x3333333344444444,
    UC_X86_REG_RDX: 0x4444444455555555,
    UC_X86_REG_RSI: 0x5555555566666666,
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


class CaptureProcess:
    def __init__(self) -> None:
        self.memory = {
            movement.CAMERA_CONTROLLER_FOCUS_HOOK_SITE: (
                movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL
            ),
        }

    def read(self, address: int, size: int) -> bytes:
        return self.memory.get(int(address), bytes(size))[:size]

    def write(self, address: int, data: bytes) -> None:
        self.memory[int(address)] = bytes(data)

    def patch_code(self, address: int, data: bytes) -> None:
        self.write(address, data)


@unittest.skipUnless(Uc is not None, "unicorn is required for native cave emulation")
class CameraOutputConsumerExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_cave_base = movement.COMMAND_BLOCK - 0x200
        cls.old_dual_base = movement.DUAL_INTERACTION_BASE
        movement.set_cave_base(CAVE_BASE)
        movement.set_dual_interaction_base(DUAL_BASE)
        process = CaptureProcess()
        movement.install_controller_shared_focus(process)
        cls.cave = process.memory[movement.CAMERA_FOCUS_V2_CAVE]
        cls.helper = process.memory[movement.CAMERA_SNAPSHOT_HELPER]

    @classmethod
    def tearDownClass(cls) -> None:
        movement.set_dual_interaction_base(cls.old_dual_base)
        movement.set_cave_base(cls.old_cave_base)

    def make_emulator(
        self,
        *,
        camera_active: bool = True,
        header_magic: int = frame_sync.CAMERA_FRAME_MAGIC,
        header_version: int = frame_sync.CAMERA_FRAME_VERSION,
        generation: int = 1,
        output_active: int = 1,
        focus=(11.0, 22.0, 33.0),
    ):
        # Unicorn's Windows backend probes its reservation with a handled
        # access violation. Pytest's faulthandler would otherwise print that
        # expected first-chance exception even though mapping succeeds.
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            emulator.mem_map(CAVE_BASE, 0x10000)
            emulator.mem_map(DUAL_BASE, 0x10000)
            emulator.mem_write(movement.CAMERA_FOCUS_V2_CAVE, self.cave)
            emulator.mem_write(movement.CAMERA_SNAPSHOT_HELPER, self.helper)
            emulator.mem_map(CONTROLLER, 0x1000)
            emulator.mem_map(STACK, 0x2000)
            return_page = movement.CAMERA_CONTROLLER_FOCUS_HOOK_RETURN & ~0xFFF
            emulator.mem_map(return_page, 0x1000)
        finally:
            if restore_faulthandler:
                faulthandler.enable()

        emulator.mem_write(
            movement.COMMAND_BLOCK + movement.CAMERA_ACTIVE_OFFSET,
            struct.pack("<I", int(camera_active)),
        )
        emulator.mem_write(
            movement.CAMERA_FRAME_STATE + frame_sync.CAMERA_OUTPUT_HEADER_OFFSET,
            struct.pack("<II", int(header_magic), int(header_version)),
        )
        self.write_output(
            emulator,
            generation=generation,
            active=output_active,
            focus=focus,
        )
        emulator.mem_write(
            CONTROLLER + 0x60,
            struct.pack("<4f", *ORIGINAL_FOCUS, ORIGINAL_W),
        )

        stack_pointer = STACK + 0x1000
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        emulator.reg_write(UC_X86_REG_RDI, CONTROLLER)
        for register, value in PRESERVED_REGISTERS.items():
            emulator.reg_write(register, value)
        emulator.reg_write(UC_X86_REG_XMM0, XMM0_VALUE)
        emulator.reg_write(UC_X86_REG_EFLAGS, 0xAD7)
        initial_flags = emulator.reg_read(UC_X86_REG_EFLAGS)
        return emulator, stack_pointer, initial_flags

    @staticmethod
    def write_output(
        emulator,
        *,
        generation: int,
        active: int = 1,
        focus=(11.0, 22.0, 33.0),
    ) -> None:
        slot = frame_sync.generation_slot(
            generation,
            frame_sync.CAMERA_OUTPUT_SLOT_OFFSETS,
        )
        payload = frame_sync.CAMERA_OUTPUT_PAYLOAD.pack(
            *(float(value) for value in focus),
            4.0,
            0.25,
            -0.125,
            2.0,
            8.0,
        )
        emulator.mem_write(movement.CAMERA_FRAME_STATE + slot, payload)
        commit = (int(bool(active)) << 32) | (int(generation) & 0xFFFFFFFF)
        emulator.mem_write(
            movement.CAMERA_FRAME_STATE + frame_sync.CAMERA_OUTPUT_COMMIT_OFFSET,
            struct.pack("<Q", commit),
        )

    @staticmethod
    def controller_values(emulator) -> tuple[float, float, float, float]:
        return struct.unpack("<4f", emulator.mem_read(CONTROLLER + 0x60, 16))

    def execute(self, emulator) -> None:
        try:
            emulator.emu_start(
                movement.CAMERA_FOCUS_V2_CAVE,
                movement.CAMERA_CONTROLLER_FOCUS_HOOK_RETURN,
                count=512,
            )
        except UcError as error:  # pragma: no cover - assertion adds diagnostics
            self.fail(f"camera-output consumer faulted during emulation: {error}")
        self.assertEqual(
            emulator.reg_read(UC_X86_REG_RIP),
            movement.CAMERA_CONTROLLER_FOCUS_HOOK_RETURN,
        )

    def assert_original_path(self, emulator) -> None:
        self.assertEqual(
            self.controller_values(emulator),
            (ORIGINAL_FOCUS[0], ORIGINAL_FOCUS[1], ORIGINAL_FOCUS[2], ORIGINAL_W),
        )

    def test_stable_slot_zero_and_slot_one_apply_the_complete_xyz(self):
        for generation, focus in (
            (2, (12.5, -7.25, 91.0)),
            (3, (-44.0, 0.125, 8.5)),
        ):
            with self.subTest(generation=generation):
                emulator, _stack, _flags = self.make_emulator(
                    generation=generation,
                    focus=focus,
                )

                self.execute(emulator)

                self.assertEqual(
                    self.controller_values(emulator),
                    (*focus, 1.0),
                )

    def test_generation_change_during_slot_read_rejects_mixed_xyz(self):
        old_focus = (10.0, 20.0, 30.0)
        new_focus = (110.0, 220.0, 330.0)
        emulator, _stack, _flags = self.make_emulator(
            generation=1,
            focus=old_focus,
        )
        selected_slot = frame_sync.CAMERA_OUTPUT_SLOT_OFFSETS[1]
        second_qword_load = self.helper.index(bytes.fromhex("49 8B 52 08"))
        fired = False

        def tear_snapshot(uc, address, _size, _user_data):
            nonlocal fired
            if fired or address != movement.CAMERA_SNAPSHOT_HELPER + second_qword_load:
                return
            fired = True
            # The first qword (X/Y) is already copied. Poison Z and the coupled
            # scalar fields, then commit a new slot. The final u64 commit check
            # must reject that private mixed scratch snapshot.
            uc.mem_write(
                movement.CAMERA_FRAME_STATE + selected_slot + 0x08,
                struct.pack("<6f", new_focus[2], 9.0, 1.0, 2.0, 3.0, 4.0),
            )
            self.write_output(
                uc,
                generation=2,
                focus=new_focus,
            )
            uc.mem_write(
                movement.CAMERA_FRAME_STATE
                + frame_sync.CAMERA_OUTPUT_COMMIT_OFFSET,
                struct.pack("<Q", (1 << 32) | 2),
            )

        emulator.hook_add(UC_HOOK_CODE, tear_snapshot)

        self.execute(emulator)

        self.assertTrue(fired)
        self.assert_original_path(emulator)
        self.assertNotEqual(self.controller_values(emulator)[:3], new_focus)

    def test_inactive_output_invalid_header_and_inactive_camera_use_original(self):
        scenarios = (
            {"output_active": 0},
            {"header_magic": frame_sync.CAMERA_FRAME_MAGIC ^ 1},
            {"header_version": frame_sync.CAMERA_FRAME_VERSION + 1},
            {"camera_active": False},
        )
        for arguments in scenarios:
            with self.subTest(arguments=arguments):
                emulator, _stack, _flags = self.make_emulator(**arguments)

                self.execute(emulator)

                self.assert_original_path(emulator)

    def test_same_generation_reapplies_identically_without_state_mutation(self):
        focus = (-5.5, 14.25, 77.0)
        emulator, stack_pointer, _flags = self.make_emulator(
            generation=9,
            focus=focus,
        )

        state_before = bytes(
            emulator.mem_read(
                movement.CAMERA_FRAME_STATE,
                frame_sync.CAMERA_FRAME_STATE_SIZE,
            )
        )
        self.execute(emulator)
        state_after_first = bytes(
            emulator.mem_read(
                movement.CAMERA_FRAME_STATE,
                frame_sync.CAMERA_FRAME_STATE_SIZE,
            )
        )
        first = self.controller_values(emulator)

        emulator.mem_write(
            CONTROLLER + 0x60,
            struct.pack("<4f", *ORIGINAL_FOCUS, ORIGINAL_W),
        )
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        self.execute(emulator)
        state_after_second = bytes(
            emulator.mem_read(
                movement.CAMERA_FRAME_STATE,
                frame_sync.CAMERA_FRAME_STATE_SIZE,
            )
        )

        self.assertEqual(first, (*focus, 1.0))
        self.assertEqual(self.controller_values(emulator), first)
        self.assertEqual(state_after_first, state_before)
        self.assertEqual(state_after_second, state_before)

    def test_stable_path_preserves_gprs_flags_xmm_stack_and_returns(self):
        emulator, stack_pointer, initial_flags = self.make_emulator(
            generation=2,
            focus=(3.0, 4.0, 5.0),
        )

        self.execute(emulator)

        for register, expected in PRESERVED_REGISTERS.items():
            with self.subTest(register=register):
                self.assertEqual(emulator.reg_read(register), expected)
        self.assertEqual(emulator.reg_read(UC_X86_REG_RDI), CONTROLLER)
        self.assertEqual(emulator.reg_read(UC_X86_REG_RSP), stack_pointer)
        self.assertEqual(emulator.reg_read(UC_X86_REG_EFLAGS), initial_flags)
        self.assertEqual(emulator.reg_read(UC_X86_REG_XMM0), XMM0_VALUE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
