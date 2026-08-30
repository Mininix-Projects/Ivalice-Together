"""Execution-level regressions for the atomic world-interaction consumer."""

import faulthandler
import struct
import unittest

try:
    from unicorn import Uc, UcError, UC_ARCH_X86, UC_HOOK_CODE, UC_MODE_64
    from unicorn.x86_const import (
        UC_X86_REG_R12,
        UC_X86_REG_R13,
        UC_X86_REG_R14,
        UC_X86_REG_R15,
        UC_X86_REG_RAX,
        UC_X86_REG_RBP,
        UC_X86_REG_RBX,
        UC_X86_REG_RCX,
        UC_X86_REG_RDI,
        UC_X86_REG_RIP,
        UC_X86_REG_RSI,
        UC_X86_REG_RSP,
    )
except ImportError:  # pragma: no cover - exercised only in minimal dev installs
    Uc = None
    UcError = RuntimeError

import headless_menu_shell as menu


ALLOCATION = 0x5A000000
ATOMIC_HELPER = ALLOCATION + menu.FOLLOWER_NPC_ATOMIC_CAVE_OFFSET
CONSUMER_WRAPPER = ALLOCATION + menu.P2_INTERACTION_CONSUMER_CAVE_OFFSET
MAILBOX = ALLOCATION + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
STOP = ALLOCATION + 0xF000
STACK = 0x71000000

SOURCE_ENTRY = 0x70000000
TARGET_ENTRY = 0x70001000
GATE_ENTRY = 0x70002000
GATE_ACTOR = 0x70003000
AUXILIARY_OBJECT = 0x70004000
TARGET_ACTION_METADATA = 0x70005000
TARGET_ACTION_QUEUE = 0x70006000

SOURCE_HANDLE = 0x00100001
TARGET_HANDLE = 0x00100002
GATE_HANDLE = 0x00100003

ORIGINAL_SOURCE = 0x1122334455667788
ORIGINAL_GATE = 0x7A
ORIGINAL_DESCRIPTOR = bytes(range(24))
ORIGINAL_OBJECT_FLAG = 0x54

if Uc is not None:
    NONVOLATILE_REGISTERS = {
        UC_X86_REG_RBX: 0x11112222,
        UC_X86_REG_RBP: 0x33334444,
        UC_X86_REG_RSI: 0x55556666,
        UC_X86_REG_RDI: 0x77778888,
        UC_X86_REG_R12: 0x9999AAAA,
        UC_X86_REG_R13: 0xBBBBCCCC,
        UC_X86_REG_R14: 0xDDDDEEEE,
        UC_X86_REG_R15: 0xFFFF0001,
    }
else:  # pragma: no cover - the test class is skipped in this configuration
    NONVOLATILE_REGISTERS = {}


def _u32(emulator, address):
    return struct.unpack("<I", emulator.mem_read(address, 4))[0]


def _write_u32(emulator, address, value):
    emulator.mem_write(address, struct.pack("<I", int(value) & 0xFFFFFFFF))


def _write_u64(emulator, address, value):
    emulator.mem_write(address, struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF))


@unittest.skipUnless(Uc is not None, "unicorn is required for native cave emulation")
class NativeInteractionConsumerExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.atomic_code = menu.build_atomic_follower_npc_interaction_cave(
            ATOMIC_HELPER,
            MAILBOX,
        )
        cls.wrapper_code = menu.build_p2_interaction_consumer_wrapper(
            MAILBOX,
            ATOMIC_HELPER,
        )

    def make_emulator(
        self,
        *,
        policy=menu.P2_INTERACTION_CONSUMER_POLICY_STOCK_OWNER,
        armed=1,
        source_live=True,
        source_has_physics=True,
        target_live=True,
        target_type=1,
        target_has_data=True,
        target_flags=0,
        descriptor_state=2,
        target_has_action_metadata=True,
        target_has_action_queue=True,
        auxiliary_live=True,
        exact_candidate=True,
        despawn_target_in_consumer=False,
        mutate_object_flag_in_consumer=False,
    ):
        # Unicorn's Windows backend probes reservations with a handled access
        # violation. Suppress faulthandler only around those expected probes.
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            emulator.mem_map(ALLOCATION, 0x10000)
            emulator.mem_write(ATOMIC_HELPER, self.atomic_code)
            emulator.mem_write(CONSUMER_WRAPPER, self.wrapper_code)

            emulator.mem_map(0x00200000, 0x00200000)
            for target in (
                menu.NATIVE_ENTITY_RESOLVER,
                menu.legacy.coop.INTERACTION_NATIVE_INDICATOR_SCAN,
                menu.legacy.coop.INTERACTION_BUILD_EXACT_HANDLE,
                menu.NATIVE_INTERACTION_AUXILIARY_RESOLVER,
                menu.NATIVE_INTERACTION_CONSUMER_TARGET,
            ):
                emulator.mem_write(target, b"\xC3")

            emulator.mem_map(0x01F00000, 0x00400000)
            emulator.mem_map(SOURCE_ENTRY, 0x8000)
            emulator.mem_map(STACK, 0x2000)
        finally:
            if restore_faulthandler:
                faulthandler.enable()

        emulator.mem_write(STOP, b"\xCC")
        emulator.mem_write(
            menu.NATIVE_INTERACTION_SOURCE,
            struct.pack("<Q", ORIGINAL_SOURCE),
        )
        emulator.mem_write(
            menu.NATIVE_INTERACTION_CAN_USE_GATE,
            bytes((ORIGINAL_GATE,)),
        )
        emulator.mem_write(
            menu.NATIVE_INTERACTION_DESCRIPTOR,
            ORIGINAL_DESCRIPTOR,
        )
        emulator.mem_write(
            menu.NATIVE_RENDERING_FLAGS,
            bytes((menu.NATIVE_UI_RENDER_ENABLED_MASK,)),
        )
        _write_u32(emulator, menu.NATIVE_INTERACTION_SUPPRESSION_FLAGS, 0)
        _write_u32(emulator, menu.NATIVE_INTERACTION_EVENT_FLAGS, 0)

        emulator.mem_write(
            MAILBOX,
            menu.P2_INTERACTION_CONSUMER_MAGIC
            + b"\x00"
            * (
                menu.P2_INTERACTION_CONSUMER_MAILBOX_SIZE
                - len(menu.P2_INTERACTION_CONSUMER_MAGIC)
            ),
        )
        _write_u32(
            emulator,
            MAILBOX + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET,
            armed,
        )
        _write_u32(
            emulator,
            MAILBOX + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
            policy,
        )
        _write_u32(
            emulator,
            MAILBOX + menu.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET,
            GATE_HANDLE,
        )
        _write_u32(
            emulator,
            MAILBOX + menu.P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,
            SOURCE_HANDLE,
        )
        emulator.mem_write(
            MAILBOX + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
            struct.pack(
                "<6I",
                menu.NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS,
                (TARGET_HANDLE >> 16) & 0x0F,
                TARGET_HANDLE & 0xFFFF,
                descriptor_state,
                TARGET_HANDLE,
                menu.NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS | 1,
            ),
        )

        _write_u64(
            emulator,
            SOURCE_ENTRY + 0xB8,
            AUXILIARY_OBJECT if source_has_physics else 0,
        )
        emulator.mem_write(TARGET_ENTRY + 0x0C, bytes((ORIGINAL_OBJECT_FLAG,)))
        emulator.mem_write(TARGET_ENTRY + 0x18, bytes((target_type,)))
        _write_u32(emulator, TARGET_ENTRY + 0x1C, target_flags)
        _write_u64(
            emulator,
            TARGET_ENTRY + 0x40,
            AUXILIARY_OBJECT if target_has_data else 0,
        )
        _write_u64(
            emulator,
            TARGET_ENTRY + 0x48,
            TARGET_ACTION_METADATA if target_has_action_metadata else 0,
        )
        _write_u64(
            emulator,
            TARGET_ENTRY + 0xA0,
            TARGET_ACTION_QUEUE if target_has_action_queue else 0,
        )
        _write_u32(emulator, TARGET_ACTION_METADATA, 1)
        _write_u64(emulator, GATE_ENTRY + 0x30, GATE_ACTOR)
        emulator.mem_write(GATE_ACTOR, b"\x00" * 0x720)
        _write_u32(emulator, GATE_ACTOR + 0x6B4, 1)
        _write_u32(emulator, GATE_ACTOR + 0x714, 0x4021)

        state = {
            "source_live": bool(source_live),
            "target_live": bool(target_live),
            "auxiliary_live": bool(auxiliary_live),
            "exact_candidate": bool(exact_candidate),
            "despawn_target_in_consumer": bool(despawn_target_in_consumer),
            "mutate_object_flag_in_consumer": bool(
                mutate_object_flag_in_consumer
            ),
            "atomic_entries": 0,
            "indicator_scans": 0,
            "consumer_calls": 0,
        }

        def native_hook(current, address, _size, _user_data):
            if address == ATOMIC_HELPER:
                state["atomic_entries"] += 1
                return
            if address == menu.NATIVE_ENTITY_RESOLVER:
                handle = current.reg_read(UC_X86_REG_RCX) & 0xFFFFFFFF
                resolved = 0
                if handle == SOURCE_HANDLE and state["source_live"]:
                    resolved = SOURCE_ENTRY
                elif handle == TARGET_HANDLE and state["target_live"]:
                    resolved = TARGET_ENTRY
                elif handle == GATE_HANDLE:
                    resolved = GATE_ENTRY
                current.reg_write(UC_X86_REG_RAX, resolved)
                return
            if address == menu.legacy.coop.INTERACTION_NATIVE_INDICATOR_SCAN:
                state["indicator_scans"] += 1
                if state["indicator_scans"] == 5 and state["exact_candidate"]:
                    current.mem_write(
                        menu.NATIVE_INTERACTION_DESCRIPTOR,
                        struct.pack(
                            "<6I",
                            menu.NATIVE_INTERACTION_RESET_DISTANCE_BITS,
                            (TARGET_HANDLE >> 16) & 0x0F,
                            TARGET_HANDLE & 0xFFFF,
                            2,
                            TARGET_HANDLE,
                            menu.NATIVE_INTERACTION_RESET_READY_FLAGS,
                        ),
                    )
                    current.mem_write(
                        menu.NATIVE_INTERACTION_CAN_USE_GATE,
                        b"\x01",
                    )
                return
            if address == menu.NATIVE_INTERACTION_AUXILIARY_RESOLVER:
                current.reg_write(
                    UC_X86_REG_RAX,
                    AUXILIARY_OBJECT if state["auxiliary_live"] else 0,
                )
                return
            if address == menu.NATIVE_INTERACTION_CONSUMER_TARGET:
                state["consumer_calls"] += 1
                if state["mutate_object_flag_in_consumer"]:
                    current.mem_write(TARGET_ENTRY + 0x0C, b"\x99")
                if state["despawn_target_in_consumer"]:
                    state["target_live"] = False

        emulator.hook_add(UC_HOOK_CODE, native_hook)
        return emulator, state

    def execute(self, emulator, start):
        stack_pointer = STACK + 0x1008
        _write_u64(emulator, stack_pointer, STOP)
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        for register, value in NONVOLATILE_REGISTERS.items():
            emulator.reg_write(register, value)

        try:
            emulator.emu_start(start, STOP, count=10000)
        except UcError as error:  # pragma: no cover - assertion adds diagnostics
            self.fail(f"native interaction cave faulted during emulation: {error}")

        self.assertEqual(emulator.reg_read(UC_X86_REG_RIP), STOP)
        self.assertEqual(emulator.reg_read(UC_X86_REG_RSP), stack_pointer + 8)
        for register, value in NONVOLATILE_REGISTERS.items():
            self.assertEqual(emulator.reg_read(register), value)

    def assert_native_state_restored(self, emulator):
        self.assertEqual(
            emulator.mem_read(menu.NATIVE_INTERACTION_SOURCE, 8),
            struct.pack("<Q", ORIGINAL_SOURCE),
        )
        self.assertEqual(
            emulator.mem_read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1),
            bytes((ORIGINAL_GATE,)),
        )
        self.assertEqual(
            emulator.mem_read(menu.NATIVE_INTERACTION_DESCRIPTOR, 24),
            ORIGINAL_DESCRIPTOR,
        )
        self.assertEqual(
            _u32(
                emulator,
                MAILBOX + menu.P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET,
            ),
            0,
        )

    def execute_atomic(self, **configuration):
        emulator, state = self.make_emulator(**configuration)
        self.execute(emulator, ATOMIC_HELPER)
        self.assert_native_state_restored(emulator)
        return emulator, state, emulator.reg_read(UC_X86_REG_RAX) & 0xFFFFFFFF

    def test_cas_already_claimed_never_enters_atomic_helper(self):
        emulator, state = self.make_emulator(armed=2)

        self.execute(emulator, CONSUMER_WRAPPER)

        self.assertEqual(state["atomic_entries"], 0)
        # A failed custom claim must preserve the original stock call.
        self.assertEqual(state["consumer_calls"], 1)
        self.assertEqual(
            _u32(
                emulator,
                MAILBOX + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET,
            ),
            2,
        )
        self.assertEqual(
            _u32(emulator, MAILBOX + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET),
            0,
        )
        self.assert_native_state_restored(emulator)

    def test_stale_source_handle_fails_closed(self):
        _emulator, state, result = self.execute_atomic(source_live=False)

        self.assertEqual(result, 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_exact_source_without_physics_fails_before_native_scan(self):
        _emulator, state, result = self.execute_atomic(
            policy=menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
            source_has_physics=False,
        )

        self.assertEqual(result, 0)
        self.assertEqual(state["indicator_scans"], 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_stale_target_handle_fails_closed(self):
        _emulator, state, result = self.execute_atomic(target_live=False)

        self.assertEqual(result, 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_non_type_one_target_requires_native_data_block(self):
        _emulator, state, result = self.execute_atomic(
            target_type=2,
            target_has_data=False,
        )

        self.assertEqual(result, 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_target_with_missing_auxiliary_object_fails_closed(self):
        _emulator, state, result = self.execute_atomic(
            target_flags=0x2000,
            auxiliary_live=False,
        )

        self.assertEqual(result, 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_state_two_target_with_torn_down_action_metadata_fails_closed(self):
        _emulator, state, result = self.execute_atomic(
            target_type=2,
            descriptor_state=2,
            target_flags=0x4,
            target_has_action_metadata=False,
        )

        self.assertEqual(result, 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_non_state_two_target_with_torn_down_action_queue_fails_closed(self):
        _emulator, state, result = self.execute_atomic(
            target_type=2,
            descriptor_state=1,
            target_flags=0x400,
            target_has_action_queue=False,
        )

        self.assertEqual(result, 0)
        self.assertEqual(state["consumer_calls"], 0)

    def test_action_subobjects_are_optional_outside_native_action_branch(self):
        for target_type, descriptor_state, target_flags in (
            (2, 2, 0x400),
            (2, 1, 0x4),
            (1, 2, 0x4),
        ):
            with self.subTest(
                target_type=target_type,
                descriptor_state=descriptor_state,
                target_flags=target_flags,
            ):
                _emulator, state, result = self.execute_atomic(
                    target_type=target_type,
                    descriptor_state=descriptor_state,
                    target_flags=target_flags,
                    target_has_action_metadata=False,
                    target_has_action_queue=False,
                )

                self.assertEqual(result, 1)
                self.assertEqual(state["consumer_calls"], 1)

    def test_live_action_subobjects_allow_both_native_action_branches(self):
        for descriptor_state, target_flags in ((2, 0x4), (1, 0x400)):
            with self.subTest(
                descriptor_state=descriptor_state,
                target_flags=target_flags,
            ):
                _emulator, state, result = self.execute_atomic(
                    target_type=2,
                    descriptor_state=descriptor_state,
                    target_flags=target_flags,
                )

                self.assertEqual(result, 1)
                self.assertEqual(state["consumer_calls"], 1)

    def test_target_despawn_after_consumer_skips_stale_flag_restore(self):
        emulator, state, result = self.execute_atomic(
            despawn_target_in_consumer=True,
        )

        self.assertEqual(result, 1)
        self.assertEqual(state["consumer_calls"], 1)
        self.assertEqual(
            emulator.mem_read(TARGET_ENTRY + 0x0C, 1),
            bytes((ORIGINAL_OBJECT_FLAG | 2,)),
        )

    def test_success_restores_target_flag_byte_exactly(self):
        emulator, state, result = self.execute_atomic()

        self.assertEqual(result, 1)
        self.assertEqual(state["consumer_calls"], 1)
        self.assertEqual(
            emulator.mem_read(TARGET_ENTRY + 0x0C, 1),
            bytes((ORIGINAL_OBJECT_FLAG,)),
        )

    def test_consumer_owned_flag_change_is_not_overwritten(self):
        emulator, state, result = self.execute_atomic(
            mutate_object_flag_in_consumer=True,
        )

        self.assertEqual(result, 1)
        self.assertEqual(state["consumer_calls"], 1)
        self.assertEqual(emulator.mem_read(TARGET_ENTRY + 0x0C, 1), b"\x99")


if __name__ == "__main__":
    unittest.main()
