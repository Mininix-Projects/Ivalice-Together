import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from unicorn import (
    UC_ARCH_X86,
    UC_HOOK_CODE,
    UC_HOOK_MEM_READ,
    UC_HOOK_MEM_WRITE,
    UC_MODE_64,
    Uc,
)
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
)


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import coop_runtime as coop  # noqa: E402
import movement_mailbox  # noqa: E402
import session_state  # noqa: E402


PAGE = 0x1000
MAIN_BASE = 0x52000000
DUAL_BASE = 0x53000000
STACK_BASE = 0x71000000
RETURN_ADDRESS = 0x62000000
SETTER_MARKER = 0x63000000
SETTER_MARKER_VALUE = 0xA701C1E5

REQUEST = 0x13579BDF
PREVIOUS_APPLIED = REQUEST - 1
WORLD_EPOCH = 0x2468ACE0
LAYOUT_GENERATION = 0x10203040
JOURNAL_SEQUENCE = 0x22


def _map(emu, address, size):
    start = int(address) & ~(PAGE - 1)
    end = (int(address) + int(size) + PAGE - 1) & ~(PAGE - 1)
    emu.mem_map(start, end - start)


def _u32(emu, address):
    return struct.unpack("<I", bytes(emu.mem_read(address, 4)))[0]


def _movement_command(actor, member, generation, world=WORLD_EPOCH):
    return movement_mailbox.MovementCommand(
        True,
        actor,
        actor + 0x800,
        member,
        generation,
        world,
        0.125 + member,
        0.375 + member,
        0.625 + member,
    )


def _next_even(sequence):
    result = (int(sequence) + 2) & 0xFFFFFFFE
    return result or 2


def _setter_stub():
    """Legal Win64 callee that dirties all shadow bytes and volatile state."""

    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", SETTER_MARKER)
    code += b"\xc7\x00" + struct.pack("<I", SETTER_MARKER_VALUE)
    for index, offset in enumerate((0x08, 0x10, 0x18, 0x20), start=1):
        code += b"\x48\xb8" + struct.pack("<Q", index * 0x1111111111111111)
        code += b"\x48\x89\x44\x24" + bytes((offset,))
    for opcode, value in (
        (b"\x48\xb8", 0x1111222233334444),
        (b"\x48\xb9", 0x2222333344445555),
        (b"\x48\xba", 0x3333444455556666),
        (b"\x49\xb8", 0x4444555566667777),
        (b"\x49\xb9", 0x5555666677778888),
        (b"\x49\xba", 0x6666777788889999),
        (b"\x49\xbb", 0x777788889999AAAA),
    ):
        code += opcode + struct.pack("<Q", value)
    code += b"\x31\xc0"  # Deliberately replace arithmetic flags too.
    code += b"\xc3"
    return bytes(code)


@dataclass
class _RunResult:
    emu: Uc
    writes: list
    initial_mailboxes: dict
    initial_hud: dict
    initial_flags: int
    initial_registers: dict
    initial_stack: int
    initial_applied: int
    setter_stack_entries: list

    @property
    def setter_called(self):
        return any(address == SETTER_MARKER for address, _size, _value in self.writes)


class _BridgeScenario:
    def __init__(self, *, p2_live=2, p3_live=2, p2_changed=True, p3_changed=True):
        coop.set_cave_base(MAIN_BASE)
        coop.set_dual_interaction_base(DUAL_BASE)

        self.state = {
            coop.NATIVE_LEADER_MEMBER_OFFSET: 1,
            coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET: REQUEST,
            coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET: PREVIOUS_APPLIED,
            # The transient KO/area lane and the role-transfer lane are
            # independent. A valid role transaction exclusively owns +0x7C
            # while the legacy transient word at +0x1C remains clear.
            coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET: 0,
            coop.NATIVE_LEADER_ROLE_P1_OFFSET: 1,
            coop.NATIVE_LEADER_ROLE_P2_OFFSET: 0,
            coop.NATIVE_LEADER_ROLE_P3_OFFSET: 2,
            coop.NATIVE_LEADER_ROLE_SEQUENCE_OFFSET: REQUEST,
            coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET: REQUEST,
            coop.NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET: (
                _next_even(p2_live) if p2_changed else 0
            ),
            coop.NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET: (
                _next_even(p3_live) if p3_changed else 0
            ),
            coop.NATIVE_LEADER_ROLE_P1_HUD_SLOT_OFFSET: 1,
            coop.NATIVE_LEADER_ROLE_P2_HUD_SLOT_OFFSET: 0,
            coop.NATIVE_LEADER_ROLE_P3_HUD_SLOT_OFFSET: 2,
            coop.NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET: 0,
            coop.NATIVE_LEADER_ROLE_WORLD_EPOCH_OFFSET: WORLD_EPOCH,
            coop.NATIVE_LEADER_ROLE_LAYOUT_GENERATION_OFFSET: LAYOUT_GENERATION,
            coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET: 0,
            coop.NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET: JOURNAL_SEQUENCE,
            coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET: coop.NATIVE_LEADER_ROLE_ACTIVE,
            coop.NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET: (
                1 if p2_changed else 0
            ),
            coop.NATIVE_LEADER_ROLE_P3_NATIVE_HANDLE_OFFSET: (
                2 if p3_changed else 0
            ),
            coop.NATIVE_LEADER_ROLE_P1_NATIVE_HANDLE_OFFSET: 3,
        }
        self.journal = {
            coop.NATIVE_LEADER_JOURNAL_SEQUENCE_OFFSET: JOURNAL_SEQUENCE,
            coop.NATIVE_LEADER_JOURNAL_REQUEST_OFFSET: REQUEST,
            coop.NATIVE_LEADER_JOURNAL_WORLD_EPOCH_OFFSET: WORLD_EPOCH,
            coop.NATIVE_LEADER_JOURNAL_LAYOUT_GENERATION_OFFSET: LAYOUT_GENERATION,
            coop.NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET: self.state[
                coop.NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET
            ],
            coop.NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET: self.state[
                coop.NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET
            ],
            coop.NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET: 1,
            coop.NATIVE_LEADER_JOURNAL_P2_HUD_SLOT_OFFSET: 0,
            coop.NATIVE_LEADER_JOURNAL_P3_HUD_SLOT_OFFSET: 2,
            coop.NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET: 1,
            coop.NATIVE_LEADER_JOURNAL_P2_ROLE_OFFSET: 0,
            coop.NATIVE_LEADER_JOURNAL_P3_ROLE_OFFSET: 2,
            coop.NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET: 1,
            coop.NATIVE_LEADER_JOURNAL_P2_ROLE_OFFSET: 0,
            coop.NATIVE_LEADER_JOURNAL_P3_ROLE_OFFSET: 2,
            coop.NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET: (
                1 if p2_changed else 0
            ),
            coop.NATIVE_LEADER_JOURNAL_P3_NATIVE_HANDLE_OFFSET: (
                2 if p3_changed else 0
            ),
        }
        self.battle_info = 0x54010000
        self.location_base = 0x54020000
        self.actor_manager = 0x54030000
        self.actor_count = 4
        self.p1_handle = 3
        self.p1_bwei = 0x5400B000
        self.p1_actor = 0x5400C000
        self.p1_battle_keep = 0x5400D000
        self.location_id = 10
        self.layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
        self.live_sequences = {"p2": p2_live, "p3": p3_live}
        old_p2 = 0 if not p2_changed else (2 if p3_changed else 1)
        old_p3 = 2 if not p3_changed else 1
        old_p1 = ({0, 1, 2} - {old_p2, old_p3}).pop()
        self.old_roles = (old_p1, old_p2, old_p3)
        self.old_commands = {
            "p2": _movement_command(0x54002000, old_p2, 7),
            "p3": _movement_command(0x54003000, old_p3, 8),
        }
        self.new_commands = {
            "p2": _movement_command(0x54004000, 0, 17),
            "p3": _movement_command(0x54005000, 2, 18),
        }
        self.payload_patches = []
        self.live_mutations = []
        self.flip_journal_on_final_read = False
        self.shutdown_fenced = False
        self.close_fence_before_cancelling_store = False

    def set_state(self, offset, value):
        self.state[offset] = int(value) & 0xFFFFFFFF

    def set_journal(self, offset, value):
        self.journal[offset] = int(value) & 0xFFFFFFFF

    def set_planned_sequence(self, role, value):
        state_offset = {
            "p2": coop.NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET,
            "p3": coop.NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET,
        }[role]
        journal_offset = {
            "p2": coop.NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET,
            "p3": coop.NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET,
        }[role]
        self.set_state(state_offset, value)
        self.set_journal(journal_offset, value)

    def patch_payload(self, role, mailbox_offset, data):
        payload_offset = {
            "p2": coop.NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET,
            "p3": coop.NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET,
        }[role]
        journal_offset = payload_offset + int(mailbox_offset) - 4
        self.payload_patches.append((journal_offset, bytes(data)))

    def patch_payload_u32(self, role, mailbox_offset, value):
        self.patch_payload(role, mailbox_offset, struct.pack("<I", value))

    def patch_payload_u64(self, role, mailbox_offset, value):
        self.patch_payload(role, mailbox_offset, struct.pack("<Q", value))

    def run(self):
        emu = Uc(UC_ARCH_X86, UC_MODE_64)
        for address, size in (
            (MAIN_BASE, 0x10000),
            (DUAL_BASE, 0x10000),
            (STACK_BASE, 0x20000),
            (RETURN_ADDRESS, PAGE),
            (SETTER_MARKER, PAGE),
            (coop.NATIVE_LEADER_SETTER, PAGE),
            (session_state.BATTLE_INFO_GLOBAL, PAGE),
            (session_state.LOCATION_SAVE_GLOBAL, PAGE),
            (coop.ACTOR_MANAGER_GLOBAL, PAGE),
            (0x54000000, 0x60000),
        ):
            _map(emu, address, size)

        emu.mem_write(
            coop.NATIVE_LEADER_BRIDGE_CAVE,
            coop.build_native_leader_bridge_cave(),
        )
        emu.mem_write(
            coop.NATIVE_LEADER_ROLE_COMMIT_CAVE,
            coop.build_native_leader_role_commit_cave(),
        )
        emu.mem_write(coop.NATIVE_LEADER_SETTER, _setter_stub())

        for role, state_offset, journal_offset in (
            (
                "p2",
                coop.NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET,
                coop.NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET,
            ),
            (
                "p3",
                coop.NATIVE_LEADER_ROLE_P3_NATIVE_HANDLE_OFFSET,
                coop.NATIVE_LEADER_JOURNAL_P3_NATIVE_HANDLE_OFFSET,
            ),
        ):
            if not self.new_commands[role].active:
                self.state[state_offset] = 0
                self.journal[journal_offset] = 0

        state = bytearray(coop.P2_DEVICE_FILTER_STATE_SIZE)
        for offset, value in self.state.items():
            struct.pack_into("<I", state, offset, value)
        emu.mem_write(coop.P2_DEVICE_FILTER_STATE, bytes(state))

        journal = bytearray(coop.NATIVE_LEADER_JOURNAL_RECORD_SIZE)
        for offset, value in self.journal.items():
            struct.pack_into("<I", journal, offset, value)
        for role, payload_offset in (
            ("p2", coop.NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET),
            ("p3", coop.NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET),
        ):
            state_sequence_offset = {
                "p2": coop.NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET,
                "p3": coop.NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET,
            }[role]
            payload = (
                self.new_commands[role]
                if self.state[state_sequence_offset]
                else movement_mailbox.MovementCommand.inactive(
                    world_epoch=WORLD_EPOCH
                )
            ).payload()
            journal[payload_offset : payload_offset + len(payload)] = payload
        for offset, payload in self.payload_patches:
            journal[offset : offset + len(payload)] = payload
        struct.pack_into(
            "<QQI",
            journal,
            coop.NATIVE_LEADER_JOURNAL_BATTLE_INFO_OFFSET,
            self.battle_info,
            self.location_base,
            self.location_id,
        )
        struct.pack_into(
            "<4H",
            journal,
            coop.NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET,
            *self.layout,
        )
        struct.pack_into(
            "<4H",
            journal,
            coop.NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET,
            *self.layout,
        )
        struct.pack_into(
            "<QI",
            journal,
            coop.NATIVE_LEADER_JOURNAL_ACTOR_MANAGER_OFFSET,
            self.actor_manager,
            self.actor_count,
        )
        emu.mem_write(coop.NATIVE_LEADER_ROLE_JOURNAL, bytes(journal))
        emu.mem_write(
            session_state.BATTLE_INFO_GLOBAL,
            struct.pack("<Q", self.battle_info),
        )
        emu.mem_write(
            session_state.LOCATION_SAVE_GLOBAL,
            struct.pack("<Q", self.location_base),
        )
        emu.mem_write(
            coop.ACTOR_MANAGER_GLOBAL,
            struct.pack("<Q", self.actor_manager),
        )
        emu.mem_write(self.actor_manager, struct.pack("<I", self.actor_count))
        emu.mem_write(
            self.location_base + session_state.LOCATION_CURRENT_OFFSET,
            struct.pack("<I", self.location_id),
        )
        emu.mem_write(
            self.location_base + session_state.LOCATION_UI_OFFSET,
            struct.pack("<I", self.location_id),
        )
        for layout_index in (
            session_state.BATTLE_CURRENT_LAYOUT,
            session_state.BATTLE_UI_LAYOUT,
        ):
            emu.mem_write(
                self.battle_info
                + session_state.BATTLE_PARTY_LAYOUT_OFFSET
                + session_state.BATTLE_PARTY_LAYOUT_STRIDE * layout_index,
                struct.pack("<4H", *self.layout),
            )
        for role, actor_id, party_slot, bwei in (
            ("p2", 1, 0, 0x54008000),
            ("p3", 2, 2, 0x54009000),
        ):
            state_sequence_offset = {
                "p2": coop.NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET,
                "p3": coop.NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET,
            }[role]
            if not self.state[state_sequence_offset]:
                continue
            command = self.new_commands[role]
            # A KO/Stone follower keeps its authenticated native handle in
            # the journal, but its canonical inactive movement payload has no
            # actor/phys pointer to materialize or dereference.
            if not command.active:
                continue
            root = command.actor + 0x400
            emu.mem_write(
                self.actor_manager + 8 + actor_id * 8,
                struct.pack("<Q", bwei),
            )
            emu.mem_write(bwei + 0x12, bytes((party_slot,)))
            emu.mem_write(bwei + 0x30, struct.pack("<Q", command.actor))
            emu.mem_write(command.actor + 0x08, struct.pack("<I", actor_id))
            emu.mem_write(command.actor + 0x10, struct.pack("<Q", root))
            emu.mem_write(root + 0xB8, struct.pack("<Q", command.phys))
        emu.mem_write(
            self.actor_manager + 8 + (self.p1_handle & 0xFFFF) * 8,
            struct.pack("<Q", self.p1_bwei),
        )
        emu.mem_write(self.p1_bwei + 0x12, b"\x01")
        emu.mem_write(
            self.p1_bwei + 0x30,
            struct.pack("<Q", self.p1_actor),
        )
        emu.mem_write(
            self.p1_actor + 0x08,
            struct.pack("<I", self.p1_handle),
        )
        emu.mem_write(
            self.p1_actor + 0x698,
            struct.pack("<Q", self.p1_battle_keep),
        )
        emu.mem_write(
            self.p1_battle_keep + 0x48,
            struct.pack("<I", 100),
        )
        emu.mem_write(
            self.p1_battle_keep + 0x3C,
            struct.pack("<I", 0),
        )
        emu.mem_write(
            self.p1_battle_keep + 0x64,
            struct.pack("<I", 0),
        )
        for mutation in self.live_mutations:
            mutation(emu, self)
        if self.shutdown_fenced:
            emu.mem_write(
                coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
                struct.pack("<I", 1),
            )

        initial_mailboxes = {}
        for role, address in (
            ("p2", coop.P2_MOVEMENT_MAILBOX),
            ("p3", coop.P3_MOVEMENT_MAILBOX),
        ):
            record = struct.pack("<I", self.live_sequences[role])
            record += self.old_commands[role].payload()
            emu.mem_write(address, record)
            initial_mailboxes[role] = record

        hud_addresses = _hud_addresses()
        initial_hud = {}
        for value, address in zip(self.old_roles, hud_addresses, strict=True):
            emu.mem_write(address, struct.pack("<I", value))
            initial_hud[address] = value

        writes = []

        def record_write(_emu, _access, address, size, value, _user):
            writes.append((address, size, value))
            if (
                self.close_fence_before_cancelling_store
                and address
                == coop.P2_DEVICE_FILTER_STATE
                + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
                and size == 4
                and value == coop.NATIVE_LEADER_ROLE_CANCELLING
            ):
                self.close_fence_before_cancelling_store = False
                _emu.mem_write(
                    coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
                    struct.pack("<I", 1),
                )

        emu.hook_add(UC_HOOK_MEM_WRITE, record_write)
        setter_stack_entries = []

        def record_setter_entry(inner, address, _size, _user):
            if address == coop.NATIVE_LEADER_SETTER:
                setter_stack_entries.append(inner.reg_read(UC_X86_REG_RSP))

        emu.hook_add(
            UC_HOOK_CODE,
            record_setter_entry,
            begin=coop.NATIVE_LEADER_SETTER,
            end=coop.NATIVE_LEADER_SETTER,
        )

        if self.flip_journal_on_final_read:
            sequence_reads = 0

            def flip_journal(inner, _access, address, size, _value, _user):
                nonlocal sequence_reads
                journal_address = (
                    coop.NATIVE_LEADER_ROLE_JOURNAL
                    + coop.NATIVE_LEADER_JOURNAL_SEQUENCE_OFFSET
                )
                if address <= journal_address < address + size:
                    sequence_reads += 1
                    if sequence_reads == 2:
                        inner.mem_write(
                            journal_address,
                            struct.pack("<I", JOURNAL_SEQUENCE + 2),
                        )

            emu.hook_add(UC_HOOK_MEM_READ, flip_journal)

        registers = {
            UC_X86_REG_RAX: 0x0111111111111111,
            UC_X86_REG_RCX: 0x0222222222222222,
            UC_X86_REG_RDX: 0x0333333333333333,
            UC_X86_REG_RBX: 0x0444444444444444,
            UC_X86_REG_RBP: 0x0555555555555555,
            UC_X86_REG_RSI: 0x0666666666666666,
            UC_X86_REG_RDI: 0x0777777777777777,
            UC_X86_REG_R8: 0x0888888888888888,
            UC_X86_REG_R9: 0x0999999999999999,
            UC_X86_REG_R10: 0x0AAAAAAAAAAAAAAA,
            UC_X86_REG_R11: 0x0BBBBBBBBBBBBBBB,
            UC_X86_REG_R12: 0x0CCCCCCCCCCCCCCC,
            UC_X86_REG_R13: 0x0DDDDDDDDDDDDDDD,
            UC_X86_REG_R14: 0x0EEEEEEEEEEEEEEE,
            UC_X86_REG_R15: 0x0FFFFFFFFFFFFFFF,
        }
        for register, value in registers.items():
            emu.reg_write(register, value)
        emu.reg_write(UC_X86_REG_EFLAGS, 0x246)
        initial_flags = emu.reg_read(UC_X86_REG_EFLAGS)
        # Model a real Win64 function entry: CALL has already pushed this
        # bridge's return address, so incoming RSP is 8 modulo 16.
        stack = STACK_BASE + 0x18008
        emu.mem_write(stack, struct.pack("<Q", RETURN_ADDRESS))
        emu.reg_write(UC_X86_REG_RSP, stack)
        emu.emu_start(coop.NATIVE_LEADER_BRIDGE_CAVE, RETURN_ADDRESS)
        return _RunResult(
            emu,
            writes,
            initial_mailboxes,
            initial_hud,
            initial_flags,
            registers,
            stack,
            self.state[coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET],
            setter_stack_entries,
        )


def _hud_addresses():
    return (
        coop.COMMAND_BLOCK + coop.P1_HUD_PARTY_SLOT_OFFSET,
        coop.COMMAND_BLOCK + coop.P2_HUD_PARTY_SLOT_OFFSET,
        coop.third_player_block() + coop.P3_HUD_PARTY_SLOT_OFFSET,
    )


def _assert_rejected(result):
    assert not result.setter_called
    state = coop.P2_DEVICE_FILTER_STATE
    assert _u32(
        result.emu, state + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
    ) == 0
    assert _u32(
        result.emu, state + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    ) == result.initial_applied
    assert not any(
        address == state + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
        for address, _size, _value in result.writes
    )
    for role, address in (
        ("p2", coop.P2_MOVEMENT_MAILBOX),
        ("p3", coop.P3_MOVEMENT_MAILBOX),
    ):
        assert bytes(
            result.emu.mem_read(address, movement_mailbox.MAILBOX_SIZE)
        ) == result.initial_mailboxes[role]
    for address, value in result.initial_hud.items():
        assert _u32(result.emu, address) == value
    for register, value in result.initial_registers.items():
        assert result.emu.reg_read(register) == value
    assert result.emu.reg_read(UC_X86_REG_EFLAGS) == result.initial_flags
    assert result.emu.reg_read(UC_X86_REG_RSP) == result.initial_stack + 8
    assert result.emu.reg_read(UC_X86_REG_RIP) == RETURN_ADDRESS
    assert not result.setter_stack_entries


def _assert_world_stale_cancelled(result):
    assert not result.setter_called
    state = coop.P2_DEVICE_FILTER_STATE
    role_mode = state + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    prepared = state + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
    applied = state + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    claimed = state + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
    cancelled = coop.NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE

    assert _u32(result.emu, claimed) == 0
    assert _u32(result.emu, prepared) == 0
    assert _u32(result.emu, cancelled) == REQUEST
    assert _u32(result.emu, applied) == REQUEST
    assert _u32(result.emu, role_mode) == coop.NATIVE_LEADER_ROLE_CANCELLED
    for role, address in (
        ("p2", coop.P2_MOVEMENT_MAILBOX),
        ("p3", coop.P3_MOVEMENT_MAILBOX),
    ):
        assert bytes(
            result.emu.mem_read(address, movement_mailbox.MAILBOX_SIZE)
        ) == result.initial_mailboxes[role]
    for address, value in result.initial_hud.items():
        assert _u32(result.emu, address) == value

    def event_index(address, value):
        return next(
            index
            for index, (event_address, size, event_value) in enumerate(
                result.writes
            )
            if event_address == address and size == 4 and event_value == value
        )

    cancelling_index = event_index(
        role_mode,
        coop.NATIVE_LEADER_ROLE_CANCELLING,
    )
    prepared_index = event_index(prepared, 0)
    marker_index = event_index(cancelled, REQUEST)
    applied_index = event_index(applied, REQUEST)
    terminal_index = event_index(
        role_mode,
        coop.NATIVE_LEADER_ROLE_CANCELLED,
    )
    assert (
        cancelling_index
        < prepared_index
        < marker_index
        < applied_index
        < terminal_index
    )
    assert not any(
        mailbox <= address < mailbox + movement_mailbox.MAILBOX_SIZE
        for address, _size, _value in result.writes
        for mailbox in (coop.P2_MOVEMENT_MAILBOX, coop.P3_MOVEMENT_MAILBOX)
    )
    assert not any(
        address in result.initial_hud
        for address, _size, _value in result.writes
    )
    for register, value in result.initial_registers.items():
        assert result.emu.reg_read(register) == value
    assert result.emu.reg_read(UC_X86_REG_EFLAGS) == result.initial_flags
    assert result.emu.reg_read(UC_X86_REG_RSP) == result.initial_stack + 8
    assert result.emu.reg_read(UC_X86_REG_RIP) == RETURN_ADDRESS
    assert not result.setter_stack_entries


def _drift_battle_root(emu, scenario):
    emu.mem_write(
        session_state.BATTLE_INFO_GLOBAL,
        struct.pack("<Q", scenario.battle_info + 0x1000),
    )


def _drift_location_generation(emu, scenario):
    emu.mem_write(
        scenario.location_base + session_state.LOCATION_CURRENT_OFFSET,
        struct.pack("<I", scenario.location_id + 1),
    )


def _drift_party_layout(emu, scenario):
    emu.mem_write(
        scenario.battle_info
        + session_state.BATTLE_PARTY_LAYOUT_OFFSET
        + session_state.BATTLE_PARTY_LAYOUT_STRIDE
        * session_state.BATTLE_CURRENT_LAYOUT,
        struct.pack("<4H", 0, 1, 3, session_state.EMPTY_PARTY_ID),
    )


def _drift_actor_slot_pointer(emu, scenario):
    emu.mem_write(
        scenario.actor_manager + 8 + 1 * 8,
        struct.pack("<Q", 0x5400A000),
    )


def _drift_actor_native_handle(emu, scenario):
    emu.mem_write(
        scenario.new_commands["p2"].actor + 0x08,
        struct.pack("<I", 3),
    )


def _drift_actor_phys(emu, scenario):
    emu.mem_write(
        scenario.new_commands["p2"].actor + 0x400 + 0xB8,
        struct.pack("<Q", scenario.new_commands["p2"].phys + 0x100),
    )


def _drift_requested_p1_death(emu, scenario):
    emu.mem_write(
        scenario.p1_battle_keep + 0x48,
        struct.pack("<I", 0),
    )


def _corrupt_transient_lane_mode(scenario):
    scenario.set_state(
        coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET,
        coop.NATIVE_LEADER_HANDOFF_TRANSIENT,
    )


def _close_movement_shutdown_fence(scenario):
    scenario.shutdown_fenced = True


def _corrupt_role_active_phase(scenario):
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        coop.NATIVE_LEADER_ROLE_CLAIMING,
    )


def _corrupt_request_against_role(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_SEQUENCE_OFFSET, REQUEST + 1)


def _corrupt_prepared_sequence(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET, REQUEST - 1)


def _corrupt_target_role(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P1_OFFSET, 0)


def _corrupt_p2_role_identity(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P2_OFFSET, 2)


def _corrupt_p3_role_identity(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P3_OFFSET, 0)


def _corrupt_p2_payload_role(scenario):
    scenario.new_commands["p2"] = _movement_command(0x54004000, 2, 17)


def _corrupt_p3_payload_role(scenario):
    scenario.new_commands["p3"] = _movement_command(0x54005000, 0, 18)


def _corrupt_payload_active_domain(scenario):
    scenario.patch_payload_u32("p2", movement_mailbox.ACTIVE_OFFSET, 2)


def _corrupt_active_payload_actor(scenario):
    scenario.patch_payload_u64("p2", movement_mailbox.ACTOR_OFFSET, 0)


def _corrupt_active_payload_phys(scenario):
    scenario.patch_payload_u64("p3", movement_mailbox.PHYS_OFFSET, 0)


def _corrupt_active_payload_generation(scenario):
    scenario.patch_payload_u32(
        "p2",
        movement_mailbox.ACTOR_GENERATION_OFFSET,
        0,
    )


def _corrupt_payload_world(scenario):
    scenario.patch_payload_u32(
        "p3",
        movement_mailbox.WORLD_EPOCH_OFFSET,
        WORLD_EPOCH + 1,
    )


def _corrupt_inactive_payload_canonical_form(scenario):
    scenario.patch_payload_u32("p2", movement_mailbox.ACTIVE_OFFSET, 0)


def _corrupt_p1_hud_state(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P1_HUD_SLOT_OFFSET, 0)


def _corrupt_p2_hud_state(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P2_HUD_SLOT_OFFSET, 1)


def _corrupt_p3_hud_state(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P3_HUD_SLOT_OFFSET, 1)


def _corrupt_journal_hud(scenario):
    scenario.set_journal(coop.NATIVE_LEADER_JOURNAL_P2_HUD_SLOT_OFFSET, 2)


def _corrupt_hud_domain_in_both_copies(scenario):
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P1_HUD_SLOT_OFFSET, 4)
    scenario.set_journal(coop.NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET, 4)


def _corrupt_journal_state_sequence(scenario):
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,
        JOURNAL_SEQUENCE + 2,
    )


def _corrupt_journal_request(scenario):
    scenario.set_journal(coop.NATIVE_LEADER_JOURNAL_REQUEST_OFFSET, REQUEST + 1)


def _corrupt_journal_world(scenario):
    scenario.set_journal(
        coop.NATIVE_LEADER_JOURNAL_WORLD_EPOCH_OFFSET,
        WORLD_EPOCH + 1,
    )


def _corrupt_journal_layout(scenario):
    scenario.set_journal(
        coop.NATIVE_LEADER_JOURNAL_LAYOUT_GENERATION_OFFSET,
        LAYOUT_GENERATION + 1,
    )


def _corrupt_journal_to_odd(scenario):
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,
        JOURNAL_SEQUENCE | 1,
    )
    scenario.set_journal(
        coop.NATIVE_LEADER_JOURNAL_SEQUENCE_OFFSET,
        JOURNAL_SEQUENCE | 1,
    )


def _corrupt_p2_state_sequence(scenario):
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET,
        scenario.journal[coop.NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET] + 2,
    )


def _corrupt_p3_state_sequence(scenario):
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET,
        scenario.journal[coop.NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET] + 2,
    )


def _make_p2_planned_odd(scenario):
    scenario.set_planned_sequence("p2", 5)


def _make_p3_planned_odd(scenario):
    scenario.set_planned_sequence("p3", 5)


def _make_p2_live_odd(scenario):
    scenario.live_sequences["p2"] = 3


def _make_p3_live_odd(scenario):
    scenario.live_sequences["p3"] = 3


def _make_p2_live_generation_stale(scenario):
    scenario.live_sequences["p2"] = 4


def _make_p3_live_generation_stale(scenario):
    scenario.live_sequences["p3"] = 4


def _make_request_already_applied(scenario):
    scenario.set_state(coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET, REQUEST)


@pytest.mark.parametrize(
    "corruption",
    (
        _close_movement_shutdown_fence,
        _corrupt_transient_lane_mode,
        _corrupt_role_active_phase,
        _corrupt_request_against_role,
        _corrupt_prepared_sequence,
        _corrupt_target_role,
        _corrupt_p2_role_identity,
        _corrupt_p3_role_identity,
        _corrupt_p2_payload_role,
        _corrupt_p3_payload_role,
        _corrupt_payload_active_domain,
        _corrupt_active_payload_actor,
        _corrupt_active_payload_phys,
        _corrupt_active_payload_generation,
        _corrupt_payload_world,
        _corrupt_inactive_payload_canonical_form,
        _corrupt_p1_hud_state,
        _corrupt_p2_hud_state,
        _corrupt_p3_hud_state,
        _corrupt_journal_hud,
        _corrupt_hud_domain_in_both_copies,
        _corrupt_journal_state_sequence,
        _corrupt_journal_request,
        _corrupt_journal_world,
        _corrupt_journal_layout,
        _corrupt_journal_to_odd,
        _corrupt_p2_state_sequence,
        _corrupt_p3_state_sequence,
        _make_p2_planned_odd,
        _make_p3_planned_odd,
        _make_p2_live_odd,
        _make_p3_live_odd,
        _make_p2_live_generation_stale,
        _make_p3_live_generation_stale,
        _make_request_already_applied,
    ),
    ids=lambda mutation: mutation.__name__.removeprefix("_"),
)
def test_every_corruption_rejects_before_setter_without_ack(corruption):
    scenario = _BridgeScenario()
    corruption(scenario)
    _assert_rejected(scenario.run())


def test_journal_change_during_final_validation_read_rejects_before_setter():
    scenario = _BridgeScenario()
    scenario.flip_journal_on_final_read = True
    _assert_rejected(scenario.run())


@pytest.mark.parametrize(
    "mutation",
    (
        _drift_battle_root,
        _drift_location_generation,
        _drift_party_layout,
        _drift_actor_slot_pointer,
        _drift_actor_native_handle,
        _drift_actor_phys,
        _drift_requested_p1_death,
    ),
    ids=lambda mutation: mutation.__name__.removeprefix("_drift_"),
)
def test_authenticated_live_world_or_actor_drift_cancels_without_publication(
    mutation,
):
    scenario = _BridgeScenario()
    scenario.live_mutations.append(mutation)

    _assert_world_stale_cancelled(scenario.run())


def test_static_corruption_never_uses_live_drift_as_cancellation_authority():
    scenario = _BridgeScenario()
    _corrupt_journal_world(scenario)
    scenario.live_mutations.append(_drift_party_layout)

    _assert_rejected(scenario.run())


def test_fence_close_after_final_live_read_preserves_native_terminal_prefix():
    scenario = _BridgeScenario()
    scenario.live_mutations.append(_drift_party_layout)
    scenario.close_fence_before_cancelling_store = True

    result = scenario.run()

    _assert_world_stale_cancelled(result)
    assert _u32(
        result.emu,
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
    ) == 1


def test_prepared_transfer_waits_at_fence_and_commits_after_reopen():
    scenario = _BridgeScenario()
    scenario.shutdown_fenced = True
    result = scenario.run()
    _assert_rejected(result)

    # Re-enter the same native callback with the exact same PREPARED journal
    # after the movement consumer reopens admission.  Nothing was cancelled,
    # regenerated or republished while fenced.
    result.emu.mem_write(
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        struct.pack("<I", 0),
    )
    result.writes.clear()
    result.setter_stack_entries.clear()
    result.emu.mem_write(
        result.initial_stack,
        struct.pack("<Q", RETURN_ADDRESS),
    )
    result.emu.reg_write(UC_X86_REG_RSP, result.initial_stack)
    result.emu.emu_start(coop.NATIVE_LEADER_BRIDGE_CAVE, RETURN_ADDRESS)

    assert result.setter_called
    assert len(result.setter_stack_entries) == 1
    assert _u32(
        result.emu,
        coop.P2_DEVICE_FILTER_STATE
        + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,
    ) == REQUEST
    assert _u32(
        result.emu,
        coop.P2_DEVICE_FILTER_STATE
        + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
    ) == REQUEST


def test_zero_movement_lanes_still_authenticate_complete_role_identity():
    scenario = _BridgeScenario(p2_changed=False, p3_changed=False)
    # This is still a valid, unique in-domain identity and both movement lanes
    # legitimately remain at sequence zero. It must nevertheless be rejected
    # because the immutable journal authenticates the original (1, 0, 2).
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P2_OFFSET, 2)
    scenario.set_state(coop.NATIVE_LEADER_ROLE_P3_OFFSET, 0)
    _assert_rejected(scenario.run())


@pytest.mark.parametrize(
    ("p2_changed", "p3_changed", "p2_live", "p3_live"),
    (
        (True, False, 2, 2),
        (False, True, 2, 2),
        (True, True, 2, 2),
        (False, False, 2, 2),
        (True, True, 0xFFFFFFFE, 0xFFFFFFFE),
    ),
    ids=("p2-only", "p3-only", "both", "zero-unchanged", "generation-wrap"),
)
def test_valid_transfer_is_one_ordered_abi_clean_transaction(
    p2_changed,
    p3_changed,
    p2_live,
    p3_live,
):
    scenario = _BridgeScenario(
        p2_live=p2_live,
        p3_live=p3_live,
        p2_changed=p2_changed,
        p3_changed=p3_changed,
    )
    result = scenario.run()
    emu = result.emu

    assert result.setter_called
    assert len(result.setter_stack_entries) == 1
    assert result.setter_stack_entries[0] & 0xF == 8
    assert _u32(emu, SETTER_MARKER) == SETTER_MARKER_VALUE
    assert _u32(
        emu,
        coop.P2_DEVICE_FILTER_STATE
        + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,
    ) == REQUEST
    assert _u32(
        emu,
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
    ) == REQUEST

    for role, changed, address in (
        ("p2", p2_changed, coop.P2_MOVEMENT_MAILBOX),
        ("p3", p3_changed, coop.P3_MOVEMENT_MAILBOX),
    ):
        raw = bytes(emu.mem_read(address, movement_mailbox.MAILBOX_SIZE))
        if changed:
            expected_sequence = _next_even(scenario.live_sequences[role])
            assert struct.unpack_from("<I", raw)[0] == expected_sequence
            assert raw[4:] == scenario.new_commands[role].payload()
        else:
            assert raw == result.initial_mailboxes[role]

    hud_addresses = _hud_addresses()
    assert tuple(_u32(emu, address) for address in hud_addresses) == (1, 0, 2)

    def indices_for(address):
        return [
            index
            for index, (event_address, _size, _value) in enumerate(result.writes)
            if event_address == address
        ]

    claim_address = (
        coop.P2_DEVICE_FILTER_STATE
        + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
    )
    applied_address = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    )
    claim_index = indices_for(claim_address)[-1]
    setter_index = indices_for(SETTER_MARKER)[-1]
    applied_index = indices_for(applied_address)[-1]
    assert claim_index < setter_index < applied_index

    last_publication_index = setter_index
    for role, changed, mailbox in (
        ("p2", p2_changed, coop.P2_MOVEMENT_MAILBOX),
        ("p3", p3_changed, coop.P3_MOVEMENT_MAILBOX),
    ):
        sequence_events = [
            (index, value)
            for index, (address, size, value) in enumerate(result.writes)
            if address == mailbox and size == 4
        ]
        if not changed:
            assert not sequence_events
            continue
        expected_even = _next_even(scenario.live_sequences[role])
        assert [value for _index, value in sequence_events] == [
            expected_even | 1,
            expected_even,
        ]
        odd_index, even_index = (event[0] for event in sequence_events)
        payload_indices = [
            index
            for index, (address, size, _value) in enumerate(result.writes)
            if address < mailbox + movement_mailbox.MAILBOX_SIZE
            and address + size > mailbox + movement_mailbox.ACTIVE_OFFSET
        ]
        assert payload_indices
        assert setter_index < odd_index < min(payload_indices)
        assert max(payload_indices) < even_index
        last_publication_index = max(last_publication_index, even_index)

    hud_indices = [indices_for(address)[-1] for address in hud_addresses]
    assert last_publication_index < hud_indices[0] < hud_indices[1] < hud_indices[2]
    assert hud_indices[2] < applied_index
    assert not any(
        address in hud_addresses and value == coop.INVALID_HUD_PARTY_SLOT
        for address, _size, value in result.writes
    )

    for register, value in result.initial_registers.items():
        assert emu.reg_read(register) == value
    assert emu.reg_read(UC_X86_REG_EFLAGS) == result.initial_flags
    assert emu.reg_read(UC_X86_REG_RSP) == result.initial_stack + 8
    assert emu.reg_read(UC_X86_REG_RIP) == RETURN_ADDRESS


def test_valid_ko_inactive_payload_commits_without_aborting_role_transfer():
    scenario = _BridgeScenario(p2_changed=True, p3_changed=False)
    inactive = movement_mailbox.MovementCommand.inactive(
        heading=1.25,
        visual_heading=1.5,
        world_epoch=WORLD_EPOCH,
    )
    scenario.new_commands["p2"] = inactive
    result = scenario.run()

    assert result.setter_called
    assert _u32(
        result.emu,
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
    ) == REQUEST
    raw = bytes(
        result.emu.mem_read(
            coop.P2_MOVEMENT_MAILBOX,
            movement_mailbox.MAILBOX_SIZE,
        )
    )
    assert struct.unpack_from("<I", raw)[0] == 4
    assert raw[4:] == inactive.payload()
    assert tuple(_u32(result.emu, address) for address in _hud_addresses()) == (
        1,
        0,
        2,
    )
    for register, value in result.initial_registers.items():
        assert result.emu.reg_read(register) == value
    assert result.emu.reg_read(UC_X86_REG_EFLAGS) == result.initial_flags
    assert result.emu.reg_read(UC_X86_REG_RSP) == result.initial_stack + 8


def test_valid_two_player_transfer_keeps_absent_p3_explicit_and_unchanged():
    scenario = _BridgeScenario(p2_changed=True, p3_changed=False)
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_P3_OFFSET,
        coop.NATIVE_LEADER_ROLE_NONE,
    )
    scenario.set_journal(
        coop.NATIVE_LEADER_JOURNAL_P3_ROLE_OFFSET,
        coop.NATIVE_LEADER_ROLE_NONE,
    )
    scenario.set_state(
        coop.NATIVE_LEADER_ROLE_P3_HUD_SLOT_OFFSET,
        coop.INVALID_HUD_PARTY_SLOT,
    )
    scenario.set_journal(
        coop.NATIVE_LEADER_JOURNAL_P3_HUD_SLOT_OFFSET,
        coop.INVALID_HUD_PARTY_SLOT,
    )
    scenario.set_journal(
        coop.NATIVE_LEADER_JOURNAL_P3_ROLE_OFFSET,
        coop.NATIVE_LEADER_ROLE_NONE,
    )
    result = scenario.run()

    assert result.setter_called
    assert _u32(
        result.emu,
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
    ) == REQUEST
    assert bytes(
        result.emu.mem_read(
            coop.P3_MOVEMENT_MAILBOX,
            movement_mailbox.MAILBOX_SIZE,
        )
    ) == result.initial_mailboxes["p3"]
    assert tuple(_u32(result.emu, address) for address in _hud_addresses()) == (
        1,
        0,
        coop.INVALID_HUD_PARTY_SLOT,
    )


class _MemoryProcess:
    def __init__(self, address=0x10000):
        self.address = address
        self.memory = bytearray(movement_mailbox.MAILBOX_STORAGE_SIZE)
        self.writes = []

    def read(self, address, size):
        offset = int(address) - self.address
        return bytes(self.memory[offset : offset + int(size)])

    def write(self, address, data):
        payload = bytes(data)
        offset = int(address) - self.address
        self.memory[offset : offset + len(payload)] = payload
        self.writes.append((int(address), payload))


def test_public_reservation_freezes_publish_until_native_commit_is_adopted():
    process = _MemoryProcess()
    publisher = movement_mailbox.publisher_for(process, process.address)
    old = _movement_command(0x20000, 1, 1)
    desired = _movement_command(0x30000, 2, 2)
    unrelated = _movement_command(0x40000, 3, 3)
    assert publisher.publish(old)
    old_record = process.read(process.address, movement_mailbox.MAILBOX_SIZE)

    reservation = publisher.reserve(desired)
    assert reservation.sequence == 4
    assert not publisher.publish(unrelated)
    assert process.read(process.address, movement_mailbox.MAILBOX_SIZE) == old_record

    process.write(process.address, struct.pack("<I", reservation.sequence | 1))
    process.write(process.address + 4, reservation.payload)
    assert movement_mailbox.read_consistent(process, process.address, attempts=1) is None
    process.write(process.address, struct.pack("<I", reservation.sequence))
    assert publisher.finalize_reserved(reservation.sequence)
    assert publisher.reserved is None
    assert publisher.last_command == desired


def test_public_restart_reconstructs_freeze_generation_and_committed_payload():
    process = _MemoryProcess()
    first = movement_mailbox.publisher_for(process, process.address)
    old = _movement_command(0x50000, 1, 1)
    desired = _movement_command(0x60000, 2, 2)
    assert first.publish(old)
    original = first.reserve(desired)

    del process._movement_mailbox_publishers
    restarted = movement_mailbox.publisher_for(process, process.address)
    assert restarted.adopt_current()
    reconstructed = restarted.reserve(desired)
    assert reconstructed.sequence == original.sequence
    assert not restarted.publish(_movement_command(0x70000, 3, 3))

    process.write(process.address, struct.pack("<I", reconstructed.sequence | 1))
    process.write(process.address + 4, reconstructed.payload)
    process.write(process.address, struct.pack("<I", reconstructed.sequence))

    del process._movement_mailbox_publishers
    after_commit = movement_mailbox.publisher_for(process, process.address)
    assert after_commit.adopt_committed(reconstructed.sequence)
    assert after_commit.sequence == reconstructed.sequence
    assert after_commit.last_command == desired
