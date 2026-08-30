import struct
import sys
from pathlib import Path

from unicorn import UC_ARCH_X86, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_MODE_64, Uc
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
MAIN_BASE = 0x50000000
DUAL_BASE = 0x51000000
STACK_BASE = 0x70000000
RETURN_ADDRESS = 0x60000000


def _map(emu, address, size):
    start = address & ~(PAGE - 1)
    end = (address + size + PAGE - 1) & ~(PAGE - 1)
    emu.mem_map(start, end - start)


def _movement_command(actor, member, generation, world):
    return movement_mailbox.MovementCommand(
        True,
        actor,
        actor + 0x800,
        member,
        generation,
        world,
        0.25 + member,
        0.5 + member,
        0.1,
    )


def test_role_transfer_bridge_commits_commands_hud_then_applied_in_one_callback():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    for address, size in (
        (MAIN_BASE, 0x10000),
        (DUAL_BASE, 0x10000),
        (STACK_BASE, 0x10000),
        (RETURN_ADDRESS, PAGE),
        (coop.NATIVE_LEADER_SETTER, PAGE),
        (session_state.BATTLE_INFO_GLOBAL, PAGE),
        (session_state.LOCATION_SAVE_GLOBAL, PAGE),
        (coop.ACTOR_MANAGER_GLOBAL, PAGE),
        (0x52000000, 0x80000),
    ):
        _map(emu, address, size)

    emu.mem_write(coop.NATIVE_LEADER_BRIDGE_CAVE, coop.build_native_leader_bridge_cave())
    emu.mem_write(
        coop.NATIVE_LEADER_ROLE_COMMIT_CAVE,
        coop.build_native_leader_role_commit_cave(),
    )
    # Deliberately vandalize all 32 bytes of caller-provided shadow space. The
    # bridge's sequence/role locals at +0x20/+0x24 must survive this legal callee.
    setter = b"".join(
        b"\x48\xc7\x44\x24" + bytes((offset,)) + b"\x00\x00\x00\x00"
        for offset in (0x08, 0x10, 0x18, 0x20)
    ) + b"\xc3"
    emu.mem_write(coop.NATIVE_LEADER_SETTER, setter)

    request = 7
    world = 9
    layout_generation = 0x12345678
    journal_sequence = 2
    p2_sequence = 4
    p3_sequence = 4
    p2_new = _movement_command(0x52040000, 0, 11, world)
    p3_new = _movement_command(0x52050000, 2, 12, world)
    p2_old = _movement_command(0x50070000, 1, 7, world)
    p3_old = _movement_command(0x50080000, 0, 8, world)
    battle_info = 0x52000000
    location_base = 0x52010000
    actor_manager = 0x52020000
    actor_count = 4
    location_id = 10
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    p2_handle = 1
    p3_handle = 2
    p1_handle = 3
    p1_actor = 0x52060000
    p1_battle_keep = 0x52062000
    emu.mem_write(session_state.BATTLE_INFO_GLOBAL, struct.pack("<Q", battle_info))
    emu.mem_write(
        session_state.LOCATION_SAVE_GLOBAL, struct.pack("<Q", location_base)
    )
    emu.mem_write(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", actor_manager))
    emu.mem_write(actor_manager, struct.pack("<I", actor_count))
    emu.mem_write(
        location_base + session_state.LOCATION_CURRENT_OFFSET,
        struct.pack("<I", location_id),
    )
    emu.mem_write(
        location_base + session_state.LOCATION_UI_OFFSET,
        struct.pack("<I", location_id),
    )
    for layout_index in (
        session_state.BATTLE_CURRENT_LAYOUT,
        session_state.BATTLE_UI_LAYOUT,
    ):
        emu.mem_write(
            battle_info
            + session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + session_state.BATTLE_PARTY_LAYOUT_STRIDE * layout_index,
            struct.pack("<4H", *layout),
        )
    for actor_id, command, handle, party_slot, bwei, root in (
        (1, p2_new, p2_handle, 0, 0x52030000, 0x52041000),
        (2, p3_new, p3_handle, 2, 0x52031000, 0x52051000),
    ):
        emu.mem_write(
            actor_manager + 8 + actor_id * 8,
            struct.pack("<Q", bwei),
        )
        emu.mem_write(bwei + 0x12, bytes((party_slot,)))
        emu.mem_write(bwei + 0x30, struct.pack("<Q", command.actor))
        emu.mem_write(command.actor + 0x08, struct.pack("<I", handle))
        emu.mem_write(command.actor + 0x10, struct.pack("<Q", root))
        emu.mem_write(root + 0xB8, struct.pack("<Q", command.phys))
    p1_bwei = 0x52032000
    emu.mem_write(
        actor_manager + 8 + (p1_handle & 0xFFFF) * 8,
        struct.pack("<Q", p1_bwei),
    )
    emu.mem_write(p1_bwei + 0x12, b"\x01")
    emu.mem_write(p1_bwei + 0x30, struct.pack("<Q", p1_actor))
    emu.mem_write(p1_actor + 0x08, struct.pack("<I", p1_handle))
    emu.mem_write(p1_actor + 0x698, struct.pack("<Q", p1_battle_keep))
    emu.mem_write(p1_battle_keep + 0x48, struct.pack("<I", 100))
    emu.mem_write(p1_battle_keep + 0x3C, struct.pack("<I", 0))
    emu.mem_write(p1_battle_keep + 0x64, struct.pack("<I", 0))

    for address, old in (
        (coop.P2_MOVEMENT_MAILBOX, p2_old),
        (coop.P3_MOVEMENT_MAILBOX, p3_old),
    ):
        emu.mem_write(address, struct.pack("<I", 2) + old.payload())

    state = bytearray(coop.P2_DEVICE_FILTER_STATE_SIZE)
    struct.pack_into("<4I", state, 0x10, 1, request, request - 1, 0)
    struct.pack_into(
        "<15I",
        state,
        coop.NATIVE_LEADER_ROLE_P1_OFFSET,
        1,
        0,
        2,
        request,
        request,
        p2_sequence,
        p3_sequence,
        1,
        0,
        2,
        0,
        world,
        layout_generation,
        0,
        journal_sequence,
    )
    struct.pack_into(
        "<I",
        state,
        coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        coop.NATIVE_LEADER_ROLE_ACTIVE,
    )
    struct.pack_into(
        "<2I",
        state,
        coop.NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET,
        p2_handle,
        p3_handle,
    )
    struct.pack_into(
        "<I",
        state,
        coop.NATIVE_LEADER_ROLE_P1_NATIVE_HANDLE_OFFSET,
        p1_handle,
    )
    emu.mem_write(coop.P2_DEVICE_FILTER_STATE, bytes(state))

    journal = bytearray(coop.NATIVE_LEADER_JOURNAL_RECORD_SIZE)
    struct.pack_into(
        "<4I",
        journal,
        0,
        journal_sequence,
        request,
        world,
        layout_generation,
    )
    struct.pack_into(
        "<I", journal, coop.NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET, p2_sequence
    )
    journal[
        coop.NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET : coop.NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET
        + movement_mailbox.MAILBOX_SIZE
        - 4
    ] = p2_new.payload()
    struct.pack_into(
        "<I", journal, coop.NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET, p3_sequence
    )
    journal[
        coop.NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET : coop.NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET
        + movement_mailbox.MAILBOX_SIZE
        - 4
    ] = p3_new.payload()
    struct.pack_into(
        "<3I",
        journal,
        coop.NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET,
        1,
        0,
        2,
    )
    struct.pack_into(
        "<QQI",
        journal,
        coop.NATIVE_LEADER_JOURNAL_BATTLE_INFO_OFFSET,
        battle_info,
        location_base,
        location_id,
    )
    struct.pack_into(
        "<4H",
        journal,
        coop.NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET,
        *layout,
    )
    struct.pack_into(
        "<4H",
        journal,
        coop.NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET,
        *layout,
    )
    struct.pack_into(
        "<2I",
        journal,
        coop.NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET,
        p2_handle,
        p3_handle,
    )
    struct.pack_into(
        "<QI",
        journal,
        coop.NATIVE_LEADER_JOURNAL_ACTOR_MANAGER_OFFSET,
        actor_manager,
        actor_count,
    )
    struct.pack_into(
        "<3I",
        journal,
        coop.NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET,
        1,
        0,
        2,
    )
    emu.mem_write(coop.NATIVE_LEADER_ROLE_JOURNAL, bytes(journal))

    writes = []

    def record_write(_emu, _access, address, size, value, _user):
        writes.append((address, size, value))

    emu.hook_add(UC_HOOK_MEM_WRITE, record_write)
    setter_entries = []

    def record_setter_entry(machine, address, _size, _user):
        if address == coop.NATIVE_LEADER_SETTER:
            setter_entries.append(machine.reg_read(UC_X86_REG_RSP))

    emu.hook_add(UC_HOOK_CODE, record_setter_entry)
    stack = STACK_BASE + 0xF008
    emu.mem_write(stack, struct.pack("<Q", RETURN_ADDRESS))
    sentinels = {
        UC_X86_REG_RAX: 0x1111111111111111,
        UC_X86_REG_RCX: 0x2222222222222222,
        UC_X86_REG_RDX: 0x3333333333333333,
        UC_X86_REG_RBX: 0x4444444444444444,
        UC_X86_REG_RBP: 0x4545454545454545,
        UC_X86_REG_RSI: 0x5555555555555555,
        UC_X86_REG_RDI: 0x6666666666666666,
        UC_X86_REG_R8: 0x7777777777777777,
        UC_X86_REG_R9: 0x8888888888888888,
        UC_X86_REG_R10: 0x9999999999999999,
        UC_X86_REG_R11: 0xAAAAAAAAAAAAAAAA,
        UC_X86_REG_R12: 0xBBBBBBBBBBBBBBBB,
        UC_X86_REG_R13: 0xCCCCCCCCCCCCCCCC,
        UC_X86_REG_R14: 0xDDDDDDDDDDDDDDDD,
        UC_X86_REG_R15: 0xEEEEEEEEEEEEEEEE,
    }
    for register, value in sentinels.items():
        emu.reg_write(register, value)
    emu.reg_write(UC_X86_REG_EFLAGS, 0x246)
    emu.reg_write(UC_X86_REG_RSP, stack)
    emu.emu_start(coop.NATIVE_LEADER_BRIDGE_CAVE, RETURN_ADDRESS)

    for register, value in sentinels.items():
        assert emu.reg_read(register) == value
    assert emu.reg_read(UC_X86_REG_RSP) == stack + 8
    assert emu.reg_read(UC_X86_REG_EFLAGS) == 0x246
    assert emu.reg_read(UC_X86_REG_RIP) == RETURN_ADDRESS
    assert len(setter_entries) == 1
    assert setter_entries[0] & 0xF == 8

    for address, sequence, command in (
        (coop.P2_MOVEMENT_MAILBOX, p2_sequence, p2_new),
        (coop.P3_MOVEMENT_MAILBOX, p3_sequence, p3_new),
    ):
        raw = bytes(emu.mem_read(address, movement_mailbox.MAILBOX_SIZE))
        assert struct.unpack_from("<I", raw)[0] == sequence
        assert raw[4:] == command.payload()
    assert struct.unpack(
        "<I",
        emu.mem_read(
            coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,
            4,
        ),
    )[0] == request
    assert struct.unpack(
        "<I",
        emu.mem_read(
            coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            4,
        ),
    )[0] == request

    hud_addresses = (
        coop.COMMAND_BLOCK + coop.P1_HUD_PARTY_SLOT_OFFSET,
        coop.COMMAND_BLOCK + coop.P2_HUD_PARTY_SLOT_OFFSET,
        coop.third_player_block() + coop.P3_HUD_PARTY_SLOT_OFFSET,
    )
    assert tuple(struct.unpack("<I", emu.mem_read(address, 4))[0] for address in hud_addresses) == (
        1,
        0,
        2,
    )
    applied_address = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    )
    applied_index = max(index for index, event in enumerate(writes) if event[0] == applied_address)
    for address in hud_addresses + (coop.P2_MOVEMENT_MAILBOX, coop.P3_MOVEMENT_MAILBOX):
        assert max(index for index, event in enumerate(writes) if event[0] == address) < applied_index
    assert not any(address in hud_addresses and value == 0xFFFFFFFF for address, _size, value in writes)


def test_shutdown_role_fence_never_blocks_ordinary_transient_path():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)

    def run(fenced):
        emu = Uc(UC_ARCH_X86, UC_MODE_64)
        for address, size in (
            (MAIN_BASE, 0x10000),
            (DUAL_BASE, 0x10000),
            (STACK_BASE, 0x10000),
            (RETURN_ADDRESS, PAGE),
            (coop.NATIVE_LEADER_SETTER, PAGE),
        ):
            _map(emu, address, size)
        emu.mem_write(
            coop.NATIVE_LEADER_BRIDGE_CAVE,
            coop.build_native_leader_bridge_cave(),
        )
        emu.mem_write(coop.NATIVE_LEADER_SETTER, b"\xc3")
        state = bytearray(coop.P2_DEVICE_FILTER_STATE_SIZE)
        struct.pack_into("<4I", state, 0x10, 2, 7, 6, 1)
        emu.mem_write(coop.P2_DEVICE_FILTER_STATE, bytes(state))
        emu.mem_write(
            coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
            struct.pack("<I", int(bool(fenced))),
        )
        setter_entries = []

        def record_setter(_emu, address, _size, _user):
            if address == coop.NATIVE_LEADER_SETTER:
                setter_entries.append(address)

        emu.hook_add(UC_HOOK_CODE, record_setter)
        stack = STACK_BASE + 0xF008
        emu.mem_write(stack, struct.pack("<Q", RETURN_ADDRESS))
        emu.reg_write(UC_X86_REG_RSP, stack)
        emu.emu_start(coop.NATIVE_LEADER_BRIDGE_CAVE, RETURN_ADDRESS)
        applied = struct.unpack(
            "<I",
            emu.mem_read(
                coop.P2_DEVICE_FILTER_STATE
                + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
                4,
            ),
        )[0]
        return setter_entries, applied

    blocked_setters, blocked_applied = run(True)
    open_setters, open_applied = run(False)
    assert blocked_setters == [coop.NATIVE_LEADER_SETTER]
    assert blocked_applied == 7
    assert open_setters == [coop.NATIVE_LEADER_SETTER]
    assert open_applied == 7


def test_validation_rejects_odd_reserved_generation_before_setter():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    cave = coop.build_native_leader_role_commit_cave()
    # Both role movement words are explicitly tested for oddness, and live
    # mailbox sequences are loaded through their absolute addresses.
    # Two movement payloads plus the native P1 liveness/status gate.
    assert cave[: coop.NATIVE_LEADER_ROLE_PUBLISH_ENTRY_OFFSET].count(b"\xf6\xc2\x01") == 3
    assert struct.pack("<Q", coop.P2_MOVEMENT_MAILBOX) in cave
    assert struct.pack("<Q", coop.P3_MOVEMENT_MAILBOX) in cave


def test_stock_ui_setter_has_no_mailbox_authorization_during_coop():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    guard = coop.build_native_party_leader_guard_cave()
    assert struct.pack("<Q", coop.COMMAND_BLOCK) in guard
    assert struct.pack("<Q", coop.P2_DEVICE_FILTER_STATE) not in guard
    assert guard.count(b"\xc3") == 1


def _snapshot(identity, *, leader):
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    return session_state.SessionSnapshot(
        identity,
        0x5000,
        layout,
        layout,
        leader,
        10,
        10,
        True,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        False,
        True,
        True,
        True,
        True,
        "ready",
    )


def test_applied_role_identity_survives_immediate_ko_native_repromotion():
    old_identity = session_state.SessionIdentity(0, 1, 2)
    desired_identity = session_state.SessionIdentity(1, 0, 2)
    snapshot = _snapshot(old_identity, leader=2)
    transfer = coop.NativeLeaderRoleTransfer(
        7,
        1,
        desired_identity,
        7,
        4,
        0,
        1,
        0,
        2,
        0,
        9,
        coop.leader_role_layout_generation(
            snapshot.current_layout, snapshot.ui_layout
        ),
        7,
    )

    adopted = coop.snapshot_with_native_leader_role_transfer(snapshot, transfer)

    assert adopted.identity == desired_identity
    assert adopted.leader_party_member_id == 2


def test_setter_crossed_pre_applied_window_uses_prepared_role_identity():
    old_identity = session_state.SessionIdentity(0, 1, 2)
    desired_identity = session_state.SessionIdentity(1, 0, 2)
    snapshot = _snapshot(old_identity, leader=1)
    transfer = coop.NativeLeaderRoleTransfer(
        7,
        1,
        desired_identity,
        7,
        4,
        0,
        1,
        0,
        2,
        0,
        9,
        coop.leader_role_layout_generation(
            snapshot.current_layout, snapshot.ui_layout
        ),
        6,
    )

    assert (
        coop.snapshot_with_native_leader_role_transfer(snapshot, transfer).identity
        == desired_identity
    )
