"""Execution-level regressions for generation-safe follower action repair."""

from __future__ import annotations

import faulthandler
import struct
import unittest

try:
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs
except ImportError:  # pragma: no cover
    Cs = None

try:
    from unicorn import UC_ARCH_X86, UC_HOOK_CODE, UC_MODE_64, Uc
    from unicorn.x86_const import (
        UC_X86_REG_EFLAGS,
        UC_X86_REG_R8,
        UC_X86_REG_R9,
        UC_X86_REG_R10,
        UC_X86_REG_R11,
        UC_X86_REG_R12,
        UC_X86_REG_R13,
        UC_X86_REG_R14,
        UC_X86_REG_R15,
        UC_X86_REG_RAX,
        UC_X86_REG_RBP,
        UC_X86_REG_RBX,
        UC_X86_REG_RCX,
        UC_X86_REG_RDI,
        UC_X86_REG_RDX,
        UC_X86_REG_RIP,
        UC_X86_REG_RSP,
        UC_X86_REG_RSI,
        UC_X86_REG_XMM0,
        UC_X86_REG_XMM1,
        UC_X86_REG_XMM2,
        UC_X86_REG_XMM3,
        UC_X86_REG_XMM4,
        UC_X86_REG_XMM5,
    )
except ImportError:  # pragma: no cover
    Uc = None

import coop_runtime as coop


CAVE_BASE = 0x50000000
DUAL_BASE = 0x51000000
HELPER = DUAL_BASE + coop.FOLLOWER_ACTION_TRANSITION_HELPER_OFFSET
MAILBOX = DUAL_BASE + coop.FOLLOWER_ACTION_MAILBOX_OFFSET
RESOLVER = DUAL_BASE + 0x1C00
PHYS_HELPER = DUAL_BASE + 0x1D00
ACTOR = 0x52000000
OTHER_ACTOR = 0x52010000
ROOT = ACTOR + 0x3000
PHYS = ACTOR + 0x4000
ENTRY = ACTOR + 0x5000
DESCRIPTOR = ACTOR + 0x6000
STACK = 0x53000000
STOP = 0x54000000
HANDLE = 0x00100031


class MailboxProcess:
    def __init__(self):
        self.memory: dict[int, int] = {}
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, bytes]] = []

    def read(self, address, size):
        self.reads.append((address, size))
        if ACTOR <= address < ACTOR + 0x10000:
            raise AssertionError("Python attempted to dereference actor state")
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address, data):
        payload = bytes(data)
        self.writes.append((address, payload))
        for offset, value in enumerate(payload):
            self.memory[address + offset] = value

    def put(self, address, data):
        for offset, value in enumerate(bytes(data)):
            self.memory[address + offset] = value


class FollowerActionPublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        coop.set_cave_base(CAVE_BASE)
        coop.set_dual_interaction_base(DUAL_BASE)

    def unpack(self, process, role="p2"):
        address = coop.follower_action_mailbox_address(role)
        return coop.FOLLOWER_ACTION_MAILBOX_STRUCT.unpack(
            bytes(
                process.memory.get(address + offset, 0)
                for offset in range(coop.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE)
            )
        )

    def test_sanitize_publishes_only_a_stable_generation_record(self):
        process = MailboxProcess()

        self.assertFalse(
            coop.sanitize_residual_follower_action(
                process,
                ACTOR,
                True,
                role="p2",
                handle=HANDLE,
                expected_phys=PHYS,
            )
        )

        record = self.unpack(process)
        self.assertEqual(record[0], 2)
        self.assertEqual(record[1:4], (0, 0, HANDLE))
        self.assertEqual(record[4:8], (ACTOR, PHYS, 0x4124, 0x4021))
        self.assertEqual(
            [address for address, _size in process.reads],
            [coop.P2_FOLLOWER_ACTION_MAILBOX],
        )
        self.assertTrue(
            all(
                coop.P2_FOLLOWER_ACTION_MAILBOX
                <= address
                < coop.P2_FOLLOWER_ACTION_MAILBOX
                + coop.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE
                for address, _payload in process.writes
            )
        )
        self.assertEqual(
            process.writes[0],
            (
                coop.P2_FOLLOWER_ACTION_MAILBOX,
                struct.pack("<I", 3),
            ),
        )
        self.assertEqual(process.writes[-1][1], struct.pack("<I", 2))

    def test_persistent_sanitize_observes_ack_without_disarming_request(self):
        process = MailboxProcess()
        kwargs = dict(
            role="p2",
            handle=HANDLE,
            expected_phys=PHYS,
        )
        coop.sanitize_residual_follower_action(process, ACTOR, True, **kwargs)
        address = coop.P2_FOLLOWER_ACTION_MAILBOX
        process.put(
            address + coop.FOLLOWER_ACTION_RESULT_OFFSET,
            struct.pack("<I", coop.FOLLOWER_ACTION_RESULT_APPLIED),
        )
        process.put(
            address + coop.FOLLOWER_ACTION_ACK_SEQUENCE_OFFSET,
            struct.pack("<I", 2),
        )

        self.assertTrue(
            coop.sanitize_residual_follower_action(process, ACTOR, True, **kwargs)
        )
        record = self.unpack(process)
        self.assertEqual(record[0:2], (2, 2))
        self.assertEqual(record[2], coop.FOLLOWER_ACTION_RESULT_PENDING)
        self.assertEqual(record[3:8], (HANDLE, ACTOR, PHYS, 0x4124, 0x4021))

    def test_resume_clears_its_one_shot_record_after_ack(self):
        process = MailboxProcess()
        kwargs = dict(
            role="p3",
            handle=HANDLE,
            expected_phys=PHYS,
        )
        coop.resume_sanitized_follower_action(process, ACTOR, **kwargs)
        address = coop.P3_FOLLOWER_ACTION_MAILBOX
        process.put(
            address + coop.FOLLOWER_ACTION_RESULT_OFFSET,
            struct.pack("<I", coop.FOLLOWER_ACTION_RESULT_ALREADY_DESIRED),
        )
        process.put(
            address + coop.FOLLOWER_ACTION_ACK_SEQUENCE_OFFSET,
            struct.pack("<I", 2),
        )

        self.assertTrue(coop.resume_sanitized_follower_action(process, ACTOR, **kwargs))
        self.assertEqual(
            self.unpack(process, "p3"),
            (0, 0, 0, 0, 0, 0, 0, 0, 0),
        )

    def test_missing_handle_role_or_physics_fails_without_any_io(self):
        for kwargs in (
            dict(role=None, handle=HANDLE, expected_phys=PHYS),
            dict(role="p2", handle=0, expected_phys=PHYS),
            dict(role="p2", handle=HANDLE, expected_phys=0),
        ):
            with self.subTest(kwargs=kwargs):
                process = MailboxProcess()
                self.assertFalse(
                    coop.sanitize_residual_follower_action(
                        process,
                        ACTOR,
                        True,
                        **kwargs,
                    )
                )
                self.assertEqual(process.reads, [])
                self.assertEqual(process.writes, [])


@unittest.skipUnless(Uc is not None, "unicorn is required for native emulation")
class FollowerActionNativeExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = bytes(
            coop.build_follower_action_transition_helper(
                HELPER,
                MAILBOX,
                entity_resolver=RESOLVER,
                physics_helper=PHYS_HELPER,
            )
        )
        cls.physics_helper = bytes(coop.build_actor_physics_helper())

    @staticmethod
    def resolver_stub(handle=HANDLE, entry=ENTRY):
        # cmp ecx,handle ; jne miss ; mov rax,entry ; ret ; xor eax,eax ; ret
        return (
            b"\x81\xf9"
            + struct.pack("<I", handle)
            + b"\x75\x0b\x48\xb8"
            + struct.pack("<Q", entry)
            + b"\xc3\x31\xc0\xc3"
        )

    def make_emulator(
        self,
        *,
        context_actor=ACTOR,
        entry_actor=ACTOR,
        handle=HANDLE,
        expected_phys=PHYS,
        expected_action=coop.FOLLOWER_IDLE_ACTION,
        desired_action=coop.NATIVE_IDLE_ACTION,
        live_action=None,
        shadow_action=None,
        sequence=2,
        ack=0,
        state=1,
        target=0,
        descriptor_action=coop.FOLLOWER_IDLE_ACTION,
        native_list=coop.FOLLOWER_LIST_PRIMARY,
        slot_index=0,
    ):
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            emulator.mem_map(DUAL_BASE, 0x10000)
            emulator.mem_write(HELPER, self.helper)
            emulator.mem_write(RESOLVER, self.resolver_stub())
            emulator.mem_write(PHYS_HELPER, self.physics_helper)
            emulator.mem_map(ACTOR, 0x20000)
            emulator.mem_map(STACK, 0x4000)
            emulator.mem_map(STOP, 0x1000)
        finally:
            if restore_faulthandler:
                faulthandler.enable()

        if live_action is None:
            live_action = expected_action
        if shadow_action is None:
            shadow_action = live_action
        emulator.mem_write(ACTOR + 0x10, struct.pack("<Q", ROOT))
        emulator.mem_write(ROOT + 0xB8, struct.pack("<Q", PHYS))
        emulator.mem_write(ENTRY + 0x30, struct.pack("<Q", entry_actor))
        emulator.mem_write(ACTOR + 0x6B4, bytes((state,)))
        emulator.mem_write(ACTOR + 0x708, struct.pack("<Q", DESCRIPTOR))
        emulator.mem_write(
            ACTOR + 0x710,
            struct.pack("<IHH", target, live_action, 0),
        )
        emulator.mem_write(
            ACTOR + 0x728,
            struct.pack("<IHH", target, shadow_action, 0),
        )
        emulator.mem_write(DESCRIPTOR, struct.pack("<H", descriptor_action))
        slot = MAILBOX + slot_index * coop.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE
        emulator.mem_write(
            slot,
            coop.FOLLOWER_ACTION_MAILBOX_STRUCT.pack(
                sequence,
                ack,
                0,
                handle,
                ACTOR,
                expected_phys,
                expected_action,
                desired_action,
                0,
            ),
        )

        stack_pointer = STACK + 0x2008
        emulator.mem_write(stack_pointer, struct.pack("<Q", STOP))
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        emulator.reg_write(UC_X86_REG_RIP, HELPER)
        emulator.reg_write(UC_X86_REG_R13, context_actor)
        emulator.reg_write(UC_X86_REG_R10, native_list)
        emulator.reg_write(UC_X86_REG_RAX, 0x1111111122222222)
        emulator.reg_write(UC_X86_REG_RBX, 0x3333333344444444)
        emulator.reg_write(UC_X86_REG_RCX, 0x5555555566666666)
        emulator.reg_write(UC_X86_REG_RDX, 0x7777777788888888)
        emulator.reg_write(UC_X86_REG_RBP, 0x0101010102020202)
        emulator.reg_write(UC_X86_REG_RSI, 0x0303030304040404)
        emulator.reg_write(UC_X86_REG_RDI, 0x0505050506060606)
        emulator.reg_write(UC_X86_REG_R8, 0x0707070708080808)
        emulator.reg_write(UC_X86_REG_R9, 0x090909090A0A0A0A)
        emulator.reg_write(UC_X86_REG_R11, 0x0B0B0B0B0C0C0C0C)
        emulator.reg_write(UC_X86_REG_R12, 0x0D0D0D0D0E0E0E0E)
        emulator.reg_write(UC_X86_REG_R14, 0x0F0F0F0F10101010)
        emulator.reg_write(UC_X86_REG_R15, 0x1111111112121212)
        emulator.reg_write(UC_X86_REG_EFLAGS, 0x246)
        emulator.reg_write(UC_X86_REG_XMM0, 0x112233445566778899AABBCCDDEEFF00)
        emulator.reg_write(UC_X86_REG_XMM1, 0xFFEEDDCCBBAA99887766554433221100)
        emulator.reg_write(UC_X86_REG_XMM2, 0x102030405060708090A0B0C0D0E0F000)
        emulator.reg_write(UC_X86_REG_XMM3, 0x00112233445566778899AABBCCDDEEFF)
        emulator.reg_write(UC_X86_REG_XMM4, 0x13579BDF2468ACE00123456789ABCDEF)
        emulator.reg_write(UC_X86_REG_XMM5, 0xFEDCBA98765432100ECA8642FDB97531)
        return emulator

    @staticmethod
    def execute(emulator):
        emulator.emu_start(HELPER, STOP, count=2000)

    @staticmethod
    def action_pair(emulator):
        live = struct.unpack("<H", emulator.mem_read(ACTOR + 0x714, 2))[0]
        shadow = struct.unpack("<H", emulator.mem_read(ACTOR + 0x72C, 2))[0]
        return live, shadow

    @staticmethod
    def result_pair(emulator, slot=MAILBOX):
        ack, result = struct.unpack(
            "<II",
            emulator.mem_read(slot + coop.FOLLOWER_ACTION_ACK_SEQUENCE_OFFSET, 8),
        )
        # The contiguous order is ack then result.
        return result, ack

    def test_sanitize_updates_live_and_shadow_then_acknowledges(self):
        emulator = self.make_emulator()
        self.execute(emulator)

        self.assertEqual(self.action_pair(emulator), (0x4021, 0x4021))
        self.assertEqual(
            self.result_pair(emulator),
            (coop.FOLLOWER_ACTION_RESULT_APPLIED, 2),
        )

    def test_resume_updates_live_and_shadow(self):
        emulator = self.make_emulator(
            expected_action=coop.NATIVE_IDLE_ACTION,
            desired_action=coop.FOLLOWER_IDLE_ACTION,
        )
        self.execute(emulator)

        self.assertEqual(self.action_pair(emulator), (0x4124, 0x4124))
        self.assertEqual(
            self.result_pair(emulator),
            (coop.FOLLOWER_ACTION_RESULT_APPLIED, 2),
        )

    def test_second_role_slot_is_consumed_independently(self):
        emulator = self.make_emulator(slot_index=1)
        self.execute(emulator)

        slot = MAILBOX + coop.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE
        self.assertEqual(self.action_pair(emulator), (0x4021, 0x4021))
        self.assertEqual(self.result_pair(emulator), (0, 0))
        self.assertEqual(
            self.result_pair(emulator, slot),
            (coop.FOLLOWER_ACTION_RESULT_APPLIED, 2),
        )

    def test_already_desired_acknowledges_without_changing_action(self):
        emulator = self.make_emulator(live_action=0x4021, shadow_action=0x4021)
        self.execute(emulator)

        self.assertEqual(self.action_pair(emulator), (0x4021, 0x4021))
        self.assertEqual(
            self.result_pair(emulator),
            (coop.FOLLOWER_ACTION_RESULT_ALREADY_DESIRED, 2),
        )

    def test_acknowledged_persistent_request_repairs_a_new_residual(self):
        emulator = self.make_emulator(ack=2)
        self.execute(emulator)

        self.assertEqual(self.action_pair(emulator), (0x4021, 0x4021))
        self.assertEqual(
            self.result_pair(emulator),
            (coop.FOLLOWER_ACTION_RESULT_APPLIED, 2),
        )

    def test_odd_sequence_and_other_actor_are_bounded_misses(self):
        for kwargs in (dict(sequence=3), dict(context_actor=OTHER_ACTOR)):
            with self.subTest(kwargs=kwargs):
                emulator = self.make_emulator(**kwargs)
                self.execute(emulator)
                self.assertEqual(self.action_pair(emulator), (0x4124, 0x4124))
                self.assertEqual(self.result_pair(emulator), (0, 0))

    def test_stale_handle_actor_and_physics_fail_closed(self):
        cases = (
            (dict(handle=HANDLE + 1), coop.FOLLOWER_ACTION_RESULT_STALE_IDENTITY),
            (dict(entry_actor=OTHER_ACTOR), coop.FOLLOWER_ACTION_RESULT_STALE_IDENTITY),
            (dict(expected_phys=PHYS + 8), coop.FOLLOWER_ACTION_RESULT_STALE_PHYSICS),
        )
        for kwargs, result in cases:
            with self.subTest(kwargs=kwargs):
                emulator = self.make_emulator(**kwargs)
                self.execute(emulator)
                self.assertEqual(self.action_pair(emulator), (0x4124, 0x4124))
                self.assertEqual(self.result_pair(emulator), (result, 2))

    def test_state_target_descriptor_and_action_mismatch_fail_closed(self):
        cases = (
            dict(state=0),
            dict(target=0x100011),
            dict(descriptor_action=0x4021),
            dict(live_action=0x5001, shadow_action=0x5001),
            dict(live_action=0x4124, shadow_action=0x4021),
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                emulator = self.make_emulator(**kwargs)
                self.execute(emulator)
                self.assertEqual(
                    self.action_pair(emulator),
                    (kwargs.get("live_action", 0x4124), kwargs.get("shadow_action", kwargs.get("live_action", 0x4124))),
                )
                result, ack = self.result_pair(emulator)
                self.assertEqual(ack, 2)
                self.assertIn(
                    result,
                    (
                        coop.FOLLOWER_ACTION_RESULT_INVALID_SIGNATURE,
                        coop.FOLLOWER_ACTION_RESULT_ACTION_CHANGED,
                    ),
                )

    def test_sequence_change_during_second_resolution_prevents_write_and_ack(self):
        emulator = self.make_emulator()
        calls = 0

        def mutate_on_second_call(uc, address, _size, _user):
            nonlocal calls
            if address != RESOLVER:
                return
            calls += 1
            if calls == 2:
                uc.mem_write(MAILBOX, struct.pack("<I", 4))

        emulator.hook_add(UC_HOOK_CODE, mutate_on_second_call)
        self.execute(emulator)

        self.assertEqual(calls, 2)
        self.assertEqual(self.action_pair(emulator), (0x4124, 0x4124))
        self.assertEqual(self.result_pair(emulator), (0, 0))

    def test_register_flags_xmm_and_nested_call_alignment_are_transparent(self):
        emulator = self.make_emulator()
        before = {
            register: emulator.reg_read(register)
            for register in (
                UC_X86_REG_RAX,
                UC_X86_REG_RBX,
                UC_X86_REG_RCX,
                UC_X86_REG_RDX,
                UC_X86_REG_RBP,
                UC_X86_REG_RSI,
                UC_X86_REG_RDI,
                UC_X86_REG_R8,
                UC_X86_REG_R9,
                UC_X86_REG_R10,
                UC_X86_REG_R11,
                UC_X86_REG_R12,
                UC_X86_REG_R13,
                UC_X86_REG_R14,
                UC_X86_REG_R15,
                UC_X86_REG_EFLAGS,
                UC_X86_REG_XMM0,
                UC_X86_REG_XMM1,
                UC_X86_REG_XMM2,
                UC_X86_REG_XMM3,
                UC_X86_REG_XMM4,
                UC_X86_REG_XMM5,
            )
        }
        alignments = []

        def capture_alignment(uc, address, _size, _user):
            if address in (RESOLVER, PHYS_HELPER):
                alignments.append(uc.reg_read(UC_X86_REG_RSP) & 0xF)

        emulator.hook_add(UC_HOOK_CODE, capture_alignment)
        initial_rsp = emulator.reg_read(UC_X86_REG_RSP)
        self.execute(emulator)

        for register, value in before.items():
            self.assertEqual(emulator.reg_read(register), value, hex(register))
        self.assertEqual(emulator.reg_read(UC_X86_REG_RSP), initial_rsp + 8)
        self.assertEqual(alignments, [8, 8, 8, 8])

    @unittest.skipUnless(Cs is not None, "capstone is required for decode coverage")
    def test_helper_decodes_completely_and_all_direct_branches_are_mapped(self):
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = tuple(decoder.disasm(self.helper, HELPER))
        self.assertEqual(sum(instruction.size for instruction in instructions), len(self.helper))
        starts = {instruction.address for instruction in instructions}
        end = HELPER + len(self.helper)
        for instruction in instructions:
            if not instruction.mnemonic.startswith("j"):
                continue
            target = int(instruction.op_str, 16)
            self.assertGreaterEqual(target, HELPER)
            self.assertLess(target, end)
            self.assertIn(target, starts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
