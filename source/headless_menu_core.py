from __future__ import annotations

import ctypes as ct
import math
import struct
import threading
import time
from ctypes import wintypes as wt

import numpy as np
from PIL import Image

import d3d_panel_transport
from ff12_st2e import BYTE_TO_TEXT
import menu_cursor_overlay_win32 as ui
import coop_runtime as coop


UI_MANAGER_TICK_CALL = 0x28E480
UI_MANAGER_TICK = 0x290CF0
UI_MANAGER_TICK_RETURN = UI_MANAGER_TICK_CALL + 5
SUBMIT_MANUAL_ACTION = 0x311CD0
SUBMIT_RESERVE_ACTION = 0x311B20
SUBMIT_DISMISS_ACTION = 0x3138D0
QUERY_MEMBER_GAMBITS = 0x309B40
SET_MEMBER_GAMBITS = 0x311AF0
RESOLVE_PARTY_MEMBER_RECORD = 0x320A40
PARTY_MEMBER_RUNTIME_ROOT_GLOBAL = 0x2EBF190
PARTY_MEMBER_RUNTIME_BASE_OFFSET = 0x08
PARTY_MEMBER_RUNTIME_STRIDE = 0x1C8
GAMBIT_BLOCKED_STATUS_MASK = coop.KO_STATUS_MASK | coop.STONE_STATUS_MASK
PLAY_UI_SOUND = 0x249C60
ORIGINAL_TICK_CALL = b"\xe8\x6b\x28\x00\x00"
P2_WORLD_TARGET_FOCUS = 0x2B45974


ATTACK_TARGET_HORIZONTAL_RANGE = 24.0


# Unlike actor+0x04 bit 0x100, this does not disappear merely because a neutral
# monster such as the Estersand Cockatrice has not become aggressive yet.
ATTACKABLE_UNIT_TYPES = frozenset((1, 4, 7))
ACTOR_ACTIVE_FLAG = 0x01
ACTOR_HIDDEN_FLAG = 0x02


BKEI_DESPAWNED_FLAG = 0x08
BKEI_DEAD_FLAG = 0x10
BKEI_INACTIVE_FLAG = 0x20
BKEI_INACTIVE_UNTARGETABLE_FLAG = 0x40
BKEI_TARGET_BLOCK_MASK = (
    BKEI_DESPAWNED_FLAG
    | BKEI_DEAD_FLAG
    | BKEI_INACTIVE_FLAG
    | BKEI_INACTIVE_UNTARGETABLE_FLAG
)


def combined_status_effects(process, battle_keep: int) -> int:
    """Mirror the game's temporary-or-permanent status query."""

    return process.u32(
        battle_keep + coop.TEMPORARY_STATUS_EFFECTS_OFFSET
    ) | process.u32(battle_keep + coop.PERMANENT_STATUS_EFFECTS_OFFSET)


DISPATCH_CAVE_OFFSET = 0xE000
DISPATCH_CAVE_END_OFFSET = 0xE400
DISPATCH_STATE_OFFSET = 0xE800
DISPATCH_CAVE_SIZE = DISPATCH_CAVE_END_OFFSET - DISPATCH_CAVE_OFFSET
DISPATCH_STATE_SIZE = 0x100
SOUND_CAVE_OFFSET = 0xEC00
SOUND_CAVE_SIZE = 0x100
SOUND_STATE_OFFSET = 0xED00
INDICATOR_CAVE_OFFSET = 0xE900
INDICATOR_STATE_OFFSET = 0xEB00
INDICATOR_CAVE_SIZE = INDICATOR_STATE_OFFSET - INDICATOR_CAVE_OFFSET
SET_NATIVE_COMBAT_INDICATOR = 0x291010


# The native command menu queries bit 2 with FUN_00309B40 and passes its
# inverse to FUN_00311AF0.  Keep the same operation on the UI/game thread, but
# bind a delayed request to a complete generation identity before invoking it.
GAMBIT_DISPATCH_STATE_OFFSET = 0x00
GAMBIT_DISPATCH_SEQUENCE_OFFSET = 0x04
GAMBIT_DISPATCH_ACK_SEQUENCE_OFFSET = 0x08
GAMBIT_DISPATCH_RESULT_OFFSET = 0x0C
GAMBIT_DISPATCH_HANDLE_OFFSET = 0x10
GAMBIT_DISPATCH_MEMBER_ID_OFFSET = 0x14
GAMBIT_DISPATCH_EXPECTED_ACTOR_OFFSET = 0x18
GAMBIT_DISPATCH_EXPECTED_PHYSICS_OFFSET = 0x20
GAMBIT_DISPATCH_EXPECTED_BATTLE_KEEP_OFFSET = 0x28
GAMBIT_DISPATCH_EXPECTED_ROOT_OFFSET = 0x30
GAMBIT_DISPATCH_FINAL_ENABLED_OFFSET = 0x38
GAMBIT_DISPATCH_MAILBOX_STRUCT = struct.Struct("<IIIIIIQQQQII")
assert GAMBIT_DISPATCH_MAILBOX_STRUCT.size == 0x40
assert GAMBIT_DISPATCH_MAILBOX_STRUCT.size <= DISPATCH_STATE_SIZE

# The process-lifetime UI-tick callback uses an immutable operation mux.  The
# request payload remains comparison/data-only and state is the final release
# store.  E400 is the first byte of the native Axis bridge and is therefore a
# hard boundary, not merely spare padding in the primary cave allocation.
DISPATCH_OPERATION_OFFSET = 0x40
DISPATCH_OPERATION_NONE = 0
DISPATCH_OPERATION_ACTION = 1
DISPATCH_OPERATION_GAMBITS = 2
DISPATCH_RESERVE_MEMBER_NONE = 0xFFFFFFFF
DISPATCH_LAYOUT_MAGIC_OFFSET = 0xF0
DISPATCH_LAYOUT_MAGIC = b"UICORE05"
assert DISPATCH_LAYOUT_MAGIC_OFFSET + len(DISPATCH_LAYOUT_MAGIC) <= DISPATCH_STATE_SIZE

ACTION_DISPATCH_ISSUER_OFFSET = 0x10
ACTION_DISPATCH_TARGET_OFFSET = 0x14
ACTION_DISPATCH_ACTION_OFFSET = 0x18
ACTION_DISPATCH_RESERVE_MEMBER_OFFSET = 0x1C
ACTION_DISPATCH_RESULT_INVALID_OPERATION = 0x20

INDICATOR_DISPATCH_STATE_OFFSET = 0x00
INDICATOR_DISPATCH_SEQUENCE_OFFSET = 0x04
INDICATOR_DISPATCH_ACK_SEQUENCE_OFFSET = 0x08
INDICATOR_DISPATCH_RESULT_OFFSET = 0x10
INDICATOR_DISPATCH_SOURCE_OFFSET = 0x18
INDICATOR_DISPATCH_TARGET_OFFSET = 0x1C
INDICATOR_LAYOUT_MAGIC_OFFSET = 0xF0
INDICATOR_LAYOUT_MAGIC = b"UIIND005"
INDICATOR_DISPATCH_MAILBOX_STRUCT = struct.Struct("<IIIIQII")
assert INDICATOR_DISPATCH_MAILBOX_STRUCT.size == 0x20
assert INDICATOR_LAYOUT_MAGIC_OFFSET + len(INDICATOR_LAYOUT_MAGIC) <= DISPATCH_STATE_SIZE

CORE_DYNAMIC_REGION_OFFSET = DISPATCH_STATE_OFFSET
CORE_DYNAMIC_REGION_END_OFFSET = SOUND_CAVE_OFFSET
CORE_DYNAMIC_REGION_SIZE = CORE_DYNAMIC_REGION_END_OFFSET - CORE_DYNAMIC_REGION_OFFSET

GAMBIT_DISPATCH_RESULT_PENDING = 0
GAMBIT_DISPATCH_RESULT_APPLIED = 1
GAMBIT_DISPATCH_RESULT_STALE_SEQUENCE = 0x10
GAMBIT_DISPATCH_RESULT_STALE_IDENTITY = 0x11
GAMBIT_DISPATCH_RESULT_STALE_PHYSICS = 0x12
GAMBIT_DISPATCH_RESULT_INVALID_MEMBER = 0x13
GAMBIT_DISPATCH_RESULT_STALE_BATTLE_KEEP = 0x14
GAMBIT_DISPATCH_RESULT_VERIFY_FAILED = 0x15
GAMBIT_DISPATCH_RESULT_BLOCKED_STATUS = 0x16


SOUND_PUMP_HOOK = UI_MANAGER_TICK
SOUND_PUMP_RETURN = UI_MANAGER_TICK + 6
SOUND_PUMP_ORIGINAL = b"\x40\x53\x48\x83\xec\x30"

REFERENCE_WIDTH = 1920
REFERENCE_HEIGHT = 1080


OVERLAY_X = 544
OVERLAY_Y = 681
HELP_X = 116
HELP_Y = 116

VK_SPACE = 0x20
VK_BACK = 0x08
VK_I = 0x49
VK_J = 0x4A
VK_K = 0x4B
VK_L = 0x4C
VK_Q = 0x51
VK_E = 0x45
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_NUMPAD0 = 0x60

HWND_TOPMOST = wt.HWND(-1)
HWND_NOTOPMOST = wt.HWND(-2)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200

_RETIRED_WINDOW_PROCEDURES: dict[tuple[int, str], object] = {}

ui.user32.SetWindowPos.argtypes = [
    wt.HWND,
    wt.HWND,
    ct.c_int,
    ct.c_int,
    ct.c_int,
    ct.c_int,
    wt.UINT,
]
ui.user32.SetWindowPos.restype = wt.BOOL
ui.user32.IsWindow.argtypes = [wt.HWND]
ui.user32.IsWindow.restype = wt.BOOL
ui.user32.IsIconic.argtypes = [wt.HWND]
ui.user32.IsIconic.restype = wt.BOOL


def add_rel32(code: bytearray, opcode: bytes, cave: int, target: int) -> None:
    source = cave + len(code)
    code += opcode
    code += coop.rel32(source, len(opcode) + 4, target)


def build_dispatcher(
    cave: int,
    state: int,
    issuer: int,
    target: int,
    action: int,
    reserve_party_member_id: int | None = None,
) -> bytes:
    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", state)
    code += b"\x83\x38\x01"
    code += b"\x0f\x85"
    pass_fixup = len(code)
    code += b"\x00" * 4
    code += b"\xc7\x00\x02\x00\x00\x00"

    code += b"\x51"
    code += b"\x48\x83\xec\x28"
    if action == 0x113 and reserve_party_member_id is None:
        # The stock Dismiss helper verifies summon mode, resolves party slot 3
        # (the materialized Esper), and queues action 0x113 on that actor.  The
        # generic manual route accepts a summoner handle but silently does not
        # end the summon in the co-op party layout.
        code += b"\x33\xc9"  # xor ecx,ecx: clear summon flag after request
        code += b"\x48\xb8" + struct.pack("<Q", SUBMIT_DISMISS_ACTION)
        code += b"\xff\xd0"
        code += b"\x33\xc0"  # stable success result; native helper is void
    elif reserve_party_member_id is None:
        code += b"\xb9" + struct.pack("<I", issuer)
        code += b"\xba" + struct.pack("<I", target)
        code += b"\x41\xb8" + struct.pack("<I", action)
        code += b"\x45\x33\xc9"
        entrypoint = SUBMIT_MANUAL_ACTION
        code += b"\x48\xb8" + struct.pack("<Q", entrypoint)
        code += b"\xff\xd0"
    else:
        code += b"\xb9" + struct.pack("<I", issuer)
        if not 0 <= int(reserve_party_member_id) <= 0xFF:
            raise ValueError("reserve party-member id must fit in one byte")
        code += b"\x33\xd2"
        code += b"\x41\xb8" + struct.pack("<I", int(reserve_party_member_id))
        code += b"\x41\xb9" + struct.pack("<I", action)
        code += b"\xc7\x44\x24\x20\x00\x00\x00\x00"
        entrypoint = SUBMIT_RESERVE_ACTION
        code += b"\x48\xb8" + struct.pack("<Q", entrypoint)
        code += b"\xff\xd0"
    code += b"\x48\xba" + struct.pack("<Q", state)
    code += b"\x89\x42\x08"
    code += b"\xc7\x02\x03\x00\x00\x00"
    code += b"\x48\x83\xc4\x28"
    code += b"\x59"

    passthrough = len(code)
    add_rel32(code, b"\xe8", cave, UI_MANAGER_TICK)
    add_rel32(code, b"\xe9", cave, UI_MANAGER_TICK_RETURN)
    struct.pack_into(
        "<i",
        code,
        pass_fixup,
        cave + passthrough - (cave + pass_fixup + 4),
    )
    return bytes(code)


def build_gambit_dispatcher(
    cave: int,
    state: int,
    *,
    entity_resolver: int | None = None,
    physics_helper: int | None = None,
    member_record_resolver: int = RESOLVE_PARTY_MEMBER_RECORD,
    query_gambits: int = QUERY_MEMBER_GAMBITS,
    set_gambits: int = SET_MEMBER_GAMBITS,
    runtime_root_global: int = PARTY_MEMBER_RUNTIME_ROOT_GLOBAL,
) -> bytes:
    """Build a one-shot, generation-safe native Gambits toggle.

    The detour replaces the existing UI-manager tick call, so the cave runs on
    the same game thread as the stock command menu.  Mailbox pointers are only
    comparison values: the cave resolves the generation handle and every
    native root again before calling the game's own Gambits setter.
    """

    cave = int(cave)
    state = int(state)
    entity_resolver = int(
        coop.ENTITY_ENTRY_RESOLVER if entity_resolver is None else entity_resolver
    )
    physics_helper = int(
        coop.ACTOR_PHYSICS_HELPER if physics_helper is None else physics_helper
    )
    native_targets = (
        entity_resolver,
        physics_helper,
        int(member_record_resolver),
        int(query_gambits),
        int(set_gambits),
        int(runtime_root_global),
    )
    if not cave or not state or not all(native_targets):
        raise ValueError("native Gambits dispatcher addresses must be non-zero")

    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(label: str) -> None:
        labels[label] = len(code)

    def branch(opcode: bytes, label: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    def call_absolute(target: int) -> None:
        # RAX is the custom input of ACTOR_PHYSICS_HELPER.  Use volatile R11 as
        # the call carrier so resolving physics cannot destroy that argument.
        code.extend(b"\x49\xbb" + struct.pack("<Q", int(target)))
        code.extend(b"\x41\xff\xd3")

    def sequence_still_current() -> None:
        code.extend(b"\x41\x39\x5f\x04")  # cmp [r15+sequence],ebx
        branch(b"\x0f\x85", "reject_sequence")

    def validate_identity() -> None:
        sequence_still_current()

        # A full 32-bit entity handle contains registry, slot and generation.
        # FUN_003588B0 rejects a reused slot whose generation no longer agrees.
        code.extend(b"\x41\x8b\x4f\x10")  # mov ecx,[r15+handle]
        code.extend(b"\x85\xc9")
        branch(b"\x0f\x84", "reject_identity")
        call_absolute(entity_resolver)
        code.extend(b"\x48\x85\xc0")
        branch(b"\x0f\x84", "reject_identity")
        code.extend(b"\x48\x8b\x40\x30")  # registry entry -> live actor
        code.extend(b"\x4c\x39\xe0")  # cmp rax,r12
        branch(b"\x0f\x85", "reject_identity")
        sequence_still_current()

        # Derive physics from the resolved live actor; the mailbox value is
        # never used as a dereference base.
        code.extend(b"\x4c\x89\xe0")  # mov rax,r12
        call_absolute(physics_helper)
        branch(b"\x0f\x82", "reject_physics")  # CF => broken native chain
        code.extend(b"\x4c\x39\xe8")  # cmp rax,r13
        branch(b"\x0f\x85", "reject_physics")
        sequence_still_current()

        # actor+698h and the native member resolver must name the same record,
        # under the same runtime root that the publisher observed.
        code.extend(b"\x49\x8b\x84\x24\x98\x06\x00\x00")
        code.extend(b"\x4c\x39\xf0")  # cmp rax,r14
        branch(b"\x0f\x85", "reject_battle_keep")
        code.extend(b"\x48\xb8" + struct.pack("<Q", int(runtime_root_global)))
        code.extend(b"\x48\x8b\x00")
        code.extend(b"\x48\x39\xf0")  # cmp rax,rsi
        branch(b"\x0f\x85", "reject_battle_keep")
        code.extend(b"\x8b\xcd")  # mov ecx,ebp
        call_absolute(member_record_resolver)
        code.extend(b"\x4c\x39\xf0")  # cmp rax,r14
        branch(b"\x0f\x85", "reject_battle_keep")
        code.extend(b"\x41\x8b\x46\x3c")
        code.extend(b"\x41\x0b\x46\x64")
        code.extend(b"\xa9" + struct.pack("<I", GAMBIT_BLOCKED_STATUS_MASK))
        branch(b"\x0f\x85", "reject_status")
        sequence_still_current()

    # The recovery code recognizes this exact movabs(state) prefix.  Claiming
    # state 1 as state 2 makes every later frame a bounded pass-through.
    code.extend(b"\x48\xb8" + struct.pack("<Q", state))
    code.extend(b"\x83\x38\x01")
    branch(b"\x0f\x85", "stock_tick")
    code.extend(b"\xc7\x00\x02\x00\x00\x00")

    # The JMP detour arrives with the caller's pre-CALL alignment.  Nine pushes
    # plus 48h bytes retain Win64 call alignment and reserve 20h shadow bytes;
    # locals begin at rsp+20h.  Every nonvolatile register used below and the
    # stock tick's original RCX are restored before the displaced call.
    code.extend(b"\x51\x53\x55\x56\x57")
    code.extend(b"\x41\x54\x41\x55\x41\x56\x41\x57")
    code.extend(b"\x48\x83\xec\x48")
    code.extend(b"\x49\xbf" + struct.pack("<Q", state))
    code.extend(b"\x41\x8b\x5f\x04")  # ebx=request sequence
    code.extend(b"\x85\xdb")
    branch(b"\x0f\x84", "reject_sequence")
    code.extend(b"\xf6\xc3\x01")
    branch(b"\x0f\x85", "reject_sequence")

    code.extend(b"\x41\x8b\x6f\x14")  # ebp=member id
    code.extend(b"\x83\xfd\x05")
    branch(b"\x0f\x87", "reject_member")
    code.extend(b"\x4d\x8b\x67\x18")  # r12=expected actor
    code.extend(b"\x4d\x8b\x6f\x20")  # r13=expected physics
    code.extend(b"\x4d\x8b\x77\x28")  # r14=expected battle keep
    code.extend(b"\x49\x8b\x77\x30")  # rsi=expected runtime root
    for test in (
        b"\x4d\x85\xe4",  # r12
        b"\x4d\x85\xed",  # r13
        b"\x4d\x85\xf6",  # r14
        b"\x48\x85\xf6",  # rsi
    ):
        code.extend(test)
        branch(b"\x0f\x84", "reject_identity")

    # First proof binds the sampled role identity before querying current state.
    validate_identity()
    code.extend(b"\x8b\xcd")  # mov ecx,ebp
    call_absolute(query_gambits)
    code.extend(b"\x31\xff")  # xor edi,edi
    code.extend(b"\x85\xc0")
    code.extend(b"\x40\x0f\x94\xc7")  # setz dil => desired inverse
    sequence_still_current()

    # Repeat the complete proof immediately before mutation.  A generation
    # swap, despawn, root rebuild or member reassignment during the native query
    # therefore rejects without calling the setter.
    validate_identity()
    code.extend(b"\x41\x8b\x06")  # mov eax,[r14]
    code.extend(b"\x89\x44\x24\x20")  # save complete pre-set dword
    sequence_still_current()
    code.extend(b"\x8b\xcd")  # mov ecx,ebp
    code.extend(b"\x8b\xd7")  # mov edx,edi
    call_absolute(set_gambits)

    # Verify the native resolver still returns the same record and that the
    # setter preserved all 31 unrelated bits.  Never retry automatically after
    # this point: a verification failure is mutation-ambiguous.
    code.extend(b"\x48\xb8" + struct.pack("<Q", int(runtime_root_global)))
    code.extend(b"\x48\x8b\x00")
    code.extend(b"\x48\x39\xf0")
    branch(b"\x0f\x85", "reject_verify")
    code.extend(b"\x8b\xcd")
    call_absolute(member_record_resolver)
    code.extend(b"\x4c\x39\xf0")
    branch(b"\x0f\x85", "reject_verify")
    code.extend(b"\x8b\x44\x24\x20")
    code.extend(b"\x83\xe0\xfb")
    code.extend(b"\x85\xff")
    branch(b"\x0f\x84", "expected_ready")
    code.extend(b"\x83\xc8\x04")
    mark("expected_ready")
    code.extend(b"\x41\x39\x06")  # cmp [r14],eax
    branch(b"\x0f\x85", "reject_verify")

    code.extend(b"\x41\x89\x7f\x38")  # final enabled
    code.extend(
        b"\x41\xc7\x47\x0c"
        + struct.pack("<I", GAMBIT_DISPATCH_RESULT_APPLIED)
    )
    code.extend(b"\x41\x89\x5f\x08")  # acknowledge sequence
    code.extend(b"\x41\xc7\x07\x03\x00\x00\x00")
    branch(b"\xe9", "restore")

    for label, result in (
        ("reject_sequence", GAMBIT_DISPATCH_RESULT_STALE_SEQUENCE),
        ("reject_identity", GAMBIT_DISPATCH_RESULT_STALE_IDENTITY),
        ("reject_physics", GAMBIT_DISPATCH_RESULT_STALE_PHYSICS),
        ("reject_member", GAMBIT_DISPATCH_RESULT_INVALID_MEMBER),
        ("reject_battle_keep", GAMBIT_DISPATCH_RESULT_STALE_BATTLE_KEEP),
        ("reject_verify", GAMBIT_DISPATCH_RESULT_VERIFY_FAILED),
        ("reject_status", GAMBIT_DISPATCH_RESULT_BLOCKED_STATUS),
    ):
        mark(label)
        code.extend(b"\xb8" + struct.pack("<I", result))
        branch(b"\xe9", "publish_reject")

    mark("publish_reject")
    code.extend(b"\x41\x89\x47\x0c")
    code.extend(b"\x41\x89\x5f\x08")
    code.extend(b"\x41\xc7\x07\x04\x00\x00\x00")

    mark("restore")
    code.extend(b"\x48\x83\xc4\x48")
    code.extend(b"\x41\x5f\x41\x5e\x41\x5d\x41\x5c")
    code.extend(b"\x5f\x5e\x5d\x5b\x59")

    mark("stock_tick")
    add_rel32(code, b"\xe8", cave, UI_MANAGER_TICK)
    add_rel32(code, b"\xe9", cave, UI_MANAGER_TICK_RETURN)

    for position, label in fixups:
        if label not in labels:
            raise RuntimeError(f"unknown native Gambits label: {label}")
        struct.pack_into("<i", code, position, labels[label] - (position + 4))
    if len(code) > DISPATCH_CAVE_SIZE:
        raise RuntimeError(f"native Gambits dispatcher overflow: {len(code):#x}")
    return bytes(code)


def build_core_tick_callback(
    cave: int,
    state: int,
    indicator_state: int,
    *,
    proof_cave: int | None = None,
    entity_resolver: int | None = None,
    physics_helper: int | None = None,
    member_record_resolver: int = RESOLVE_PARTY_MEMBER_RECORD,
    query_gambits: int = QUERY_MEMBER_GAMBITS,
    set_gambits: int = SET_MEMBER_GAMBITS,
    runtime_root_global: int = PARTY_MEMBER_RUNTIME_ROOT_GLOBAL,
    submit_manual_action: int = SUBMIT_MANUAL_ACTION,
    submit_reserve_action: int = SUBMIT_RESERVE_ACTION,
    submit_dismiss_action: int = SUBMIT_DISMISS_ACTION,
    set_combat_indicator: int = SET_NATIVE_COMBAT_INDICATOR,
) -> bytes:
    """Build the immutable Action/Gambits/Indicator UI-tick callback.

    The callback is reached through the process-lifetime UI-tick mux and always
    returns to that mux.  Terminal mailbox stores are deliberately followed
    only by register/stack restoration: observing state 3/4 can never authorize
    Python to rewrite code that this invocation may still be executing.
    """

    cave = int(cave)
    state = int(state)
    indicator_state = int(indicator_state)
    proof_cave = int(
        cave + (INDICATOR_CAVE_OFFSET - DISPATCH_CAVE_OFFSET)
        if proof_cave is None
        else proof_cave
    )
    entity_resolver = int(
        coop.ENTITY_ENTRY_RESOLVER if entity_resolver is None else entity_resolver
    )
    physics_helper = int(
        coop.ACTOR_PHYSICS_HELPER if physics_helper is None else physics_helper
    )
    native_targets = (
        entity_resolver,
        physics_helper,
        int(member_record_resolver),
        int(query_gambits),
        int(set_gambits),
        int(runtime_root_global),
        int(submit_manual_action),
        int(submit_reserve_action),
        int(submit_dismiss_action),
        int(set_combat_indicator),
    )
    if (
        not cave
        or not state
        or not indicator_state
        or not proof_cave
        or not all(native_targets)
    ):
        raise ValueError("native core dispatcher addresses must be non-zero")

    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(label: str) -> None:
        labels[label] = len(code)

    def branch(opcode: bytes, label: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    def call_absolute(target: int) -> None:
        code.extend(b"\x49\xbb" + struct.pack("<Q", int(target)))
        code.extend(b"\x41\xff\xd3")

    def check_main_sequence(label: str = "reject_sequence") -> None:
        code.extend(b"\x41\x39\x5f\x04")
        branch(b"\x0f\x85", label)

    # Preserve the mux-provided UI manager for the native indicator ABI and
    # provide shadow space for the inline handlers.  The mux itself preserves
    # all volatile state required by the displaced stock call.
    code.extend(b"\x51\x48\x83\xec\x20")
    code.extend(b"\x48\xb8" + struct.pack("<Q", state))
    code.extend(b"\x83\x38\x01")
    branch(b"\x0f\x85", "indicator")
    code.extend(b"\x83\x78\x40" + bytes((DISPATCH_OPERATION_GAMBITS,)))
    branch(b"\x0f\x84", "gambit_claim")
    code.extend(b"\x83\x78\x40" + bytes((DISPATCH_OPERATION_ACTION,)))
    branch(b"\x0f\x84", "action_claim")

    # Unknown operations are claimed and rejected once.  They can never wedge
    # an armed mailbox indefinitely or fall through to a native mutator.
    code.extend(b"\xc7\x00\x02\x00\x00\x00")
    code.extend(b"\x8b\x48\x04")
    code.extend(
        b"\xc7\x40\x0c"
        + struct.pack("<I", ACTION_DISPATCH_RESULT_INVALID_OPERATION)
    )
    code.extend(b"\x89\x48\x08")
    code.extend(b"\xc7\x00\x04\x00\x00\x00")
    branch(b"\xe9", "indicator")

    mark("gambit_claim")
    code.extend(b"\xc7\x00\x02\x00\x00\x00")
    code.extend(b"\x53\x55\x56\x57")
    code.extend(b"\x41\x54\x41\x55\x41\x56\x41\x57")
    code.extend(b"\x48\x83\xec\x30")
    code.extend(b"\x49\xbf" + struct.pack("<Q", state))
    code.extend(b"\x41\x8b\x5f\x04")
    code.extend(b"\x85\xdb")
    branch(b"\x0f\x84", "reject_sequence")
    code.extend(b"\xf6\xc3\x01")
    branch(b"\x0f\x85", "reject_sequence")
    code.extend(b"\x41\x8b\x6f\x14")
    code.extend(b"\x83\xfd\x05")
    branch(b"\x0f\x87", "reject_member")
    code.extend(b"\x4d\x8b\x67\x18")
    code.extend(b"\x4d\x8b\x6f\x20")
    code.extend(b"\x4d\x8b\x77\x28")
    code.extend(b"\x49\x8b\x77\x30")
    for test in (
        b"\x4d\x85\xe4",
        b"\x4d\x85\xed",
        b"\x4d\x85\xf6",
        b"\x48\x85\xf6",
    ):
        code.extend(test)
        branch(b"\x0f\x84", "reject_identity")

    # The same complete proof subroutine runs before the query and immediately
    # before the setter.  It returns zero or the precise fail-closed result.
    add_rel32(code, b"\xe8", cave, proof_cave)
    code.extend(b"\x85\xc0")
    branch(b"\x0f\x85", "publish_reject")
    code.extend(b"\x8b\xcd")
    call_absolute(query_gambits)
    code.extend(b"\x31\xff\x85\xc0\x40\x0f\x94\xc7")
    check_main_sequence()
    add_rel32(code, b"\xe8", cave, proof_cave)
    code.extend(b"\x85\xc0")
    branch(b"\x0f\x85", "publish_reject")
    code.extend(b"\x41\x8b\x06")
    code.extend(b"\x89\x44\x24\x20")
    check_main_sequence()
    code.extend(b"\x8b\xcd\x8b\xd7")
    call_absolute(set_gambits)

    # Verify the exact native record and a bit-preserving RMW result.  A failure
    # here is mutation-ambiguous and is never retried automatically.
    code.extend(b"\x48\xb8" + struct.pack("<Q", int(runtime_root_global)))
    code.extend(b"\x48\x8b\x00\x48\x39\xf0")
    branch(b"\x0f\x85", "reject_verify")
    code.extend(b"\x8b\xcd")
    call_absolute(member_record_resolver)
    code.extend(b"\x4c\x39\xf0")
    branch(b"\x0f\x85", "reject_verify")
    code.extend(b"\x8b\x44\x24\x20\x83\xe0\xfb\x85\xff")
    branch(b"\x0f\x84", "expected_ready")
    code.extend(b"\x83\xc8\x04")
    mark("expected_ready")
    code.extend(b"\x41\x39\x06")
    branch(b"\x0f\x85", "reject_verify")
    code.extend(b"\x41\x89\x7f\x38")
    code.extend(
        b"\x41\xc7\x47\x0c"
        + struct.pack("<I", GAMBIT_DISPATCH_RESULT_APPLIED)
    )
    code.extend(b"\x41\x89\x5f\x08")
    code.extend(b"\x41\xc7\x07\x03\x00\x00\x00")
    branch(b"\xe9", "gambit_restore")

    for label, result in (
        ("reject_sequence", GAMBIT_DISPATCH_RESULT_STALE_SEQUENCE),
        ("reject_identity", GAMBIT_DISPATCH_RESULT_STALE_IDENTITY),
        ("reject_member", GAMBIT_DISPATCH_RESULT_INVALID_MEMBER),
        ("reject_verify", GAMBIT_DISPATCH_RESULT_VERIFY_FAILED),
    ):
        mark(label)
        code.extend(b"\xb8" + struct.pack("<I", result))
        branch(b"\xe9", "publish_reject")
    mark("publish_reject")
    code.extend(b"\x41\x89\x47\x0c")
    code.extend(b"\x41\x89\x5f\x08")
    code.extend(b"\x41\xc7\x07\x04\x00\x00\x00")
    mark("gambit_restore")
    code.extend(b"\x48\x83\xc4\x30")
    code.extend(b"\x41\x5f\x41\x5e\x41\x5d\x41\x5c")
    code.extend(b"\x5f\x5e\x5d\x5b")
    branch(b"\xe9", "indicator")

    mark("action_claim")
    code.extend(b"\xc7\x00\x02\x00\x00\x00")
    code.extend(b"\x53\x56\x48\x83\xec\x20")
    code.extend(b"\x48\xbb" + struct.pack("<Q", state))
    code.extend(b"\x8b\x73\x04\x85\xf6")
    branch(b"\x0f\x84", "action_bad_sequence")
    code.extend(b"\x40\xf6\xc6\x01")
    branch(b"\x0f\x85", "action_bad_sequence")
    code.extend(b"\x81\x7b\x18\x13\x01\x00\x00")
    branch(b"\x0f\x85", "action_not_dismiss")
    code.extend(b"\x83\x7b\x1c\xff")
    branch(b"\x0f\x85", "action_reserve")
    code.extend(b"\x33\xc9")
    call_absolute(submit_dismiss_action)
    code.extend(b"\x33\xc0")
    branch(b"\xe9", "action_publish")
    mark("action_not_dismiss")
    code.extend(b"\x83\x7b\x1c\xff")
    branch(b"\x0f\x85", "action_reserve")
    code.extend(b"\x8b\x4b\x10\x8b\x53\x14\x44\x8b\x43\x18\x45\x33\xc9")
    code.extend(b"\x39\x73\x04")
    branch(b"\x0f\x85", "action_bad_sequence")
    call_absolute(submit_manual_action)
    branch(b"\xe9", "action_publish")
    mark("action_reserve")
    code.extend(b"\x81\x7b\x1c\xff\x00\x00\x00")
    branch(b"\x0f\x87", "action_invalid")
    code.extend(b"\x8b\x4b\x10\x33\xd2\x44\x8b\x43\x1c\x44\x8b\x4b\x18")
    code.extend(b"\xc7\x44\x24\x20\x00\x00\x00\x00")
    code.extend(b"\x39\x73\x04")
    branch(b"\x0f\x85", "action_bad_sequence")
    call_absolute(submit_reserve_action)
    mark("action_publish")
    code.extend(b"\x89\x43\x0c\x89\x73\x08")
    code.extend(b"\xc7\x03\x03\x00\x00\x00")
    branch(b"\xe9", "action_restore")
    mark("action_bad_sequence")
    code.extend(b"\xb8" + struct.pack("<I", GAMBIT_DISPATCH_RESULT_STALE_SEQUENCE))
    branch(b"\xe9", "action_reject")
    mark("action_invalid")
    code.extend(
        b"\xb8" + struct.pack("<I", ACTION_DISPATCH_RESULT_INVALID_OPERATION)
    )
    mark("action_reject")
    code.extend(b"\x89\x43\x0c\x89\x73\x08")
    code.extend(b"\xc7\x03\x04\x00\x00\x00")
    mark("action_restore")
    code.extend(b"\x48\x83\xc4\x20\x5e\x5b")

    mark("indicator")
    code.extend(b"\x48\xb8" + struct.pack("<Q", indicator_state))
    code.extend(b"\x83\x38\x01")
    branch(b"\x0f\x85", "callback_return")
    code.extend(b"\xc7\x00\x02\x00\x00\x00")
    code.extend(b"\x48\x8b\x4c\x24\x20")
    code.extend(b"\x53\x56\x48\x83\xec\x20")
    code.extend(b"\x48\xbb" + struct.pack("<Q", indicator_state))
    code.extend(b"\x8b\x73\x04\x85\xf6")
    branch(b"\x0f\x84", "indicator_reject")
    code.extend(b"\x40\xf6\xc6\x01")
    branch(b"\x0f\x85", "indicator_reject")
    code.extend(b"\x39\x73\x04")
    branch(b"\x0f\x85", "indicator_reject")
    code.extend(b"\x8b\x53\x18\x44\x8b\x43\x1c")
    call_absolute(set_combat_indicator)
    code.extend(b"\x48\x89\x43\x10\x89\x73\x08")
    code.extend(b"\xc7\x03\x03\x00\x00\x00")
    branch(b"\xe9", "indicator_restore")
    mark("indicator_reject")
    code.extend(b"\x48\xc7\x43\x10\x00\x00\x00\x00")
    code.extend(b"\x89\x73\x08\xc7\x03\x04\x00\x00\x00")
    mark("indicator_restore")
    code.extend(b"\x48\x83\xc4\x20\x5e\x5b")
    mark("callback_return")
    code.extend(b"\x48\x83\xc4\x20\x59\xc3")

    for position, label in fixups:
        if label not in labels:
            raise RuntimeError(f"unknown native core dispatcher label: {label}")
        struct.pack_into("<i", code, position, labels[label] - (position + 4))
    if len(code) > DISPATCH_CAVE_SIZE:
        raise RuntimeError(
            "native core dispatcher crosses the E400 Axis boundary: "
            f"{len(code):#x} > {DISPATCH_CAVE_SIZE:#x}"
        )
    return bytes(code)


def build_core_identity_proof(
    cave: int,
    state: int,
    *,
    entity_resolver: int | None = None,
    physics_helper: int | None = None,
    member_record_resolver: int = RESOLVE_PARTY_MEMBER_RECORD,
    runtime_root_global: int = PARTY_MEMBER_RUNTIME_ROOT_GLOBAL,
) -> bytes:
    """Build the shared, immutable double-invoked Gambits identity proof."""

    cave = int(cave)
    state = int(state)
    entity_resolver = int(
        coop.ENTITY_ENTRY_RESOLVER if entity_resolver is None else entity_resolver
    )
    physics_helper = int(
        coop.ACTOR_PHYSICS_HELPER if physics_helper is None else physics_helper
    )
    if not all(
        (
            cave,
            state,
            entity_resolver,
            physics_helper,
            int(member_record_resolver),
            int(runtime_root_global),
        )
    ):
        raise ValueError("native Gambits proof addresses must be non-zero")

    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(label: str) -> None:
        labels[label] = len(code)

    def branch(opcode: bytes, label: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    def call_absolute(target: int) -> None:
        code.extend(b"\x49\xbb" + struct.pack("<Q", int(target)))
        code.extend(b"\x41\xff\xd3")

    def check_sequence() -> None:
        code.extend(b"\x41\x39\x5f\x04")
        branch(b"\x0f\x85", "proof_sequence")

    # The parent callback is aligned before CALL.  This nested frame supplies
    # fresh shadow space for every game ABI invoked by the proof.
    code.extend(b"\x48\x83\xec\x28")
    check_sequence()
    code.extend(b"\x41\x8b\x4f\x10\x85\xc9")
    branch(b"\x0f\x84", "proof_identity")
    call_absolute(entity_resolver)
    code.extend(b"\x48\x85\xc0")
    branch(b"\x0f\x84", "proof_identity")
    code.extend(b"\x48\x8b\x40\x30\x4c\x39\xe0")
    branch(b"\x0f\x85", "proof_identity")
    # A registry entry may briefly retain its actor while teardown has already
    # made that actor inactive/hidden.  Treat that interval as a stale
    # generation too; no native query or setter may run for a despawning actor.
    code.extend(b"\x49\x8b\x04\x24\xa8" + bytes((ACTOR_ACTIVE_FLAG,)))
    branch(b"\x0f\x84", "proof_identity")
    code.extend(b"\xa8" + bytes((ACTOR_HIDDEN_FLAG,)))
    branch(b"\x0f\x85", "proof_identity")
    check_sequence()
    code.extend(b"\x4c\x89\xe0")
    call_absolute(physics_helper)
    branch(b"\x0f\x82", "proof_physics")
    code.extend(b"\x4c\x39\xe8")
    branch(b"\x0f\x85", "proof_physics")
    check_sequence()
    code.extend(b"\x49\x8b\x84\x24\x98\x06\x00\x00\x4c\x39\xf0")
    branch(b"\x0f\x85", "proof_battle_keep")
    code.extend(b"\x48\xb8" + struct.pack("<Q", int(runtime_root_global)))
    code.extend(b"\x48\x8b\x00\x48\x39\xf0")
    branch(b"\x0f\x85", "proof_battle_keep")
    code.extend(b"\x8b\xcd")
    call_absolute(member_record_resolver)
    code.extend(b"\x4c\x39\xf0")
    branch(b"\x0f\x85", "proof_battle_keep")
    code.extend(b"\x41\x8b\x46\x3c\x41\x0b\x46\x64")
    code.extend(b"\xa9" + struct.pack("<I", GAMBIT_BLOCKED_STATUS_MASK))
    branch(b"\x0f\x85", "proof_status")
    check_sequence()
    code.extend(b"\x33\xc0")
    branch(b"\xe9", "proof_return")
    for label, result in (
        ("proof_sequence", GAMBIT_DISPATCH_RESULT_STALE_SEQUENCE),
        ("proof_identity", GAMBIT_DISPATCH_RESULT_STALE_IDENTITY),
        ("proof_physics", GAMBIT_DISPATCH_RESULT_STALE_PHYSICS),
        ("proof_battle_keep", GAMBIT_DISPATCH_RESULT_STALE_BATTLE_KEEP),
        ("proof_status", GAMBIT_DISPATCH_RESULT_BLOCKED_STATUS),
    ):
        mark(label)
        code.extend(b"\xb8" + struct.pack("<I", result))
        branch(b"\xe9", "proof_return")
    mark("proof_return")
    code.extend(b"\x48\x83\xc4\x28\xc3")

    for position, label in fixups:
        if label not in labels:
            raise RuntimeError(f"unknown native Gambits proof label: {label}")
        struct.pack_into("<i", code, position, labels[label] - (position + 4))
    if len(code) > INDICATOR_CAVE_SIZE:
        raise RuntimeError(
            f"native Gambits proof overflow: {len(code):#x} > {INDICATOR_CAVE_SIZE:#x}"
        )
    return bytes(code)


_CORE_TOPOLOGY_LOCK = threading.RLock()
_MAILBOX_LOCK_GUARD = threading.Lock()
_MAILBOX_LOCKS: dict[tuple[int, int], threading.RLock] = {}


def _mailbox_lock(process: object, address: int) -> threading.RLock:
    key = (id(process), int(address))
    with _MAILBOX_LOCK_GUARD:
        return _MAILBOX_LOCKS.setdefault(key, threading.RLock())


def _initial_core_dynamic_image(proof_image: bytes) -> bytes:
    image = bytearray(CORE_DYNAMIC_REGION_SIZE)
    dispatch_magic = DISPATCH_LAYOUT_MAGIC_OFFSET
    image[dispatch_magic : dispatch_magic + len(DISPATCH_LAYOUT_MAGIC)] = (
        DISPATCH_LAYOUT_MAGIC
    )
    proof_offset = INDICATOR_CAVE_OFFSET - CORE_DYNAMIC_REGION_OFFSET
    image[proof_offset : proof_offset + len(proof_image)] = proof_image
    indicator_offset = INDICATOR_STATE_OFFSET - CORE_DYNAMIC_REGION_OFFSET
    indicator_magic = indicator_offset + INDICATOR_LAYOUT_MAGIC_OFFSET
    image[indicator_magic : indicator_magic + len(INDICATOR_LAYOUT_MAGIC)] = (
        INDICATOR_LAYOUT_MAGIC
    )
    return bytes(image)


def _valid_dispatch_slot(payload: bytes) -> bool:
    if len(payload) != DISPATCH_STATE_SIZE:
        return False
    if (
        payload[DISPATCH_LAYOUT_MAGIC_OFFSET : DISPATCH_LAYOUT_MAGIC_OFFSET + 8]
        != DISPATCH_LAYOUT_MAGIC
        or any(payload[DISPATCH_OPERATION_OFFSET + 4 : DISPATCH_LAYOUT_MAGIC_OFFSET])
        or any(payload[DISPATCH_LAYOUT_MAGIC_OFFSET + 8 :])
    ):
        return False
    state, sequence, ack, _result = struct.unpack_from("<IIII", payload)
    operation = struct.unpack_from("<I", payload, DISPATCH_OPERATION_OFFSET)[0]
    if state not in range(5) or operation not in (
        DISPATCH_OPERATION_NONE,
        DISPATCH_OPERATION_ACTION,
        DISPATCH_OPERATION_GAMBITS,
    ):
        return False
    if state in (1, 2, 3, 4):
        if not sequence or sequence & 1 or operation == DISPATCH_OPERATION_NONE:
            return False
    if state == 1 and ack != 0:
        return False
    if state == 2 and ack not in (0, sequence):
        return False
    if state in (3, 4) and ack != sequence:
        return False
    return not (state == 0 and sequence & 1)


def _valid_indicator_slot(payload: bytes) -> bool:
    if len(payload) != DISPATCH_STATE_SIZE:
        return False
    if (
        payload[INDICATOR_LAYOUT_MAGIC_OFFSET : INDICATOR_LAYOUT_MAGIC_OFFSET + 8]
        != INDICATOR_LAYOUT_MAGIC
        or any(
            payload[
                INDICATOR_DISPATCH_MAILBOX_STRUCT.size : INDICATOR_LAYOUT_MAGIC_OFFSET
            ]
        )
        or any(payload[INDICATOR_LAYOUT_MAGIC_OFFSET + 8 :])
    ):
        return False
    state, sequence, ack = struct.unpack_from("<III", payload)
    if state not in range(5):
        return False
    if state in (1, 2, 3, 4) and (not sequence or sequence & 1):
        return False
    if state == 1 and ack != 0:
        return False
    if state == 2 and ack not in (0, sequence):
        return False
    if state in (3, 4) and ack != sequence:
        return False
    return not (state == 0 and sequence & 1)


class _NativeCoreDispatchTopology:
    """Transactional owner of immutable callback/proof code and mux slot."""

    def __init__(self, process: coop.RemoteProcess, cave_base: int) -> None:
        self.process = process
        self.base = int(cave_base)
        self.cave = self.base + DISPATCH_CAVE_OFFSET
        self.state = self.base + DISPATCH_STATE_OFFSET
        self.proof_cave = self.base + INDICATOR_CAVE_OFFSET
        self.indicator_state = self.base + INDICATOR_STATE_OFFSET
        callback = build_core_tick_callback(
            self.cave,
            self.state,
            self.indicator_state,
            proof_cave=self.proof_cave,
        )
        proof = build_core_identity_proof(self.proof_cave, self.state)
        self.callback_image = callback.ljust(DISPATCH_CAVE_SIZE, b"\x00")
        proof_image = proof.ljust(INDICATOR_CAVE_SIZE, b"\x00")
        self.dynamic_initial_image = _initial_core_dynamic_image(proof_image)
        self.installed = False
        self.owns_static = False
        self.mux = None

    def _read_local(self) -> tuple[bytes, bytes]:
        return (
            self.process.read(self.cave, DISPATCH_CAVE_SIZE),
            self.process.read(
                self.base + CORE_DYNAMIC_REGION_OFFSET,
                CORE_DYNAMIC_REGION_SIZE,
            ),
        )

    def _active_dynamic_valid(self, payload: bytes) -> bool:
        dispatch_offset = DISPATCH_STATE_OFFSET - CORE_DYNAMIC_REGION_OFFSET
        indicator_offset = INDICATOR_STATE_OFFSET - CORE_DYNAMIC_REGION_OFFSET
        proof_offset = INDICATOR_CAVE_OFFSET - CORE_DYNAMIC_REGION_OFFSET
        proof_end = proof_offset + INDICATOR_CAVE_SIZE
        return bool(
            payload[proof_offset:proof_end]
            == self.dynamic_initial_image[proof_offset:proof_end]
            and _valid_dispatch_slot(
                payload[dispatch_offset : dispatch_offset + DISPATCH_STATE_SIZE]
            )
            and _valid_indicator_slot(
                payload[indicator_offset : indicator_offset + DISPATCH_STATE_SIZE]
            )
        )

    def install(self) -> None:
        if self.installed:
            return
        with _CORE_TOPOLOGY_LOCK:
            if self.installed:
                return
            callback_image, dynamic_image = self._read_local()
            zero_callback = b"\x00" * DISPATCH_CAVE_SIZE
            zero_dynamic = b"\x00" * CORE_DYNAMIC_REGION_SIZE
            local_fresh = callback_image == zero_callback and dynamic_image == zero_dynamic
            local_staged = (
                callback_image == self.callback_image and dynamic_image == zero_dynamic
            )
            local_prepared = (
                callback_image == self.callback_image
                and dynamic_image == self.dynamic_initial_image
            )
            local_active = (
                callback_image == self.callback_image
                and self._active_dynamic_valid(dynamic_image)
            )
            if not (local_fresh or local_staged or local_prepared or local_active):
                raise RuntimeError(
                    "partial/foreign native core dispatcher topology found; "
                    "restart the game"
                )

            import native_ui_tick_mux

            mux = native_ui_tick_mux.ensure_installed(self.process)
            handler = mux.handler(native_ui_tick_mux.SLOT_HEADLESS_CORE)
            if handler not in (0, self.cave):
                raise RuntimeError(
                    "native UI-tick headless-core slot is owned by another handler"
                )
            if handler == self.cave:
                if not local_active:
                    raise RuntimeError(
                        "active native core dispatcher points at an incomplete image"
                    )
                self.mux = mux
                self.installed = True
                self.owns_static = False
                return
            if local_active and not local_prepared:
                raise RuntimeError(
                    "unpublished native core dispatcher contains dynamic state"
                )

            wrote_static = False
            if local_fresh:
                self.process.patch_code(self.cave, self.callback_image)
                wrote_static = True
            if local_fresh or local_staged:
                self.process.patch_code(
                    self.base + CORE_DYNAMIC_REGION_OFFSET,
                    self.dynamic_initial_image,
                )
                wrote_static = True

            if self._read_local() != (
                self.callback_image,
                self.dynamic_initial_image,
            ):
                raise RuntimeError(
                    "native core dispatcher changed before callback publication"
                )
            if mux.handler(native_ui_tick_mux.SLOT_HEADLESS_CORE) != 0:
                raise RuntimeError(
                    "native UI-tick headless-core slot changed before publication"
                )
            mux.register_handler(native_ui_tick_mux.SLOT_HEADLESS_CORE, self.cave)
            self.mux = mux
            self.installed = True
            self.owns_static = wrote_static

    def resident_exact(self) -> bool:
        """Read-only proof that callback, proof, states and mux slot still agree."""

        try:
            callback_image, dynamic_image = self._read_local()
            if callback_image != self.callback_image:
                return False
            if not self._active_dynamic_valid(dynamic_image):
                return False
            if self.mux is None or not self.mux.resident_exact():
                return False
            import native_ui_tick_mux

            return self.mux.handler_registered(
                native_ui_tick_mux.SLOT_HEADLESS_CORE,
                self.cave,
            )
        except (OSError, RuntimeError, ValueError, struct.error):
            return False


class NativeActionSubmitter:
    def __init__(self, process: coop.RemoteProcess, cave_base: int):
        self.process = process
        self.cave = int(cave_base) + DISPATCH_CAVE_OFFSET
        self.state = int(cave_base) + DISPATCH_STATE_OFFSET
        self._topology = _NativeCoreDispatchTopology(process, cave_base)
        self._topology.install()
        self._gambit_sequence = self.process.u32(
            self.state + GAMBIT_DISPATCH_SEQUENCE_OFFSET
        ) & 0xFFFFFFFE
        self._pending_sequence = 0
        self._pending_operation = DISPATCH_OPERATION_NONE
        self.patched = False
        self.quarantined = False
        self.installed = True
        self.owns_static = self._topology.owns_static
        self._dispatch_code = self._topology.callback_image
        self._dispatch_patch = None
        self._lock = _mailbox_lock(process, self.state)
        self._adopt_in_flight()

    def _adopt_in_flight(self) -> None:
        dispatch_state = self.process.u32(self.state)
        if dispatch_state not in (1, 2):
            return
        sequence = self.process.u32(self.state + GAMBIT_DISPATCH_SEQUENCE_OFFSET)
        operation = self.process.u32(self.state + DISPATCH_OPERATION_OFFSET)
        if not sequence or sequence & 1 or operation not in (
            DISPATCH_OPERATION_ACTION,
            DISPATCH_OPERATION_GAMBITS,
        ):
            raise RuntimeError("active native core dispatcher mailbox is invalid")
        self._pending_sequence = sequence
        self._pending_operation = operation
        self.patched = True
        self.quarantined = True

    def maintain(self) -> bool:
        """Poll dynamic ownership; immutable callback/root are never reclaimed."""

        with self._lock:
            if not self._topology.resident_exact():
                self.quarantined = True
                return False
            if not self.patched:
                try:
                    self._adopt_in_flight()
                except (OSError, RuntimeError, ValueError):
                    self.quarantined = True
                    return False
                if not self.patched:
                    return True
            try:
                dispatch_state = self.process.u32(self.state)
                if dispatch_state in (1, 2):
                    self.quarantined = True
                    return False
                if dispatch_state not in (3, 4):
                    self.quarantined = True
                    return False
                sequence, ack, operation = (
                    self.process.u32(self.state + GAMBIT_DISPATCH_SEQUENCE_OFFSET),
                    self.process.u32(self.state + GAMBIT_DISPATCH_ACK_SEQUENCE_OFFSET),
                    self.process.u32(self.state + DISPATCH_OPERATION_OFFSET),
                )
                if (
                    sequence != self._pending_sequence
                    or ack != sequence
                    or operation != self._pending_operation
                ):
                    self.quarantined = True
                    return False
            except (OSError, RuntimeError, ValueError):
                self.quarantined = True
                return False
            self.patched = False
            self.quarantined = False
            self._pending_sequence = 0
            self._pending_operation = DISPATCH_OPERATION_NONE
            return True

    def _next_sequence(self) -> int:
        remote = self.process.u32(self.state + GAMBIT_DISPATCH_SEQUENCE_OFFSET)
        base = max(self._gambit_sequence, remote & 0xFFFFFFFE)
        sequence = (base + 2) & 0xFFFFFFFE
        sequence = sequence or 2
        self._gambit_sequence = sequence
        return sequence

    def _publish(self, payload: bytes, sequence: int, operation: int) -> None:
        if len(payload) != DISPATCH_OPERATION_OFFSET + 4:
            raise ValueError("native core dispatcher payload has unexpected size")
        self.process.write(self.state, payload)
        if self.process.read(self.state, len(payload)) != payload:
            raise RuntimeError("native core dispatcher payload verification failed")
        self._pending_sequence = int(sequence)
        self._pending_operation = int(operation)
        self.patched = True
        try:
            self.process.write(
                self.state + GAMBIT_DISPATCH_STATE_OFFSET,
                struct.pack("<I", 1),
            )
        except BaseException:
            try:
                current = self.process.u32(self.state)
            except OSError:
                current = 2
            if current not in (1, 2, 3, 4):
                self.patched = False
                self._pending_sequence = 0
                self._pending_operation = DISPATCH_OPERATION_NONE
            else:
                self.quarantined = current in (1, 2)
            raise

    def _wait_terminal(self, sequence: int, label: str) -> tuple[int, bytes]:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            dispatch_state = self.process.u32(self.state)
            if dispatch_state in (3, 4):
                payload = self.process.read(
                    self.state,
                    DISPATCH_OPERATION_OFFSET + 4,
                )
                ack = struct.unpack_from(
                    "<I", payload, GAMBIT_DISPATCH_ACK_SEQUENCE_OFFSET
                )[0]
                if ack != sequence:
                    raise RuntimeError(
                        f"native {label} dispatcher returned a stale acknowledgement"
                    )
                return dispatch_state, payload
            time.sleep(0.002)
        raise RuntimeError(
            f"native {label} dispatcher timed out "
            f"(state={self.process.u32(self.state)})"
        )

    def submit(
        self,
        issuer: int,
        target: int,
        action: int,
        *,
        reserve_party_member_id: int | None = None,
    ) -> int:
        issuer = int(issuer)
        target = int(target)
        action = int(action)
        reserve = (
            DISPATCH_RESERVE_MEMBER_NONE
            if reserve_party_member_id is None
            else int(reserve_party_member_id)
        )
        if any(not 0 <= value <= 0xFFFFFFFF for value in (issuer, target, action)):
            raise ValueError("native action values must fit u32")
        if reserve != DISPATCH_RESERVE_MEMBER_NONE and not 0 <= reserve <= 0xFF:
            raise ValueError("reserve party-member id must fit in one byte")

        with self._lock:
            if not self.maintain():
                raise RuntimeError("native UI tick command dispatcher is still in flight")
            sequence = self._next_sequence()
            payload = bytearray(DISPATCH_OPERATION_OFFSET + 4)
            struct.pack_into("<I", payload, GAMBIT_DISPATCH_SEQUENCE_OFFSET, sequence)
            struct.pack_into("<I", payload, ACTION_DISPATCH_ISSUER_OFFSET, issuer)
            struct.pack_into("<I", payload, ACTION_DISPATCH_TARGET_OFFSET, target)
            struct.pack_into("<I", payload, ACTION_DISPATCH_ACTION_OFFSET, action)
            struct.pack_into(
                "<I", payload, ACTION_DISPATCH_RESERVE_MEMBER_OFFSET, reserve
            )
            struct.pack_into(
                "<I", payload, DISPATCH_OPERATION_OFFSET, DISPATCH_OPERATION_ACTION
            )
            self._publish(payload, sequence, DISPATCH_OPERATION_ACTION)
            try:
                dispatch_state, terminal = self._wait_terminal(sequence, "command")
                result = struct.unpack_from(
                    "<i", terminal, GAMBIT_DISPATCH_RESULT_OFFSET
                )[0]
                if dispatch_state == 4:
                    raise RuntimeError(
                        f"native command dispatcher rejected request ({result:#x})"
                    )
                return result
            finally:
                self.maintain()

    def toggle_gambits(
        self,
        *,
        member_id: int,
        handle: int,
        actor: int,
        physics: int,
        battle_keep: int,
        runtime_root: int,
    ) -> bool:
        """Toggle one generation-proven member through the stock native setter."""

        try:
            member_id = int(member_id)
            handle = int(handle)
            actor = int(actor)
            physics = int(physics)
            battle_keep = int(battle_keep)
            runtime_root = int(runtime_root)
        except (TypeError, ValueError) as error:
            raise ValueError("native Gambits identity must contain integers") from error
        if not 0 <= member_id <= 5:
            raise ValueError("native Gambits member id must be a main-party id")
        if not 0 < handle <= 0xFFFFFFFF:
            raise ValueError("native Gambits handle must fit a non-zero u32")
        pointers = (actor, physics, battle_keep, runtime_root)
        if any(not 0 < pointer <= 0xFFFFFFFFFFFFFFFF for pointer in pointers):
            raise ValueError("native Gambits identity pointers must be non-zero u64")
        expected_record = (
            runtime_root
            + PARTY_MEMBER_RUNTIME_BASE_OFFSET
            + member_id * PARTY_MEMBER_RUNTIME_STRIDE
        )
        if battle_keep != expected_record:
            raise ValueError("native Gambits battle-keep/member identity mismatch")

        with self._lock:
            if not self.maintain():
                raise RuntimeError("native UI tick command dispatcher is still in flight")
            sequence = self._next_sequence()
            payload = bytearray(DISPATCH_OPERATION_OFFSET + 4)
            payload[: GAMBIT_DISPATCH_MAILBOX_STRUCT.size] = (
                GAMBIT_DISPATCH_MAILBOX_STRUCT.pack(
                    0,
                    sequence,
                    0,
                    GAMBIT_DISPATCH_RESULT_PENDING,
                    handle,
                    member_id,
                    actor,
                    physics,
                    battle_keep,
                    runtime_root,
                    0,
                    0,
                )
            )
            struct.pack_into(
                "<I", payload, DISPATCH_OPERATION_OFFSET, DISPATCH_OPERATION_GAMBITS
            )
            self._publish(payload, sequence, DISPATCH_OPERATION_GAMBITS)
            try:
                dispatch_state, terminal = self._wait_terminal(sequence, "Gambits")
                record = GAMBIT_DISPATCH_MAILBOX_STRUCT.unpack_from(terminal)
                result = record[3]
                final_enabled = record[10]
                if dispatch_state == 4:
                    raise RuntimeError(
                        f"native Gambits dispatcher rejected identity ({result:#x})"
                    )
                if (
                    result != GAMBIT_DISPATCH_RESULT_APPLIED
                    or final_enabled not in (0, 1)
                ):
                    raise RuntimeError("native Gambits dispatcher returned invalid data")
                return bool(final_enabled)
            finally:
                self.maintain()

    def restore(self) -> bool:
        with self._lock:
            if not self.maintain():
                return False
            try:
                if self.process.u32(self.state) != 0:
                    self.process.write(self.state, b"\x00" * 4)
            except (OSError, RuntimeError, ValueError):
                return False
            self.installed = False
            self.owns_static = False
            return True


def build_indicator_dispatcher(
    cave: int, state: int, source: int, target: int
) -> bytes:
    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", state)
    code += b"\x83\x38\x01"
    code += b"\x0f\x85"
    pass_fixup = len(code)
    code += b"\x00" * 4
    code += b"\xc7\x00\x02\x00\x00\x00"

    code += b"\x51"
    code += b"\x48\x83\xec\x28"
    code += b"\xba" + struct.pack("<I", source)
    code += b"\x41\xb8" + struct.pack("<I", target)
    code += b"\x48\xb8" + struct.pack("<Q", SET_NATIVE_COMBAT_INDICATOR)
    code += b"\xff\xd0"
    code += b"\x49\xbb" + struct.pack("<Q", state)
    code += b"\x49\x89\x43\x08"
    code += b"\x41\xc7\x03\x03\x00\x00\x00"
    code += b"\x48\x83\xc4\x28"
    code += b"\x59"

    passthrough = len(code)
    add_rel32(code, b"\xe8", cave, UI_MANAGER_TICK)
    add_rel32(code, b"\xe9", cave, UI_MANAGER_TICK_RETURN)
    struct.pack_into(
        "<i",
        code,
        pass_fixup,
        cave + passthrough - (cave + pass_fixup + 4),
    )
    return bytes(code)


class NativeCombatIndicatorRouter:
    def __init__(self, process: coop.RemoteProcess, cave_base: int):
        self.process = process
        self.cave = int(cave_base) + DISPATCH_CAVE_OFFSET
        self.state = int(cave_base) + INDICATOR_STATE_OFFSET
        self._topology = _NativeCoreDispatchTopology(process, cave_base)
        self._topology.install()
        self._sequence = self.process.u32(
            self.state + INDICATOR_DISPATCH_SEQUENCE_OFFSET
        ) & 0xFFFFFFFE
        self._pending_sequence = 0
        self.patched = False
        self.quarantined = False
        self.installed = True
        self.owns_static = self._topology.owns_static
        self._dispatch_code = self._topology.callback_image
        self._dispatch_patch = None
        self._lock = _mailbox_lock(process, self.state)
        self._adopt_in_flight()

    def _adopt_in_flight(self) -> None:
        dispatch_state = self.process.u32(self.state)
        if dispatch_state not in (1, 2):
            return
        sequence = self.process.u32(
            self.state + INDICATOR_DISPATCH_SEQUENCE_OFFSET
        )
        if not sequence or sequence & 1:
            raise RuntimeError("active native combat-indicator mailbox is invalid")
        self._pending_sequence = sequence
        self.patched = True
        self.quarantined = True

    def maintain(self) -> bool:
        with self._lock:
            if not self._topology.resident_exact():
                self.quarantined = True
                return False
            if not self.patched:
                try:
                    self._adopt_in_flight()
                except (OSError, RuntimeError, ValueError):
                    self.quarantined = True
                    return False
                if not self.patched:
                    return True
            try:
                dispatch_state = self.process.u32(self.state)
                if dispatch_state in (1, 2):
                    self.quarantined = True
                    return False
                if dispatch_state not in (3, 4):
                    self.quarantined = True
                    return False
                sequence = self.process.u32(
                    self.state + INDICATOR_DISPATCH_SEQUENCE_OFFSET
                )
                ack = self.process.u32(
                    self.state + INDICATOR_DISPATCH_ACK_SEQUENCE_OFFSET
                )
                if sequence != self._pending_sequence or ack != sequence:
                    self.quarantined = True
                    return False
            except (OSError, RuntimeError, ValueError):
                self.quarantined = True
                return False
            self._pending_sequence = 0
            self.patched = False
            self.quarantined = False
            return True

    def _next_sequence(self) -> int:
        remote = self.process.u32(
            self.state + INDICATOR_DISPATCH_SEQUENCE_OFFSET
        )
        sequence = (max(self._sequence, remote & 0xFFFFFFFE) + 2) & 0xFFFFFFFE
        sequence = sequence or 2
        self._sequence = sequence
        return sequence

    def set(self, source: int, target: int) -> int:
        source = int(source) & 0xFFFFFFFF
        target = int(target) & 0xFFFFFFFF
        with self._lock:
            if not self.maintain():
                raise RuntimeError(
                    "native combat-indicator dispatcher is still in flight"
                )
            sequence = self._next_sequence()
            payload = INDICATOR_DISPATCH_MAILBOX_STRUCT.pack(
                0,
                sequence,
                0,
                0,
                0,
                source,
                target,
            )
            self.process.write(self.state, payload)
            if self.process.read(self.state, len(payload)) != payload:
                raise RuntimeError("native combat-indicator payload verification failed")
            self._pending_sequence = sequence
            self.patched = True
            try:
                self.process.write(
                    self.state + INDICATOR_DISPATCH_STATE_OFFSET,
                    struct.pack("<I", 1),
                )
            except BaseException:
                try:
                    current = self.process.u32(self.state)
                except OSError:
                    current = 2
                if current not in (1, 2, 3, 4):
                    self.patched = False
                    self._pending_sequence = 0
                else:
                    self.quarantined = current in (1, 2)
                raise
            try:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    dispatch_state = self.process.u32(self.state)
                    if dispatch_state in (3, 4):
                        record = INDICATOR_DISPATCH_MAILBOX_STRUCT.unpack(
                            self.process.read(
                                self.state,
                                INDICATOR_DISPATCH_MAILBOX_STRUCT.size,
                            )
                        )
                        if record[2] != sequence:
                            raise RuntimeError(
                                "native combat-indicator returned a stale acknowledgement"
                            )
                        if dispatch_state == 4:
                            raise RuntimeError(
                                "native combat-indicator rejected its mailbox"
                            )
                        return int(record[4])
                    time.sleep(0.002)
                raise RuntimeError(
                    "native indicator dispatcher timed out "
                    f"(state={self.process.u32(self.state)})"
                )
            finally:
                self.maintain()

    def restore(self) -> bool:
        with self._lock:
            if not self.maintain():
                return False
            try:
                if self.process.u32(self.state) != 0:
                    self.process.write(self.state, b"\x00" * 4)
            except (OSError, RuntimeError, ValueError):
                return False
            self.installed = False
            self.owns_static = False
            return True


def recover_orphaned_ui_tick_dispatcher(
    process: coop.RemoteProcess,
    cave_base: int,
) -> str | None:
    current = process.read(UI_MANAGER_TICK_CALL, len(ORIGINAL_TICK_CALL))
    if current == ORIGINAL_TICK_CALL or len(current) != 5 or current[0] != 0xE9:
        return None
    target = UI_MANAGER_TICK_CALL + len(current) + struct.unpack("<i", current[1:])[0]

    dispatchers: dict[int, tuple[int, str]] = {
        cave_base + DISPATCH_CAVE_OFFSET: (
            cave_base + DISPATCH_STATE_OFFSET,
            "action",
        ),
        cave_base + INDICATOR_CAVE_OFFSET: (
            cave_base + INDICATOR_STATE_OFFSET,
            "combat-indicator",
        ),
    }

    # These constants identify the retired one-shot target-circle image, not
    # the current process-lifetime router.  Keep them literal: the new mux
    # deliberately reuses legacy +0x100 as its permanent root cave, under a
    # different magic, and importing current layout constants would make hot
    # upgrade recovery silently forget the old in-flight dispatcher.
    legacy_circle_signatures = (
        (b"FF12NCR1", "target-circle"),
        (b"FF12NAO1", "area-circle"),
    )
    legacy_circle_code_offset = 0x100
    legacy_circle_state_offset = 0x800
    standalone_region = target - legacy_circle_code_offset
    try:
        standalone_magic = process.read(standalone_region, 8)
    except OSError:
        standalone_magic = b""
    for legacy_circle_magic, label in legacy_circle_signatures:
        if standalone_magic == legacy_circle_magic:
            dispatchers[target] = (
                standalone_region + legacy_circle_state_offset,
                label,
            )
            break

    expected = dispatchers.get(target)
    if expected is None:
        return None
    state, label = expected
    signature = process.read(target, 10)
    if (
        len(signature) != 10
        or signature[:2] != b"\x48\xb8"
        or struct.unpack("<Q", signature[2:])[0] != state
    ):
        return None
    dispatcher_state = process.u32(state)
    if dispatcher_state not in (1, 2, 3, 4):
        return None

    # Legacy transient dispatchers are never repaired or unpublished in a live
    # process.  Even terminal state is published before the game thread has
    # restored its frame and left the cave, so it is not a code-lifetime fence.
    if dispatcher_state in (1, 2):
        return f"{label}:in-flight"
    raise RuntimeError(
        f"legacy {label} UI-tick dispatcher remains process-resident; "
        "restart the game"
    )


def build_sound_pump(cave: int, state: int) -> bytes:
    code = bytearray()

    # This detour is transparent to the UI manager entry.  Preserve every
    # volatile integer register touched by the sound bridge.
    code += b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
    code += b"\x49\xbb" + struct.pack("<Q", state)
    code += b"\x33\xc9"
    code += b"\x41\x87\x4b\x04"
    code += b"\x85\xc9"
    has_priority_sound = len(code)
    code += b"\x75\x00"
    code += b"\x41\x87\x0b"
    code += b"\x85\xc9"
    no_sound = len(code)
    code += b"\x74\x00"
    play_sound = len(code)
    code += b"\x48\x83\xec\x20"
    code += b"\x48\xb8" + struct.pack("<Q", PLAY_UI_SOUND)
    code += b"\xff\xd0"
    code += b"\x48\x83\xc4\x20"
    restore = len(code)
    code[has_priority_sound + 1] = (play_sound - (has_priority_sound + 2)) & 0xFF
    code[no_sound + 1] = (restore - (no_sound + 2)) & 0xFF
    code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x58"
    code += SOUND_PUMP_ORIGINAL
    code += b"\xe9" + coop.rel32(cave + len(code), 5, SOUND_PUMP_RETURN)
    return bytes(code)


class NativeUISoundPlayer:
    # 0x2C is the native proximity-marker sound.
    VALID_IDS = frozenset((1, 2, 3, 4, 5, 0x0A, 0x12, 0x13, 0x27, 0x2C))

    def __init__(self, process: coop.RemoteProcess, cave_base: int):
        self.process = process
        self.cave = cave_base + SOUND_CAVE_OFFSET
        self.state = cave_base + SOUND_STATE_OFFSET
        self.patch = (
            b"\xe9"
            + coop.rel32(SOUND_PUMP_HOOK, 5, self.cave)
            + b"\x90" * (len(SOUND_PUMP_ORIGINAL) - 5)
        )
        current = self.process.read(SOUND_PUMP_HOOK, len(SOUND_PUMP_ORIGINAL))
        if current not in (SOUND_PUMP_ORIGINAL, self.patch):
            raise RuntimeError(
                "unexpected actor-update bytes for sound pump: " + current.hex(" ")
            )
        cave = build_sound_pump(self.cave, self.state)
        if len(cave) > SOUND_CAVE_SIZE:
            raise RuntimeError("native UI sound cave exceeds its reserved slot")
        cave_image = cave + b"\x00" * (SOUND_CAVE_SIZE - len(cave))
        current_cave = self.process.read(self.cave, SOUND_CAVE_SIZE)
        self.owns_static = False
        if current == self.patch:
            if current_cave != cave_image:
                raise RuntimeError("active native UI sound cave mismatch")
            self.patched = True
            return
        if current_cave != b"\x00" * SOUND_CAVE_SIZE:
            raise RuntimeError(
                "non-empty native UI sound cave with a stock root; restart the game"
            )
        self.process.write(self.state, b"\x00" * 0x10)
        self.process.write(self.cave, cave_image)
        if current == SOUND_PUMP_ORIGINAL:
            self.process.patch_code(SOUND_PUMP_HOOK, self.patch)
        self.patched = True
        self.owns_static = True

    def play(self, sound_id: int) -> bool:
        if sound_id not in self.VALID_IDS:
            raise ValueError(f"unsupported native UI sound id {sound_id}")
        if self.process.u32(self.state):
            return False
        self.process.write(self.state, struct.pack("<I", sound_id))
        return True

    def restore(self) -> None:
        if not self.patched:
            return
        # Zero mailboxes turn the resident pump into an exact pass-through.
        # Never unpublish its root: another thread may already be inside the
        # cave, and a replacement helper must adopt the same bytes read-only.
        self.process.write(self.state, b"\x00" * 0x10)
        self.patched = False
        self.owns_static = False


def decode_ff12_text(process: coop.RemoteProcess, address: int, limit: int = 96) -> str:
    if not address:
        return ""
    try:
        encoded = process.read(address, limit)
    except OSError:
        return ""
    result = []
    index = 0
    while index < len(encoded):
        value = encoded[index]
        if value == 0:
            break
        if value == 0x0F:
            index += 4
            continue
        mapped = BYTE_TO_TEXT.get(value)
        if mapped is None:
            result.append("?")
        elif mapped in ("\n", "\f"):
            result.append(" ")
        else:
            result.append(mapped)
        index += 1
    return "".join(result).strip()


def bkei_allows_native_target(flags: int) -> bool:
    return not (int(flags) & BKEI_TARGET_BLOCK_MASK)


def live_target_candidates(process, party, *, active_actors=None):
    party_actors = {entry[2] for entry in party}
    resolved = (
        active_actors
        if active_actors is not None
        else coop.resolve_active_actors(process)
    )
    candidates = []
    for actor_id, actor, _phys, position in resolved:
        if actor in party_actors:
            continue
        try:
            actor_state = process.u64(actor)
            if not (actor_state & ACTOR_ACTIVE_FLAG) or actor_state & ACTOR_HIDDEN_FLAG:
                continue
            bwei = process.u64(actor + 0x10)
            if not bwei:
                continue
            unit_type = process.read(bwei + 0x0E, 1)[0] & 0x0F
            if unit_type not in ATTACKABLE_UNIT_TYPES and unit_type != 3:
                continue
            battle_keep = process.u64(actor + 0x698)
            battle_keep_extended = process.u64(actor + 0x6A0)
            if not battle_keep or not battle_keep_extended:
                continue
            if not bkei_allows_native_target(process.read(battle_keep_extended, 1)[0]):
                continue
            if process.u32(battle_keep + 0x48) == 0:
                continue
            if combined_status_effects(process, battle_keep) & coop.KO_STATUS_MASK:
                continue
            handle = process.u32(actor + 0x08)
            if not handle:
                continue
            name_address = process.u64(bwei + 0xF8)
            fallback = "Ally" if unit_type == 3 else "Foe"
            name = decode_ff12_text(process, name_address) or fallback
            native_instance = process.u32(bwei + 0x100) & 0xFFFF
            candidates.append(
                (
                    actor_id,
                    actor,
                    position,
                    unit_type,
                    name,
                    handle,
                    name_address,
                    native_instance,
                )
            )
        except (OSError, struct.error):
            continue

    instances = {}
    for actor_id, *_rest, name_address, _native_instance in candidates:
        if name_address:
            instances.setdefault(name_address, []).append(actor_id)

    result = []
    for (
        actor_id,
        actor,
        position,
        unit_type,
        name,
        handle,
        name_address,
        native_instance,
    ) in candidates:
        if 1 <= native_instance <= 26:
            name = f"{name} {chr(ord('A') + native_instance - 1)}"
        elif native_instance > 26 and native_instance != 0xFFFF:
            name = f"{name} {native_instance}"
        else:
            siblings = instances.get(name_address, ())
            if len(siblings) > 1:
                ordinal = siblings.index(actor_id)
                suffix = chr(ord("A") + ordinal) if ordinal < 26 else ordinal + 1
                name = f"{name} {suffix}"
        result.append((actor, position, unit_type, name, handle))
    return result


def targets_for_issuer(process, candidates, issuer_actor, unit_types):
    if not issuer_actor:
        return []
    try:
        issuer_position = struct.unpack("<3f", process.read(issuer_actor + 0xD0, 12))
    except (OSError, struct.error):
        return []
    result = []
    for actor, position, unit_type, name, handle in candidates:
        if unit_type not in unit_types:
            continue
        distance = math.hypot(
            position[0] - issuer_position[0],
            position[2] - issuer_position[2],
        )
        if distance <= ATTACK_TARGET_HORIZONTAL_RANGE:
            result.append((distance, name, handle, actor))
    result.sort(key=lambda entry: entry[0])
    return [(name, handle, actor) for _distance, name, handle, actor in result]


def live_foes(
    process: coop.RemoteProcess,
    party,
    *,
    issuer_actor: int | None = None,
    active_actors=None,
    candidates=None,
):
    if issuer_actor is None and len(party) >= 2:
        issuer_actor = party[1][2]
    if candidates is None:
        candidates = live_target_candidates(process, party, active_actors=active_actors)
    return targets_for_issuer(process, candidates, issuer_actor, ATTACKABLE_UNIT_TYPES)


def live_neutrals(
    process: coop.RemoteProcess,
    party,
    *,
    issuer_actor: int | None = None,
    active_actors=None,
    candidates=None,
):
    if issuer_actor is None and party:
        issuer_actor = party[0][2]
    if candidates is None:
        candidates = live_target_candidates(process, party, active_actors=active_actors)
    return targets_for_issuer(process, candidates, issuer_actor, (3,))


class Keys:
    INITIAL_REPEAT_DELAY = 0.250
    REPEAT_INTERVAL = 0.100

    def __init__(self):
        self.previous = {}
        self.next_repeat = {}
        for key in (
            VK_SPACE,
            VK_BACK,
            VK_NUMPAD0,
            VK_I,
            VK_J,
            VK_K,
            VK_L,
            VK_LEFT,
            VK_UP,
            VK_RIGHT,
            VK_DOWN,
            VK_Q,
            VK_E,
        ):
            self.previous[key] = coop.key_down(key)

    def edge(self, key: int, enabled: bool = True) -> bool:
        current = enabled and coop.key_down(key)
        prior = self.previous.get(key, False)
        self.previous[key] = current
        return current and not prior

    def repeat(self, key: int, enabled: bool = True) -> bool:
        now = time.monotonic()
        current = enabled and coop.key_down(key)
        prior = self.previous.get(key, False)
        self.previous[key] = current
        if current and not prior:
            self.next_repeat[key] = now + self.INITIAL_REPEAT_DELAY
            return True
        if current and now >= self.next_repeat.get(key, math.inf):
            self.next_repeat[key] = now + self.REPEAT_INTERVAL
            return True
        if not current:
            self.next_repeat.pop(key, None)
        return False


class LayeredPanel:
    def __init__(self, pid: int, tag: str = "menu"):
        self.tag = tag
        self.game = ui.game_window(pid)
        self.visible = False
        self.d3d_visible = False
        self.d3d_transport = d3d_panel_transport.panel_transport(pid, tag)
        self.width = 0
        self.height = 0
        self.bits = None
        self.bitmap = None
        self.old_bitmap = None
        self.hwnd = None
        self.screen_dc = None
        self.memory_dc = None
        self._class_registered = False
        self._last_draw_key = None
        self._last_draw_image = None
        self._last_d3d_draw_key = None
        self._last_d3d_mapping_epoch = None
        self._last_d3d_touch = 0.0
        self._foreground_band = True
        self.wndproc = ui.WNDPROC(
            lambda hwnd, message, wparam, lparam: ui.user32.DefWindowProcW(
                hwnd, message, wparam, lparam
            )
        )
        self.instance = ui.kernel32.GetModuleHandleW(None)
        self.class_name = f"FF12HeadlessP2Menu_{tag}_{pid}"
        window_class = ui.WNDCLASSW()
        window_class.lpfnWndProc = ct.cast(self.wndproc, ct.c_void_p).value
        window_class.hInstance = self.instance
        window_class.lpszClassName = self.class_name
        if not ui.user32.RegisterClassW(ct.byref(window_class)):
            raise OSError(ct.get_last_error(), "RegisterClassW")
        self._class_registered = True
        _RETIRED_WINDOW_PROCEDURES.pop((int(self.instance or 0), self.class_name), None)
        styles = (
            ui.WS_EX_LAYERED
            | ui.WS_EX_TRANSPARENT
            | ui.WS_EX_TOOLWINDOW
            | ui.WS_EX_TOPMOST
            | ui.WS_EX_NOACTIVATE
        )
        self.hwnd = ui.user32.CreateWindowExW(
            styles,
            self.class_name,
            "",
            ui.WS_POPUP,
            0,
            0,
            1,
            1,
            None,
            None,
            self.instance,
            None,
        )
        if not self.hwnd:
            error = ct.get_last_error()
            if ui.user32.UnregisterClassW(self.class_name, self.instance):
                self._class_registered = False
                self.wndproc = None
            else:
                # Win32 retains the callback address for a registered class.
                # If rollback cannot unregister it, keep the ctypes trampoline
                # alive for the remainder of the helper process.
                _RETIRED_WINDOW_PROCEDURES[
                    (int(self.instance or 0), self.class_name)
                ] = self.wndproc
            raise OSError(error, "CreateWindowExW")
        self.screen_dc = ui.user32.GetDC(None)
        if not self.screen_dc:
            error = ct.get_last_error()
            self.close()
            raise OSError(error, "GetDC")
        self.memory_dc = ui.gdi32.CreateCompatibleDC(self.screen_dc)
        if not self.memory_dc:
            error = ct.get_last_error()
            self.close()
            raise OSError(error, "CreateCompatibleDC")
        self.source = wt.POINT(0, 0)
        self.blend = ui.BLENDFUNCTION(ui.AC_SRC_OVER, 0, 255, ui.AC_SRC_ALPHA)

    def game_can_present(self) -> bool:
        return bool(ui.user32.IsWindow(self.game) and not ui.user32.IsIconic(self.game))

    def set_game_foreground(self, foreground: bool) -> None:
        foreground = bool(foreground)
        if foreground == self._foreground_band:
            return
        insert_after = HWND_TOPMOST if foreground else HWND_NOTOPMOST
        if not ui.user32.SetWindowPos(
            self.hwnd,
            insert_after,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        ):
            raise OSError(ct.get_last_error(), "SetWindowPos")
        self._foreground_band = foreground

    def resize(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height):
            return
        info = ui.BITMAPINFO()
        info.bmiHeader.biSize = ct.sizeof(ui.BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = ui.BI_RGB
        bits = ct.c_void_p()
        bitmap = ui.gdi32.CreateDIBSection(
            self.screen_dc, ct.byref(info), ui.DIB_RGB_COLORS, ct.byref(bits), None, 0
        )
        if not bitmap or not bits:
            if bitmap:
                ui.gdi32.DeleteObject(bitmap)
            raise OSError(ct.get_last_error(), "CreateDIBSection")
        previous_bitmap = ui.gdi32.SelectObject(self.memory_dc, bitmap)
        if not previous_bitmap or int(previous_bitmap) == ct.c_void_p(-1).value:
            error = ct.get_last_error()
            ui.gdi32.DeleteObject(bitmap)
            raise OSError(error, "SelectObject")
        if self.bitmap:
            if not ui.gdi32.DeleteObject(self.bitmap):
                error = ct.get_last_error()
                ui.gdi32.SelectObject(self.memory_dc, previous_bitmap)
                ui.gdi32.DeleteObject(bitmap)
                raise OSError(error, "DeleteObject")
        else:
            self.old_bitmap = previous_bitmap
        self.bitmap = bitmap
        self.bits = bits
        self.width, self.height = width, height
        self.size = ui.SIZE(width, height)

    def _invalidate_d3d_transport(self) -> None:
        if self.d3d_transport is None:
            return
        try:
            self.d3d_transport.invalidate()
        except (
            AttributeError,
            BufferError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            struct.error,
        ):
            pass

    def _hide_d3d_slot(self) -> None:
        if self.d3d_transport is not None:
            try:
                # A publish can commit a visible slot and still return False
                # when the renderer heartbeat expires.  Always revoke that
                # pending slot so a later renderer reconnect cannot resurrect
                # a panel which the UI has already hidden or painted via GDI.
                self.d3d_transport.hide(self.tag)
            except (
                AttributeError,
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                self._invalidate_d3d_transport()
        self.d3d_visible = False
        self._last_d3d_draw_key = None
        self._last_d3d_mapping_epoch = None

    def hide(self) -> None:
        self._hide_d3d_slot()
        if self.visible:
            ui.user32.ShowWindow(self.hwnd, ui.SW_HIDE)
            self.visible = False

    def draw(
        self, image: Image.Image, logical_x: float, logical_y: float, opacity=1.0
    ) -> None:
        if self.d3d_transport is not None:
            now = time.monotonic()
            d3d_draw_key = (
                id(image),
                image.width,
                image.height,
                round(float(logical_x), 4),
                round(float(logical_y), 4),
                round(float(opacity), 5),
            )
            if (
                self.d3d_visible
                and self.d3d_transport.enabled
                and getattr(self.d3d_transport, "mapping_epoch", None)
                == self._last_d3d_mapping_epoch
                and d3d_draw_key == self._last_d3d_draw_key
                and now - self._last_d3d_touch < 0.5
            ):
                return
            self._last_d3d_touch = now
            try:
                d3d_published = self.d3d_transport.publish(
                    self.tag, image, logical_x, logical_y, opacity
                )
            except (
                AttributeError,
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                self._invalidate_d3d_transport()
                d3d_published = False
            if d3d_published:
                self._last_d3d_draw_key = d3d_draw_key
                self._last_d3d_mapping_epoch = getattr(
                    self.d3d_transport,
                    "mapping_epoch",
                    None,
                )
                if self.visible:
                    ui.user32.ShowWindow(self.hwnd, ui.SW_HIDE)
                    self.visible = False
                self.d3d_visible = True
                return
            self._hide_d3d_slot()
        source_image = image
        client = wt.RECT()
        origin = wt.POINT(0, 0)
        if not ui.user32.GetClientRect(self.game, ct.byref(client)):
            raise RuntimeError("game client no longer exists")
        if not ui.user32.ClientToScreen(self.game, ct.byref(origin)):
            raise RuntimeError("cannot locate game client")
        client_width = client.right - client.left
        client_height = client.bottom - client.top
        scale_x = client_width / REFERENCE_WIDTH
        scale_y = client_height / REFERENCE_HEIGHT
        width = max(1, round(image.width * scale_x))
        height = max(1, round(image.height * scale_y))
        destination = wt.POINT(
            round(origin.x + logical_x * scale_x),
            round(origin.y + logical_y * scale_y),
        )
        draw_key = (
            id(image),
            width,
            height,
            destination.x,
            destination.y,
            round(float(opacity), 5),
        )
        if (
            self.visible
            and source_image is self._last_draw_image
            and draw_key == self._last_draw_key
        ):
            return
        if (width, height) != image.size:
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
        if opacity < 0.999:
            rgba[..., 3] = np.clip(
                rgba[..., 3].astype(np.float32) * opacity, 0, 255
            ).astype(np.uint8)
        alpha = rgba[..., 3:4].astype(np.uint16)
        premul = (rgba[..., :3].astype(np.uint16) * alpha // 255).astype(np.uint8)
        bgra = np.empty_like(rgba)
        bgra[..., 0] = premul[..., 2]
        bgra[..., 1] = premul[..., 1]
        bgra[..., 2] = premul[..., 0]
        bgra[..., 3] = rgba[..., 3]
        self.resize(width, height)
        ct.memmove(self.bits, bgra.tobytes(), bgra.nbytes)
        if not ui.user32.UpdateLayeredWindow(
            self.hwnd,
            None,
            ct.byref(destination),
            ct.byref(self.size),
            self.memory_dc,
            ct.byref(self.source),
            0,
            ct.byref(self.blend),
            ui.ULW_ALPHA,
        ):
            raise OSError(ct.get_last_error(), "UpdateLayeredWindow")
        if not self.visible:
            ui.user32.ShowWindow(self.hwnd, ui.SW_SHOWNOACTIVATE)
            self.visible = True
        self._last_draw_key = draw_key
        self._last_draw_image = source_image

    def close(self) -> None:
        self.hide()
        if self.old_bitmap:
            ui.gdi32.SelectObject(self.memory_dc, self.old_bitmap)
            self.old_bitmap = None
        if self.bitmap:
            ui.gdi32.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.memory_dc:
            ui.gdi32.DeleteDC(self.memory_dc)
            self.memory_dc = None
        if self.screen_dc:
            ui.user32.ReleaseDC(None, self.screen_dc)
            self.screen_dc = None
        if self.hwnd:
            if ui.user32.DestroyWindow(self.hwnd):
                self.hwnd = None
            else:
                _RETIRED_WINDOW_PROCEDURES[
                    (int(self.instance or 0), self.class_name)
                ] = self.wndproc
                return
        if self._class_registered and ui.user32.UnregisterClassW(
            self.class_name, self.instance
        ):
            self._class_registered = False
            self.wndproc = None
        elif self._class_registered:
            _RETIRED_WINDOW_PROCEDURES[(int(self.instance or 0), self.class_name)] = (
                self.wndproc
            )
