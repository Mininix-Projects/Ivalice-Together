"""Process-lifetime regressions for the shared native UI dispatcher callback."""

from __future__ import annotations

import struct
import threading
import unittest

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
        UC_X86_REG_RIP,
        UC_X86_REG_RSI,
        UC_X86_REG_RSP,
    )
except ImportError:  # pragma: no cover
    Uc = None

import coop_runtime as coop
import headless_menu_core as core
import native_ui_tick_mux as tick_mux


PRIMARY_BASE = 0x50000000
DAMAGE_BASE = 0x60000000


class InjectedProcess:
    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.operations: list[tuple[str, int, bytes]] = []
        self.operation_count = 0
        self.fail_operation: int | None = None
        self.fail_after = False
        self.seed(core.UI_MANAGER_TICK_CALL, core.ORIGINAL_TICK_CALL)

    def seed(self, address: int, payload: bytes) -> None:
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(
            self.memory.get(int(address) + offset, 0) for offset in range(int(size))
        )

    def _mutate(self, kind: str, address: int, payload: bytes) -> None:
        address = int(address)
        payload = bytes(payload)
        self.operation_count += 1
        failing = self.operation_count == self.fail_operation
        if failing and not self.fail_after:
            raise OSError(f"injected {kind} failure before mutation")
        self.operations.append((kind, address, payload))
        self.seed(address, payload)
        if failing:
            raise OSError(f"injected {kind} failure after mutation")

    def write(self, address: int, payload: bytes) -> None:
        self._mutate("write", address, payload)

    def patch_code(self, address: int, payload: bytes) -> None:
        self._mutate("patch", address, payload)

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def ensure_damage_number_theme_allocation(self) -> int:
        return DAMAGE_BASE

    def ensure_cave_allocation(self, *, allow_allocate: bool = True) -> int:
        del allow_allocate
        return PRIMARY_BASE

    def arm_failure(self, operation: int, *, after: bool) -> None:
        self.operation_count = 0
        self.fail_operation = int(operation)
        self.fail_after = bool(after)

    def clear_failure(self) -> None:
        self.operation_count = 0
        self.fail_operation = None
        self.fail_after = False
        self.operations.clear()


def action_mailbox(*, state: int, sequence: int, ack: int = 0) -> bytes:
    payload = bytearray(core.DISPATCH_OPERATION_OFFSET + 4)
    struct.pack_into("<I", payload, 0, state)
    struct.pack_into("<I", payload, 4, sequence)
    struct.pack_into("<I", payload, 8, ack)
    struct.pack_into("<I", payload, core.ACTION_DISPATCH_ISSUER_OFFSET, 0x11)
    struct.pack_into("<I", payload, core.ACTION_DISPATCH_TARGET_OFFSET, 0x22)
    struct.pack_into("<I", payload, core.ACTION_DISPATCH_ACTION_OFFSET, 0x33)
    struct.pack_into(
        "<I",
        payload,
        core.ACTION_DISPATCH_RESERVE_MEMBER_OFFSET,
        core.DISPATCH_RESERVE_MEMBER_NONE,
    )
    struct.pack_into(
        "<I",
        payload,
        core.DISPATCH_OPERATION_OFFSET,
        core.DISPATCH_OPERATION_ACTION,
    )
    return bytes(payload)


class NativeCoreTopologyTests(unittest.TestCase):
    def assert_complete(self, process: InjectedProcess, dispatcher) -> None:
        topology = dispatcher._topology
        mux = topology.mux
        self.assertTrue(topology.resident_exact())
        self.assertEqual(
            process.read(topology.cave, core.DISPATCH_CAVE_SIZE),
            topology.callback_image,
        )
        self.assertEqual(
            mux.handler(tick_mux.SLOT_HEADLESS_CORE),
            topology.cave,
        )
        self.assertEqual(process.read(core.UI_MANAGER_TICK_CALL, 5), mux.patch)

    def test_failure_before_and_after_every_publication_is_retry_safe(self):
        # mux image, mux root, callback, proof/state image, callback slot
        for operation in range(1, 6):
            for after in (False, True):
                with self.subTest(operation=operation, after=after):
                    process = InjectedProcess()
                    process.arm_failure(operation, after=after)
                    with self.assertRaises(BaseException):
                        core.NativeActionSubmitter(process, PRIMARY_BASE)

                    self.assertNotIn(
                        ("patch", core.UI_MANAGER_TICK_CALL, core.ORIGINAL_TICK_CALL),
                        process.operations,
                    )
                    process.clear_failure()
                    retried = core.NativeActionSubmitter(process, PRIMARY_BASE)
                    self.assert_complete(process, retried)

    def test_exact_active_adoption_is_read_only_and_keeps_quarantine(self):
        process = InjectedProcess()
        owner = core.NativeActionSubmitter(process, PRIMARY_BASE)
        process.seed(owner.state, action_mailbox(state=1, sequence=2))
        expected_memory = dict(process.memory)
        process.operations.clear()

        adopted = core.NativeActionSubmitter(process, PRIMARY_BASE)

        self.assertTrue(adopted.patched)
        self.assertTrue(adopted.quarantined)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])
        self.assertFalse(adopted.restore())
        self.assertEqual(process.operations, [])

    def test_partial_or_foreign_local_images_abort_before_broker_writes(self):
        for case in ("callback", "proof"):
            with self.subTest(case=case):
                process = InjectedProcess()
                topology = core._NativeCoreDispatchTopology(process, PRIMARY_BASE)
                if case == "callback":
                    process.seed(topology.cave, b"\xcc")
                else:
                    process.seed(topology.cave, topology.callback_image)
                    process.seed(
                        PRIMARY_BASE + core.CORE_DYNAMIC_REGION_OFFSET,
                        topology.dynamic_initial_image,
                    )
                    process.seed(topology.proof_cave, b"\xcc")
                expected = dict(process.memory)
                process.operations.clear()

                with self.assertRaisesRegex(RuntimeError, "partial/foreign"):
                    core.NativeActionSubmitter(process, PRIMARY_BASE)

                self.assertEqual(process.memory, expected)
                self.assertEqual(process.operations, [])
                self.assertEqual(
                    process.read(core.UI_MANAGER_TICK_CALL, 5),
                    core.ORIGINAL_TICK_CALL,
                )

    def test_terminal_release_and_restore_never_touch_root_or_code(self):
        process = InjectedProcess()
        dispatcher = core.NativeActionSubmitter(process, PRIMARY_BASE)
        root = process.read(core.UI_MANAGER_TICK_CALL, 5)
        callback = process.read(dispatcher.cave, core.DISPATCH_CAVE_SIZE)
        proof = process.read(
            PRIMARY_BASE + core.INDICATOR_CAVE_OFFSET,
            core.INDICATOR_CAVE_SIZE,
        )
        process.seed(dispatcher.state, action_mailbox(state=3, sequence=2, ack=2))
        dispatcher._pending_sequence = 2
        dispatcher._pending_operation = core.DISPATCH_OPERATION_ACTION
        dispatcher.patched = True
        process.operations.clear()

        self.assertTrue(dispatcher.maintain())
        self.assertEqual(process.operations, [])
        self.assertTrue(dispatcher.restore())

        self.assertEqual(process.read(core.UI_MANAGER_TICK_CALL, 5), root)
        self.assertEqual(
            process.read(dispatcher.cave, core.DISPATCH_CAVE_SIZE), callback
        )
        self.assertEqual(
            process.read(
                PRIMARY_BASE + core.INDICATOR_CAVE_OFFSET,
                core.INDICATOR_CAVE_SIZE,
            ),
            proof,
        )
        self.assertEqual(process.operations, [("write", dispatcher.state, b"\0" * 4)])

    def test_action_and_indicator_mailboxes_can_complete_concurrently(self):
        process = InjectedProcess()
        action = core.NativeActionSubmitter(process, PRIMARY_BASE)
        indicator = core.NativeCombatIndicatorRouter(process, PRIMARY_BASE)
        root = process.read(core.UI_MANAGER_TICK_CALL, 5)
        callback = process.read(action.cave, core.DISPATCH_CAVE_SIZE)
        action_armed = threading.Event()
        indicator_armed = threading.Event()
        original_write = process.write

        def complete_when_both_armed(address: int, payload: bytes) -> None:
            original_write(address, payload)
            if bytes(payload) != struct.pack("<I", 1):
                return
            if address == action.state:
                action_armed.set()
                self.assertTrue(indicator_armed.wait(2.0))
                sequence = process.u32(action.state + 4)
                process.seed(action.state + 8, struct.pack("<I", sequence))
                process.seed(action.state + 12, struct.pack("<i", 0x1234))
                process.seed(action.state, struct.pack("<I", 3))
            elif address == indicator.state:
                indicator_armed.set()
                self.assertTrue(action_armed.wait(2.0))
                sequence = process.u32(indicator.state + 4)
                process.seed(indicator.state + 8, struct.pack("<I", sequence))
                process.seed(indicator.state + 0x10, struct.pack("<Q", 0xABCDEF))
                process.seed(indicator.state, struct.pack("<I", 3))

        process.write = complete_when_both_armed
        process.operations.clear()
        results: dict[str, int] = {}
        errors: list[BaseException] = []

        def run_action() -> None:
            try:
                results["action"] = action.submit(0x11, 0x22, 0x33)
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        def run_indicator() -> None:
            try:
                results["indicator"] = indicator.set(0x44, 0x55)
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = (threading.Thread(target=run_action), threading.Thread(target=run_indicator))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3.0)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, {"action": 0x1234, "indicator": 0xABCDEF})
        self.assertFalse(any(kind == "patch" for kind, _address, _data in process.operations))
        self.assertEqual(process.read(core.UI_MANAGER_TICK_CALL, 5), root)
        self.assertEqual(process.read(action.cave, core.DISPATCH_CAVE_SIZE), callback)


class NativeCoreCodeShapeTests(unittest.TestCase):
    @unittest.skipUnless(Cs is not None, "capstone is required for decode coverage")
    def test_callback_and_proof_decode_within_their_strict_reservations(self):
        callback_address = PRIMARY_BASE + core.DISPATCH_CAVE_OFFSET
        proof_address = PRIMARY_BASE + core.INDICATOR_CAVE_OFFSET
        callback = core.build_core_tick_callback(
            callback_address,
            PRIMARY_BASE + core.DISPATCH_STATE_OFFSET,
            PRIMARY_BASE + core.INDICATOR_STATE_OFFSET,
            proof_cave=proof_address,
        )
        proof = core.build_core_identity_proof(
            proof_address,
            PRIMARY_BASE + core.DISPATCH_STATE_OFFSET,
        )
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        callback_instructions = tuple(decoder.disasm(callback, callback_address))
        proof_instructions = tuple(decoder.disasm(proof, proof_address))

        self.assertEqual(
            sum(instruction.size for instruction in callback_instructions),
            len(callback),
        )
        self.assertEqual(
            sum(instruction.size for instruction in proof_instructions),
            len(proof),
        )
        self.assertLessEqual(len(callback), core.DISPATCH_CAVE_SIZE)
        self.assertLessEqual(len(proof), core.INDICATOR_CAVE_SIZE)
        self.assertLessEqual(
            callback_address + len(callback),
            PRIMARY_BASE + core.DISPATCH_CAVE_END_OFFSET,
        )
        self.assertLessEqual(
            proof_address + len(proof),
            PRIMARY_BASE + core.INDICATOR_STATE_OFFSET,
        )
        proof_calls = tuple(
            instruction
            for instruction in callback_instructions
            if instruction.mnemonic == "call"
            and instruction.op_str == hex(proof_address)
        )
        self.assertEqual(len(proof_calls), 2)
        self.assertEqual(callback_instructions[-1].mnemonic, "ret")
        self.assertEqual(proof_instructions[-1].mnemonic, "ret")


@unittest.skipUnless(Uc is not None, "unicorn is required for native execution")
class NativeCoreGambitExecutionTests(unittest.TestCase):
    NATIVE = 0x51000000
    ENTITY = NATIVE
    PHYSICS_HELPER = NATIVE + 0x100
    MEMBER = NATIVE + 0x200
    QUERY = NATIVE + 0x300
    SETTER = NATIVE + 0x400
    ROOT_GLOBAL = 0x52000000
    ACTOR = 0x53000000
    ENTRY = 0x53002000
    ACTOR_ROOT = 0x53003000
    PHYSICS = 0x53004000
    RUNTIME_ROOT = 0x53008000
    MEMBER_ID = 5
    BATTLE_KEEP = (
        RUNTIME_ROOT
        + core.PARTY_MEMBER_RUNTIME_BASE_OFFSET
        + MEMBER_ID * core.PARTY_MEMBER_RUNTIME_STRIDE
    )
    STACK = 0x54000000
    RETURN = 0x55000000
    HANDLE = 0x03100031
    SEQUENCE = 2

    @staticmethod
    def resolver_stub(expected: int, result: int) -> bytes:
        return (
            b"\x81\xf9"
            + struct.pack("<I", expected)
            + b"\x75\x0b\x48\xb8"
            + struct.pack("<Q", result)
            + b"\xc3\x31\xc0\xc3"
        )

    def make_emulator(
        self,
        *,
        status: int = 0,
        actor_flags: int = core.ACTOR_ACTIVE_FLAG,
    ) -> tuple[Uc, bytes]:
        callback_address = PRIMARY_BASE + core.DISPATCH_CAVE_OFFSET
        proof_address = PRIMARY_BASE + core.INDICATOR_CAVE_OFFSET
        state = PRIMARY_BASE + core.DISPATCH_STATE_OFFSET
        indicator_state = PRIMARY_BASE + core.INDICATOR_STATE_OFFSET
        callback = core.build_core_tick_callback(
            callback_address,
            state,
            indicator_state,
            proof_cave=proof_address,
            entity_resolver=self.ENTITY,
            physics_helper=self.PHYSICS_HELPER,
            member_record_resolver=self.MEMBER,
            query_gambits=self.QUERY,
            set_gambits=self.SETTER,
            runtime_root_global=self.ROOT_GLOBAL,
        )
        proof = core.build_core_identity_proof(
            proof_address,
            state,
            entity_resolver=self.ENTITY,
            physics_helper=self.PHYSICS_HELPER,
            member_record_resolver=self.MEMBER,
            runtime_root_global=self.ROOT_GLOBAL,
        )
        emulator = Uc(UC_ARCH_X86, UC_MODE_64)
        emulator.mem_map(PRIMARY_BASE, 0x10000)
        emulator.mem_write(callback_address, callback)
        emulator.mem_write(proof_address, proof)
        emulator.mem_write(
            state,
            core.GAMBIT_DISPATCH_MAILBOX_STRUCT.pack(
                1,
                self.SEQUENCE,
                0,
                0,
                self.HANDLE,
                self.MEMBER_ID,
                self.ACTOR,
                self.PHYSICS,
                self.BATTLE_KEEP,
                self.RUNTIME_ROOT,
                0,
                0,
            )
            + struct.pack("<I", core.DISPATCH_OPERATION_GAMBITS),
        )
        emulator.mem_map(self.NATIVE, 0x1000)
        emulator.mem_write(
            self.ENTITY, self.resolver_stub(self.HANDLE, self.ENTRY)
        )
        emulator.mem_write(
            self.PHYSICS_HELPER,
            bytes(coop.build_actor_physics_helper()),
        )
        emulator.mem_write(
            self.MEMBER,
            self.resolver_stub(self.MEMBER_ID, self.BATTLE_KEEP),
        )
        emulator.mem_write(
            self.QUERY,
            b"\x48\xb8"
            + struct.pack("<Q", self.BATTLE_KEEP)
            + b"\x8b\x00\xc1\xe8\x02\x83\xe0\x01\xc3",
        )
        emulator.mem_write(
            self.SETTER,
            b"\x48\xb8"
            + struct.pack("<Q", self.BATTLE_KEEP)
            + b"\x85\xd2\x74\x04\x83\x08\x04\xc3\x83\x20\xfb\xc3",
        )
        emulator.mem_map(self.ROOT_GLOBAL, 0x1000)
        emulator.mem_write(
            self.ROOT_GLOBAL, struct.pack("<Q", self.RUNTIME_ROOT)
        )
        emulator.mem_map(self.ACTOR, 0x20000)
        emulator.mem_write(self.ACTOR, struct.pack("<Q", actor_flags))
        emulator.mem_write(self.ACTOR + 0x10, struct.pack("<Q", self.ACTOR_ROOT))
        emulator.mem_write(
            self.ACTOR_ROOT + 0xB8, struct.pack("<Q", self.PHYSICS)
        )
        emulator.mem_write(
            self.ACTOR + 0x698, struct.pack("<Q", self.BATTLE_KEEP)
        )
        emulator.mem_write(self.ENTRY + 0x30, struct.pack("<Q", self.ACTOR))
        emulator.mem_write(self.BATTLE_KEEP, struct.pack("<I", 0xA5A5A5A1))
        emulator.mem_write(
            self.BATTLE_KEEP + coop.PERMANENT_STATUS_EFFECTS_OFFSET,
            struct.pack("<I", status),
        )
        emulator.mem_map(self.STACK, 0x4000)
        emulator.mem_map(self.RETURN, 0x1000)
        stack_pointer = self.STACK + 0x2008
        emulator.mem_write(stack_pointer, struct.pack("<Q", self.RETURN))
        emulator.reg_write(UC_X86_REG_RSP, stack_pointer)
        emulator.reg_write(UC_X86_REG_RIP, callback_address)
        emulator.reg_write(UC_X86_REG_RCX, 0x1122334455667788)
        emulator.reg_write(UC_X86_REG_RBX, 0x0101010102020202)
        emulator.reg_write(UC_X86_REG_RBP, 0x0303030304040404)
        emulator.reg_write(UC_X86_REG_RSI, 0x0505050506060606)
        emulator.reg_write(UC_X86_REG_RDI, 0x0707070708080808)
        emulator.reg_write(UC_X86_REG_R12, 0x090909090A0A0A0A)
        emulator.reg_write(UC_X86_REG_R13, 0x0B0B0B0B0C0C0C0C)
        emulator.reg_write(UC_X86_REG_R14, 0x0D0D0D0D0E0E0E0E)
        emulator.reg_write(UC_X86_REG_R15, 0x0F0F0F0F10101010)
        return emulator, callback

    def execute(self, emulator: Uc) -> None:
        emulator.emu_start(
            PRIMARY_BASE + core.DISPATCH_CAVE_OFFSET,
            self.RETURN,
            count=5000,
        )

    def test_terminal_mailbox_reuse_cannot_trigger_code_teardown_or_late_write(self):
        emulator, callback = self.make_emulator()
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
        state = PRIMARY_BASE + core.DISPATCH_STATE_OFFSET
        terminal_instruction = (
            PRIMARY_BASE
            + core.DISPATCH_CAVE_OFFSET
            + callback.index(b"\x41\xc7\x07\x03\x00\x00\x00")
        )
        after_terminal = False
        reused = False

        def reuse_on_next_instruction(uc, address, _size, _user):
            nonlocal after_terminal, reused
            if after_terminal:
                next_payload = bytearray(action_mailbox(state=1, sequence=4))
                uc.mem_write(state, bytes(next_payload))
                reused = True
                after_terminal = False
            if address == terminal_instruction:
                after_terminal = True

        emulator.hook_add(UC_HOOK_CODE, reuse_on_next_instruction)
        self.execute(emulator)

        self.assertTrue(reused)
        self.assertEqual(struct.unpack("<I", emulator.mem_read(state, 4))[0], 1)
        self.assertEqual(
            struct.unpack("<I", emulator.mem_read(self.BATTLE_KEEP, 4))[0],
            0xA5A5A5A5,
        )
        self.assertEqual(emulator.reg_read(UC_X86_REG_RCX), original_rcx)
        for register, value in before.items():
            self.assertEqual(emulator.reg_read(register), value)

    def test_generation_swap_and_stone_reject_without_setter(self):
        for case in ("generation", "stone", "despawn"):
            with self.subTest(case=case):
                emulator, _callback = self.make_emulator(
                    status=coop.STONE_STATUS_MASK if case == "stone" else 0,
                    actor_flags=0 if case == "despawn" else core.ACTOR_ACTIVE_FLAG,
                )
                resolver_calls = 0
                setter_calls = 0

                def invalidate_or_count(uc, address, _size, _user):
                    nonlocal resolver_calls, setter_calls
                    if address == self.ENTITY:
                        resolver_calls += 1
                        if case == "generation" and resolver_calls == 2:
                            uc.mem_write(
                                self.ENTRY + 0x30,
                                struct.pack("<Q", self.ACTOR + 0x10000),
                            )
                    elif address == self.SETTER:
                        setter_calls += 1

                emulator.hook_add(UC_HOOK_CODE, invalidate_or_count)
                before = emulator.mem_read(self.BATTLE_KEEP, 4)
                self.execute(emulator)
                self.assertEqual(setter_calls, 0)
                self.assertEqual(emulator.mem_read(self.BATTLE_KEEP, 4), before)
                record = core.GAMBIT_DISPATCH_MAILBOX_STRUCT.unpack(
                    emulator.mem_read(
                        PRIMARY_BASE + core.DISPATCH_STATE_OFFSET,
                        core.GAMBIT_DISPATCH_MAILBOX_STRUCT.size,
                    )
                )
                self.assertEqual(record[0], 4)
                self.assertEqual(
                    record[3],
                    core.GAMBIT_DISPATCH_RESULT_STALE_IDENTITY
                    if case in ("generation", "despawn")
                    else core.GAMBIT_DISPATCH_RESULT_BLOCKED_STATUS,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
