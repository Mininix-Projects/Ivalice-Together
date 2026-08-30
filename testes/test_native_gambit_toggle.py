from __future__ import annotations

import inspect
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs
except ImportError:  # pragma: no cover
    Cs = None

try:
    from unicorn import UC_ARCH_X86, UC_HOOK_CODE, UC_MODE_64, Uc
    from unicorn.x86_const import (
        UC_X86_REG_R12,
        UC_X86_REG_R13,
        UC_X86_REG_R14,
        UC_X86_REG_R15,
        UC_X86_REG_RBP,
        UC_X86_REG_RBX,
        UC_X86_REG_RCX,
        UC_X86_REG_RDI,
        UC_X86_REG_RDX,
        UC_X86_REG_RIP,
        UC_X86_REG_RSI,
        UC_X86_REG_RSP,
    )
except ImportError:  # pragma: no cover
    Uc = None

import coop_runtime as coop
import headless_menu_core as menu
import headless_menu_shell as shell


CAVE_BASE = 0x50000000
CAVE = CAVE_BASE + menu.DISPATCH_CAVE_OFFSET
MAILBOX = CAVE_BASE + menu.DISPATCH_STATE_OFFSET
NATIVE = 0x51000000
ENTITY_RESOLVER = NATIVE + 0x000
PHYSICS_HELPER = NATIVE + 0x100
MEMBER_RESOLVER = NATIVE + 0x200
QUERY_GAMBITS = NATIVE + 0x300
SET_GAMBITS = NATIVE + 0x400
RUNTIME_ROOT_GLOBAL = 0x52000000
ACTOR = 0x53000000
ENTRY = 0x53002000
ACTOR_ROOT = 0x53003000
PHYSICS = 0x53004000
RUNTIME_ROOT = 0x53008000
MEMBER_ID = 5
BATTLE_KEEP = (
    RUNTIME_ROOT
    + menu.PARTY_MEMBER_RUNTIME_BASE_OFFSET
    + MEMBER_ID * menu.PARTY_MEMBER_RUNTIME_STRIDE
)
OTHER_ACTOR = 0x53010000
STACK = 0x54000000
HANDLE = 0x03100031
SEQUENCE = 2


def build_test_dispatcher() -> bytes:
    return menu.build_gambit_dispatcher(
        CAVE,
        MAILBOX,
        entity_resolver=ENTITY_RESOLVER,
        physics_helper=PHYSICS_HELPER,
        member_record_resolver=MEMBER_RESOLVER,
        query_gambits=QUERY_GAMBITS,
        set_gambits=SET_GAMBITS,
        runtime_root_global=RUNTIME_ROOT_GLOBAL,
    )


class GambitDispatcherShapeTests(unittest.TestCase):
    @unittest.skipUnless(Cs is not None, "capstone is required for decode coverage")
    def test_cave_decodes_completely_and_calls_only_the_proven_native_abi(self):
        code = build_test_dispatcher()
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = tuple(decoder.disasm(code, CAVE))
        self.assertEqual(sum(instruction.size for instruction in instructions), len(code))
        self.assertLessEqual(len(code), menu.DISPATCH_CAVE_SIZE)
        self.assertEqual(code[:2], b"\x48\xb8")
        self.assertEqual(struct.unpack("<Q", code[2:10])[0], MAILBOX)

        starts = {instruction.address for instruction in instructions}
        end = CAVE + len(code)
        for instruction in instructions:
            if not instruction.mnemonic.startswith("j"):
                continue
            target = int(instruction.op_str, 16)
            if target == menu.UI_MANAGER_TICK_RETURN:
                continue
            self.assertGreaterEqual(target, CAVE)
            self.assertLess(target, end)
            self.assertIn(target, starts)

        def native_call(target: int) -> bytes:
            return b"\x49\xbb" + struct.pack("<Q", target) + b"\x41\xff\xd3"

        self.assertEqual(code.count(native_call(ENTITY_RESOLVER)), 2)
        self.assertEqual(code.count(native_call(PHYSICS_HELPER)), 2)
        self.assertEqual(code.count(native_call(MEMBER_RESOLVER)), 3)
        self.assertEqual(code.count(native_call(QUERY_GAMBITS)), 1)
        self.assertEqual(code.count(native_call(SET_GAMBITS)), 1)
        setter_at = code.index(native_call(SET_GAMBITS))
        self.assertEqual(code[setter_at - 4 : setter_at], b"\x8b\xcd\x8b\xd7")
        self.assertNotIn(struct.pack("<Q", 0x2092758), code)

        default = menu.build_gambit_dispatcher(CAVE, MAILBOX)
        self.assertEqual(menu.SET_MEMBER_GAMBITS, 0x311AF0)
        self.assertEqual(menu.QUERY_MEMBER_GAMBITS, 0x309B40)
        self.assertEqual(menu.RESOLVE_PARTY_MEMBER_RECORD, 0x320A40)
        self.assertEqual(default.count(native_call(menu.SET_MEMBER_GAMBITS)), 1)

    def test_mailbox_layout_is_inside_the_existing_dispatch_state_reservation(self):
        self.assertEqual(menu.GAMBIT_DISPATCH_MAILBOX_STRUCT.size, 0x40)
        self.assertLessEqual(
            menu.GAMBIT_DISPATCH_MAILBOX_STRUCT.size,
            menu.DISPATCH_STATE_SIZE,
        )
        self.assertLessEqual(
            menu.DISPATCH_STATE_OFFSET + menu.DISPATCH_STATE_SIZE,
            menu.INDICATOR_CAVE_OFFSET,
        )


@unittest.skipUnless(Uc is not None, "unicorn is required for native emulation")
class GambitDispatcherExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = build_test_dispatcher()
        cls.physics_helper = bytes(coop.build_actor_physics_helper())

    @staticmethod
    def resolver_stub(expected: int, result: int) -> bytes:
        # cmp ecx,expected ; jne miss ; mov rax,result ; ret ; xor eax,eax ; ret
        return (
            b"\x81\xf9"
            + struct.pack("<I", expected)
            + b"\x75\x0b\x48\xb8"
            + struct.pack("<Q", result)
            + b"\xc3\x31\xc0\xc3"
        )

    @staticmethod
    def query_stub() -> bytes:
        return (
            b"\x48\xb8"
            + struct.pack("<Q", BATTLE_KEEP)
            + b"\x8b\x00\xc1\xe8\x02\x83\xe0\x01\xc3"
        )

    @staticmethod
    def setter_stub() -> bytes:
        return (
            b"\x48\xb8"
            + struct.pack("<Q", BATTLE_KEEP)
            + b"\x85\xd2\x74\x04\x83\x08\x04\xc3\x83\x20\xfb\xc3"
        )

    def make_emulator(
        self,
        *,
        word: int = 0xA5A5A5A1,
        handle: int = HANDLE,
        permanent_status: int = 0,
        temporary_status: int = 0,
        runtime_root: int = RUNTIME_ROOT,
    ) -> Uc:
        emulator = Uc(UC_ARCH_X86, UC_MODE_64)
        emulator.mem_map(CAVE_BASE, 0x10000)
        emulator.mem_write(CAVE, self.code)
        emulator.mem_map(NATIVE, 0x1000)
        emulator.mem_write(
            ENTITY_RESOLVER,
            self.resolver_stub(HANDLE, ENTRY),
        )
        emulator.mem_write(PHYSICS_HELPER, self.physics_helper)
        emulator.mem_write(
            MEMBER_RESOLVER,
            self.resolver_stub(MEMBER_ID, BATTLE_KEEP),
        )
        emulator.mem_write(QUERY_GAMBITS, self.query_stub())
        emulator.mem_write(SET_GAMBITS, self.setter_stub())
        emulator.mem_map(RUNTIME_ROOT_GLOBAL, 0x1000)
        emulator.mem_write(
            RUNTIME_ROOT_GLOBAL,
            struct.pack("<Q", runtime_root),
        )
        emulator.mem_map(ACTOR, 0x20000)
        emulator.mem_write(ACTOR + 0x10, struct.pack("<Q", ACTOR_ROOT))
        emulator.mem_write(ACTOR_ROOT + 0xB8, struct.pack("<Q", PHYSICS))
        emulator.mem_write(ACTOR + 0x698, struct.pack("<Q", BATTLE_KEEP))
        emulator.mem_write(ENTRY + 0x30, struct.pack("<Q", ACTOR))
        emulator.mem_write(BATTLE_KEEP, struct.pack("<I", word))
        emulator.mem_write(
            BATTLE_KEEP + coop.PERMANENT_STATUS_EFFECTS_OFFSET,
            struct.pack("<I", permanent_status),
        )
        emulator.mem_write(
            BATTLE_KEEP + coop.TEMPORARY_STATUS_EFFECTS_OFFSET,
            struct.pack("<I", temporary_status),
        )
        emulator.mem_write(
            MAILBOX,
            menu.GAMBIT_DISPATCH_MAILBOX_STRUCT.pack(
                1,
                SEQUENCE,
                0,
                menu.GAMBIT_DISPATCH_RESULT_PENDING,
                handle,
                MEMBER_ID,
                ACTOR,
                PHYSICS,
                BATTLE_KEEP,
                RUNTIME_ROOT,
                0,
                0,
            ),
        )
        emulator.mem_map(0x00280000, 0x20000)
        emulator.mem_write(menu.UI_MANAGER_TICK, b"\xc3")
        emulator.mem_map(STACK, 0x4000)
        emulator.reg_write(UC_X86_REG_RSP, STACK + 0x2000)
        emulator.reg_write(UC_X86_REG_RIP, CAVE)
        emulator.reg_write(UC_X86_REG_RCX, 0x1122334455667788)
        emulator.reg_write(UC_X86_REG_RBX, 0x0101010102020202)
        emulator.reg_write(UC_X86_REG_RBP, 0x0303030304040404)
        emulator.reg_write(UC_X86_REG_RSI, 0x0505050506060606)
        emulator.reg_write(UC_X86_REG_RDI, 0x0707070708080808)
        emulator.reg_write(UC_X86_REG_R12, 0x090909090A0A0A0A)
        emulator.reg_write(UC_X86_REG_R13, 0x0B0B0B0B0C0C0C0C)
        emulator.reg_write(UC_X86_REG_R14, 0x0D0D0D0D0E0E0E0E)
        emulator.reg_write(UC_X86_REG_R15, 0x0F0F0F0F10101010)
        return emulator

    @staticmethod
    def execute(emulator: Uc) -> None:
        emulator.emu_start(CAVE, menu.UI_MANAGER_TICK_RETURN, count=5000)

    @staticmethod
    def mailbox(emulator: Uc) -> tuple[int, ...]:
        return menu.GAMBIT_DISPATCH_MAILBOX_STRUCT.unpack(
            emulator.mem_read(MAILBOX, menu.GAMBIT_DISPATCH_MAILBOX_STRUCT.size)
        )

    @staticmethod
    def word(emulator: Uc) -> int:
        return struct.unpack("<I", emulator.mem_read(BATTLE_KEEP, 4))[0]

    def test_native_setter_toggles_both_directions_and_preserves_other_bits(self):
        for before in (0xA5A5A5A1, 0x5A5A5A5E):
            with self.subTest(before=hex(before)):
                emulator = self.make_emulator(word=before)
                setter_calls: list[tuple[int, int, int]] = []

                def observe_setter(uc, address, _size, _user):
                    if address == SET_GAMBITS:
                        setter_calls.append(
                            (
                                uc.reg_read(UC_X86_REG_RCX),
                                uc.reg_read(UC_X86_REG_RDX),
                                uc.reg_read(UC_X86_REG_RSP) & 0xF,
                            )
                        )

                emulator.hook_add(UC_HOOK_CODE, observe_setter)
                self.execute(emulator)
                desired = int(not bool(before & 4))
                expected = (before & ~4) | (desired << 2)
                self.assertEqual(self.word(emulator), expected)
                record = self.mailbox(emulator)
                self.assertEqual(record[0], 3)
                self.assertEqual(record[2:4], (SEQUENCE, menu.GAMBIT_DISPATCH_RESULT_APPLIED))
                self.assertEqual(record[10], desired)
                self.assertEqual(setter_calls, [(MEMBER_ID, desired, 8)])

    def test_generation_swap_between_the_two_proofs_rejects_without_setter(self):
        emulator = self.make_emulator()
        resolver_calls = 0
        setter_calls = 0

        def swap_generation(uc, address, _size, _user):
            nonlocal resolver_calls, setter_calls
            if address == ENTITY_RESOLVER:
                resolver_calls += 1
                if resolver_calls == 2:
                    uc.mem_write(ENTRY + 0x30, struct.pack("<Q", OTHER_ACTOR))
            elif address == SET_GAMBITS:
                setter_calls += 1

        emulator.hook_add(UC_HOOK_CODE, swap_generation)
        before = self.word(emulator)
        self.execute(emulator)
        self.assertEqual(resolver_calls, 2)
        self.assertEqual(setter_calls, 0)
        self.assertEqual(self.word(emulator), before)
        record = self.mailbox(emulator)
        self.assertEqual(record[0], 4)
        self.assertEqual(record[3], menu.GAMBIT_DISPATCH_RESULT_STALE_IDENTITY)

    def test_ko_stone_and_loading_root_reject_without_mutation(self):
        cases = (
            dict(permanent_status=coop.KO_STATUS_MASK),
            dict(temporary_status=coop.STONE_STATUS_MASK),
            dict(runtime_root=0),
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                emulator = self.make_emulator(**kwargs)
                setter_calls = 0

                def count_setter(_uc, address, _size, _user):
                    nonlocal setter_calls
                    if address == SET_GAMBITS:
                        setter_calls += 1

                emulator.hook_add(UC_HOOK_CODE, count_setter)
                before = self.word(emulator)
                self.execute(emulator)
                self.assertEqual(setter_calls, 0)
                self.assertEqual(self.word(emulator), before)
                record = self.mailbox(emulator)
                self.assertEqual(record[0], 4)
                expected = (
                    menu.GAMBIT_DISPATCH_RESULT_STALE_BATTLE_KEEP
                    if kwargs.get("runtime_root") == 0
                    else menu.GAMBIT_DISPATCH_RESULT_BLOCKED_STATUS
                )
                self.assertEqual(record[3], expected)

    def test_full_generation_handle_is_required(self):
        emulator = self.make_emulator(handle=HANDLE ^ (1 << 20))
        before = self.word(emulator)
        self.execute(emulator)
        self.assertEqual(self.word(emulator), before)
        self.assertEqual(
            self.mailbox(emulator)[3],
            menu.GAMBIT_DISPATCH_RESULT_STALE_IDENTITY,
        )

    def test_nonvolatile_registers_and_stock_tick_argument_are_transparent(self):
        emulator = self.make_emulator()
        registers = (
            UC_X86_REG_RBX,
            UC_X86_REG_RBP,
            UC_X86_REG_RSI,
            UC_X86_REG_RDI,
            UC_X86_REG_R12,
            UC_X86_REG_R13,
            UC_X86_REG_R14,
            UC_X86_REG_R15,
        )
        before = {register: emulator.reg_read(register) for register in registers}
        original_rcx = emulator.reg_read(UC_X86_REG_RCX)
        stock_rcx: list[int] = []

        def observe_stock(uc, address, _size, _user):
            if address == menu.UI_MANAGER_TICK:
                stock_rcx.append(uc.reg_read(UC_X86_REG_RCX))

        emulator.hook_add(UC_HOOK_CODE, observe_stock)
        self.execute(emulator)
        self.assertEqual(stock_rcx, [original_rcx])
        for register, value in before.items():
            self.assertEqual(emulator.reg_read(register), value, hex(register))


class MemoryProcess:
    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.mutations: list[tuple[str, int, bytes]] = []
        self.forced_state_address = 0
        self.forced_state: int | None = None
        self.damage_base = 0x60000000
        self.seed(menu.UI_MANAGER_TICK_CALL, menu.ORIGINAL_TICK_CALL)

    def seed(self, address: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.memory[address + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address: int, data: bytes | bytearray) -> None:
        payload = bytes(data)
        self.mutations.append(("write", address, payload))
        self.seed(address, payload)

    def patch_code(self, address: int, data: bytes) -> None:
        payload = bytes(data)
        self.mutations.append(("patch", address, payload))
        self.seed(address, payload)

    def u32(self, address: int) -> int:
        if address == self.forced_state_address and self.forced_state is not None:
            return self.forced_state
        return struct.unpack("<I", self.read(address, 4))[0]

    def ensure_damage_number_theme_allocation(self) -> int:
        return self.damage_base

    def ensure_cave_allocation(self, *, allow_allocate: bool = True) -> int:
        del allow_allocate
        return CAVE_BASE


class GambitPublisherTests(unittest.TestCase):
    def identity(self) -> dict[str, int]:
        return dict(
            member_id=MEMBER_ID,
            handle=HANDLE,
            actor=ACTOR,
            physics=PHYSICS,
            battle_keep=BATTLE_KEEP,
            runtime_root=RUNTIME_ROOT,
        )

    def test_python_request_mutates_only_the_mailbox_after_static_install(self):
        process = MemoryProcess()
        dispatcher = menu.NativeActionSubmitter(process, CAVE_BASE)
        root = process.read(menu.UI_MANAGER_TICK_CALL, 5)
        callback = process.read(dispatcher.cave, menu.DISPATCH_CAVE_SIZE)
        process.mutations.clear()
        original_write = process.write

        def complete_on_arm(address: int, data: bytes | bytearray) -> None:
            original_write(address, data)
            if address != dispatcher.state or bytes(data) != struct.pack("<I", 1):
                return
            sequence = process.u32(dispatcher.state + menu.GAMBIT_DISPATCH_SEQUENCE_OFFSET)
            process.seed(
                dispatcher.state + menu.GAMBIT_DISPATCH_ACK_SEQUENCE_OFFSET,
                struct.pack("<I", sequence),
            )
            process.seed(
                dispatcher.state + menu.GAMBIT_DISPATCH_RESULT_OFFSET,
                struct.pack("<I", menu.GAMBIT_DISPATCH_RESULT_APPLIED),
            )
            process.seed(
                dispatcher.state + menu.GAMBIT_DISPATCH_FINAL_ENABLED_OFFSET,
                struct.pack("<I", 1),
            )
            process.seed(dispatcher.state, struct.pack("<I", 3))

        process.write = complete_on_arm
        self.assertTrue(dispatcher.toggle_gambits(**self.identity()))

        writes = [item for item in process.mutations if item[0] == "write"]
        self.assertTrue(writes)
        for _kind, address, payload in writes:
            in_mailbox = (
                dispatcher.state
                <= address
                < dispatcher.state + menu.DISPATCH_STATE_SIZE
            )
            self.assertTrue(in_mailbox, (hex(address), len(payload)))
        self.assertFalse(any(kind == "patch" for kind, _address, _data in process.mutations))
        self.assertEqual(process.read(menu.UI_MANAGER_TICK_CALL, 5), root)
        self.assertEqual(
            process.read(dispatcher.cave, menu.DISPATCH_CAVE_SIZE),
            callback,
        )
        self.assertFalse(any(address == BATTLE_KEEP for _kind, address, _data in writes))
        self.assertFalse(any(address == 0x2092758 for _kind, address, _data in writes))

    def test_armed_and_claimed_timeouts_remain_quarantined(self):
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                dispatcher = menu.NativeActionSubmitter(process, CAVE_BASE)
                process.forced_state_address = dispatcher.state
                original_write = process.write

                def hold_after_arm(address, data):
                    original_write(address, data)
                    if address == dispatcher.state and bytes(data) == struct.pack(
                        "<I", 1
                    ):
                        process.forced_state = dispatch_state

                process.write = hold_after_arm
                with mock.patch.object(menu.time, "monotonic", side_effect=(0.0, 3.0)):
                    with self.assertRaisesRegex(RuntimeError, "timed out"):
                        dispatcher.toggle_gambits(**self.identity())
                root = process.read(menu.UI_MANAGER_TICK_CALL, 5)
                mailbox = process.read(dispatcher.state, 0x40)
                cave = process.read(dispatcher.cave, len(dispatcher._dispatch_code))
                mutation_count = len(process.mutations)

                self.assertTrue(dispatcher.patched)
                self.assertTrue(dispatcher.quarantined)
                with self.assertRaisesRegex(RuntimeError, "still in flight"):
                    dispatcher.toggle_gambits(**self.identity())
                self.assertFalse(dispatcher.restore())
                self.assertEqual(len(process.mutations), mutation_count)
                self.assertEqual(process.read(menu.UI_MANAGER_TICK_CALL, 5), root)
                self.assertEqual(process.read(dispatcher.state, 0x40), mailbox)
                self.assertEqual(
                    process.read(dispatcher.cave, len(dispatcher._dispatch_code)),
                    cave,
                )

    def test_shell_toggle_methods_contain_no_direct_actor_or_save_write(self):
        source = "\n".join(
            inspect.getsource(method)
            for method in (
                shell.CoopMenuRuntime._gambit_role_identity,
                shell.CoopMenuRuntime._toggle_role_gambits,
                shell.CoopMenuRuntime.toggle_p1_gambits,
                shell.CoopMenuRuntime.toggle_p2_gambits,
                shell.CoopMenuRuntime.toggle_p3_gambits,
            )
        )
        self.assertNotIn("process.write", source)
        self.assertNotIn("MAIN_SAVE_OWNER_GLOBAL", source)
        self.assertNotIn("^ 0x04", source)


class RoleIdentityProcess:
    def __init__(self, entries: tuple[tuple[int, int, int, int], ...]) -> None:
        self.u32_values: dict[int, int] = {}
        self.u64_values: dict[int, int] = {
            shell.session_state.BATTLE_INFO_GLOBAL: RUNTIME_ROOT,
        }
        for member_id, handle, actor, physics in entries:
            actor_root = actor + 0x1000
            self.u32_values[actor + 0x08] = handle
            self.u64_values[actor + 0x10] = actor_root
            self.u64_values[actor_root + 0xB8] = physics
            self.u64_values[actor + 0x698] = (
                RUNTIME_ROOT
                + menu.PARTY_MEMBER_RUNTIME_BASE_OFFSET
                + member_id * menu.PARTY_MEMBER_RUNTIME_STRIDE
            )

    def u32(self, address: int) -> int:
        return self.u32_values.get(address, 0)

    def u64(self, address: int) -> int:
        return self.u64_values.get(address, 0)


class GambitRoleIntegrationTests(unittest.TestCase):
    def make_runtime(self, *, loading: bool = False):
        entries = (
            (0, 0x01100001, 0x61000000, 0x62000000),
            (5, 0x01200002, 0x61010000, 0x62010000),
            (2, 0x01300003, 0x61020000, 0x62020000),
        )
        runtime = shell.CoopMenuRuntime.__new__(shell.CoopMenuRuntime)
        runtime.process = RoleIdentityProcess(entries)
        runtime.active_party = [
            (member_id, str(member_id), handle, actor)
            for member_id, handle, actor, _physics in entries
        ]
        runtime.session_snapshot = SimpleNamespace(
            loading=loading,
            battle_info=RUNTIME_ROOT,
            p1_owner_party_member_id=0,
            identity=SimpleNamespace(
                p2_party_member_id=5,
                p3_party_member_id=2,
            ),
        )
        runtime.refresh_actors = mock.Mock()
        runtime.native_submitter = mock.Mock()
        runtime.native_submitter.toggle_gambits.return_value = True
        return runtime

    def test_p1_p2_and_p3_publish_their_exact_member_generation_contracts(self):
        runtime = self.make_runtime()
        self.assertTrue(runtime.toggle_p1_gambits())
        self.assertTrue(runtime.toggle_p2_gambits())
        self.assertTrue(runtime.toggle_p3_gambits())

        contracts = [
            call.kwargs
            for call in runtime.native_submitter.toggle_gambits.call_args_list
        ]
        self.assertEqual([contract["member_id"] for contract in contracts], [0, 5, 2])
        self.assertEqual(
            [contract["handle"] for contract in contracts],
            [0x01100001, 0x01200002, 0x01300003],
        )
        for contract in contracts:
            self.assertEqual(
                contract["battle_keep"],
                RUNTIME_ROOT
                + menu.PARTY_MEMBER_RUNTIME_BASE_OFFSET
                + contract["member_id"] * menu.PARTY_MEMBER_RUNTIME_STRIDE,
            )

    def test_loading_rejects_before_arming_the_native_dispatcher(self):
        runtime = self.make_runtime(loading=True)
        with self.assertRaisesRegex(RuntimeError, "while loading"):
            runtime.toggle_p2_gambits()
        runtime.native_submitter.toggle_gambits.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
