"""Execution-level regressions for the visual-facing mailbox helper."""

import faulthandler
import struct
import unittest

try:
    from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_64
    from unicorn.x86_const import (
        UC_X86_REG_EFLAGS,
        UC_X86_REG_R10,
        UC_X86_REG_R9,
        UC_X86_REG_RAX,
        UC_X86_REG_R8,
        UC_X86_REG_RCX,
        UC_X86_REG_RIP,
        UC_X86_REG_RSP,
    )
except ImportError:  # pragma: no cover - exercised only in minimal dev installs
    Uc = None
    UcError = RuntimeError

import coop_runtime as movement
import movement_mailbox as mailbox


CAVE_BASE = 0x5BAA0000
ACTOR = 0x70000000
ROOT = 0x70001000
PHYSICS = 0x70002000
MAILBOX = 0x70003000
STACK = 0x71000000
STOP = 0x72000000
POISONED_HEADING = 0xBFB1ECCC
MOVEMENT_PROLOGUE = b"\x40\x53\x56\x41\x56"


class CaptureProcess:
    def __init__(self):
        self.writes = {}
        self.site = movement.VISUAL_FACING_CALL_SITE
        self.original = b"\xe8\x98\xd9\x02\x00"

    def read(self, address, size):
        data = self.writes.get(address)
        if data is not None:
            return data[:size]
        if address == self.site and size == len(self.original):
            return self.original
        return b"\x00" * size

    def write(self, address, data):
        self.writes[address] = bytes(data)

    def patch_code(self, address, data):
        self.writes[address] = bytes(data)


@unittest.skipUnless(Uc is not None, "unicorn is required for native cave emulation")
class VisualFacingHelperExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        movement.set_cave_base(CAVE_BASE)
        movement.set_dual_interaction_base(0x5C000000)
        process = CaptureProcess()
        movement.install_visual_facing_update(process)
        cls.helper = process.writes[movement.VISUAL_FALLBACK_HELPER]
        cls.actor_helper = process.writes[movement.ACTOR_PHYSICS_HELPER]

    def make_emulator(self):
        # Unicorn's Windows backend probes its reservation with a handled
        # access violation.  Pytest's faulthandler otherwise prints that
        # expected first-chance exception even though mem_map succeeds.
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            # The shared actor helper lives at +0x89E0, outside the visual
            # cave's compact region but inside the primary allocation.
            emulator.mem_map(CAVE_BASE, 0x10000)
            emulator.mem_write(movement.VISUAL_FALLBACK_HELPER, self.helper)
            emulator.mem_write(movement.ACTOR_PHYSICS_HELPER, self.actor_helper)

            emulator.mem_map(ACTOR, 0x5000)
            emulator.mem_write(ACTOR + 0x10, struct.pack("<Q", ROOT))
            emulator.mem_write(ROOT + 0xB8, struct.pack("<Q", PHYSICS))

            emulator.mem_map(STACK, 0x2000)
            emulator.mem_map(STOP, 0x1000)
            emulator.mem_write(STOP, b"\xcc")
        finally:
            if restore_faulthandler:
                faulthandler.enable()
        stack_pointer = STACK + 0x1000
        emulator.mem_write(stack_pointer, struct.pack("<Q", STOP))
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        emulator.reg_write(UC_X86_REG_RAX, ACTOR)
        emulator.reg_write(UC_X86_REG_R9, MAILBOX)
        emulator.reg_write(UC_X86_REG_EFLAGS, 0x202)
        return emulator

    @staticmethod
    def write_snapshot(
        emulator,
        *,
        fallback=False,
        sequence=2,
        active=1,
        actor=ACTOR,
        physics=PHYSICS,
        visual=POISONED_HEADING,
    ):
        base = mailbox.FALLBACK_SEQUENCE_OFFSET if fallback else 0
        emulator.mem_write(MAILBOX + base, struct.pack("<I", sequence))
        emulator.mem_write(
            MAILBOX
            + (
                mailbox.FALLBACK_ACTIVE_OFFSET
                if fallback
                else mailbox.ACTIVE_OFFSET
            ),
            struct.pack("<I", active),
        )
        emulator.mem_write(
            MAILBOX
            + (
                mailbox.FALLBACK_ACTOR_OFFSET
                if fallback
                else mailbox.ACTOR_OFFSET
            ),
            struct.pack("<Q", actor),
        )
        emulator.mem_write(
            MAILBOX
            + (
                mailbox.FALLBACK_PHYS_OFFSET
                if fallback
                else mailbox.PHYS_OFFSET
            ),
            struct.pack("<Q", physics),
        )
        emulator.mem_write(
            MAILBOX
            + (
                mailbox.FALLBACK_VISUAL_HEADING_OFFSET
                if fallback
                else mailbox.VISUAL_HEADING_OFFSET
            ),
            struct.pack("<I", visual),
        )

    def execute(self, emulator):
        try:
            emulator.emu_start(
                movement.VISUAL_FALLBACK_HELPER,
                STOP,
                count=512,
            )
        except UcError as error:  # pragma: no cover - assertion adds diagnostics
            self.fail(f"visual-facing helper faulted during emulation: {error}")
        self.assertEqual(emulator.reg_read(UC_X86_REG_RIP), STOP)

    def test_main_snapshot_returns_heading_without_destroying_actor(self):
        emulator = self.make_emulator()
        self.write_snapshot(emulator)

        self.execute(emulator)

        self.assertEqual(emulator.reg_read(UC_X86_REG_RAX), ACTOR)
        self.assertEqual(emulator.reg_read(UC_X86_REG_R10) & 0xFFFFFFFF, POISONED_HEADING)
        self.assertEqual(emulator.reg_read(UC_X86_REG_EFLAGS) & 1, 0)

    def test_odd_main_uses_one_stable_fallback_without_spinning(self):
        emulator = self.make_emulator()
        self.write_snapshot(emulator, sequence=3)
        self.write_snapshot(emulator, fallback=True)

        self.execute(emulator)

        self.assertEqual(emulator.reg_read(UC_X86_REG_RAX), ACTOR)
        self.assertEqual(emulator.reg_read(UC_X86_REG_R10) & 0xFFFFFFFF, POISONED_HEADING)
        self.assertEqual(emulator.reg_read(UC_X86_REG_EFLAGS) & 1, 0)

    def test_odd_main_and_fallback_fail_open_in_bounded_time(self):
        emulator = self.make_emulator()
        self.write_snapshot(emulator, sequence=3)
        self.write_snapshot(emulator, fallback=True, sequence=3)

        self.execute(emulator)

        self.assertEqual(emulator.reg_read(UC_X86_REG_RAX), ACTOR)
        self.assertEqual(emulator.reg_read(UC_X86_REG_EFLAGS) & 1, 1)


@unittest.skipUnless(Uc is not None, "unicorn is required for native cave emulation")
class MovementCaveExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        movement.set_cave_base(CAVE_BASE)
        cls.code = bytes(
            movement.build_movement_cave(
                movement.COMMAND_BLOCK,
                MOVEMENT_PROLOGUE,
            )
        )
        cls.p1_helper = bytes(
            movement.build_p1_berserk_movement_helper(movement.COMMAND_BLOCK)
        )
        cls.stop = movement.MOVEMENT_CAVE + len(cls.code) - len(MOVEMENT_PROLOGUE)

    def make_emulator(self, *, mailbox_actor=ACTOR):
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            emulator.mem_map(CAVE_BASE, 0x10000)
            emulator.mem_write(movement.MOVEMENT_CAVE, self.code)
            emulator.mem_write(
                movement.P1_BERSERK_MOVEMENT_HELPER,
                self.p1_helper,
            )
            emulator.mem_map(ACTOR, 0x5000)
            emulator.mem_write(ROOT + 0x30, struct.pack("<Q", ACTOR))
            emulator.mem_write(ROOT + 0xB8, struct.pack("<Q", PHYSICS))
            emulator.mem_map(STACK, 0x2000)
        finally:
            if restore_faulthandler:
                faulthandler.enable()

        emulator.mem_write(
            movement.COMMAND_BLOCK + movement.MOVEMENT_ACTIVE_OFFSET,
            struct.pack("<I", 1),
        )
        command = movement.P2_MOVEMENT_MAILBOX
        emulator.mem_write(
            command + mailbox.SEQUENCE_OFFSET,
            struct.pack("<I", 2),
        )
        emulator.mem_write(
            command + mailbox.ACTIVE_OFFSET,
            struct.pack("<I", 1),
        )
        emulator.mem_write(
            command + mailbox.ACTOR_OFFSET,
            struct.pack("<Q", mailbox_actor),
        )
        emulator.mem_write(
            command + mailbox.PHYS_OFFSET,
            struct.pack("<Q", PHYSICS),
        )
        emulator.mem_write(
            command + mailbox.HEADING_OFFSET,
            struct.pack("<f", 1.25),
        )
        emulator.mem_write(
            command + mailbox.MAGNITUDE_OFFSET,
            struct.pack("<f", 0.10),
        )

        stack_pointer = STACK + 0x1000
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        emulator.reg_write(UC_X86_REG_RCX, ROOT)
        emulator.reg_write(UC_X86_REG_R8, 0)
        emulator.reg_write(UC_X86_REG_R9, 0)
        emulator.reg_write(UC_X86_REG_EFLAGS, 0x202)
        return emulator, stack_pointer

    def execute(self, emulator):
        try:
            emulator.emu_start(
                movement.MOVEMENT_CAVE,
                self.stop,
                count=1024,
            )
        except UcError as error:  # pragma: no cover - assertion adds diagnostics
            self.fail(f"movement cave faulted during emulation: {error}")
        self.assertEqual(emulator.reg_read(UC_X86_REG_RIP), self.stop)

    def test_valid_native_actor_applies_one_complete_p2_snapshot(self):
        emulator, stack_pointer = self.make_emulator()

        self.execute(emulator)

        heading = struct.unpack("<f", emulator.mem_read(PHYSICS + 0xA4, 4))[0]
        magnitude = struct.unpack("<f", emulator.mem_read(PHYSICS + 0xD0, 4))[0]
        pitch = struct.unpack("<f", emulator.mem_read(PHYSICS + 0xA0, 4))[0]
        self.assertAlmostEqual(heading, 1.25)
        self.assertAlmostEqual(magnitude, 0.10)
        self.assertEqual(pitch, 0.0)
        self.assertEqual(emulator.reg_read(UC_X86_REG_RSP), stack_pointer)

    def test_poisoned_mailbox_actor_fails_open_without_dereference(self):
        emulator, stack_pointer = self.make_emulator(
            mailbox_actor=POISONED_HEADING,
        )

        self.execute(emulator)

        self.assertEqual(bytes(emulator.mem_read(PHYSICS + 0xA0, 0x34)), b"\x00" * 0x34)
        self.assertEqual(emulator.reg_read(UC_X86_REG_RSP), stack_pointer)


if __name__ == "__main__":
    unittest.main()
