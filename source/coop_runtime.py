"""Run P2/P3 movement, camera, and interaction inside the game process.

This worker remains attached to FF12. It reads input, resolves each player's
actor, publishes polar commands to shared memory, and maintains the native code
caves that consume them. It also owns the shared camera, keyboard and device
filters, interaction caves, and the identity exposed to companion processes.

The module name is historical: keyboard movement is only one input source.
"""

import ctypes as ct
import hashlib
import math
import os
import struct
import sys
import time
from ctypes import wintypes as wt
from dataclasses import dataclass, replace
from pathlib import Path

import area_transition
import camera_frame_sync
import controller_bindings as control_bindings
import follow_handoff
import game_process
import helper_lease
import menu_themes
import movement_mailbox
import party_hud_layout
import runtime_lifecycle
import runtime_paths
import runtime_shutdown
import session_state
from shared_camera import (
    InternalCollisionLatchGuard,
    SharedCameraController,
    formation_corridor_radius,
)

# Compatibility exports keep the established runtime API while policy lives in
# a process-independent module.
advance_camera_reacquire_gate = runtime_lifecycle.advance_camera_reacquire_gate
berserk_leader_owns_movement = runtime_lifecycle.berserk_leader_owns_movement
camera_party_positions_ready = runtime_lifecycle.camera_party_positions_ready
camera_session_gate = runtime_lifecycle.camera_session_gate
coop_lifecycle_flags = runtime_lifecycle.coop_lifecycle_flags
coop_movement_control_flags = runtime_lifecycle.coop_movement_control_flags
follower_interaction_routing_active = (
    runtime_lifecycle.follower_interaction_routing_active
)
follower_movement_master_enabled = runtime_lifecycle.follower_movement_master_enabled
follower_role_owns_movement = runtime_lifecycle.follower_role_owns_movement
follower_session_movement_allowed = runtime_lifecycle.follower_session_movement_allowed
native_desktop_filter_required = runtime_lifecycle.native_desktop_filter_required
fresh_role_actor_mapping = runtime_lifecycle.fresh_role_actor_mapping
p1_session_movement_allowed = runtime_lifecycle.p1_session_movement_allowed
retain_owner_during_mapping_gap = runtime_lifecycle.retain_owner_during_mapping_gap
retain_owner_during_liveness_gap = runtime_lifecycle.retain_owner_during_liveness_gap
role_cache_survives_scan_gap = runtime_lifecycle.role_cache_survives_scan_gap
role_member_changed = runtime_lifecycle.role_member_changed
role_member_layout_coherent = runtime_lifecycle.role_member_layout_coherent

PROCESS_NAME = "FFXII_TZA.exe"

# Volatile process samples can fail because the actor disappeared between two
# reads, because the process transport is reconnecting, or because a short
# native read could not be decoded.  Actor policy must turn all of these into an
# UNKNOWN sample instead of terminating the long-lived runtime worker.
ACTOR_SAMPLE_ERRORS = (
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    OverflowError,
    RuntimeError,
    TypeError,
    ValueError,
    struct.error,
)


ACTOR_MANAGER_GLOBAL = 0x2098E18
MOVEMENT_HOOK_SITE = 0x2E4380
MOVEMENT_HOOK_RETURN = 0x2E4385
KEYBOARD_HOOK_SITE = 0x7A175A
KEYBOARD_HOOK_RETURN = 0x7A1762
KEYBOARD_FINALIZER = 0x7A15A0
KEYBOARD_NATIVE_KEYMAP = 0x1F3DAB0
MEMSET_FUNCTION = 0x861EC0
FACING_HOOK_SITE = 0x236328
FACING_HOOK_RETURN = 0x236330
VISUAL_FACING_CALL_SITE = 0x236323
READ_PHYS_HEADING = 0x263CC0
SET_VISUAL_FACING = 0x2E5E10
PHYS_FACING_STORE_SITE = 0x2E5EBE
FOLLOWER_TURN_CALL_SITE = 0x305C71
FOLLOWER_LIST_EVALUATOR_SITE = 0x305D02
FOLLOWER_LIST_EVALUATOR_RETURN = 0x305D0A
FOLLOWER_LIST_EMPTY_RETURN = 0x305CF7
FOLLOWER_LIST_PRIMARY = 0x01E09350
FOLLOWER_LIST_SECONDARY = 0x01EEC3C0


FOLLOWER_LIST_GUARD_ENABLED = True

# The mod retains rotation control during actions. Releasing this section gives
# part of movement back to the game and makes the character jitter.
FOLLOWER_ACTION_FACING_RELEASE = False


FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE = False


FOLLOWER_LIST_GUARD_INCLUDES_LEADER = False


MOVEMENT_HOLDS_LEADER = False


VISUAL_FACING_HOOK_ENABLED = True


FOLLOWER_LIST_GUARD_REJECTS_EVERY_LIST = False


FOLLOWER_LOCOMOTION_ANIMATION_GUARD_ENABLED = False


FOLLOWER_ACTION_CHASE_GUARD_ENABLED = False


FOLLOWER_ACTION_APPROACH_GATE_ENABLED = True
FOLLOWER_ARBITRATOR_SITE = 0x305AB0
CONTROLLED_HANDLE_GLOBAL = 0x22C7FE0


FOLLOWER_ACTION_CHASE_SITE = 0x23A0EB
FOLLOWER_ACTION_CHASE_RETURN = 0x23A0F3
FOLLOWER_ACTION_CHASE_ORIGINAL = bytes.fromhex("F3 0F 10 8B 58 01 00 00")
INTERACTION_BROADPHASE_FILTER_SITE = 0x25CC74
INTERACTION_BROADPHASE_FILTER_ACCEPT = 0x25CC7D
INTERACTION_BROADPHASE_FILTER_REJECT = 0x25D1DC
INTERACTION_BROADPHASE_FILTER_ORIGINAL = b"\x44\x85\xf8\x0f\x84\x5f\x05\x00\x00"
INTERACTION_ACTOR_FILTER_SITE = 0x25CC8C
INTERACTION_ACTOR_FILTER_ORIGINAL = b"\x48\x3b\x5d\x8f\x0f\x85\x46\x05\x00\x00"
INTERACTION_GEOMETRY_ENTRY_SITE = 0x25CC96
INTERACTION_GEOMETRY_ENTRY_RETURN = 0x25CC9E
INTERACTION_GEOMETRY_ENTRY_ORIGINAL = b"\x48\x8b\xcb\xe8\x02\xda\x08\x00"
INTERACTION_GEOMETRY_FIRST_CHECK = 0x2EA6A0
INTERACTION_UPDATE_ENTRY_SITE = 0x25C830
INTERACTION_UPDATE_ENTRY_RETURN = 0x25C835
INTERACTION_UPDATE_ENTRY_ORIGINAL = b"\x48\x89\x5c\x24\x18"
INTERACTION_SOURCE_SITE = 0x25C898
INTERACTION_SOURCE_RETURN = 0x25C8A3
INTERACTION_SOURCE_ORIGINAL = b"\x48\x8b\x05\xd9\xd4\xe3\x01\x45\x0f\x28\xe3"
NATIVE_INTERACTION_SOURCE_GLOBAL = 0x2099D78
# The trigger reads the volume beneath an entity, resolves the gateway at
# DAT_0209A210 and passes that entity to the area-change opcode. It does not
# iterate the party, so followers require their own pass.
GATEWAY_TRIGGER_NATIVE = 0x25B600
GATEWAY_TRIGGER_SITE = 0x25DED0
GATEWAY_TRIGGER_RETURN = 0x25DED5
GATEWAY_TRIGGER_ORIGINAL = b"\xe8\x2b\xd7\xff\xff"

ENTITY_HANDLE_RESOLVER = 0x3589B0
# Resolve a generation-coded handle through the game's live actor registry.
# Unlike ENTITY_HANDLE_RESOLVER (entry -> handle), this direction never needs
# to trust or dereference a pointer published asynchronously by the helper.
ENTITY_ENTRY_RESOLVER = 0x3588B0
INTERACTION_ENABLE_GATE_SITE = 0x25C928
INTERACTION_ENABLE_GATE_ACCEPT = 0x25C930
INTERACTION_ENABLE_GATE_REJECT = 0x25D219
INTERACTION_ENABLE_GATE_ORIGINAL = b"\x85\xdb\x0f\x84\xe9\x08\x00\x00"
INTERACTION_INNER_RANGE_GATE_SITE = 0x25D044
INTERACTION_INNER_RANGE_GATE_ACCEPT = 0x25D04A
INTERACTION_INNER_RANGE_GATE_REJECT = 0x25D00A
INTERACTION_INNER_RANGE_GATE_ORIGINAL = b"\xf6\x46\x5d\x04\x74\xc0"
INTERACTION_OUTER_RANGE_GATE_SITE = 0x25D10D
INTERACTION_OUTER_RANGE_GATE_ACCEPT = 0x25D117
INTERACTION_OUTER_RANGE_GATE_REJECT = 0x25D00A
INTERACTION_OUTER_RANGE_GATE_ORIGINAL = b"\xf6\x46\x5d\x02\x0f\x84\xf3\xfe\xff\xff"
INTERACTION_GEOMETRY_RESULT_SITE = 0x25CF6E
INTERACTION_GEOMETRY_RESULT_RETURN = 0x25CF75
INTERACTION_GEOMETRY_RESULT_ORIGINAL = b"\x48\x8b\x4d\x8f\x48\x3b\xd9"


INTERACTION_PROXIMITY_SOURCE_SITE = 0x25C27C
INTERACTION_PROXIMITY_SOURCE_RETURN = 0x25C283
INTERACTION_PROXIMITY_SOURCE_ORIGINAL = b"\x48\x8b\x35\xf5\xda\xe3\x01"

INTERACTION_INDICATOR_SOURCE_SITE = 0x25B849
INTERACTION_INDICATOR_SOURCE_RETURN = 0x25B850
INTERACTION_INDICATOR_SOURCE_ORIGINAL = b"\x4c\x8b\x35\x28\xe5\xe3\x01"


INTERACTION_DUAL_SCAN_CALL_SITE = 0x25DEB6
INTERACTION_DUAL_SCAN_CALL_ORIGINAL = b"\xe8\xb5\xe2\xff\xff"
INTERACTION_NATIVE_SCAN = 0x25C170


INTERACTION_NATIVE_INDICATOR_SCAN = 0x25B820
INTERACTION_CLEAR_CAN_USE = 0x25D640
INTERACTION_CLEAR_DESCRIPTOR = 0x25D650
INTERACTION_BUILD_EXACT_HANDLE = 0x268650
INTERACTION_PUBLISHER_SITE = 0x3DF760
INTERACTION_PUBLISHER_ORIGINAL = b"\x48\x8b\x44\x24\x28"
INTERACTION_PROXIMITY_FILTER_SITE = 0x25C3E7
INTERACTION_PROXIMITY_FILTER_ACCEPT = 0x25C3F0
INTERACTION_PROXIMITY_FILTER_REJECT = 0x25C6B9
INTERACTION_PROXIMITY_FILTER_ORIGINAL = b"\x48\x3b\xde\x0f\x85\xc9\x02\x00\x00"
INTERACTION_SECONDARY_LEADER_FILTER_SITE = 0x25C4FE
INTERACTION_SECONDARY_LEADER_FILTER_ACCEPT = 0x25C509
INTERACTION_SECONDARY_LEADER_FILTER_REJECT = 0x25C6A1
INTERACTION_SECONDARY_LEADER_FILTER_ORIGINAL = (
    b"\x48\x3b\x5c\x24\x48\x0f\x85\x98\x01\x00\x00"
)
STEERING_HEADING_STORE_SITE = 0x2E4797
STEERING_HEADING_STORE_RETURN = 0x2E479F
CAMERA_HOOK_SITE = 0x382B11
CAMERA_HOOK_RETURN = 0x382B18
CAMERA_MASTER_STATE = 0x2AED630
CAMERA_DISTANCE_MUL1_SITE = 0x32CD65
CAMERA_DISTANCE_MUL2_SITE = 0x32CD79
CAMERA_RIG_BUILD1_RETURN = 0x32CD74
CAMERA_RIG_BUILD2_RETURN = 0x32CD98
CAMERA_RIG_BUILD1_ORIGINAL = bytes.fromhex(
    "F3 0F 59 5E 38 F3 0F 10 56 34 F3 0F 10 4E 30"
)
CAMERA_RIG_BUILD2_ORIGINAL = bytes.fromhex(
    "F3 0F 59 76 38 48 8D 7E 10 48 8D 4E 20 "
    "48 89 7C 24 20 F3 0F 10 56 34 F3 0F 10 4E 30 0F 28 DE"
)
CAMERA_PRIMARY_RIG = 0x2098060


CAMERA_BACK_ON_WALL_MASK = 0x04000000


CAMERA_WALL_BIT_RELEASED_BY_RESCUE = False


CAMERA_DISTANCE_YIELDS_TO_WALL = True


CAMERA_CONTROLLER_FOCUS_HOOK_SITE = 0x32B265
CAMERA_CONTROLLER_FOCUS_HOOK_RETURN = 0x32B26A
CAMERA_CONTROLLER_FOCUS_ORIGINAL = bytes.fromhex("F3 0F 11 47 64")


CAMERA_COLLISION_QUERY = 0x230C00
CAMERA_COLLISION_CALL_SITES = (0x32BE12, 0x32BF38, 0x32C047, 0x32CC10)
CAMERA_FOCUS_V2_HOOK_SITE = 0x32CD5B
CAMERA_FOCUS_V2_ORIGINAL = bytes.fromhex("48 8B D9 48 8D 0D FB 06 BC 01")
SKELETON_MAIN_BASE_GLOBAL = 0x22C6F28
GAUGE_COLOR_HOOK_SITE = 0x2C5F90
GAUGE_COLOR_SIDE_PATH = 0x2C60CD
GAUGE_COLOR_NONZERO_PATH = 0x2C5F9B
HUD_TEXT_COLOR_HOOK_SITE = 0x2C872A
HUD_TEXT_COLOR_ORIGINAL = bytes.fromhex(
    "C7 46 40 80 00 00 00 C7 46 44 80 00 00 00 C7 46 48 80 00 00 00"
)
SIDE_GAUGE_PALETTE = 0x1E0CEF8
OTHER_GAUGE_PALETTE = 0x1E0CF18
FOCUS_CLASSIFY_SITE = 0x7994C6
FOCUS_INACTIVE_DISPATCH_CALL = 0x799556
FOCUS_EVENT_DISPATCH = 0x7A3BD0
MENU_SIDE_ARROW_ENABLE_SITE = 0x27A73F
MENU_SIDE_ARROW_ENABLE_ORIGINAL = b"\x41\x0f\x4f\xf4"
MENU_SIDE_ARROW_ENABLE_PATCH = b"\x90\x90\x90\x90"
NATIVE_CURSOR_DRAW_SITE = 0x2D8D10
NATIVE_CURSOR_DRAW_ORIGINAL = b"\x48\x83\xec\x48\xf3\x0f\x10\x44\x24\x78"
FOLLOWER_ACTION_STORE_SITE = 0x30F806


FOLLOWER_ACTION_APPROACH_GATE_SITE = 0x325E07
FOLLOWER_ACTION_APPROACH_GATE_RETURN = 0x325E0E


FOLLOWER_ACTION_APPROACH_NO_APPROACH = 0x325DA5
FOLLOWER_ACTION_APPROACH_GATE_ORIGINAL = bytes.fromhex("3B C3 48 8B 5C 24 30")


FOLLOWER_LOCOMOTION_STORE_SITE = 0x2682A7
FOLLOWER_LOCOMOTION_STORE_RETURN = 0x2682AE
FOLLOWER_LOCOMOTION_STORE_ORIGINAL = b"\x66\x89\x99\x80\x00\x00\x00"


FOLLOWER_GATEWAY_COLLISION_FILTER_SITE = FOLLOWER_LOCOMOTION_STORE_SITE
FOLLOWER_GATEWAY_COLLISION_FILTER_RETURN = FOLLOWER_LOCOMOTION_STORE_RETURN
FOLLOWER_GATEWAY_COLLISION_FILTER_ORIGINAL = FOLLOWER_LOCOMOTION_STORE_ORIGINAL


XINPUT_DEVICE_FINALIZE_SITE = 0x7A2409
XINPUT_DEVICE_FINALIZE_RETURN = 0x7A240E
XINPUT_DEVICE_FINALIZER = 0x7A15A0
XINPUT_DEVICE_FINALIZE_ORIGINAL = b"\xe8\x92\xf1\xff\xff"

# The stock controller update has already called XInputGetState here, but has
# not interpreted the returned XINPUT_STATE yet.  Replacing the result at this
# boundary lets an SDL-only P1 travel through the game's complete native input
# pipeline (sticks, triggers, D-pad, buttons, transitions and connection state).
XINPUT_STATE_RESULT_SITE = 0x7A21C5
XINPUT_STATE_RESULT_RETURN = 0x7A21CC
XINPUT_STATE_RESULT_ORIGINAL = b"\x0f\xb6\x8b\x98\x00\x00\x00"


# DirectInput joystick polling is a separate native path from XInput. A HID
# gamepad (notably a DualSense) can therefore reach the game once through
# DirectInput and a second time through the co-op SDL route. This boundary is
# after the raw DIJOYSTATE2 has been translated into m_platformData and directly
# before the shared transition/filter finalizer. Keyboard and mouse use other
# update functions.
DINPUT_GAMEPAD_FINALIZE_SITE = 0x7A1C9F
DINPUT_GAMEPAD_FINALIZE_RETURN = 0x7A1CA7
DINPUT_GAMEPAD_FINALIZE_ORIGINAL = bytes.fromhex("48 8B CB E8 F9 F8 FF FF")

# Steam Input owns a third native pad path. At this boundary its update has
# already translated the Steam state into the shared m_platformData fields and
# is about to run the same transition/filter finalizer used by DirectInput and
# XInput. Clearing those fields before the finalizer releases both the current
# state and the filtered/edge state instead of leaving a button latched.
STEAM_GAMEPAD_FINALIZE_SITE = 0x7A2113
STEAM_GAMEPAD_FINALIZE_RETURN = 0x7A211B
STEAM_GAMEPAD_FINALIZE_ORIGINAL = bytes.fromhex("48 8B CB E8 85 F4 FF FF")


COMMAND_BLOCK = 0x5BAA0200


COOP_ACTIVE_OFFSET = 0x08
MOVEMENT_ACTIVE_OFFSET = 0x38
CAMERA_ACTIVE_OFFSET = 0x3C


P2_INTERACTION_OWNER_OFFSET = 0x40
INTERACTION_OWNER_NONE = 0
INTERACTION_OWNER_P2 = 2
INTERACTION_OWNER_P3 = 3


P2_SKELETON_KEEP_OFFSET = 0x48


WAIT_MENU_DESIRED_OFFSET = 0x50
WAIT_MENU_APPLIED_OFFSET = 0x51


P2_LOOT_HANDLE_OFFSET = 0x54

# Interaction ownership is narrower than the co-op lifecycle.  The runtime stays
# logically active through towns, native modals, and area hand-offs so that its
# other hooks retain their state.  Private P2/P3 interaction scans, however, must
# run only while a follower actually owns field movement.  Keeping this as an
# independent flag prevents those synthetic scans from touching the stock P1
# NPC/door/crystal pipeline while co-op is merely dormant.
INTERACTION_ROUTING_ACTIVE_OFFSET = 0x58


CAMERA_COLLISION_EXCLUSION_START_OFFSET = 0x64


P1_HUD_PARTY_SLOT_OFFSET = 0x6C
P2_HUD_PARTY_SLOT_OFFSET = 0x70


KEYBOARD_FILTER_ACTIVE_OFFSET = 0x74


KEYBOARD_FILTER_BUDGET_CAVE_OFFSET = 0xE0
KEYBOARD_FILTER_BUDGET_REFILL = 240
# FF12 builds the DirectInput keyboard state at device + 0x231 immediately
# before KEYBOARD_HOOK_SITE.  Filtering still has to suppress every
# player-specific key.  Map/map transparency and speed selection remain global
# to the shared game, but camera directions and reset belong exclusively to P1.
# The filter is inactive when P1 owns the desktop (and whenever co-op is off),
# so those camera keys reach the stock path only in those two cases.  Their
# internal indices were verified against the executable's normalizer table at
# KEYBOARD_NATIVE_KEYMAP.  Esc is deliberately absent because the follower
# Pause action returns through the native dialog bridge; allowing both paths
# would generate two Pause edges.
KEYBOARD_NATIVE_PASSTHROUGH_INDICES = (
    0x0C,  # M: display map
    0x19,  # Z: display map
    0x25,  # F1: regular speed
    0x26,  # F2: double speed
    0x27,  # F3: quadruple speed
    0x33,  # Tab: map transparency
    0x56,  # Numpad +: display map
)
# The 256-byte block is full, with only four bytes free at 0x74, while a third
# player needs an actor pointer and a polar command. A 64 KiB cave occupancy
# audit identified 0x8200 as the largest verified gap, immediately after the
# retired menu-clone range (0x2000..0x81F0). It therefore collides with neither
# live code nor historical addresses; test_runtime_cave_layout enforces this.
P3_COMMAND_BLOCK_OFFSET = 0x8200
P3_COMMAND_BLOCK_SIZE = 0x100

P3_HEADING_OFFSET = 0x00
P3_MAGNITUDE_OFFSET = 0x04
P3_ACTOR_OFFSET = 0x08
P3_PHYS_OFFSET = 0x10
P3_MOVEMENT_ACTIVE_OFFSET = 0x18
P3_HUD_PARTY_SLOT_OFFSET = 0x1C
P3_VISUAL_HEADING_OFFSET = 0x20

P3_LOOT_HANDLE_OFFSET = 0x24


P3_INTERACTION_ENTRY_OFFSET = 0x28
P3_INTERACTION_SCAN_ENTRY_OFFSET = 0x30
P3_INTERACTION_STATE_POINTER_OFFSET = 0x38


P3_GAUGE_PALETTE_OFFSET = 0x40
P3_HUD_TEXT_THEME_COLOR_OFFSET = 0x60
P3_MENU_THEME_COLOR_OFFSET = 0x70
P3_DAMAGE_NUMBER_COLOR_OFFSET = 0x80


P3_SKELETON_KEEP_OFFSET = 0x90
MANUAL_CONTROL_STATE_OFFSET = 0xA0
MANUAL_CONTROL_STATE_MAGIC = 0x314C5443
P2_MANUAL_CONTROL_BIT = 1 << 0
P3_MANUAL_CONTROL_BIT = 1 << 1
DEFAULT_MANUAL_CONTROL_MASK = P2_MANUAL_CONTROL_BIT | P3_MANUAL_CONTROL_BIT

# Optional mailbox for isolated mechanics that temporarily move P1 without
# dragging the shared camera. The producer writes eight floats first and arms
# the record last; absent producers leave it zero and cost one bounded read.
EXTERNAL_CAMERA_HOLD_OFFSET = 0xA8
EXTERNAL_CAMERA_HOLD_MAGIC = 0x4D4A5249
EXTERNAL_CAMERA_HOLD_SIZE = 0x28

# Helper liveness is infrastructure state, not gameplay policy.  It must remain
# armed while F8/manual control, focus, KO/status and party materialization make
# the movement command mailboxes legitimately inactive.  A seqlock prevents the
# supervisor from accepting a mixed heartbeat while the worker refreshes it.
MOVEMENT_HELPER_LIVENESS_OFFSET = 0xD0
MOVEMENT_HELPER_LIVENESS_SIZE = 0x20
MOVEMENT_HELPER_LIVENESS_MAGIC = 0x31564C4D  # "MLV1"
MOVEMENT_HELPER_LIVENESS_VERSION = 1
MOVEMENT_HELPER_LIVENESS_HEARTBEAT_SECONDS = 0.250
MOVEMENT_HELPER_LIVENESS_STALE_SECONDS = 2.000
MOVEMENT_HELPER_LIVENESS_STRUCT = struct.Struct("<IIIIQII")

P3_GAUGE_COLOR_CAVE_OFFSET = 0x8300


P1_BERSERK_MOVEMENT_STATE_OFFSET = 0x8620
P1_BERSERK_MOVEMENT_STATE_SIZE = 0x30
P1_BERSERK_HEADING_OFFSET = 0x00
P1_BERSERK_MAGNITUDE_OFFSET = 0x04
P1_BERSERK_ACTOR_OFFSET = 0x08
P1_BERSERK_PHYS_OFFSET = 0x10
P1_BERSERK_ACTIVE_OFFSET = 0x18
P1_BERSERK_VISUAL_HEADING_OFFSET = 0x1C
P1_BERSERK_INTERACTION_ENTRY_OFFSET = 0x20
P1_BERSERK_SKELETON_KEEP_OFFSET = 0x28
P1_BERSERK_MOVEMENT_HELPER_OFFSET = 0x8660
P1_BERSERK_MOVEMENT_HELPER_SIZE = 0x80

# P2 and P3 share the exact same sequence-numbered movement ABI.  The audited
# gap begins after the combat-log and P1 Berserk state regions and ends before
# the follower/camera caves at +0xB000.
MOVEMENT_MAILBOX_REGION_OFFSET = 0x8700
MOVEMENT_MAILBOX_ROLE_STRIDE = 0x60
P2_MOVEMENT_MAILBOX_OFFSET = MOVEMENT_MAILBOX_REGION_OFFSET
P3_MOVEMENT_MAILBOX_OFFSET = (
    MOVEMENT_MAILBOX_REGION_OFFSET + MOVEMENT_MAILBOX_ROLE_STRIDE
)
MOVEMENT_MAILBOX_REGION_SIZE = MOVEMENT_MAILBOX_ROLE_STRIDE * 2
STEERING_FALLBACK_HELPER_OFFSET = (
    MOVEMENT_MAILBOX_REGION_OFFSET + MOVEMENT_MAILBOX_REGION_SIZE
)
STEERING_FALLBACK_HELPER_SIZE = 0x40
MOVEMENT_CAVE_OFFSET = 0x8800
MOVEMENT_CAVE_SIZE = 0x200
ACTOR_PHYSICS_HELPER_OFFSET = MOVEMENT_CAVE_OFFSET + 0x1E0
ACTOR_PHYSICS_HELPER_SIZE = 0x20
VISUAL_FALLBACK_HELPER_OFFSET = 0x1200
VISUAL_FALLBACK_HELPER_SIZE = 0x100

assert movement_mailbox.MAILBOX_STORAGE_SIZE <= MOVEMENT_MAILBOX_ROLE_STRIDE
assert (
    STEERING_FALLBACK_HELPER_OFFSET + STEERING_FALLBACK_HELPER_SIZE
    <= MOVEMENT_CAVE_OFFSET
)
assert ACTOR_PHYSICS_HELPER_OFFSET + ACTOR_PHYSICS_HELPER_SIZE <= (
    MOVEMENT_CAVE_OFFSET + MOVEMENT_CAVE_SIZE
)
assert VISUAL_FALLBACK_HELPER_OFFSET + VISUAL_FALLBACK_HELPER_SIZE <= 0x1300


INVALID_HUD_PARTY_SLOT = 0xFFFFFFFF
NATIVE_COMMAND_MENU_OPEN = 0x252B40
NATIVE_COMMAND_MENU_CLOSE = 0x252D90
NATIVE_COMMAND_ACTOR_GLOBAL = 0x2092730
NATIVE_COMMAND_FLAG_GLOBAL = 0x2092734

SETTINGS_POINTER_GLOBAL = 0x215E6E0
BATTLE_MODE_ACTIVE_MASK = 0x08
MOVEMENT_CAVE = 0x5BAA8800
ACTOR_PHYSICS_HELPER = 0x5BAA89E0
P2_MOVEMENT_MAILBOX = 0x5BAA8700
P3_MOVEMENT_MAILBOX = 0x5BAA8760
STEERING_FALLBACK_HELPER = 0x5BAA87C0
KEYBOARD_CAVE = 0x5BAA0300
KEYBOARD_FILTER_BUDGET = 0x5BAA03E0
FACING_CAVE = 0x5BAA0400
VISUAL_FACING_CAVE = 0x5BAA1000
VISUAL_FALLBACK_HELPER = 0x5BAA1200
CAMERA_CAVE = 0x5BAA0600
CAMERA_DISTANCE_CAVE1 = 0x5BAA0700
CAMERA_DISTANCE_CAVE2 = 0x5BAA0800
GAUGE_COLOR_CAVE = 0x5BAA0900
GAUGE_CUSTOM_COLOR_CAVE = 0x5BAA0A00
GAUGE_P3_COLOR_CAVE = 0x5BAA8300
GAUGE_P3_PALETTE = 0x5BAA8240
HUD_P3_TEXT_COLOR = 0x5BAA8260
P3_MENU_THEME_COLOR = 0x5BAA8270


P3_DAMAGE_NUMBER_COLOR = 0x5BAA8280
GAUGE_CUSTOM_PALETTE = 0x5BAA0B00
HUD_P2_TEXT_COLOR = 0x5BAA0B20
P2_MENU_THEME_COLOR = 0x5BAA0B30
P2_DAMAGE_NUMBER_COLOR = 0x5BAA0B40
FOCUS_PAUSE_CAVE = 0x5BAA0C00


CAMERA_FOCUS_V2_CAVE = 0
CAMERA_FRAME_STATE = 0x5BAA2000
CAMERA_SMOOTH_STATE = CAMERA_FRAME_STATE
CAMERA_SNAPSHOT_HELPER_OFFSET = 0x2100
CAMERA_SNAPSHOT_HELPER_SIZE = 0x100
CAMERA_SNAPSHOT_HELPER = 0x5BAA2100
# Compatibility name used only to recognize and remove the retired smoothing
# detour.  Its old page now hosts the coherent camera-snapshot reader.
CAMERA_SMOOTH_CAVE = CAMERA_SNAPSHOT_HELPER
GAUGE_P1_PALETTE = 0x5BAA0E00
HUD_P1_TEXT_COLOR = 0x5BAA0E20
P1_MENU_THEME_COLOR = 0x5BAA0E30
P1_DAMAGE_NUMBER_COLOR = 0x5BAA0E40
GAUGE_P1_COLOR_CAVE = 0x5BAA0F00
HUD_TEXT_COLOR_CAVE = 0x5BAAF100
NATIVE_CURSOR_FILTER_CAVE = 0x5BAA1A00
FOLLOWER_ACTION_FILTER_CAVE = 0x5BAA1B00
PHYS_FACING_GUARD_CAVE = 0x5BAA1C00


FOLLOWER_TURN_GUARD_CAVE = 0x5BAAF000
STEERING_HEADING_CAVE = 0x5BAAF400
FOLLOWER_LIST_GUARD_CAVE = 0x5BAAF600
P1_BERSERK_MOVEMENT_STATE = 0x5BAA8620
P1_BERSERK_MOVEMENT_HELPER = 0x5BAA8660
FOLLOWER_ARBITRATOR_GUARD_CAVE = 0x5BAAF800
INTERACTION_ACTOR_FILTER_CAVE = 0x5BAAFA00
INTERACTION_BROADPHASE_FILTER_CAVE = 0x5BAAFC00
INTERACTION_GEOMETRY_ENTRY_CAVE = 0x5BAAFE00
INTERACTION_UPDATE_ENTRY_CAVE = 0x5BAAFF00
INTERACTION_SOURCE_CAVE = 0x5BAAF200
INTERACTION_ENABLE_GATE_CAVE = 0x5BAAF300
INTERACTION_INNER_RANGE_GATE_CAVE = 0x5BAAF500
INTERACTION_OUTER_RANGE_GATE_CAVE = 0x5BAAF700
INTERACTION_GEOMETRY_RESULT_CAVE = 0x5BAAF900
INTERACTION_PROXIMITY_SOURCE_CAVE = 0x5BAAFB00
INTERACTION_INDICATOR_SOURCE_CAVE = 0x5BAAFD00
INTERACTION_GEOMETRY_RESULT_R12 = 0x80
INTERACTION_GEOMETRY_RESULT_R13 = 0x84
INTERACTION_GEOMETRY_RESULT_COUNT = 0x88


P2_INTERACTION_ENTRY_OFFSET = 0xB8
P2_INTERACTION_STATE_POINTER_OFFSET = 0xB0
INTERACTION_DEBUG_LAST_ENTRY_OFFSET = 0xC0
INTERACTION_DEBUG_TOTAL_OFFSET = 0xC8
INTERACTION_DEBUG_P2_MATCH_OFFSET = 0xD0
INTERACTION_GEOMETRY_LAST_ENTRY_OFFSET = 0xE0
INTERACTION_GEOMETRY_TOTAL_OFFSET = 0xE8
INTERACTION_GEOMETRY_P2_MATCH_OFFSET = 0xF0
INTERACTION_UPDATE_COUNT_OFFSET = 0xF8

DUAL_INTERACTION_MAGIC = b"FF12INT2"
DUAL_INTERACTION_ALLOCATION_SIZE = 0x10000
# This moved out of the 0x100..0x300 slot, whose 0x200 bytes already used 0x1FA
# when follower NPC interaction was enabled. The span from the leader bridge to
# the state block was free, providing 0x700 bytes without moving another region.
DUAL_INTERACTION_SCAN_CAVE_OFFSET = 0x900
DUAL_INTERACTION_SCAN_CAVE_LIMIT = 0x700
DUAL_INTERACTION_PUBLISHER_CAVE_OFFSET = 0x300
DUAL_INTERACTION_PROXIMITY_CAVE_OFFSET = 0x500
DUAL_INTERACTION_SECONDARY_FILTER_CAVE_OFFSET = 0x700
NATIVE_LEADER_BRIDGE_CAVE_OFFSET = 0x1F00
NATIVE_LEADER_BRIDGE_CAVE_SIZE = 0x200
NATIVE_LEADER_ROLE_COMMIT_CAVE_OFFSET = 0x2200
NATIVE_LEADER_ROLE_COMMIT_CAVE_SIZE = 0xA00
NATIVE_LEADER_ROLE_PUBLISH_ENTRY_OFFSET = 0x800
NATIVE_LEADER_ROLE_JOURNAL_OFFSET = 0x2100
NATIVE_LEADER_ROLE_JOURNAL_SIZE = 0x100
NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE_OFFSET = 0xC0
NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET = 0xC4
NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE_OFFSET = 0xC8
NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE_OFFSET = 0xCC
NATIVE_PARTY_LEADER_GUARD_CAVE_OFFSET = 0x1E00
DUAL_INTERACTION_STATE_OFFSET = 0x1000

# Cross-thread action repair is published here, but consumed only from the
# native follower-list evaluator.  The existing interaction state ends at
# +0x1600; the next native reservation starts at +0x3000.  Keep data and code in
# separately audited subranges so hot-reuse manifests never mistake a mutable
# mailbox for executable code.
FOLLOWER_ACTION_MAILBOX_OFFSET = 0x1600
FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE = 0x40
FOLLOWER_ACTION_MAILBOX_ROLE_COUNT = 2
FOLLOWER_ACTION_MAILBOX_REGION_SIZE = (
    FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE * FOLLOWER_ACTION_MAILBOX_ROLE_COUNT
)
FOLLOWER_ACTION_TRANSITION_HELPER_OFFSET = 0x1800
FOLLOWER_ACTION_TRANSITION_HELPER_SIZE = 0x400

FOLLOWER_ACTION_REQUEST_SEQUENCE_OFFSET = 0x00
FOLLOWER_ACTION_ACK_SEQUENCE_OFFSET = 0x04
FOLLOWER_ACTION_RESULT_OFFSET = 0x08
FOLLOWER_ACTION_HANDLE_OFFSET = 0x0C
FOLLOWER_ACTION_EXPECTED_ACTOR_OFFSET = 0x10
FOLLOWER_ACTION_EXPECTED_PHYS_OFFSET = 0x18
FOLLOWER_ACTION_EXPECTED_ACTION_OFFSET = 0x20
FOLLOWER_ACTION_DESIRED_ACTION_OFFSET = 0x22
FOLLOWER_ACTION_MAILBOX_STRUCT = struct.Struct("<IIIIQQHHI24x")
assert FOLLOWER_ACTION_MAILBOX_STRUCT.size == FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE

FOLLOWER_ACTION_RESULT_PENDING = 0
FOLLOWER_ACTION_RESULT_APPLIED = 1
FOLLOWER_ACTION_RESULT_ALREADY_DESIRED = 2
FOLLOWER_ACTION_RESULT_STALE_IDENTITY = 0x10
FOLLOWER_ACTION_RESULT_STALE_PHYSICS = 0x11
FOLLOWER_ACTION_RESULT_INVALID_SIGNATURE = 0x12
FOLLOWER_ACTION_RESULT_ACTION_CHANGED = 0x13


FOLLOWER_NPC_ENTRY_GUARD_CAVE_OFFSET = 0x3000
FOLLOWER_NPC_ACCEPT_GUARD_CAVE_OFFSET = 0x3100
FOLLOWER_NPC_CLEANUP_GUARD_CAVE_OFFSET = 0x3200
FOLLOWER_NPC_EXACT_SCAN_CAVE_OFFSET = 0x3400
FOLLOWER_NPC_EXACT_SCAN_CAVE_LIMIT = 0x400
FOLLOWER_NPC_ENTRY_GUARD_CAVE_SIZE = (
    FOLLOWER_NPC_ACCEPT_GUARD_CAVE_OFFSET - FOLLOWER_NPC_ENTRY_GUARD_CAVE_OFFSET
)
FOLLOWER_NPC_ACCEPT_GUARD_CAVE_SIZE = (
    FOLLOWER_NPC_CLEANUP_GUARD_CAVE_OFFSET - FOLLOWER_NPC_ACCEPT_GUARD_CAVE_OFFSET
)
FOLLOWER_NPC_CLEANUP_GUARD_CAVE_SIZE = (
    FOLLOWER_NPC_EXACT_SCAN_CAVE_OFFSET - FOLLOWER_NPC_CLEANUP_GUARD_CAVE_OFFSET
)
FOLLOWER_NPC_GUARD_TOPOLOGY_MAGIC = b"FNPG2"
P2_DEVICE_FILTER_CAVE_OFFSET = 0xD000
P2_DEVICE_FILTER_CAVE_SIZE = 0x200
P2_DEVICE_FILTER_STATE_OFFSET = 0xD200
# D200..D2FF is a dedicated mutable-state page.  The original 0x40-byte image
# ended immediately after the native-controller slot even though the next
# reservation starts at D300.  The extended tail carries the authenticated
# leader-role transfer payload and its movement-owner acknowledgement.
P2_DEVICE_FILTER_STATE_SIZE = 0x8C
P1_SDL_XINPUT_CAVE_OFFSET = 0xD300
P1_SDL_XINPUT_CAVE_SIZE = 0x100
P1_SDL_XINPUT_STATE_OFFSET = 0xD400
P1_SDL_XINPUT_STATE_SIZE = 0x30
P1_SDL_XINPUT_ENABLED_OFFSET = 0x00
P1_SDL_XINPUT_SLOT_OFFSET = 0x04
P1_SDL_XINPUT_ACTIVE_BUFFER_OFFSET = 0x08
P1_SDL_XINPUT_BUFFER_0_OFFSET = 0x10
P1_SDL_XINPUT_BUFFER_1_OFFSET = 0x20
P1_SDL_XINPUT_VIRTUAL_SLOT = 0


# Background gamepad input (Alt-Tab).
#
# The game has two input functions, each with its own focus gate, and both compare
# GetForegroundWindow with the window stored at 0x2EA5248:
#
#   gamepad        0x7A21AE..0x7A241F   gate at 0x7A2236
#   mouse/keyboard 0x7A2440..0x7A3105   gate at 0x7A30BB
#
# Only the gamepad gate is bypassed here. Mouse and keyboard still stop on
# Alt-Tab exactly as they do in the original game.
#
# XInputGetState (0x861EA2) is already called at 0x7A21C0 before the gate, and
# Windows does not silence it when focus is lost. The gamepad is already read in
# the background; the gate only discards consumption of that state. Letting the
# block run is sufficient, with no additional read to install.
#
# 0x2EA5213 is a focus flag SHARED by both functions, consumed by WndProc at
# 0x7996FA. Skipping straight past the gate would write 1 while the window is in
# the background and make gamepad and mouse disagree every frame. The cave
# therefore rejoins after the store (0x7A2243) and writes 0 itself when the
# window is unfocused: the gamepad block runs while the shared flag stays honest.
#
# RAX carries the returned HWND, but 0x7A2243 is `mov eax, [rbx+0x45C]`, so no
# later instruction in the block depends on it.
PAD_BACKGROUND_INPUT_SITE = 0x7A2236
PAD_BACKGROUND_INPUT_ORIGINAL = b"\x0f\x85\xd4\x01\x00\x00"
PAD_BACKGROUND_INPUT_FOCUSED_ENTRY = 0x7A223C
PAD_BACKGROUND_INPUT_RESUME_ENTRY = 0x7A2243
INPUT_FOCUS_FLAG_GLOBAL = 0x2EA5213
PAD_BACKGROUND_INPUT_CAVE_OFFSET = 0xD500
PAD_BACKGROUND_INPUT_CAVE_SIZE = 0x100
PAD_BACKGROUND_INPUT_ENABLED = True
DINPUT_GAMEPAD_FILTER_CAVE_OFFSET = 0xD600
DINPUT_GAMEPAD_FILTER_CAVE_SIZE = 0x100
STEAM_GAMEPAD_FILTER_CAVE_OFFSET = 0xD700
STEAM_GAMEPAD_FILTER_CAVE_SIZE = 0x100
COOP_FLEE_REQUEST_OFFSET = 0x04
COOP_FLEE_P1_PHYSICAL_OFFSET = 0x08
COOP_FLEE_ACTIVE_OFFSET = 0x0C
P1_NATIVE_CONTROLLER_SLOT_OFFSET = 0x3C
INVALID_NATIVE_CONTROLLER_SLOT = 0xFFFFFFFF


NATIVE_LEADER_MEMBER_OFFSET = 0x10
NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET = 0x14
NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET = 0x18
NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET = 0x1C
NATIVE_LEADER_HANDOFF_TRANSIENT = 1
NATIVE_LEADER_HANDOFF_ROLE_TRANSFER = 2

# A custom P1 leader selection changes two things that live in different
# workers: FF12's native leader and the persistent P1/P2/P3 role identity.  A
# bare boolean handoff flag allowed the native half to land up to two 250 ms
# polling windows before movement adopted the new identity.  During that gap
# the freshly selected leader was rejected as a follower, its movement mailbox
# was disarmed and its HUD slot was published as invalid.
#
# The request sequence is the commit word for this payload.  The producer fills
# all three desired role members, publishes ROLE_TRANSFER mode, then publishes
# the native target and request sequence last.  Movement accepts it only after
# the native bridge has acknowledged the same sequence and publishes its own
# acknowledgement only after the new owner commands/metadata are committed.
NATIVE_LEADER_ROLE_P1_OFFSET = 0x40
NATIVE_LEADER_ROLE_P2_OFFSET = 0x44
NATIVE_LEADER_ROLE_P3_OFFSET = 0x48
NATIVE_LEADER_ROLE_SEQUENCE_OFFSET = 0x4C
NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET = 0x50
NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET = 0x54
NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET = 0x58
NATIVE_LEADER_ROLE_P1_HUD_SLOT_OFFSET = 0x5C
NATIVE_LEADER_ROLE_P2_HUD_SLOT_OFFSET = 0x60
NATIVE_LEADER_ROLE_P3_HUD_SLOT_OFFSET = 0x64
NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET = 0x68
NATIVE_LEADER_ROLE_WORLD_EPOCH_OFFSET = 0x6C
NATIVE_LEADER_ROLE_LAYOUT_GENERATION_OFFSET = 0x70
NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET = 0x74
NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET = 0x78
NATIVE_LEADER_ROLE_ACTIVE_OFFSET = 0x7C
NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET = 0x80
NATIVE_LEADER_ROLE_P3_NATIVE_HANDLE_OFFSET = 0x84
NATIVE_LEADER_ROLE_P1_NATIVE_HANDLE_OFFSET = 0x88
NATIVE_LEADER_ROLE_CLAIMING = 1
NATIVE_LEADER_ROLE_ACTIVE = 2
NATIVE_LEADER_ROLE_CANCELLING = 3
NATIVE_LEADER_ROLE_CANCELLED = 4
NATIVE_LEADER_ROLE_NONE = 0xFFFFFFFF
NATIVE_LEADER_ROLE_EMPTY_P3 = 0xFFFFFFFE

NATIVE_LEADER_JOURNAL_SEQUENCE_OFFSET = 0x00
NATIVE_LEADER_JOURNAL_REQUEST_OFFSET = 0x04
NATIVE_LEADER_JOURNAL_WORLD_EPOCH_OFFSET = 0x08
NATIVE_LEADER_JOURNAL_LAYOUT_GENERATION_OFFSET = 0x0C
NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET = 0x10
NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET = 0x14
NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET = 0x40
NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET = 0x44
NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET = 0x70
NATIVE_LEADER_JOURNAL_P2_HUD_SLOT_OFFSET = 0x74
NATIVE_LEADER_JOURNAL_P3_HUD_SLOT_OFFSET = 0x78
NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET = 0x7C
NATIVE_LEADER_JOURNAL_P2_ROLE_OFFSET = 0x80
NATIVE_LEADER_JOURNAL_P3_ROLE_OFFSET = 0x84
NATIVE_LEADER_JOURNAL_BATTLE_INFO_OFFSET = 0x88
NATIVE_LEADER_JOURNAL_LOCATION_BASE_OFFSET = 0x90
NATIVE_LEADER_JOURNAL_LOCATION_ID_OFFSET = 0x98
NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET = 0x9C
NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET = 0xA4
NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET = 0xAC
NATIVE_LEADER_JOURNAL_P3_NATIVE_HANDLE_OFFSET = 0xB0
NATIVE_LEADER_JOURNAL_ACTOR_MANAGER_OFFSET = 0xB4
NATIVE_LEADER_JOURNAL_ACTOR_COUNT_OFFSET = 0xBC
NATIVE_LEADER_JOURNAL_RECORD_SIZE = 0xC0
assert NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET >= NATIVE_LEADER_JOURNAL_RECORD_SIZE
assert NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE_OFFSET >= NATIVE_LEADER_JOURNAL_RECORD_SIZE
assert NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE_OFFSET + 4 <= (
    NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET
)
assert NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET + 4 <= NATIVE_LEADER_ROLE_JOURNAL_SIZE
assert NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE_OFFSET >= (
    NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET + 4
)
assert NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE_OFFSET + 4 <= (
    NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE_OFFSET
)
assert NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE_OFFSET >= (
    NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE_OFFSET + 4
)
assert NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE_OFFSET + 4 <= (
    NATIVE_LEADER_ROLE_JOURNAL_SIZE
)
NATIVE_LEADER_SETTER = 0x327850
NATIVE_PARTY_LEADER_SETTER = 0x35BAA0
NATIVE_PARTY_LEADER_SETTER_RETURN = 0x35BAA6
NATIVE_PARTY_LEADER_SETTER_ORIGINAL = bytes.fromhex("40 53 48 83 EC 20")


NATIVE_AREA_TARGET_OFFSET = 0x20
NATIVE_AREA_SPAWN_OFFSET = 0x24
NATIVE_AREA_OPTIONS_OFFSET = 0x28
NATIVE_AREA_FLAG_OFFSET = 0x2C
NATIVE_AREA_REQUEST_SEQUENCE_OFFSET = 0x30
NATIVE_AREA_APPLIED_SEQUENCE_OFFSET = 0x34
NATIVE_AREA_RESULT_OFFSET = 0x38
NATIVE_AREA_REQUEST_WRAPPER = 0x314440


FOLLOWER_LOCOMOTION_GUARD_CAVE_OFFSET = 0xB000
FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE_OFFSET = 0xB100
CAMERA_FOCUS_V2_CAVE_OFFSET = 0xB200


FOLLOWER_ACTION_APPROACH_GATE_CAVE_OFFSET = 0xB300


FOLLOWER_ACTION_NATIVE_APPROACH_HELPER_OFFSET = 0xB380
CAMERA_COLLISION_FILTER_CAVE_OFFSET = 0xB400
CAMERA_COLLISION_FILTER_SCRATCH_OFFSET = 0xB7E0
# Two dedicated pages sit between camera scratch and the interaction router at
# +0xC000. The counters push the code beyond a single page.

FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET = 0xB900
FOLLOWER_GATEWAY_TRIGGER_SCRATCH_OFFSET = 0xBAE0
FOLLOWER_GATEWAY_TRIGGER_SCRATCH_SIZE = 0x20

GATEWAY_TRIGGER_SAVED_HANDLE = 0x00
GATEWAY_TRIGGER_SAVED_SOURCE = 0x08
GATEWAY_TRIGGER_COUNT_ACTIVE = 0x10
GATEWAY_TRIGGER_COUNT_ENTITY = 0x14
GATEWAY_TRIGGER_COUNT_HANDLE = 0x18
GATEWAY_TRIGGER_COUNT_PASS = 0x1C
CAMERA_COLLISION_FORMATION_OFFSET = 0x20
CAMERA_COLLISION_FOCUS_ESCAPE_ACTIVE_OFFSET = 0x44
DUAL_PUBLISH_EXACT_DESCRIPTOR_OFFSET = 0x100
DUAL_PUBLISH_EXACT_CAN_USE_OFFSET = 0x118
DUAL_INTERACTION_P2_SNAPSHOT_OFFSET = 0x200
DUAL_INTERACTION_P3_SNAPSHOT_OFFSET = 0x400
# The snapshot is 0x120 bytes and its counters follow immediately. TICK advances
# on every pass; MARK receives the same value only when a publication occurred.


DUAL_INTERACTION_PUBLISH_TICK_OFFSET = 0x120
DUAL_INTERACTION_PUBLISH_MARK_OFFSET = 0x124


DUAL_INTERACTION_STATE_SLOTS_OFFSET = 0xC0
DUAL_INTERACTION_STATE_SLOT_COUNT = 8
NATIVE_INTERACTION_DESCRIPTOR = 0x209A2B0
NATIVE_INTERACTION_CAN_USE_GATE = 0x209A2AA
FOLLOWER_NPC_ENTRY_GUARD_SITE = 0x25BAF2
FOLLOWER_NPC_ENTRY_GUARD_RETURN = 0x25BAF9
FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL = bytes.fromhex("80 62 0C FD 48 8B FA")
FOLLOWER_NPC_ACCEPT_GUARD_SITE = 0x25BC82
FOLLOWER_NPC_ACCEPT_GUARD_RETURN = 0x25BC8A
FOLLOWER_NPC_ACCEPT_GUARD_PRIVATE_RETURN = 0x25BD74
FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL = bytes.fromhex("80 4F 0C 02 0F B6 47 0B")
FOLLOWER_NPC_CLEANUP_GUARD_SITE = 0x25BDF3
FOLLOWER_NPC_CLEANUP_GUARD_RETURN = 0x25BDF9
FOLLOWER_NPC_CLEANUP_GUARD_PRIVATE_RETURN = 0x25BE0B
FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL = bytes.fromhex("0F B6 47 0B A8 08")
FOLLOWER_NPC_EXACT_SCAN_CALL_SITE = 0x25DEC6
FOLLOWER_NPC_EXACT_SCAN_CALL_ORIGINAL = b"\xe8" + struct.pack(
    "<i",
    INTERACTION_NATIVE_INDICATOR_SCAN - (FOLLOWER_NPC_EXACT_SCAN_CALL_SITE + 5),
)

DUAL_INTERACTION_BASE = 0
DUAL_INTERACTION_SCAN_CAVE = 0
DUAL_INTERACTION_PUBLISHER_CAVE = 0
DUAL_INTERACTION_PROXIMITY_CAVE = 0
DUAL_INTERACTION_SECONDARY_FILTER_CAVE = 0
FOLLOWER_NPC_ENTRY_GUARD_CAVE = 0
FOLLOWER_NPC_ACCEPT_GUARD_CAVE = 0
FOLLOWER_NPC_CLEANUP_GUARD_CAVE = 0
FOLLOWER_NPC_EXACT_SCAN_CAVE = 0
FOLLOWER_GATEWAY_TRIGGER_CAVE = 0
FOLLOWER_GATEWAY_TRIGGER_SCRATCH = 0
NATIVE_LEADER_BRIDGE_CAVE = 0
NATIVE_PARTY_LEADER_GUARD_CAVE = 0
NATIVE_LEADER_ROLE_COMMIT_CAVE = 0
NATIVE_LEADER_ROLE_JOURNAL = 0
NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE = 0
NATIVE_LEADER_ROLE_SHUTDOWN_FENCE = 0
NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE = 0
NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE = 0
DUAL_INTERACTION_STATE = 0
P2_FOLLOWER_ACTION_MAILBOX = 0
P3_FOLLOWER_ACTION_MAILBOX = 0
FOLLOWER_ACTION_TRANSITION_HELPER = 0
P2_DEVICE_FILTER_CAVE = 0
P2_DEVICE_FILTER_STATE = 0
P1_SDL_XINPUT_CAVE = 0
P1_SDL_XINPUT_STATE = 0
PAD_BACKGROUND_INPUT_CAVE = 0
DINPUT_GAMEPAD_FILTER_CAVE = 0
STEAM_GAMEPAD_FILTER_CAVE = 0
FOLLOWER_LOCOMOTION_GUARD_CAVE = 0
FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE = 0
FOLLOWER_ACTION_APPROACH_GATE_CAVE = 0
FOLLOWER_ACTION_NATIVE_APPROACH_HELPER = 0
CAMERA_COLLISION_FILTER_CAVE = 0
CAMERA_COLLISION_FILTER_SCRATCH = 0


CAMERA_VIEW_MATRIX = 0x1F7E528


RUN_STEP = 0.10
ARMED_RUN_STEP = 0.08333333333333333
WALK_STEP = 0.04
# The game does not pass raw analogue magnitude to its walking command. Measuring
# the leader at physObj+0xD0 while moving the stick through its full range showed
# silence up to 0.4, a short ramp, a flat walk at exactly one third of the run
# command, then a flat run above the threshold. Reading the stick directly
# exposed every intermediate value without a ceiling: followers moved while the
# leader was still idle and ran at more than twice the leader's speed.
NATIVE_STICK_WALK_START = 0.40
NATIVE_STICK_WALK_FULL = 0.55
NATIVE_STICK_RUN_START = 0.92


NATIVE_STICK_WALK_FRACTION = 1.0 / 3.0


def camera_direction_from_yaw(yaw):
    if yaw is None or not math.isfinite(yaw):
        return None
    return math.sin(yaw), 0.0, math.cos(yaw)


def adopt_committed_orientation(process, transfer, p2_token, p3_token,
                                world_epoch, last_heading, visual_heading,
                                last_third_player_heading, third_player_visual_heading):
    """Adopt each changed role's current committed command once, before input.

    Called only by main's validated seamless_role_permutation branch. Never
    reads an orientation from a different actor, restores a journal snapshot,
    writes game memory, or changes movement magnitude/camera/role ownership.
    """
    result = [last_heading, visual_heading,
              last_third_player_heading, third_player_visual_heading]
    if transfer.world_epoch != world_epoch or not transfer.native_applied:
        raise OSError('orientation adoption requires the current committed world')
    prior = getattr(process, '_orientation_adopted_roles', {})
    updated = dict(prior)
    for role, token, address, index in (
        ('p2', p2_token, P2_MOVEMENT_MAILBOX, 0),
        ('p3', p3_token, P3_MOVEMENT_MAILBOX, 2),
    ):
        sequence = getattr(transfer, role + '_movement_sequence')
        if not sequence:
            continue
        key = (transfer.sequence, world_epoch, sequence)
        if prior.get(role) == key:
            continue
        command = movement_mailbox.publisher_for(process, address).last_command
        if command is None:
            raise OSError('committed orientation command is unavailable')
        if command.active:
            if token is None or any(
                getattr(command, field) != getattr(token, field)
                for field in ('actor', 'phys', 'member_id', 'actor_generation', 'world_epoch')
            ):
                raise OSError('committed orientation identity changed')
            if command.world_epoch != world_epoch:
                raise OSError('committed orientation world changed')
            if not (math.isfinite(command.heading) and math.isfinite(command.visual_heading)):
                raise OSError('committed orientation is nonfinite')
            result[index:index + 2] = (command.heading, command.visual_heading)
        # Inactive KO/manual-off commands have no direction to adopt. Retire
        # this transaction too; later revival uses normal ACQUIRE_NEW sampling.
        updated[role] = key
    process._orientation_adopted_roles = updated
    return tuple(result)


def leader_heading_pair(process, leader_phys):
    if not leader_phys:
        return None
    try:
        commanded, visible = struct.unpack("<2f", process.read(leader_phys + 0xA4, 8))
    except (OSError, struct.error):
        return None
    if not math.isfinite(commanded) or not math.isfinite(visible):
        return None
    return commanded, visible


def initial_follower_heading_pair(process, leader_phys, follower_phys):
    del leader_phys
    return leader_heading_pair(process, follower_phys)


def native_stick_fraction(strength):
    if strength >= NATIVE_STICK_RUN_START:
        return 1.0
    if strength <= NATIVE_STICK_WALK_START:
        return 0.0
    if strength >= NATIVE_STICK_WALK_FULL:
        return NATIVE_STICK_WALK_FRACTION
    span = NATIVE_STICK_WALK_FULL - NATIVE_STICK_WALK_START
    return NATIVE_STICK_WALK_FRACTION * (strength - NATIVE_STICK_WALK_START) / span


FIGHTING_STANCE_OFFSET = 0x02
FIGHTING_STANCE_MASK = 0x80


KO_STATUS_MASK = 1 << 0
STONE_STATUS_MASK = 1 << 1
STOP_STATUS_MASK = 1 << 3
SLEEP_STATUS_MASK = 1 << 4
CONFUSE_STATUS_MASK = 1 << 5
DISABLE_STATUS_MASK = 1 << 13
IMMOBILIZE_STATUS_MASK = 1 << 14
BERSERK_STATUS_MASK = 1 << 27
X_ZONE_STATUS_MASK = 1 << 31

# Only statuses that physically prevent locomotion belong here. Confuse and
# Disable still restrict native commands, but neither duplicates Immobilize's
# movement rule. Berserk retains the dedicated co-op locomotion path below.
MOVEMENT_BLOCKING_STATUS_MASK = (
    KO_STATUS_MASK
    | STONE_STATUS_MASK
    | STOP_STATUS_MASK
    | SLEEP_STATUS_MASK
    | IMMOBILIZE_STATUS_MASK
    | X_ZONE_STATUS_MASK
)
FOLLOW_REQUEST_BLOCKING_STATUS_MASK = (
    KO_STATUS_MASK
    | STONE_STATUS_MASK
    | STOP_STATUS_MASK
    | SLEEP_STATUS_MASK
    | CONFUSE_STATUS_MASK
    | DISABLE_STATUS_MASK
    | BERSERK_STATUS_MASK
    | X_ZONE_STATUS_MASK
)
# The co-op native-action patch deliberately removes Berserk from the game's
# manual submission gate (0x8800203B -> 0x8000203B).  Configured P1 can therefore
# remain the native leader while Berserk, whereas a follower still cannot accept
# the ordinary native follow request used to make it a replacement leader.
CONFIGURED_P1_LEADER_BLOCKING_STATUS_MASK = (
    FOLLOW_REQUEST_BLOCKING_STATUS_MASK & ~BERSERK_STATUS_MASK
)
PERMANENT_STATUS_EFFECTS_OFFSET = 0x3C
TEMPORARY_STATUS_EFFECTS_OFFSET = 0x64
STATUS_SNAPSHOT_ATTEMPTS = 3
# 0x311CF0 is the single manual-action submission body:
# 0x311CD0 -> 0x311B20 -> 0x311CF0. Before accepting a request, it reads the same
# fields as actor_has_berserk (battle_keep +0x64 temporary | +0x3C permanent)
# and rejects the action when the issuer carries any bit from 0x8800203B:
# KO(0), Stone(1), Stop(3), Sleep(4), Confuse(5), Disable(13), Berserk(27) or
# X-Zone(31).
#
# Berserk is the only bit removed by co-op. Under that status, the player already
# drives the character through manual locomotion ownership and must also be able
# to choose an Attack target, including a neutral target that has not attacked.
# The other seven bits remain effective. Berserk AI does not pass through this
# path; if it did, the same gate would block its automatic attacks.
MANUAL_COMMAND_STATUS_GATE_SITE = 0x311D72
MANUAL_COMMAND_STATUS_GATE_ORIGINAL = b"\xf7\xc1\x3b\x20\x00\x88"
MANUAL_COMMAND_STATUS_GATE_PATCHED = b"\xf7\xc1\x3b\x20\x00\x80"
BERSERK_MANUAL_COMMAND_ENABLED = True
# Turn speed is independent from locomotion. Coupling them made the native
# controller compete with mod rotation. The 13.63-degree-per-frame limit was
# measured on the leader.

NATIVE_TURN_DEGREES_PER_FRAME = 13.63
TURN_RATE_RADIANS = math.radians(NATIVE_TURN_DEGREES_PER_FRAME * 60.0)


ACTION_TURN_RATE_RADIANS = 25.0
POLL_SECONDS = 0.010
RESOLVE_SECONDS = 0.250
FRAME_IO_RETRY_MAX_BACKOFF_SECONDS = 0.250
FRAME_IO_RETRY_LOG_INTERVAL_SECONDS = 5.0

STILL_ACTIVE = 259


AREA_ID_TABLE = 0x22C6D28
FOLLOWER_GATEWAY_MOVING_MAGNITUDE = 0.030


FOLLOWER_GATEWAY_PROGRESS_DISTANCE = 0.055
FOLLOWER_GATEWAY_STALL_SECONDS = 0.08
FOLLOWER_GATEWAY_NATIVE_PULSE_SECONDS = 1.20
FOLLOWER_GATEWAY_REQUEST_TIMEOUT_SECONDS = 0.80
FOLLOWER_GATEWAY_RESTORE_RETRY_SECONDS = 1.00
KO_LEADER_REQUEST_RETRY_SECONDS = 0.50
KO_LEADER_UNKNOWN_PROMOTION_SETTLE_SECONDS = 0.20


FOLLOWER_AREA_TRANSITIONS_ENABLED = False
FOLLOWER_AREA_TRANSITION_REQUEST_ENABLED = False
# The follower must cross the plane before collision publishes the current
# volume. The trigger above then performs the transition through the native path.
FOLLOWER_GATEWAY_COLLISION_FILTER_ENABLED = True


# The follower-specific exact-NPC pass temporarily reuses FF12's single global
# descriptor.  Even with the private-scan town gate, merely keeping its three
# native hooks installed can invalidate P1's NPC/save-crystal descriptor.  Keep
# that experimental path off until ownership is isolated completely.  Generic
# door/crystal scans remain available through the separate dual scan below.
FOLLOWER_NPC_INTERACTIONS_ENABLED = False
# Run the native door-trigger pass once more per follower with the controlled
# handle substituted. The door pass remains independent: it reads only that
# character's collision state and does not share the global NPC descriptor.
FOLLOWER_GATEWAY_TRIGGER_ENABLED = True
CAMERA_NATIVE_READY_SAMPLES = 3
CAMERA_YAW_MAX_STEP = 0.35
CAMERA_YAW_CONFIRM_SAMPLES = 3
FOLLOWER_IDLE_ACTION = 0x4124
NATIVE_IDLE_ACTION = 0x4021


PARTY_HANDLE_CLASS = 0x00100000


KO_PARTY_HANDLE_CLASS = 0x00200000
LEADER_PARTY_HANDLE_CLASS = 0x00500000
ACTIVE_PARTY_ACTOR_FLAG = 0x00020000


ENABLE_IDLE_MOTION_CANCEL = False

VK_S = 0x53
VK_D = 0x44
VK_F8 = 0x77


EXECUTION_ANIMATION_FLAG = 1 << 46
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
MEM_FREE = 0x10000
MEM_EXTENDED_PARAMETER_ADDRESS_REQUIREMENTS = 1
REL32_ALLOCATION_LOW = 0x00010000
REL32_ALLOCATION_HIGH = 0x7FFFFFFF
ALLOCATION_GRANULARITY = 0x10000
REL32_ALLOCATION_RETRY_DELAYS = (0.0, 0.05, 0.15)


CAVE_ALLOCATION_BASE = 0x10000000
CAVE_ALLOCATION_SIZE = 0x10000
CAVE_MAGIC = b"FF12COOP"
# A complete install is described by one immutable manifest and committed in
# the headers of all three runtime allocations.  A helper restart only reuses a
# runtime when the three records and every native region agree byte-for-byte.
# It never repairs executable memory in a live game.
RUNTIME_INSTALL_MARKER_OFFSET = 0x10
RUNTIME_INSTALL_MARKER_MAGIC = b"ITRTIN02"
RUNTIME_INSTALL_MARKER_VERSION = 2
RUNTIME_INSTALL_PREPARED = 1
RUNTIME_INSTALL_COMMITTED = 2
RUNTIME_INSTALL_MARKER_STRUCT = struct.Struct("<8sIIIIQQ16s16s")
RUNTIME_INSTALL_MANIFEST_MAGIC = b"ITRMNF02"
RUNTIME_INSTALL_MANIFEST_VERSION = 2
RUNTIME_INSTALL_MANIFEST_HEADER = struct.Struct("<8sII")
RUNTIME_INSTALL_MANIFEST_ENTRY = struct.Struct("<QII16s")
RUNTIME_INSTALL_MANIFEST_MAX_SIZE = 0x10000
RUNTIME_INSTALL_REGION_ROOT = 1
RUNTIME_INSTALL_REGION_EXECUTABLE = 2
RUNTIME_INSTALL_MIN_ROOT_REGIONS = 40
RUNTIME_INSTALL_MIN_EXECUTABLE_REGIONS = 30
RUNTIME_INSTALL_MAX_REMOTE_ADDRESS = 0x00007FFFFFFFFFFF
# Explicit native-layout revision.  It must change whenever generated code,
# root topology or the manifest ABI changes.  Runtime feature choices are
# mixed into the build id separately below.
RUNTIME_NATIVE_LAYOUT_ID = bytes.fromhex("16acde4d60574c358eb568df833a0d7a")
RUNTIME_PREFLIGHT_BASES = (0x30000000, 0x31000000, 0x32000000)


DAMAGE_NUMBER_THEME_MAGIC = b"FF12DMG1"
DAMAGE_NUMBER_THEME_ALLOCATION_SIZE = 0x10000
DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET = 0x100
DAMAGE_NUMBER_INIT_WRAPPER_OFFSET = 0x300
DAMAGE_NUMBER_STATE_OFFSET = 0x500


DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET = 0x700
DAMAGE_NUMBER_SOURCE_CALL_SITE = 0x3006C5
DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL = bytes.fromhex("E8 06 7D 02 00")
DAMAGE_NUMBER_FACTORY = 0x3283D0
DAMAGE_NUMBER_INIT_CALL_SITE = 0x3BBEE7
DAMAGE_NUMBER_INIT_CALL_ORIGINAL = bytes.fromhex("E8 14 A8 14 00")
DAMAGE_NUMBER_INIT = 0x506700
DAMAGE_NUMBER_OBJECT_KIND_OFFSET = 0x3C
DAMAGE_NUMBER_DESCRIPTOR_PALETTE_OFFSET = 0x0D
DAMAGE_NUMBER_DESCRIPTOR_PALETTE_MAX = 8
DAMAGE_NUMBER_RECOVERY_PALETTE_BIT = 0x01
DAMAGE_NUMBER_PALETTE_MAX = 114
# The game modulates battle glyphs GS-style, where 0x80 is 1.0.  The published
# RGB is therefore only a gentle theme tint.  The native Font CLUT selected by
# the same style record owns the bright core and saturated outline.  A smaller
# k moves the tint closer to neutral white; 0x100 applies the raw RGB unchanged.

DAMAGE_NUMBER_BRIGHTNESS_OFFSET = 0x508
# A damage-meter call proxy must remain within rel32 reach of the game's image,
# but its 64 KiB event ring does not.  Only the small proxy lives here; the
# larger allocation can therefore use an ordinary unconstrained allocation.
DAMAGE_METER_PROXY_WORKSPACE_OFFSET = 0x900
DAMAGE_METER_PROXY_WORKSPACE_SIZE = 0x100
DAMAGE_METER_PROXY_MAGIC = b"ITDMPXY1"
DAMAGE_METER_HOOK_MAGIC = b"ITDMG003"
DAMAGE_METER_PROXY_ALLOCATION_OFFSET = 0x08
DAMAGE_METER_PROXY_OLD_TARGET_OFFSET = 0x10
DAMAGE_METER_PROXY_CODE_OFFSET = 0x20
DAMAGE_METER_HOOK_OLD_TARGET_OFFSET = 0x18
# The party-story guard needs executable memory within rel32 reach of the
# game's low image.  Keep its complete 4 KiB workspace in the already-reserved
# damage allocation instead of asking Windows for an unconstrained address.
PARTY_GUARD_WORKSPACE_OFFSET = 0x1000
PARTY_GUARD_WORKSPACE_SIZE = 0x1000
# Everything below is runtime-owned scratch/data that used to request extra
# low-address pages.  Those requests made otherwise unrelated features depend
# on each PC's ASLR layout.  Keep them in the already reserved damage region so
# one successful bootstrap is sufficient for the complete co-op runtime.
NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET = 0x2000
NATIVE_TARGET_ROUTER_WORKSPACE_SIZE = 0x1000
NATIVE_AREA_TARGET_WORKSPACE_OFFSET = 0x3000
NATIVE_AREA_TARGET_WORKSPACE_SIZE = 0x1000
NATIVE_WAIT_PREDICATE_WORKSPACE_OFFSET = 0x4000
NATIVE_WAIT_PREDICATE_WORKSPACE_SIZE = 0x1000
LOOT_OWNERSHIP_WORKSPACE_OFFSET = 0x5000
LOOT_OWNERSHIP_WORKSPACE_SIZE = 0x1000
STORY_EXTENDED_WORKSPACE_OFFSET = 0x6000
STORY_EXTENDED_WORKSPACE_SIZE = 0xA000
DAMAGE_NUMBER_NEUTRAL_CHANNEL = 0x7F


DAMAGE_NUMBER_CHANNEL_CEILING = 0xFF
DAMAGE_NUMBER_BRIGHTNESS_UNCHANGED = 0x100


# The published values are already final atlas modulation.  Identity preserves
# intentional over-neutral channels used to whiten the core independently from
# the darker outline.
DAMAGE_NUMBER_BRIGHTNESS_DEFAULT = 0x100
DAMAGE_NUMBER_BRIGHTNESS_LEGACY_DEFAULTS = (0x40, 0xC0)
DAMAGE_NUMBER_THEME_BASE = 0
DAMAGE_NUMBER_SOURCE_WRAPPER = 0
DAMAGE_NUMBER_INIT_WRAPPER = 0
DAMAGE_NUMBER_INIT_WRAPPER_ALT = 0
DAMAGE_NUMBER_SOURCE_STATE = 0
DAMAGE_NUMBER_BRIGHTNESS_STATE = 0


CAVE_ALLOCATION_RANGES = (
    (CAVE_ALLOCATION_BASE, 0x70000000),
    (0x03000000, CAVE_ALLOCATION_BASE),
    (0x70000000, 0x7F000000),
    (REL32_ALLOCATION_LOW, 0x03000000),
    (0x7F000000, 0x80000000),
)


def cave_allocation_candidates():
    seen: set[int] = set()
    for start, stop in CAVE_ALLOCATION_RANGES:
        for candidate in range(start, stop, 0x10000):
            if candidate in seen:
                continue
            seen.add(candidate)
            yield candidate


class MEM_ADDRESS_REQUIREMENTS(ct.Structure):
    _fields_ = [
        ("LowestStartingAddress", ct.c_void_p),
        ("HighestEndingAddress", ct.c_void_p),
        ("Alignment", ct.c_size_t),
    ]


class MEM_EXTENDED_PARAMETER(ct.Structure):
    _fields_ = [
        ("Type", ct.c_ulonglong),
        ("Pointer", ct.c_void_p),
    ]


class MEMORY_BASIC_INFORMATION(ct.Structure):
    _fields_ = [
        ("BaseAddress", ct.c_void_p),
        ("AllocationBase", ct.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ct.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernelbase = ct.WinDLL("kernelbase", use_last_error=True)
user32 = ct.WinDLL("user32", use_last_error=True)

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_void_p,
    ct.c_size_t,
    ct.POINTER(ct.c_size_t),
]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_void_p,
    ct.c_size_t,
    ct.POINTER(ct.c_size_t),
]
kernel32.WriteProcessMemory.restype = wt.BOOL
kernel32.VirtualProtectEx.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    wt.DWORD,
    ct.POINTER(wt.DWORD),
]
kernel32.VirtualProtectEx.restype = wt.BOOL
kernel32.VirtualAllocEx.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    wt.DWORD,
    wt.DWORD,
]
kernel32.VirtualAllocEx.restype = ct.c_void_p
kernel32.VirtualFreeEx.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    wt.DWORD,
]
kernel32.VirtualFreeEx.restype = wt.BOOL
kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.POINTER(MEMORY_BASIC_INFORMATION),
    ct.c_size_t,
]
kernel32.VirtualQueryEx.restype = ct.c_size_t
_virtual_alloc2 = getattr(kernelbase, "VirtualAlloc2", None)
if _virtual_alloc2 is not None:
    _virtual_alloc2.argtypes = [
        wt.HANDLE,
        ct.c_void_p,
        ct.c_size_t,
        wt.DWORD,
        wt.DWORD,
        ct.POINTER(MEM_EXTENDED_PARAMETER),
        wt.ULONG,
    ]
    _virtual_alloc2.restype = ct.c_void_p
kernel32.FlushInstructionCache.argtypes = [wt.HANDLE, ct.c_void_p, ct.c_size_t]
kernel32.FlushInstructionCache.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
kernel32.GetExitCodeProcess.restype = wt.BOOL
user32.GetAsyncKeyState.argtypes = [ct.c_int]
user32.GetAsyncKeyState.restype = ct.c_short
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD


def _is_rel32_allocation(address: int, size: int) -> bool:
    return (
        REL32_ALLOCATION_LOW <= address
        and address + size - 1 <= REL32_ALLOCATION_HIGH
        and address % ALLOCATION_GRANULARITY == 0
    )


def _make_remote_allocation_executable(handle, address: int, size: int) -> bool:
    old_protection = wt.DWORD()
    return bool(
        kernel32.VirtualProtectEx(
            handle,
            ct.c_void_p(address),
            size,
            PAGE_EXECUTE_READWRITE,
            ct.byref(old_protection),
        )
    )


def _release_remote_allocation(handle, address: int) -> None:
    kernel32.VirtualFreeEx(handle, ct.c_void_p(address), 0, MEM_RELEASE)


def _reserve_executable_at(handle, address: int, size: int) -> int:
    target = ct.c_void_p(address)
    for protection in (PAGE_EXECUTE_READWRITE, PAGE_READWRITE):
        allocation = kernel32.VirtualAllocEx(
            handle,
            target,
            size,
            MEM_COMMIT | MEM_RESERVE,
            protection,
        )
        allocated = int(allocation or 0)
        if not allocated:
            continue
        if allocated != address:
            _release_remote_allocation(handle, allocated)
            continue
        if protection == PAGE_READWRITE and not _make_remote_allocation_executable(
            handle,
            allocated,
            size,
        ):
            _release_remote_allocation(handle, allocated)
            continue
        return allocated
    return 0


def _allocate_rel32_virtual_alloc2(handle, size: int) -> int:
    if _virtual_alloc2 is None:
        return 0
    requirements = MEM_ADDRESS_REQUIREMENTS(
        ct.c_void_p(REL32_ALLOCATION_LOW),
        ct.c_void_p(REL32_ALLOCATION_HIGH),
        ALLOCATION_GRANULARITY,
    )
    parameter = MEM_EXTENDED_PARAMETER(
        MEM_EXTENDED_PARAMETER_ADDRESS_REQUIREMENTS,
        ct.cast(ct.pointer(requirements), ct.c_void_p).value,
    )
    for protection in (PAGE_EXECUTE_READWRITE, PAGE_READWRITE):
        allocation = _virtual_alloc2(
            handle,
            None,
            size,
            MEM_COMMIT | MEM_RESERVE,
            protection,
            ct.byref(parameter),
            1,
        )
        address = int(allocation or 0)
        if not address:
            continue
        if not _is_rel32_allocation(address, size):
            _release_remote_allocation(handle, address)
            continue
        if protection == PAGE_READWRITE and not _make_remote_allocation_executable(
            handle,
            address,
            size,
        ):
            _release_remote_allocation(handle, address)
            continue
        return address
    return 0


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & -alignment


def rel32_free_region_candidates(handle, size: int):
    requested = int(size)
    for range_start, range_stop in CAVE_ALLOCATION_RANGES:
        cursor = max(int(range_start), REL32_ALLOCATION_LOW)
        stop = min(int(range_stop), REL32_ALLOCATION_HIGH + 1)
        while cursor < stop:
            information = MEMORY_BASIC_INFORMATION()
            queried = kernel32.VirtualQueryEx(
                handle,
                ct.c_void_p(cursor),
                ct.byref(information),
                ct.sizeof(information),
            )
            if not queried:
                raise OSError(ct.get_last_error(), "VirtualQueryEx")
            base = int(information.BaseAddress or 0)
            region_size = int(information.RegionSize)
            if region_size <= 0:
                raise OSError(487, "VirtualQueryEx returned an empty region")
            region_end = base + region_size
            if information.State == MEM_FREE:
                candidate = _align_up(
                    max(cursor, base, REL32_ALLOCATION_LOW),
                    ALLOCATION_GRANULARITY,
                )
                candidate_stop = min(region_end, stop)
                while candidate + requested <= candidate_stop:
                    yield candidate
                    candidate += ALLOCATION_GRANULARITY
            cursor = max(cursor + ALLOCATION_GRANULARITY, region_end)


def committed_rel32_region_bases(handle, minimum_size: int):
    requested = max(1, int(minimum_size))
    seen: set[int] = set()
    for range_start, range_stop in CAVE_ALLOCATION_RANGES:
        cursor = max(int(range_start), REL32_ALLOCATION_LOW)
        stop = min(int(range_stop), REL32_ALLOCATION_HIGH + 1)
        while cursor < stop:
            information = MEMORY_BASIC_INFORMATION()
            queried = kernel32.VirtualQueryEx(
                handle,
                ct.c_void_p(cursor),
                ct.byref(information),
                ct.sizeof(information),
            )
            if not queried:
                raise OSError(ct.get_last_error(), "VirtualQueryEx")
            base = int(information.BaseAddress or 0)
            region_size = int(information.RegionSize)
            if region_size <= 0:
                raise OSError(487, "VirtualQueryEx returned an empty region")
            region_end = base + region_size
            protection = int(information.Protect)
            if (
                information.State == MEM_COMMIT
                and base not in seen
                and base % ALLOCATION_GRANULARITY == 0
                and base >= REL32_ALLOCATION_LOW
                and base + requested - 1 <= REL32_ALLOCATION_HIGH
                and region_size >= requested
                and not protection & (PAGE_NOACCESS | PAGE_GUARD)
            ):
                seen.add(base)
                yield base
            cursor = max(cursor + ALLOCATION_GRANULARITY, region_end)


def find_rel32_magic_allocation(process, magic: bytes) -> int:
    signature = bytes(magic)
    if not signature:
        raise ValueError("allocation magic must not be empty")
    for candidate in committed_rel32_region_bases(process.handle, len(signature)):
        try:
            if process.read(candidate, len(signature)) == signature:
                return candidate
        except OSError:
            continue
    return 0


def allocate_rel32_executable(handle, size: int) -> int:
    requested = int(size)
    if requested <= 0:
        raise ValueError("allocation size must be positive")

    query_supported = True
    for delay in REL32_ALLOCATION_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        allocation = _allocate_rel32_virtual_alloc2(handle, requested)
        if allocation:
            return allocation
        if query_supported:
            try:
                candidates = rel32_free_region_candidates(handle, requested)
                for candidate in candidates:
                    allocation = _reserve_executable_at(
                        handle,
                        candidate,
                        requested,
                    )
                    if allocation:
                        return allocation
            except OSError:
                query_supported = False
        if not query_supported:
            for candidate in cave_allocation_candidates():
                allocation = _reserve_executable_at(
                    handle,
                    candidate,
                    requested,
                )
                if allocation:
                    return allocation
    return 0


def allocate_executable(handle, size: int) -> int:
    requested = int(size)
    if requested <= 0:
        raise ValueError("allocation size must be positive")
    for protection in (PAGE_EXECUTE_READWRITE, PAGE_READWRITE):
        allocation = kernel32.VirtualAllocEx(
            handle,
            None,
            requested,
            MEM_COMMIT | MEM_RESERVE,
            protection,
        )
        allocated = int(allocation or 0)
        if not allocated:
            continue
        if protection == PAGE_READWRITE and not _make_remote_allocation_executable(
            handle,
            allocated,
            requested,
        ):
            _release_remote_allocation(handle, allocated)
            continue
        return allocated
    return 0


def published_allocation_candidate(process, variable: str, magic: bytes) -> int:
    raw = os.environ.get(variable)
    if not raw:
        return 0
    try:
        candidate = int(raw, 0)
    except (TypeError, ValueError):
        return 0
    if not (
        REL32_ALLOCATION_LOW <= candidate <= REL32_ALLOCATION_HIGH
        and candidate % ALLOCATION_GRANULARITY == 0
    ):
        return 0
    try:
        if process.read(candidate, len(magic)) != magic:
            return 0
    except OSError:
        return 0
    return candidate


def expected_process_path() -> Path:
    root = runtime_paths.resolve_game_root(__file__)
    return (root / "x64" / PROCESS_NAME).resolve(strict=False)


def find_pid(pid: int | None = None) -> int:
    expected = expected_process_path()
    if pid is not None:
        return game_process.validate_process_id(int(pid), expected)
    return game_process.find_process_id(expected)


class RemoteProcess:
    def __init__(self, pid):
        rights = (
            PROCESS_CREATE_THREAD
            | PROCESS_VM_OPERATION
            | PROCESS_VM_READ
            | PROCESS_VM_WRITE
            | PROCESS_QUERY_INFORMATION
        )
        self.handle = kernel32.OpenProcess(rights, False, pid)
        if not self.handle:
            raise OSError(ct.get_last_error(), "OpenProcess")

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, size):
        buffer = (ct.c_ubyte * size)()
        transferred = ct.c_size_t()
        ok = kernel32.ReadProcessMemory(
            self.handle,
            ct.c_void_p(address),
            buffer,
            size,
            ct.byref(transferred),
        )
        if not ok or transferred.value != size:
            raise OSError(ct.get_last_error(), f"ReadProcessMemory({address:#x})")
        return bytes(buffer)

    def write(self, address, data):
        data = bytes(data)
        buffer = (ct.c_ubyte * len(data)).from_buffer_copy(data)
        transferred = ct.c_size_t()
        ok = kernel32.WriteProcessMemory(
            self.handle,
            ct.c_void_p(address),
            buffer,
            len(data),
            ct.byref(transferred),
        )
        if not ok or transferred.value != len(data):
            raise OSError(ct.get_last_error(), f"WriteProcessMemory({address:#x})")

    def patch_code(self, address, data):
        old = wt.DWORD()
        if not kernel32.VirtualProtectEx(
            self.handle,
            ct.c_void_p(address),
            len(data),
            PAGE_EXECUTE_READWRITE,
            ct.byref(old),
        ):
            raise OSError(ct.get_last_error(), "VirtualProtectEx")
        try:
            self.write(address, data)
            kernel32.FlushInstructionCache(self.handle, ct.c_void_p(address), len(data))
        finally:
            ignored = wt.DWORD()
            kernel32.VirtualProtectEx(
                self.handle,
                ct.c_void_p(address),
                len(data),
                old.value,
                ct.byref(ignored),
            )

    def ensure_cave_allocation(self, *, allow_allocate=True):
        for site, cave_offsets in (
            # Recognize the previous +0x100 movement hook only so allocation
            # discovery can classify and reject that live legacy topology.  A
            # new installation always uses the expanded +0x8800 cave.
            (MOVEMENT_HOOK_SITE, (MOVEMENT_CAVE_OFFSET, 0x100)),
            (KEYBOARD_HOOK_SITE, (0x300,)),
        ):
            try:
                instruction = self.read(site, 5)
                if instruction[0] == 0xE9:
                    target = site + 5 + struct.unpack("<i", instruction[1:])[0]
                    for cave_offset in cave_offsets:
                        candidate = target - cave_offset
                        if self.read(candidate, len(CAVE_MAGIC)) == CAVE_MAGIC:
                            set_cave_base(candidate)
                            return candidate
            except OSError:
                pass

        candidate = published_allocation_candidate(
            self,
            runtime_paths.CAVE_BASE_ENV,
            CAVE_MAGIC,
        )
        if candidate:
            set_cave_base(candidate)
            return candidate

        candidate = find_rel32_magic_allocation(self, CAVE_MAGIC)
        if candidate:
            set_cave_base(candidate)
            return candidate

        if not allow_allocate:
            return 0

        allocation = allocate_rel32_executable(
            self.handle,
            CAVE_ALLOCATION_SIZE,
        )
        if allocation:
            set_cave_base(allocation)
            self.write(allocation, CAVE_MAGIC)
            return allocation
        allocation_error = ct.get_last_error()

        for candidate in cave_allocation_candidates():
            try:
                if self.read(candidate, len(CAVE_MAGIC)) == CAVE_MAGIC:
                    set_cave_base(candidate)
                    return candidate
            except OSError:
                pass

        raise OSError(allocation_error, "No suitable rel32 codecave found")

    def ensure_dual_interaction_allocation(self, *, allow_allocate=True):
        try:
            instruction = self.read(INTERACTION_DUAL_SCAN_CALL_SITE, 5)
            if instruction[0] == 0xE8:
                target = (
                    INTERACTION_DUAL_SCAN_CALL_SITE
                    + 5
                    + struct.unpack("<i", instruction[1:])[0]
                )
                candidate = target - DUAL_INTERACTION_SCAN_CAVE_OFFSET
                if (
                    self.read(candidate, len(DUAL_INTERACTION_MAGIC))
                    == DUAL_INTERACTION_MAGIC
                ):
                    set_dual_interaction_base(candidate)
                    return candidate
        except OSError:
            pass

        candidate = published_allocation_candidate(
            self,
            runtime_paths.DUAL_CAVE_BASE_ENV,
            DUAL_INTERACTION_MAGIC,
        )
        if candidate:
            set_dual_interaction_base(candidate)
            return candidate

        candidate = find_rel32_magic_allocation(self, DUAL_INTERACTION_MAGIC)
        if candidate:
            set_dual_interaction_base(candidate)
            return candidate

        if not allow_allocate:
            return 0

        allocation = allocate_rel32_executable(
            self.handle,
            DUAL_INTERACTION_ALLOCATION_SIZE,
        )
        if allocation:
            set_dual_interaction_base(allocation)
            self.write(allocation, DUAL_INTERACTION_MAGIC)
            return allocation
        allocation_error = ct.get_last_error()

        for candidate in cave_allocation_candidates():
            try:
                if (
                    self.read(candidate, len(DUAL_INTERACTION_MAGIC))
                    == DUAL_INTERACTION_MAGIC
                ):
                    set_dual_interaction_base(candidate)
                    return candidate
            except OSError:
                pass

        raise OSError(allocation_error, "No suitable dual-interaction codecave found")

    def ensure_damage_number_theme_allocation(self, *, allow_allocate=True):
        for site, cave_offset in (
            (
                DAMAGE_NUMBER_SOURCE_CALL_SITE,
                DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET,
            ),
            (
                DAMAGE_NUMBER_INIT_CALL_SITE,
                DAMAGE_NUMBER_INIT_WRAPPER_OFFSET,
            ),
            (
                DAMAGE_NUMBER_INIT_CALL_SITE,
                DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET,
            ),
            (
                DAMAGE_NUMBER_SOURCE_CALL_SITE,
                DAMAGE_METER_PROXY_WORKSPACE_OFFSET + DAMAGE_METER_PROXY_CODE_OFFSET,
            ),
        ):
            try:
                instruction = self.read(site, 5)
                if instruction[0] == 0xE8:
                    target = site + 5 + struct.unpack("<i", instruction[1:])[0]
                    candidate = target - cave_offset
                    if (
                        self.read(candidate, len(DAMAGE_NUMBER_THEME_MAGIC))
                        == DAMAGE_NUMBER_THEME_MAGIC
                    ):
                        set_damage_number_theme_base(candidate)
                        return candidate
            except OSError:
                pass

        candidate = published_allocation_candidate(
            self,
            runtime_paths.DAMAGE_CAVE_BASE_ENV,
            DAMAGE_NUMBER_THEME_MAGIC,
        )
        if candidate:
            set_damage_number_theme_base(candidate)
            return candidate

        candidate = find_rel32_magic_allocation(
            self,
            DAMAGE_NUMBER_THEME_MAGIC,
        )
        if candidate:
            set_damage_number_theme_base(candidate)
            return candidate

        if not allow_allocate:
            return 0

        allocation = allocate_rel32_executable(
            self.handle,
            DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
        )
        if allocation:
            set_damage_number_theme_base(allocation)
            self.write(allocation, DAMAGE_NUMBER_THEME_MAGIC)
            return allocation
        allocation_error = ct.get_last_error()

        for candidate in cave_allocation_candidates():
            try:
                if (
                    self.read(candidate, len(DAMAGE_NUMBER_THEME_MAGIC))
                    == DAMAGE_NUMBER_THEME_MAGIC
                ):
                    set_damage_number_theme_base(candidate)
                    return candidate
            except OSError:
                pass

        raise OSError(
            allocation_error,
            "No suitable battle-number theme codecave found",
        )

    def allocate_runtime_manifest(self, size):
        requested = int(size)
        if not 0 < requested <= RUNTIME_INSTALL_MANIFEST_MAX_SIZE:
            raise ValueError("invalid runtime manifest allocation size")
        allocation = kernel32.VirtualAllocEx(
            self.handle,
            None,
            requested,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE,
        )
        allocated = int(allocation or 0)
        if not allocated:
            raise OSError(ct.get_last_error(), "runtime manifest allocation failed")
        return allocated

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def f32(self, address):
        return struct.unpack("<f", self.read(address, 4))[0]


def set_dual_interaction_base(base):
    global DUAL_INTERACTION_BASE
    global DUAL_INTERACTION_SCAN_CAVE
    global DUAL_INTERACTION_PUBLISHER_CAVE
    global DUAL_INTERACTION_PROXIMITY_CAVE
    global DUAL_INTERACTION_SECONDARY_FILTER_CAVE
    global FOLLOWER_NPC_ENTRY_GUARD_CAVE
    global FOLLOWER_NPC_ACCEPT_GUARD_CAVE
    global FOLLOWER_NPC_CLEANUP_GUARD_CAVE
    global FOLLOWER_NPC_EXACT_SCAN_CAVE
    global NATIVE_LEADER_BRIDGE_CAVE
    global NATIVE_PARTY_LEADER_GUARD_CAVE
    global NATIVE_LEADER_ROLE_COMMIT_CAVE
    global NATIVE_LEADER_ROLE_JOURNAL
    global NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE
    global NATIVE_LEADER_ROLE_SHUTDOWN_FENCE
    global NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE
    global NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE
    global DUAL_INTERACTION_STATE
    global P2_FOLLOWER_ACTION_MAILBOX
    global P3_FOLLOWER_ACTION_MAILBOX
    global FOLLOWER_ACTION_TRANSITION_HELPER
    global P2_DEVICE_FILTER_CAVE
    global P2_DEVICE_FILTER_STATE
    global P1_SDL_XINPUT_CAVE
    global P1_SDL_XINPUT_STATE
    global PAD_BACKGROUND_INPUT_CAVE
    global DINPUT_GAMEPAD_FILTER_CAVE
    global STEAM_GAMEPAD_FILTER_CAVE
    global FOLLOWER_LOCOMOTION_GUARD_CAVE
    global FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE
    global FOLLOWER_ACTION_APPROACH_GATE_CAVE
    global FOLLOWER_ACTION_NATIVE_APPROACH_HELPER
    global FOLLOWER_GATEWAY_TRIGGER_CAVE
    global FOLLOWER_GATEWAY_TRIGGER_SCRATCH
    global CAMERA_FOCUS_V2_CAVE
    global CAMERA_COLLISION_FILTER_CAVE
    global CAMERA_COLLISION_FILTER_SCRATCH

    DUAL_INTERACTION_BASE = base
    DUAL_INTERACTION_SCAN_CAVE = base + DUAL_INTERACTION_SCAN_CAVE_OFFSET
    DUAL_INTERACTION_PUBLISHER_CAVE = base + DUAL_INTERACTION_PUBLISHER_CAVE_OFFSET
    DUAL_INTERACTION_PROXIMITY_CAVE = base + DUAL_INTERACTION_PROXIMITY_CAVE_OFFSET
    DUAL_INTERACTION_SECONDARY_FILTER_CAVE = (
        base + DUAL_INTERACTION_SECONDARY_FILTER_CAVE_OFFSET
    )
    FOLLOWER_NPC_ENTRY_GUARD_CAVE = base + FOLLOWER_NPC_ENTRY_GUARD_CAVE_OFFSET
    FOLLOWER_NPC_ACCEPT_GUARD_CAVE = base + FOLLOWER_NPC_ACCEPT_GUARD_CAVE_OFFSET
    FOLLOWER_NPC_CLEANUP_GUARD_CAVE = base + FOLLOWER_NPC_CLEANUP_GUARD_CAVE_OFFSET
    FOLLOWER_NPC_EXACT_SCAN_CAVE = base + FOLLOWER_NPC_EXACT_SCAN_CAVE_OFFSET
    NATIVE_LEADER_BRIDGE_CAVE = base + NATIVE_LEADER_BRIDGE_CAVE_OFFSET
    NATIVE_PARTY_LEADER_GUARD_CAVE = base + NATIVE_PARTY_LEADER_GUARD_CAVE_OFFSET
    NATIVE_LEADER_ROLE_COMMIT_CAVE = base + NATIVE_LEADER_ROLE_COMMIT_CAVE_OFFSET
    NATIVE_LEADER_ROLE_JOURNAL = base + NATIVE_LEADER_ROLE_JOURNAL_OFFSET
    NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE = (
        NATIVE_LEADER_ROLE_JOURNAL
        + NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE_OFFSET
    )
    NATIVE_LEADER_ROLE_SHUTDOWN_FENCE = (
        NATIVE_LEADER_ROLE_JOURNAL + NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET
    )
    NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE = (
        NATIVE_LEADER_ROLE_JOURNAL
        + NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE_OFFSET
    )
    NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE = (
        NATIVE_LEADER_ROLE_JOURNAL
        + NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE_OFFSET
    )
    DUAL_INTERACTION_STATE = base + DUAL_INTERACTION_STATE_OFFSET
    P2_FOLLOWER_ACTION_MAILBOX = base + FOLLOWER_ACTION_MAILBOX_OFFSET
    P3_FOLLOWER_ACTION_MAILBOX = (
        P2_FOLLOWER_ACTION_MAILBOX + FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE
    )
    FOLLOWER_ACTION_TRANSITION_HELPER = (
        base + FOLLOWER_ACTION_TRANSITION_HELPER_OFFSET
    )
    P2_DEVICE_FILTER_CAVE = base + P2_DEVICE_FILTER_CAVE_OFFSET
    P2_DEVICE_FILTER_STATE = base + P2_DEVICE_FILTER_STATE_OFFSET
    P1_SDL_XINPUT_CAVE = base + P1_SDL_XINPUT_CAVE_OFFSET
    P1_SDL_XINPUT_STATE = base + P1_SDL_XINPUT_STATE_OFFSET
    PAD_BACKGROUND_INPUT_CAVE = base + PAD_BACKGROUND_INPUT_CAVE_OFFSET
    DINPUT_GAMEPAD_FILTER_CAVE = base + DINPUT_GAMEPAD_FILTER_CAVE_OFFSET
    STEAM_GAMEPAD_FILTER_CAVE = base + STEAM_GAMEPAD_FILTER_CAVE_OFFSET
    FOLLOWER_LOCOMOTION_GUARD_CAVE = base + FOLLOWER_LOCOMOTION_GUARD_CAVE_OFFSET
    FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE = (
        base + FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE_OFFSET
    )
    FOLLOWER_ACTION_APPROACH_GATE_CAVE = (
        base + FOLLOWER_ACTION_APPROACH_GATE_CAVE_OFFSET
    )
    FOLLOWER_ACTION_NATIVE_APPROACH_HELPER = (
        base + FOLLOWER_ACTION_NATIVE_APPROACH_HELPER_OFFSET
    )
    FOLLOWER_GATEWAY_TRIGGER_CAVE = base + FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET
    FOLLOWER_GATEWAY_TRIGGER_SCRATCH = base + FOLLOWER_GATEWAY_TRIGGER_SCRATCH_OFFSET
    CAMERA_FOCUS_V2_CAVE = base + CAMERA_FOCUS_V2_CAVE_OFFSET
    CAMERA_COLLISION_FILTER_CAVE = base + CAMERA_COLLISION_FILTER_CAVE_OFFSET
    CAMERA_COLLISION_FILTER_SCRATCH = base + CAMERA_COLLISION_FILTER_SCRATCH_OFFSET


def set_damage_number_theme_base(base):
    global DAMAGE_NUMBER_THEME_BASE
    global DAMAGE_NUMBER_SOURCE_WRAPPER
    global DAMAGE_NUMBER_INIT_WRAPPER
    global DAMAGE_NUMBER_INIT_WRAPPER_ALT
    global DAMAGE_NUMBER_SOURCE_STATE
    global DAMAGE_NUMBER_BRIGHTNESS_STATE

    DAMAGE_NUMBER_THEME_BASE = base
    DAMAGE_NUMBER_SOURCE_WRAPPER = base + DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
    DAMAGE_NUMBER_INIT_WRAPPER = base + DAMAGE_NUMBER_INIT_WRAPPER_OFFSET
    DAMAGE_NUMBER_INIT_WRAPPER_ALT = base + DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET
    DAMAGE_NUMBER_SOURCE_STATE = base + DAMAGE_NUMBER_STATE_OFFSET
    DAMAGE_NUMBER_BRIGHTNESS_STATE = base + DAMAGE_NUMBER_BRIGHTNESS_OFFSET


def set_cave_base(base):
    global COMMAND_BLOCK
    global MOVEMENT_CAVE
    global ACTOR_PHYSICS_HELPER
    global P2_MOVEMENT_MAILBOX
    global P3_MOVEMENT_MAILBOX
    global STEERING_FALLBACK_HELPER
    global KEYBOARD_CAVE
    global KEYBOARD_FILTER_BUDGET
    global FACING_CAVE
    global VISUAL_FACING_CAVE
    global VISUAL_FALLBACK_HELPER
    global CAMERA_CAVE
    global CAMERA_DISTANCE_CAVE1
    global CAMERA_DISTANCE_CAVE2
    global GAUGE_COLOR_CAVE
    global GAUGE_CUSTOM_COLOR_CAVE
    global GAUGE_P3_COLOR_CAVE
    global GAUGE_P3_PALETTE
    global HUD_P3_TEXT_COLOR
    global P3_MENU_THEME_COLOR
    global P3_DAMAGE_NUMBER_COLOR
    global GAUGE_CUSTOM_PALETTE
    global HUD_P2_TEXT_COLOR
    global P2_MENU_THEME_COLOR
    global P2_DAMAGE_NUMBER_COLOR
    global GAUGE_P1_PALETTE
    global HUD_P1_TEXT_COLOR
    global P1_MENU_THEME_COLOR
    global P1_DAMAGE_NUMBER_COLOR
    global GAUGE_P1_COLOR_CAVE
    global HUD_TEXT_COLOR_CAVE
    global FOCUS_PAUSE_CAVE
    global CAMERA_FRAME_STATE
    global CAMERA_SMOOTH_STATE
    global CAMERA_SNAPSHOT_HELPER
    global CAMERA_SMOOTH_CAVE
    global NATIVE_CURSOR_FILTER_CAVE
    global FOLLOWER_ACTION_FILTER_CAVE
    global PHYS_FACING_GUARD_CAVE
    global FOLLOWER_TURN_GUARD_CAVE
    global STEERING_HEADING_CAVE
    global FOLLOWER_LIST_GUARD_CAVE
    global P1_BERSERK_MOVEMENT_STATE
    global P1_BERSERK_MOVEMENT_HELPER
    global FOLLOWER_ARBITRATOR_GUARD_CAVE
    global INTERACTION_ACTOR_FILTER_CAVE
    global INTERACTION_BROADPHASE_FILTER_CAVE
    global INTERACTION_GEOMETRY_ENTRY_CAVE
    global INTERACTION_UPDATE_ENTRY_CAVE
    global INTERACTION_SOURCE_CAVE
    global INTERACTION_ENABLE_GATE_CAVE
    global INTERACTION_INNER_RANGE_GATE_CAVE
    global INTERACTION_OUTER_RANGE_GATE_CAVE
    global INTERACTION_GEOMETRY_RESULT_CAVE
    global INTERACTION_PROXIMITY_SOURCE_CAVE
    global INTERACTION_INDICATOR_SOURCE_CAVE

    COMMAND_BLOCK = base + 0x200
    MOVEMENT_CAVE = base + MOVEMENT_CAVE_OFFSET
    ACTOR_PHYSICS_HELPER = base + ACTOR_PHYSICS_HELPER_OFFSET
    P2_MOVEMENT_MAILBOX = base + P2_MOVEMENT_MAILBOX_OFFSET
    P3_MOVEMENT_MAILBOX = base + P3_MOVEMENT_MAILBOX_OFFSET
    STEERING_FALLBACK_HELPER = base + STEERING_FALLBACK_HELPER_OFFSET
    KEYBOARD_CAVE = base + 0x300
    KEYBOARD_FILTER_BUDGET = base + 0x300 + KEYBOARD_FILTER_BUDGET_CAVE_OFFSET
    FACING_CAVE = base + 0x400
    VISUAL_FACING_CAVE = base + 0x1000
    VISUAL_FALLBACK_HELPER = base + VISUAL_FALLBACK_HELPER_OFFSET
    CAMERA_CAVE = base + 0x600
    CAMERA_DISTANCE_CAVE1 = base + 0x700
    CAMERA_DISTANCE_CAVE2 = base + 0x800
    GAUGE_COLOR_CAVE = base + 0x900
    GAUGE_CUSTOM_COLOR_CAVE = base + 0xA00
    GAUGE_P3_COLOR_CAVE = base + P3_GAUGE_COLOR_CAVE_OFFSET
    GAUGE_P3_PALETTE = base + P3_COMMAND_BLOCK_OFFSET + P3_GAUGE_PALETTE_OFFSET
    HUD_P3_TEXT_COLOR = base + P3_COMMAND_BLOCK_OFFSET + P3_HUD_TEXT_THEME_COLOR_OFFSET
    P3_MENU_THEME_COLOR = base + P3_COMMAND_BLOCK_OFFSET + P3_MENU_THEME_COLOR_OFFSET
    P3_DAMAGE_NUMBER_COLOR = (
        base + P3_COMMAND_BLOCK_OFFSET + P3_DAMAGE_NUMBER_COLOR_OFFSET
    )
    GAUGE_CUSTOM_PALETTE = base + 0xB00
    HUD_P2_TEXT_COLOR = base + 0xB20
    P2_MENU_THEME_COLOR = base + 0xB30
    P2_DAMAGE_NUMBER_COLOR = base + 0xB40
    FOCUS_PAUSE_CAVE = base + 0xC00
    CAMERA_FRAME_STATE = base + camera_frame_sync.CAMERA_FRAME_STATE_OFFSET
    CAMERA_SMOOTH_STATE = CAMERA_FRAME_STATE
    CAMERA_SNAPSHOT_HELPER = base + CAMERA_SNAPSHOT_HELPER_OFFSET
    CAMERA_SMOOTH_CAVE = CAMERA_SNAPSHOT_HELPER
    GAUGE_P1_PALETTE = base + 0xE00
    HUD_P1_TEXT_COLOR = base + 0xE20
    P1_MENU_THEME_COLOR = base + 0xE30
    P1_DAMAGE_NUMBER_COLOR = base + 0xE40
    GAUGE_P1_COLOR_CAVE = base + 0xF00
    HUD_TEXT_COLOR_CAVE = base + 0xF100
    NATIVE_CURSOR_FILTER_CAVE = base + 0x1A00
    FOLLOWER_ACTION_FILTER_CAVE = base + 0x1B00
    PHYS_FACING_GUARD_CAVE = base + 0x1C00
    FOLLOWER_TURN_GUARD_CAVE = base + 0xF000
    STEERING_HEADING_CAVE = base + 0xF400
    FOLLOWER_LIST_GUARD_CAVE = base + 0xF600
    P1_BERSERK_MOVEMENT_STATE = base + P1_BERSERK_MOVEMENT_STATE_OFFSET
    P1_BERSERK_MOVEMENT_HELPER = base + P1_BERSERK_MOVEMENT_HELPER_OFFSET
    FOLLOWER_ARBITRATOR_GUARD_CAVE = base + 0xF800
    INTERACTION_ACTOR_FILTER_CAVE = base + 0xFA00
    INTERACTION_BROADPHASE_FILTER_CAVE = base + 0xFC00
    INTERACTION_GEOMETRY_ENTRY_CAVE = base + 0xFE00
    INTERACTION_UPDATE_ENTRY_CAVE = base + 0xFF00
    INTERACTION_SOURCE_CAVE = base + 0xF200
    INTERACTION_ENABLE_GATE_CAVE = base + 0xF300
    INTERACTION_INNER_RANGE_GATE_CAVE = base + 0xF500
    INTERACTION_OUTER_RANGE_GATE_CAVE = base + 0xF700
    INTERACTION_GEOMETRY_RESULT_CAVE = base + 0xF900
    INTERACTION_PROXIMITY_SOURCE_CAVE = base + 0xFB00
    INTERACTION_INDICATOR_SOURCE_CAVE = base + 0xFD00


def rel32(source, instruction_size, target):
    delta = target - (source + instruction_size)
    if not -(1 << 31) <= delta < (1 << 31):
        raise RuntimeError("relative branch is outside +/-2 GB")
    return struct.pack("<i", delta)


def build_follower_action_native_approach_helper():
    code = bytearray()
    reject_branches = []

    def reject(opcode):
        reject_branches.append(len(code))
        code.extend(opcode + b"\x00")

    code += b"\x45\x85\xd2"
    reject(b"\x75")
    code += b"\x80\xb8\xb4\x06\x00\x00\x04"
    reject(b"\x75")
    code += b"\x83\xb8\x10\x07\x00\x00\x00"
    reject(b"\x74")
    code += b"\x48\x8b\x80\x98\x06\x00\x00"
    code += b"\x48\x85\xc0"
    reject(b"\x74")
    code += b"\xf6\x00\x04"
    reject(b"\x74")
    code += b"\xb8\x01\x00\x00\x00\xc3"
    rejected = len(code)
    code += b"\x31\xc0\xc3"

    for branch in reject_branches:
        displacement = rejected - (branch + 2)
        if not 0 <= displacement <= 0x7F:
            raise RuntimeError("native action-approach helper branch overflow")
        code[branch + 1] = displacement
    return code


def install_follower_action_native_approach_helper(process):
    code = build_follower_action_native_approach_helper()
    if len(code) > 0x80:
        raise RuntimeError(f"native action-approach helper overflow: {len(code):#x}")
    process.write(FOLLOWER_ACTION_NATIVE_APPROACH_HELPER, code)


def build_actor_physics_helper() -> bytearray:
    """Resolve ``actor + 0x10 -> root + 0xB8 -> physics`` in RAX.

    The helper returns CF clear only for a complete non-null chain.  Its slot is
    the audited tail of the movement-cave reservation, not an unowned gap.
    """

    code = bytearray()
    miss_branches: list[int] = []

    def miss_if(opcode: int) -> None:
        position = len(code)
        code.extend(bytes((opcode, 0)))
        miss_branches.append(position)

    code += b"\x48\x85\xc0"
    miss_if(0x74)
    code += b"\x48\x8b\x40\x10"
    code += b"\x48\x85\xc0"
    miss_if(0x74)
    code += b"\x48\x8b\x80\xb8\x00\x00\x00"
    code += b"\x48\x85\xc0"
    miss_if(0x74)
    code += b"\xf8\xc3"
    miss_target = len(code)
    code += b"\xf9\xc3"
    for position in miss_branches:
        displacement = miss_target - (position + 2)
        if not 0 <= displacement <= 0x7F:
            raise RuntimeError("actor-physics helper branch overflow")
        code[position + 1] = displacement
    if len(code) > ACTOR_PHYSICS_HELPER_SIZE:
        raise RuntimeError(f"actor-physics helper overflow: {len(code):#x}")
    return code


def install_actor_physics_helper(process) -> bool:
    code = bytes(build_actor_physics_helper())
    current = process.read(ACTOR_PHYSICS_HELPER, len(code))
    if current == code:
        return False
    if current != b"\x00" * len(code):
        raise RuntimeError(
            "unexpected bytes at shared actor-physics helper: "
            + current.hex(" ")
        )
    process.write(ACTOR_PHYSICS_HELPER, code)
    return True


def build_p1_berserk_movement_helper(command_block: int) -> bytearray:
    del command_block
    state = P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200

    def disp(field: int) -> bytes:
        return struct.pack("<i", state + field)

    code = bytearray(b"\x50")
    native_branches: list[int] = []

    def native(opcode: int) -> None:
        position = len(code)
        code.extend(bytes((opcode, 0)))
        native_branches.append(position)

    code += b"\x41\x83\xbb" + disp(P1_BERSERK_ACTIVE_OFFSET) + b"\x00"
    native(0x74)
    # RAX is [RCX+30h], the actor supplied by the native movement context.
    # Treat the published actor as identity data only; following an arbitrary
    # mailbox pointer here can fault during despawn/status transitions.
    code += b"\x48\x85\xc0"
    native(0x74)
    code += b"\x49\x3b\x83" + disp(P1_BERSERK_ACTOR_OFFSET)
    native(0x75)
    code += b"\x48\x8b\x81\xb8\x00\x00\x00"
    code += b"\x48\x85\xc0"
    native(0x74)
    code += b"\x49\x83\xbb" + disp(P1_BERSERK_PHYS_OFFSET) + b"\x00"
    native(0x74)
    code += b"\x49\x3b\x83" + disp(P1_BERSERK_PHYS_OFFSET)
    native(0x75)
    code += b"\x45\x8b\x93" + disp(P1_BERSERK_HEADING_OFFSET)
    code += b"\x44\x89\x90\xa4\x00\x00\x00"
    code += b"\x45\x8b\x93" + disp(P1_BERSERK_MAGNITUDE_OFFSET)
    code += b"\x44\x89\x90\xd0\x00\x00\x00"
    # Return the exact published magnitude and actor in scratch registers for
    # the shared native-vector arbitration performed by the caller.
    code += b"\x44\x89\xd2"
    code += b"\x45\x31\xd2"
    code += b"\x44\x89\x90\xa0\x00\x00\x00"
    code += b"\x4c\x8b\x14\x24"
    code += b"\x58\xf9\xc3"
    native_target = len(code)
    code += b"\x58\xf8\xc3"
    for position in native_branches:
        displacement = native_target - (position + 2)
        if not 0 <= displacement <= 0x7F:
            raise RuntimeError("P1 Berserk helper branch overflow")
        code[position + 1] = displacement
    return code


def install_p1_berserk_movement_helper(process):
    code = build_p1_berserk_movement_helper(COMMAND_BLOCK)
    if len(code) > P1_BERSERK_MOVEMENT_HELPER_SIZE:
        raise RuntimeError(f"P1 Berserk helper overflow: {len(code):#x}")
    process.write(P1_BERSERK_MOVEMENT_HELPER, code)


def build_movement_cave(command_block: int, prologue: bytes) -> bytearray:
    p2_mailbox = P2_MOVEMENT_MAILBOX_OFFSET - 0x200
    p3_mailbox = P3_MOVEMENT_MAILBOX_OFFSET - 0x200

    def disp32(value: int) -> bytes:
        return struct.pack("<i", value)

    body = bytearray()
    fixups: list[tuple[int, str]] = []
    short_fixups: list[tuple[int, str]] = []
    labels: dict[str, int] = {}

    def emit(*parts: bytes) -> None:
        for part in parts:
            body.extend(part)

    NEAR_FORM = {
        b"\x72": b"\x0f\x82",
        b"\x74": b"\x0f\x84",
        b"\x75": b"\x0f\x85",
        b"\xeb": b"\xe9",
    }

    def jump(opcode: bytes, label: str) -> None:
        body.extend(NEAR_FORM[opcode])
        fixups.append((len(body), label))
        body.extend(b"\x00\x00\x00\x00")

    def jump_near(label: str) -> None:
        jump(b"\xeb", label)

    def jump_short(opcode: bytes, label: str) -> None:
        if len(opcode) != 1:
            raise ValueError("short branch opcode must be one byte")
        short_fixups.append((len(body), label))
        body.extend(opcode + b"\x00")

    def mark(label: str) -> None:
        labels[label] = len(body)

    def release_native_approach() -> None:
        if not FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE:
            return
        # The helper expects actor in RAX and magnitude bits in R10D. Reload the
        # actor from the already-validated native context, never from mailbox
        # storage. Preserve both selected phys and the initial sequence.
        emit(b"\x41\x52\x50\x44\x8b\xd2")
        emit(b"\x48\x8b\x44\x24\x10")
        call_site = MOVEMENT_CAVE + len(body)
        emit(
            b"\xe8"
            + rel32(
                call_site,
                5,
                FOLLOWER_ACTION_NATIVE_APPROACH_HELPER,
            )
        )
        emit(b"\x84\xc0\x58\x41\x5a")
        jump(b"\x75", "done")

    def try_mailbox(mailbox: int, next_label: str) -> None:
        sequence = mailbox + movement_mailbox.SEQUENCE_OFFSET
        active = mailbox + movement_mailbox.ACTIVE_OFFSET
        actor = mailbox + movement_mailbox.ACTOR_OFFSET
        phys = mailbox + movement_mailbox.PHYS_OFFSET
        heading = mailbox + movement_mailbox.HEADING_OFFSET
        magnitude = mailbox + movement_mailbox.MAGNITUDE_OFFSET

        # R10D holds the first sequence, R9D/EDX hold a complete polar sample.
        emit(b"\x45\x8b\x93" + disp32(sequence))
        emit(b"\x45\x85\xd2")
        jump(b"\x74", next_label)
        emit(b"\x41\xf6\xc2\x01")
        jump(b"\x75", next_label)
        emit(b"\x41\x83\xbb" + disp32(active) + b"\x00")
        jump(b"\x74", next_label)
        emit(b"\x48\x8b\x04\x24")
        emit(b"\x48\x85\xc0")
        jump(b"\x74", next_label)
        emit(b"\x49\x3b\x83" + disp32(actor))
        jump(b"\x75", next_label)
        emit(b"\x48\x8b\x81\xb8\x00\x00\x00")
        emit(b"\x48\x85\xc0")
        jump(b"\x74", next_label)
        emit(b"\x49\x3b\x83" + disp32(phys))
        jump(b"\x75", next_label)
        emit(b"\x45\x8b\x8b" + disp32(heading))
        emit(b"\x41\x8b\x93" + disp32(magnitude))
        release_native_approach()
        emit(b"\x45\x3b\x93" + disp32(sequence))
        jump(b"\x75", next_label)

        # Only a stable even sequence can reach the stores.  An update in
        # progress leaves the last native locomotion sample untouched.
        emit(b"\x44\x89\x88\xa4\x00\x00\x00")
        emit(b"\x89\x90\xd0\x00\x00\x00")
        emit(b"\x45\x31\xd2")
        emit(b"\x44\x89\x90\xa0\x00\x00\x00")
        # The sequence register is no longer live. Carry the native actor into
        # common root-motion arbitration; mailbox.actor remains comparison-only.
        emit(b"\x4c\x8b\x14\x24")
        jump_near("manual_done")

    emit(b"\x50\x41\x53\x41\x52\x41\x51\x52")
    emit(b"\x49\xbb" + struct.pack("<Q", command_block))
    emit(b"\x48\x8b\x41\x30")
    # Preserve the native actor for the complete bounded selection.  Mailbox
    # identity is compared against this value and can never become a pointer
    # source.  The common arbitration revalidates it against RCX+30h.
    emit(b"\x50")
    call_site = MOVEMENT_CAVE + len(body)
    emit(b"\xe8" + rel32(call_site, 5, P1_BERSERK_MOVEMENT_HELPER))
    jump(b"\x72", "manual_done")
    emit(b"\x41\x83\x7b\x38\x00")
    jump(b"\x74", "done")
    if MOVEMENT_HOLDS_LEADER:
        emit(b"\x48\x8b\x04\x24")
        emit(b"\x48\x85\xc0")
        jump(b"\x74", "not_leader")
        emit(b"\x49\x3b\x43\x18")
        jump(b"\x75", "not_leader")
        emit(b"\x48\x8b\x81\xb8\x00\x00\x00")
        emit(b"\x48\x85\xc0")
        jump(b"\x74", "done")
        emit(b"\x45\x31\xd2")
        emit(b"\x44\x89\x90\xd0\x00\x00\x00")
        emit(b"\x44\x89\x90\xa0\x00\x00\x00")
        jump_near("done")
        mark("not_leader")
        emit(b"\x48\x8b\x41\x30")

    try_mailbox(p2_mailbox, "check_p3")
    mark("check_p3")
    try_mailbox(p3_mailbox, "done")
    mark("manual_done")
    # FUN_002e4380 selects R9's native root-motion vector whenever R8D is
    # non-zero and then ignores phys+A4/D0/A0.  Empty root-motion records can
    # remain selected after follower AI relinquishes an actor, which used to
    # disable manual movement indefinitely.  Override that stale selector only
    # for actual manual input, outside execution animation, and only when its
    # XYZ vector is exactly +/-0.  Real attack, knockback and scripted motion
    # remains authoritative.  Rejected/native paths preserve the original R8D.
    emit(b"\x4c\x3b\x51\x30")
    jump_short(b"\x75", "done")
    emit(b"\x4d\x85\xd2")
    jump_short(b"\x74", "done")
    emit(b"\xf7\xc2\xff\xff\xff\x7f")
    jump_short(b"\x74", "done")
    emit(b"\x45\x85\xc0")
    jump_short(b"\x74", "done")
    emit(b"\x49\x0f\xba\x22\x2e")
    jump_short(b"\x72", "done")
    emit(b"\x48\x8b\x44\x24\x10")
    emit(b"\x48\x85\xc0")
    jump_short(b"\x74", "done")
    emit(b"\x44\x8b\x08")
    emit(b"\x44\x0b\x48\x04")
    emit(b"\x44\x0b\x48\x08")
    emit(b"\x41\x81\xe1\xff\xff\xff\x7f")
    jump_short(b"\x75", "done")
    emit(b"\x45\x31\xc0")
    mark("done")
    emit(b"\x58")
    emit(b"\x5a\x41\x59\x41\x5a\x41\x5b\x58")
    emit(prologue)

    for position, label in fixups:
        struct.pack_into("<i", body, position, labels[label] - (position + 4))
    for position, label in short_fixups:
        displacement = labels[label] - (position + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError(f"short movement branch overflow: {label}")
        struct.pack_into("<b", body, position + 1, displacement)
    return body


def _preflight_static_detour(
    process,
    *,
    label: str,
    site: int,
    original: bytes,
    patch: bytes,
    cave_address: int,
    cave_size: int,
    cave: bytes,
    current_root: bytes | None = None,
) -> str:
    """Classify one immutable root/cave pair without mutating the process.

    A stock root is installable only into an entirely zeroed reserved slot.  An
    active root is adoptable only when the complete reserved slot is the exact
    image this build would have produced.  Prepared, legacy, partial and foreign
    topologies are deliberately not repaired in a live game.
    """

    original = bytes(original)
    patch = bytes(patch)
    cave = bytes(cave)
    cave_size = int(cave_size)
    if len(original) != len(patch):
        raise RuntimeError(f"{label} root size differs from its stock image")
    if len(cave) > cave_size:
        raise RuntimeError(f"{label} cave overflow: {len(cave):#x}")

    current = (
        process.read(site, len(original))
        if current_root is None
        else bytes(current_root)
    )
    if current not in (original, patch):
        raise RuntimeError(
            f"unexpected bytes at {label} hook site: {current.hex(' ')}"
        )

    current_cave = process.read(cave_address, cave_size)
    if len(current_cave) != cave_size:
        raise RuntimeError(f"short read from {label} cave at {cave_address:#x}")
    expected_image = cave + b"\x00" * (cave_size - len(cave))
    if current == patch:
        if current_cave != expected_image:
            raise RuntimeError(
                f"active {label} cave mismatch at {cave_address:#x}; "
                "restart the game"
            )
        recorder = getattr(process, "record_executable_region", None)
        if recorder is not None:
            recorder(cave_address, cave_size)
        return "adopt"

    if current_cave != b"\x00" * cave_size:
        raise RuntimeError(
            f"non-empty {label} cave with a stock root at {cave_address:#x}; "
            "restart the game"
        )
    return "fresh"


def _preflight_process_lifetime_detour(
    process,
    *,
    label: str,
    site: int,
    original: bytes,
    patch: bytes,
    regions,
) -> tuple[str, tuple[tuple[int, bytes], ...]]:
    """Classify an immutable root and all code it can reach, read-only.

    ``regions`` contains ``(name, address, reservation_size, image, shared)``.
    A shared image may already have been prepared by another fresh detour in the
    same transaction.  Unique prepared caves are intentionally rejected: only
    an exact active topology may be adopted after a root has been published.
    """

    original = bytes(original)
    patch = bytes(patch)
    if len(original) != len(patch):
        raise RuntimeError(f"{label} root size differs from its stock image")
    current_root = process.read(site, len(original))
    if current_root not in (original, patch):
        raise RuntimeError(
            f"unexpected bytes at {label} hook site: {current_root.hex(' ')}"
        )

    inspected = []
    for region_label, address, reservation_size, image, shared in regions:
        image = bytes(image)
        reservation_size = int(reservation_size)
        if len(image) > reservation_size:
            raise RuntimeError(
                f"{region_label} overflow: {len(image):#x} > {reservation_size:#x}"
            )
        expected = image.ljust(reservation_size, b"\x00")
        current = process.read(address, reservation_size)
        if len(current) != reservation_size:
            raise RuntimeError(
                f"short read from {region_label} at {int(address):#x}"
            )
        inspected.append(
            (region_label, int(address), image, expected, current, bool(shared))
        )

    if current_root == patch:
        mismatch = next(
            (
                (region_label, address)
                for region_label, address, _image, expected, current, _shared in inspected
                if current != expected
            ),
            None,
        )
        if mismatch is not None:
            region_label, address = mismatch
            raise RuntimeError(
                f"active {label} {region_label} mismatch at {address:#x}; "
                "restart the game"
            )
        recorder = getattr(process, "record_executable_region", None)
        if recorder is not None:
            for _region_label, address, _image, expected, _current, _shared in inspected:
                recorder(address, len(expected))
        return "adopt", ()

    pending = []
    for region_label, address, image, expected, current, shared in inspected:
        if shared and current == expected:
            continue
        if current != b"\x00" * len(expected):
            raise RuntimeError(
                f"non-empty {label} {region_label} with a stock root at "
                f"{address:#x}; restart the game"
            )
        pending.append((address, image))
    return "fresh", tuple(pending)


def _publish_process_lifetime_detour(process, *, site, patch, mode, pending):
    """Publish complete immutable regions before the one-way root commit."""

    if mode == "adopt":
        return False
    for address, image in pending:
        process.write(address, image)
    process.patch_code(site, patch)
    return True


def install_movement_override(process):
    original = b"\x40\x53\x56\x41\x56"
    patch = b"\xe9" + rel32(MOVEMENT_HOOK_SITE, 5, MOVEMENT_CAVE)
    legacy_cave = COMMAND_BLOCK - 0x200 + 0x100
    legacy_patch = b"\xe9" + rel32(MOVEMENT_HOOK_SITE, 5, legacy_cave)

    cave = build_movement_cave(COMMAND_BLOCK, original)
    jump_site = MOVEMENT_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, MOVEMENT_HOOK_RETURN)

    helper_boundary = ACTOR_PHYSICS_HELPER_OFFSET - MOVEMENT_CAVE_OFFSET
    if len(cave) > helper_boundary:
        raise RuntimeError(f"movement cave overflow: {len(cave):#x} bytes")

    current = process.read(MOVEMENT_HOOK_SITE, len(original))
    if current == legacy_patch:
        raise RuntimeError(
            "legacy movement hook found; restart the game before upgrading"
        )
    mode = _preflight_static_detour(
        process,
        label="movement",
        site=MOVEMENT_HOOK_SITE,
        original=original,
        patch=patch,
        cave_address=MOVEMENT_CAVE,
        cave_size=helper_boundary,
        cave=cave,
        current_root=current,
    )
    if mode == "adopt":
        return False

    process.write(MOVEMENT_CAVE, cave)
    process.patch_code(MOVEMENT_HOOK_SITE, patch)
    return True


def install_p2_interaction_enable_gate(process):
    patch = (
        b"\xe9"
        + rel32(INTERACTION_ENABLE_GATE_SITE, 5, INTERACTION_ENABLE_GATE_CAVE)
        + b"\x90" * (len(INTERACTION_ENABLE_GATE_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_ENABLE_GATE_SITE, len(INTERACTION_ENABLE_GATE_ORIGINAL)
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_ENABLE_GATE_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction enable gate: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x85\xdb"
    native_awake = len(cave)
    cave += b"\x75\x00"
    cave += b"\x50"
    cave += b"\x48\xb8" + struct.pack("<Q", DUAL_INTERACTION_STATE)
    cave += b"\x83\x38\x00"
    native_scan = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78\x08\x00"
    inactive = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x48\x83\x38\x00"
    no_p2 = len(cave)
    cave += b"\x74\x00"
    cave += b"\x58"
    accept_jump = INTERACTION_ENABLE_GATE_CAVE + len(cave)
    cave += b"\xe9" + rel32(accept_jump, 5, INTERACTION_ENABLE_GATE_ACCEPT)
    reject = len(cave)
    cave += b"\x58"
    reject_jump = INTERACTION_ENABLE_GATE_CAVE + len(cave)
    cave += b"\xe9" + rel32(reject_jump, 5, INTERACTION_ENABLE_GATE_REJECT)
    native_accept = len(cave)
    direct_accept_jump = INTERACTION_ENABLE_GATE_CAVE + len(cave)
    cave += b"\xe9" + rel32(direct_accept_jump, 5, INTERACTION_ENABLE_GATE_ACCEPT)

    for branch, destination in (
        (native_awake, native_accept),
        (native_scan, reject),
        (inactive, reject),
        (no_p2, reject),
    ):
        displacement = destination - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("interaction-enable short branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x100:
        raise RuntimeError(f"interaction-enable cave overflow: {len(cave):#x}")
    process.write(INTERACTION_ENABLE_GATE_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_ENABLE_GATE_SITE, patch)


def install_p2_interaction_range_gate(
    process,
    *,
    site,
    original,
    cave_address,
    native_mask,
    accept_address,
    reject_address,
):
    patch = b"\xe9" + rel32(site, 5, cave_address) + b"\x90" * (len(original) - 5)
    current = process.read(site, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            f"unexpected bytes at interaction range gate {site:#x}: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\xf6\x46\x5d" + bytes((native_mask,))
    native_range = len(cave)
    cave += b"\x75\x00"
    cave += b"\x50"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78\x08\x00"
    inactive = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x48\x8b\x00"
    cave += b"\x48\x39\x45\x8f"
    wrong_source = len(cave)
    cave += b"\x75\x00"
    cave += b"\x58"
    accept_jump = cave_address + len(cave)
    cave += b"\xe9" + rel32(accept_jump, 5, accept_address)
    reject = len(cave)
    cave += b"\x58"
    reject_jump = cave_address + len(cave)
    cave += b"\xe9" + rel32(reject_jump, 5, reject_address)
    native_accept = len(cave)
    direct_accept_jump = cave_address + len(cave)
    cave += b"\xe9" + rel32(direct_accept_jump, 5, accept_address)

    for branch, destination in (
        (native_range, native_accept),
        (inactive, reject),
        (wrong_source, reject),
    ):
        displacement = destination - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("interaction-range short branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x100:
        raise RuntimeError(f"interaction-range cave overflow: {len(cave):#x}")
    process.write(cave_address, cave)
    if not already_installed:
        process.patch_code(site, patch)


def install_p2_interaction_range_gates(process):
    install_p2_interaction_range_gate(
        process,
        site=INTERACTION_INNER_RANGE_GATE_SITE,
        original=INTERACTION_INNER_RANGE_GATE_ORIGINAL,
        cave_address=INTERACTION_INNER_RANGE_GATE_CAVE,
        native_mask=0x04,
        accept_address=INTERACTION_INNER_RANGE_GATE_ACCEPT,
        reject_address=INTERACTION_INNER_RANGE_GATE_REJECT,
    )
    install_p2_interaction_range_gate(
        process,
        site=INTERACTION_OUTER_RANGE_GATE_SITE,
        original=INTERACTION_OUTER_RANGE_GATE_ORIGINAL,
        cave_address=INTERACTION_OUTER_RANGE_GATE_CAVE,
        native_mask=0x02,
        accept_address=INTERACTION_OUTER_RANGE_GATE_ACCEPT,
        reject_address=INTERACTION_OUTER_RANGE_GATE_REJECT,
    )


def install_p2_interaction_geometry_result_logger(process):
    patch = (
        b"\xe9"
        + rel32(INTERACTION_GEOMETRY_RESULT_SITE, 5, INTERACTION_GEOMETRY_RESULT_CAVE)
        + b"\x90" * (len(INTERACTION_GEOMETRY_RESULT_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_GEOMETRY_RESULT_SITE, len(INTERACTION_GEOMETRY_RESULT_ORIGINAL)
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_GEOMETRY_RESULT_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction geometry result: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x50"
    cave += b"\x48\xb8" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x48\x3b\x18"
    not_p2 = len(cave)
    cave += b"\x75\x00"
    for opcode, data_offset in (
        (b"\x44\x89\x25", INTERACTION_GEOMETRY_RESULT_R12),
        (b"\x44\x89\x2d", INTERACTION_GEOMETRY_RESULT_R13),
    ):
        instruction = INTERACTION_GEOMETRY_RESULT_CAVE + len(cave)
        target = INTERACTION_GEOMETRY_RESULT_CAVE + data_offset
        cave += opcode + struct.pack("<i", target - (instruction + 7))
    instruction = INTERACTION_GEOMETRY_RESULT_CAVE + len(cave)
    target = INTERACTION_GEOMETRY_RESULT_CAVE + INTERACTION_GEOMETRY_RESULT_COUNT
    cave += b"\x48\xff\x05" + struct.pack("<i", target - (instruction + 7))
    logged = len(cave)
    displacement = logged - (not_p2 + 2)
    if not 0 <= displacement <= 0x7F:
        raise RuntimeError("interaction-geometry-result branch overflow")
    cave[not_p2 + 1] = displacement
    cave += b"\x58"
    cave += INTERACTION_GEOMETRY_RESULT_ORIGINAL
    jump_site = INTERACTION_GEOMETRY_RESULT_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, INTERACTION_GEOMETRY_RESULT_RETURN)

    if len(cave) >= INTERACTION_GEOMETRY_RESULT_R12:
        raise RuntimeError("interaction-geometry-result code overlaps data")
    cave += b"\x00" * (INTERACTION_GEOMETRY_RESULT_R12 - len(cave))
    cave += b"\x00" * 16
    process.write(INTERACTION_GEOMETRY_RESULT_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_GEOMETRY_RESULT_SITE, patch)


def _build_live_interaction_source_cave(
    cave_address,
    return_address,
    *,
    output_register,
    trailing_original=b"",
):
    """Resolve the private scan source from its stable handle at point of use."""

    if output_register not in ("rax", "rsi"):
        raise ValueError(f"unsupported interaction source register: {output_register}")

    code = bytearray()
    labels = {}
    fixups = []
    preserve_rax = output_register != "rax"
    frame_size = 0x90 if preserve_rax else 0x88

    def near_jcc(opcode, label):
        code.extend(b"\x0f" + bytes((opcode,)))
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    def near_jmp(label):
        code.append(0xE9)
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    # A mid-function hook cannot rely on volatile GPR/SIMD state surviving the
    # native resolver.  Preserve every ABI-volatile input other than RAX when
    # RAX is itself the overwritten output, plus flags that the stock MOV left
    # untouched.  The frame also supplies aligned Win64 shadow space.
    code += b"\x9c"
    if preserve_rax:
        code += b"\x50"
    code += b"\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
    code += b"\x48\x81\xec" + struct.pack("<I", frame_size)
    for index in range(6):
        code += b"\xf3\x0f\x7f" + bytes((0x44 | (index << 3), 0x24, 0x20 + index * 0x10))

    code += b"\x49\xbb" + struct.pack("<Q", DUAL_INTERACTION_STATE)
    code += b"\x45\x8b\x13"
    code += b"\x41\x83\xfa\x01"
    near_jcc(0x84, "p2")
    code += b"\x41\x83\xfa\x02"
    near_jcc(0x84, "p3")
    near_jmp("native")

    labels["p2"] = len(code)
    code += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    code += b"\x41\x8b\x4b" + bytes((P2_LOOT_HANDLE_OFFSET,))
    near_jmp("resolve")

    labels["p3"] = len(code)
    code += b"\x49\xbb" + struct.pack("<Q", third_player_block())
    code += b"\x41\x8b\x4b" + bytes((P3_LOOT_HANDLE_OFFSET,))

    labels["resolve"] = len(code)
    code += b"\x85\xc9"
    near_jcc(0x84, "native")
    call_site = cave_address + len(code)
    code += b"\xe8" + rel32(call_site, 5, ENTITY_ENTRY_RESOLVER)
    code += b"\x48\x85\xc0"
    near_jcc(0x84, "native")
    near_jmp("selected")

    labels["native"] = len(code)
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_SOURCE_GLOBAL)
    code += b"\x49\x8b\x03"

    labels["selected"] = len(code)
    if preserve_rax:
        code += b"\x48\x89\x84\x24\x80\x00\x00\x00"
    for index in reversed(range(6)):
        code += b"\xf3\x0f\x6f" + bytes((0x44 | (index << 3), 0x24, 0x20 + index * 0x10))
    if output_register == "rsi":
        code += b"\x48\x8b\xb4\x24\x80\x00\x00\x00"
    code += b"\x48\x81\xc4" + struct.pack("<I", frame_size)
    code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59"
    if preserve_rax:
        code += b"\x58"
    code += b"\x9d"
    code += bytes(trailing_original)
    jump_site = cave_address + len(code)
    code += b"\xe9" + rel32(jump_site, 5, return_address)

    for offset, label in fixups:
        source_after = cave_address + offset + 4
        target = cave_address + labels[label]
        struct.pack_into("<i", code, offset, target - source_after)
    if len(code) > 0x100:
        raise RuntimeError(f"interaction live-source cave overflow: {len(code):#x}")
    return bytes(code)


def install_p2_interaction_source(process):
    patch = (
        b"\xe9"
        + rel32(INTERACTION_SOURCE_SITE, 5, INTERACTION_SOURCE_CAVE)
        + b"\x90" * (len(INTERACTION_SOURCE_ORIGINAL) - 5)
    )
    current = process.read(INTERACTION_SOURCE_SITE, len(INTERACTION_SOURCE_ORIGINAL))
    already_installed = current == patch
    if not already_installed and current != INTERACTION_SOURCE_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction source: {current.hex(' ')}"
        )

    cave = _build_live_interaction_source_cave(
        INTERACTION_SOURCE_CAVE,
        INTERACTION_SOURCE_RETURN,
        output_register="rax",
        trailing_original=INTERACTION_SOURCE_ORIGINAL[7:],
    )
    process.write(INTERACTION_SOURCE_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_SOURCE_SITE, patch)


def install_p2_interaction_proximity_source(process):
    patch = (
        b"\xe9"
        + rel32(
            INTERACTION_PROXIMITY_SOURCE_SITE,
            5,
            INTERACTION_PROXIMITY_SOURCE_CAVE,
        )
        + b"\x90" * (len(INTERACTION_PROXIMITY_SOURCE_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_PROXIMITY_SOURCE_SITE,
        len(INTERACTION_PROXIMITY_SOURCE_ORIGINAL),
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_PROXIMITY_SOURCE_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction proximity source: {current.hex(' ')}"
        )

    if already_installed:
        process.patch_code(
            INTERACTION_PROXIMITY_SOURCE_SITE,
            INTERACTION_PROXIMITY_SOURCE_ORIGINAL,
        )

    cave = _build_live_interaction_source_cave(
        INTERACTION_PROXIMITY_SOURCE_CAVE,
        INTERACTION_PROXIMITY_SOURCE_RETURN,
        output_register="rsi",
    )
    process.write(INTERACTION_PROXIMITY_SOURCE_CAVE, cave)
    process.patch_code(INTERACTION_PROXIMITY_SOURCE_SITE, patch)


def install_p2_interaction_indicator_source(process):
    patch = (
        b"\xe9"
        + rel32(
            INTERACTION_INDICATOR_SOURCE_SITE,
            5,
            INTERACTION_INDICATOR_SOURCE_CAVE,
        )
        + b"\x90" * (len(INTERACTION_INDICATOR_SOURCE_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_INDICATOR_SOURCE_SITE,
        len(INTERACTION_INDICATOR_SOURCE_ORIGINAL),
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_INDICATOR_SOURCE_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction indicator source: {current.hex(' ')}"
        )

    if already_installed:
        process.patch_code(
            INTERACTION_INDICATOR_SOURCE_SITE,
            INTERACTION_INDICATOR_SOURCE_ORIGINAL,
        )

    cave = bytearray()
    cave += b"\x49\xbe" + struct.pack("<Q", DUAL_INTERACTION_STATE)
    cave += b"\x41\x83\x3e\x00"
    native = len(cave)
    cave += b"\x74\x00"
    cave += b"\x49\xbe" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x4d\x8b\x36"
    cave += b"\x4d\x85\xf6"
    private_ready = len(cave)
    cave += b"\x75\x00"
    native_load = len(cave)
    native_instruction = INTERACTION_INDICATOR_SOURCE_CAVE + len(cave)
    cave += b"\x4c\x8b\x35" + rel32(
        native_instruction, 7, NATIVE_INTERACTION_SOURCE_GLOBAL
    )
    common = len(cave)
    jump_site = INTERACTION_INDICATOR_SOURCE_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, INTERACTION_INDICATOR_SOURCE_RETURN)

    for branch, destination in ((native, native_load), (private_ready, common)):
        displacement = destination - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("interaction-indicator short branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x100:
        raise RuntimeError(f"interaction-indicator cave overflow: {len(cave):#x}")
    process.write(INTERACTION_INDICATOR_SOURCE_CAVE, cave)
    process.patch_code(INTERACTION_INDICATOR_SOURCE_SITE, patch)


def restore_p2_interaction_indicator_source(process):
    patch = (
        b"\xe9"
        + rel32(
            INTERACTION_INDICATOR_SOURCE_SITE,
            5,
            INTERACTION_INDICATOR_SOURCE_CAVE,
        )
        + b"\x90" * (len(INTERACTION_INDICATOR_SOURCE_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_INDICATOR_SOURCE_SITE,
        len(INTERACTION_INDICATOR_SOURCE_ORIGINAL),
    )
    if current == patch:
        process.patch_code(
            INTERACTION_INDICATOR_SOURCE_SITE,
            INTERACTION_INDICATOR_SOURCE_ORIGINAL,
        )
    elif current != INTERACTION_INDICATOR_SOURCE_ORIGINAL:
        raise RuntimeError(
            "unexpected bytes at interaction indicator source restore: "
            f"{current.hex(' ')}"
        )


def _follower_npc_guard_patch(site, cave, original):
    return b"\xe9" + rel32(site, 5, cave) + b"\x90" * (len(original) - 5)


def _build_follower_npc_entry_guard_cave(active_state_address=None):
    state_address = (
        DUAL_INTERACTION_STATE
        if active_state_address is None
        else int(active_state_address)
    )
    cave = bytearray(b"\x48\xb8" + struct.pack("<Q", state_address))
    cave += b"\x83\x38\x00"
    stock = len(cave)
    cave += b"\x74\x00"

    cave += b"\x48\x8b\xfa"
    jump = FOLLOWER_NPC_ENTRY_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump, 5, FOLLOWER_NPC_ENTRY_GUARD_RETURN)
    stock_target = len(cave)
    cave += FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL
    jump = FOLLOWER_NPC_ENTRY_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump, 5, FOLLOWER_NPC_ENTRY_GUARD_RETURN)
    cave[stock + 1] = (stock_target - (stock + 2)) & 0xFF
    return bytes(cave)


def _build_follower_npc_accept_guard_cave(active_state_address=None):
    state_address = (
        DUAL_INTERACTION_STATE
        if active_state_address is None
        else int(active_state_address)
    )
    cave = bytearray(b"\x48\xb8" + struct.pack("<Q", state_address))
    cave += b"\x83\x38\x00"
    private = len(cave)
    cave += b"\x75\x00"
    cave += FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL
    jump = FOLLOWER_NPC_ACCEPT_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump, 5, FOLLOWER_NPC_ACCEPT_GUARD_RETURN)
    private_target = len(cave)
    jump = FOLLOWER_NPC_ACCEPT_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump, 5, FOLLOWER_NPC_ACCEPT_GUARD_PRIVATE_RETURN)
    cave[private + 1] = (private_target - (private + 2)) & 0xFF
    return bytes(cave)


def _build_follower_npc_cleanup_guard_cave(active_state_address=None):
    state_address = (
        DUAL_INTERACTION_STATE
        if active_state_address is None
        else int(active_state_address)
    )
    cave = bytearray(b"\x48\xb8" + struct.pack("<Q", state_address))
    cave += b"\x83\x38\x00"
    private = len(cave)
    cave += b"\x75\x00"
    cave += FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL
    jump = FOLLOWER_NPC_CLEANUP_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump, 5, FOLLOWER_NPC_CLEANUP_GUARD_RETURN)
    private_target = len(cave)
    jump = FOLLOWER_NPC_CLEANUP_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump, 5, FOLLOWER_NPC_CLEANUP_GUARD_PRIVATE_RETURN)
    cave[private + 1] = (private_target - (private + 2)) & 0xFF
    return bytes(cave)


def _follower_npc_private_object_guard_specs(active_state_address=None):
    raw_specs = (
        (
            FOLLOWER_NPC_ENTRY_GUARD_SITE,
            FOLLOWER_NPC_ENTRY_GUARD_CAVE,
            FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL,
            _build_follower_npc_entry_guard_cave(active_state_address),
            FOLLOWER_NPC_ENTRY_GUARD_CAVE_SIZE,
        ),
        (
            FOLLOWER_NPC_ACCEPT_GUARD_SITE,
            FOLLOWER_NPC_ACCEPT_GUARD_CAVE,
            FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL,
            _build_follower_npc_accept_guard_cave(active_state_address),
            FOLLOWER_NPC_ACCEPT_GUARD_CAVE_SIZE,
        ),
        (
            FOLLOWER_NPC_CLEANUP_GUARD_SITE,
            FOLLOWER_NPC_CLEANUP_GUARD_CAVE,
            FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL,
            _build_follower_npc_cleanup_guard_cave(active_state_address),
            FOLLOWER_NPC_CLEANUP_GUARD_CAVE_SIZE,
        ),
    )
    specs = []
    for index, (site, cave_address, original, cave, cave_size) in enumerate(raw_specs):
        marker = FOLLOWER_NPC_GUARD_TOPOLOGY_MAGIC + bytes((index,))
        if len(cave) + len(marker) > cave_size:
            raise RuntimeError(
                f"follower NPC guard cave overflow at {cave_address:#x}: "
                f"{len(cave):#x}/{cave_size:#x}"
            )
        image = bytearray(cave_size)
        image[: len(cave)] = cave
        image[-len(marker) :] = marker
        patch = _follower_npc_guard_patch(site, cave_address, original)
        specs.append((site, cave_address, original, bytes(image), patch))
    return tuple(specs)


def install_follower_npc_private_object_guards(
    process,
    *,
    active_state_address=None,
):
    specs = _follower_npc_private_object_guard_specs(active_state_address)
    snapshots = tuple(
        (
            process.read(site, len(original)),
            process.read(cave_address, len(cave_image)),
        )
        for site, cave_address, original, cave_image, _patch in specs
    )
    roots_stock = all(
        current_root == original
        for (current_root, _current_cave), (_site, _cave, original, _image, _patch)
        in zip(snapshots, specs, strict=True)
    )
    roots_active = all(
        current_root == patch
        for (current_root, _current_cave), (_site, _cave, _original, _image, patch)
        in zip(snapshots, specs, strict=True)
    )
    if roots_active:
        for (
            (_current_root, current_cave),
            (site, cave_address, _original, cave_image, _patch),
        ) in zip(snapshots, specs, strict=True):
            if current_cave != cave_image:
                raise RuntimeError(
                    f"active follower NPC guard cave mismatch at {cave_address:#x} "
                    f"(root {site:#x})"
                )
        return False
    if not roots_stock:
        raise RuntimeError(
            "partial/foreign follower NPC guard topology found; restart the game"
        )
    for (_current_root, current_cave), (_site, cave_address, *_rest) in zip(
        snapshots, specs, strict=True
    ):
        if any(current_cave):
            raise RuntimeError(
                f"stock follower NPC guard has nonzero cave slot at {cave_address:#x}"
            )

    # Complete every immutable cave while all roots are stock. Once the first
    # root is published the topology is process-lifetime and never rolled back.
    for _site, cave_address, _original, cave_image, _patch in specs:
        process.write(cave_address, cave_image)
    for site, cave_address, original, cave_image, _patch in specs:
        if process.read(cave_address, len(cave_image)) != cave_image:
            raise RuntimeError(
                f"follower NPC guard cave changed before commit at {cave_address:#x}"
            )
        if process.read(site, len(original)) != original:
            raise RuntimeError(
                f"follower NPC guard root changed before commit at {site:#x}"
            )
    for site, _cave_address, _original, _cave_image, patch in specs:
        process.patch_code(site, patch)
    return True


def restore_follower_npc_private_object_guards(
    process,
    *,
    active_state_address=None,
):
    specs = _follower_npc_private_object_guard_specs(active_state_address)
    snapshots = tuple(
        (
            process.read(site, len(original)),
            process.read(cave_address, len(cave_image)),
        )
        for site, cave_address, original, cave_image, _patch in specs
    )
    roots_stock = all(
        current_root == original
        for (current_root, _current_cave), (_site, _cave, original, _image, _patch)
        in zip(snapshots, specs, strict=True)
    )
    roots_active = all(
        current_root == patch
        for (current_root, _current_cave), (_site, _cave, _original, _image, patch)
        in zip(snapshots, specs, strict=True)
    )
    if roots_stock:
        return not any(any(current_cave) for _current_root, current_cave in snapshots)
    if not roots_active:
        return False
    if any(
        current_cave != cave_image
        for (_current_root, current_cave), (
            _site,
            _cave,
            _original,
            cave_image,
            _patch,
        ) in zip(snapshots, specs, strict=True)
    ):
        return False

    # Static roots and caves are process-lifetime. Only disarm the dynamic guard
    # state after proving that this exact topology owns the referenced address.
    state_address = (
        DUAL_INTERACTION_STATE
        if active_state_address is None
        else int(active_state_address)
    )
    process.write(state_address, b"\x00" * 4)
    return True


def _build_follower_npc_exact_scan_cave():
    state = DUAL_INTERACTION_STATE
    active_entry = interaction_scan_entry_address()
    p2_snapshot = state + DUAL_INTERACTION_P2_SNAPSHOT_OFFSET
    p3_snapshot = state + DUAL_INTERACTION_P3_SNAPSHOT_OFFSET
    p3_block = third_player_block()

    cave = bytearray(b"\x48\x83\xec\x78")
    cave += b"\x48\x89\x4c\x24\x70"
    cave += b"\x48\x89\x7c\x24\x68"
    cave += b"\x48\x89\x74\x24\x60"
    cave += b"\x48\xbe" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    cave += b"\x48\x8d\x7c\x24\x20"
    cave += b"\xb9\x03\x00\x00\x00\xf3\x48\xa5"
    cave += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    cave += b"\x8a\x00\x88\x44\x24\x38"

    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78" + bytes((INTERACTION_ROUTING_ACTIVE_OFFSET,)) + b"\x00"
    cave += b"\x75\x05"
    stock_only_jump = len(cave)
    cave += b"\xe9\x00\x00\x00\x00"

    def call_native(target):
        site = FOLLOWER_NPC_EXACT_SCAN_CAVE + len(cave)
        cave.extend(b"\xe8" + rel32(site, 5, target))

    def select_owner(flag, handle_address):
        cave.extend(b"\x49\xbb" + struct.pack("<Q", state))
        cave.extend(b"\x41\xc7\x03" + struct.pack("<I", flag))
        cave.extend(b"\x49\xba" + struct.pack("<Q", active_entry))
        cave.extend(b"\x49\xc7\x02\x00\x00\x00\x00")
        cave.extend(b"\x48\xb8" + struct.pack("<Q", handle_address))
        cave.extend(b"\x8b\x08\x85\xc9")
        absent_handle = len(cave)
        cave.extend(b"\x74\x00")
        call_native(ENTITY_ENTRY_RESOLVER)
        cave.extend(b"\x48\x85\xc0")
        absent_entry = len(cave)
        cave.extend(b"\x74\x00")
        cave.extend(b"\x49\xba" + struct.pack("<Q", active_entry))
        cave.extend(b"\x49\x89\x02")
        for absent in (absent_handle, absent_entry):
            displacement = len(cave) - (absent + 2)
            if not 0 <= displacement <= 0x7F:
                raise RuntimeError("follower exact owner-resolution branch overflow")
            cave[absent + 1] = displacement

    def zero_global_exact():
        cave.extend(b"\x48\xbf" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR))
        cave.extend(b"\x31\xc0\xb9\x03\x00\x00\x00\xf3\x48\xab")
        cave.extend(b"\x48\xbf" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE))
        cave.extend(b"\x88\x07")

    def zero_snapshot_exact(destination):
        cave.extend(
            b"\x48\xbf"
            + struct.pack("<Q", destination + DUAL_PUBLISH_EXACT_DESCRIPTOR_OFFSET)
        )
        cave.extend(b"\x31\xc0\xb9\x04\x00\x00\x00\xf3\x48\xab")

    def copy_global_exact(destination):
        cave.extend(b"\x48\xbe" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR))
        cave.extend(
            b"\x48\xbf"
            + struct.pack("<Q", destination + DUAL_PUBLISH_EXACT_DESCRIPTOR_OFFSET)
        )
        cave.extend(b"\xb9\x03\x00\x00\x00\xf3\x48\xa5")
        cave.extend(b"\x48\xbe" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE))
        cave.extend(b"\x8a\x06")
        cave.extend(
            b"\x48\xbf"
            + struct.pack("<Q", destination + DUAL_PUBLISH_EXACT_CAN_USE_OFFSET)
        )
        cave.extend(b"\x88\x07")

    def run_private_owner(flag, handle_address, destination):
        zero_snapshot_exact(destination)
        select_owner(flag, handle_address)
        cave.extend(b"\x49\xba" + struct.pack("<Q", active_entry))
        cave.extend(b"\x49\x83\x3a\x00")
        absent = len(cave)
        cave.extend(b"\x0f\x84\x00\x00\x00\x00")
        zero_global_exact()
        cave.extend(b"\x48\x8b\x4c\x24\x70")
        call_native(INTERACTION_NATIVE_INDICATOR_SCAN)
        call_native(INTERACTION_BUILD_EXACT_HANDLE)
        copy_global_exact(destination)
        struct.pack_into("<i", cave, absent + 2, len(cave) - (absent + 6))

    run_private_owner(
        1,
        COMMAND_BLOCK + P2_LOOT_HANDLE_OFFSET,
        p2_snapshot,
    )
    run_private_owner(
        2,
        p3_block + P3_LOOT_HANDLE_OFFSET,
        p3_snapshot,
    )

    stock_only = len(cave)
    struct.pack_into(
        "<i",
        cave,
        stock_only_jump + 1,
        stock_only - (stock_only_jump + 5),
    )
    cave += b"\x49\xbb" + struct.pack("<Q", state)
    cave += b"\x41\xc7\x03\x00\x00\x00\x00"
    cave += b"\x49\xba" + struct.pack("<Q", active_entry)
    cave += b"\x49\xc7\x02\x00\x00\x00\x00"
    cave += b"\x48\x8d\x74\x24\x20"
    cave += b"\x48\xbf" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    cave += b"\xb9\x03\x00\x00\x00\xf3\x48\xa5"
    cave += b"\x8a\x44\x24\x38"
    cave += b"\x48\xbf" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    cave += b"\x88\x07"
    cave += b"\x48\x8b\x4c\x24\x70"
    call_native(INTERACTION_NATIVE_INDICATOR_SCAN)
    cave += b"\x48\x8b\x74\x24\x60"
    cave += b"\x48\x8b\x7c\x24\x68"
    cave += b"\x48\x83\xc4\x78\xc3"

    if len(cave) > FOLLOWER_NPC_EXACT_SCAN_CAVE_LIMIT:
        raise RuntimeError(f"follower exact-NPC cave overflow: {len(cave):#x}")
    return bytes(cave)


def install_follower_npc_exact_scan(process):
    patch = b"\xe8" + rel32(
        FOLLOWER_NPC_EXACT_SCAN_CALL_SITE,
        5,
        FOLLOWER_NPC_EXACT_SCAN_CAVE,
    )
    current = process.read(
        FOLLOWER_NPC_EXACT_SCAN_CALL_SITE,
        len(FOLLOWER_NPC_EXACT_SCAN_CALL_ORIGINAL),
    )
    if current not in (FOLLOWER_NPC_EXACT_SCAN_CALL_ORIGINAL, patch):
        raise RuntimeError(
            f"unexpected bytes at follower exact-NPC call: {current.hex(' ')}"
        )
    process.write(
        FOLLOWER_NPC_EXACT_SCAN_CAVE,
        _build_follower_npc_exact_scan_cave(),
    )
    if current != patch:
        process.patch_code(FOLLOWER_NPC_EXACT_SCAN_CALL_SITE, patch)


def restore_follower_npc_exact_scan(process):
    patch = b"\xe8" + rel32(
        FOLLOWER_NPC_EXACT_SCAN_CALL_SITE,
        5,
        FOLLOWER_NPC_EXACT_SCAN_CAVE,
    )
    current = process.read(
        FOLLOWER_NPC_EXACT_SCAN_CALL_SITE,
        len(FOLLOWER_NPC_EXACT_SCAN_CALL_ORIGINAL),
    )
    if current == patch:
        process.patch_code(
            FOLLOWER_NPC_EXACT_SCAN_CALL_SITE,
            FOLLOWER_NPC_EXACT_SCAN_CALL_ORIGINAL,
        )
    elif current != FOLLOWER_NPC_EXACT_SCAN_CALL_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes restoring follower exact-NPC call: {current.hex(' ')}"
        )


def install_dual_interaction_proximity_filter(process):
    patch = (
        b"\xe9"
        + rel32(
            INTERACTION_PROXIMITY_FILTER_SITE,
            5,
            DUAL_INTERACTION_PROXIMITY_CAVE,
        )
        + b"\x90" * (len(INTERACTION_PROXIMITY_FILTER_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_PROXIMITY_FILTER_SITE,
        len(INTERACTION_PROXIMITY_FILTER_ORIGINAL),
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_PROXIMITY_FILTER_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at dual interaction proximity filter: {current.hex(' ')}"
        )

    cave = bytearray(b"\x50")
    cave += b"\x48\xb8" + struct.pack("<Q", DUAL_INTERACTION_STATE)
    cave += b"\x83\x38\x00"
    native_scan = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78\x08\x00"
    inactive = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x48\x3b\x18"
    p2_match = len(cave)
    cave += b"\x74\x00"
    private_reject = len(cave)
    cave += b"\xeb\x00"

    native = len(cave)
    cave += b"\x48\x3b\xde"
    leader_match = len(cave)
    cave += b"\x74\x00"

    reject = len(cave)
    cave += b"\x58"
    reject_jump = DUAL_INTERACTION_PROXIMITY_CAVE + len(cave)
    cave += b"\xe9" + rel32(reject_jump, 5, INTERACTION_PROXIMITY_FILTER_REJECT)
    accept = len(cave)
    cave += b"\x58"
    accept_jump = DUAL_INTERACTION_PROXIMITY_CAVE + len(cave)
    cave += b"\xe9" + rel32(accept_jump, 5, INTERACTION_PROXIMITY_FILTER_ACCEPT)

    for branch, destination in (
        (native_scan, native),
        (inactive, reject),
        (p2_match, accept),
        (private_reject, reject),
        (leader_match, accept),
    ):
        displacement = destination - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("dual-interaction proximity branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x100:
        raise RuntimeError("dual-interaction proximity cave overflow")
    process.write(DUAL_INTERACTION_PROXIMITY_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_PROXIMITY_FILTER_SITE, patch)


def install_dual_interaction_secondary_filter(process):
    patch = (
        b"\xe9"
        + rel32(
            INTERACTION_SECONDARY_LEADER_FILTER_SITE,
            5,
            DUAL_INTERACTION_SECONDARY_FILTER_CAVE,
        )
        + b"\x90" * (len(INTERACTION_SECONDARY_LEADER_FILTER_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_SECONDARY_LEADER_FILTER_SITE,
        len(INTERACTION_SECONDARY_LEADER_FILTER_ORIGINAL),
    )
    already_installed = current == patch
    if (
        not already_installed
        and current != INTERACTION_SECONDARY_LEADER_FILTER_ORIGINAL
    ):
        raise RuntimeError(
            "unexpected bytes at secondary interaction leader filter: "
            f"{current.hex(' ')}"
        )

    cave = bytearray(b"\x50")
    cave += b"\x48\xb8" + struct.pack("<Q", DUAL_INTERACTION_STATE)
    cave += b"\x83\x38\x00"
    native_scan = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78\x08\x00"
    inactive = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x48\x3b\x18"
    p2_match = len(cave)
    cave += b"\x74\x00"

    private_reject = len(cave)
    cave += b"\xeb\x00"

    native = len(cave)
    cave += b"\x58"
    cave += b"\x48\x3b\x5c\x24\x48"
    native_leader = len(cave)
    cave += b"\x74\x00"
    native_reject_jump = DUAL_INTERACTION_SECONDARY_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(
        native_reject_jump, 5, INTERACTION_SECONDARY_LEADER_FILTER_REJECT
    )

    reject = len(cave)
    cave += b"\x58"
    reject_jump = DUAL_INTERACTION_SECONDARY_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(reject_jump, 5, INTERACTION_SECONDARY_LEADER_FILTER_REJECT)
    accept_after_push = len(cave)
    cave += b"\x58"
    accept_jump = DUAL_INTERACTION_SECONDARY_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(accept_jump, 5, INTERACTION_SECONDARY_LEADER_FILTER_ACCEPT)
    accept_native = len(cave)
    native_jump = DUAL_INTERACTION_SECONDARY_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(native_jump, 5, INTERACTION_SECONDARY_LEADER_FILTER_ACCEPT)

    for branch, destination in (
        (native_scan, native),
        (inactive, reject),
        (p2_match, accept_after_push),
        (private_reject, reject),
        (native_leader, accept_native),
    ):
        displacement = destination - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("secondary-interaction short branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x100:
        raise RuntimeError("secondary-interaction cave overflow")
    process.write(DUAL_INTERACTION_SECONDARY_FILTER_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_SECONDARY_LEADER_FILTER_SITE, patch)


def restore_p2_interaction_actor_filter(process):
    patch = (
        b"\xe9"
        + rel32(INTERACTION_ACTOR_FILTER_SITE, 5, INTERACTION_ACTOR_FILTER_CAVE)
        + b"\x90" * (len(INTERACTION_ACTOR_FILTER_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_ACTOR_FILTER_SITE, len(INTERACTION_ACTOR_FILTER_ORIGINAL)
    )
    if current == patch:
        process.patch_code(
            INTERACTION_ACTOR_FILTER_SITE, INTERACTION_ACTOR_FILTER_ORIGINAL
        )
    elif current != INTERACTION_ACTOR_FILTER_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes restoring interaction actor filter: {current.hex(' ')}"
        )


def install_dual_interaction_publisher(process):
    patch = b"\xe9" + rel32(
        INTERACTION_PUBLISHER_SITE, 5, DUAL_INTERACTION_PUBLISHER_CAVE
    )
    current = process.read(
        INTERACTION_PUBLISHER_SITE, len(INTERACTION_PUBLISHER_ORIGINAL)
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_PUBLISHER_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction publisher: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x48\x8b\x44\x24\x28"
    cave += b"\x4c\x63\xd2"
    cave += b"\x49\xbb" + struct.pack("<Q", DUAL_INTERACTION_STATE)
    cave += b"\x41\x83\x3b\x00"
    native = len(cave)
    cave += b"\x74\x00"
    cave += b"\x83\xfa\x07"
    invalid_slot = len(cave)
    cave += b"\x77\x00"
    cave += b"\x4f\x89\x4c\xd3\x40"
    cave += b"\x4b\x89\x84\xd3\x80\x00\x00\x00"
    cave += b"\x47\x89\x84\x93\xc0\x00\x00\x00"
    cave += b"\x43\x89\x8c\x93\xe0\x00\x00\x00"
    cave += b"\xc3"
    native_path = len(cave)
    cave += b"\x49\xbb" + struct.pack("<Q", 0x2B59C40)
    cave += b"\x4f\x89\x0c\xd3"
    cave += b"\x4b\x89\x44\xd3\x40"
    cave += b"\x47\x89\x84\x93\x80\x00\x00\x00"
    cave += b"\x43\x89\x8c\x93\xa0\x00\x00\x00"
    cave += b"\xc3"

    for branch in (native, invalid_slot):
        displacement = native_path - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("dual-interaction publisher branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x200:
        raise RuntimeError("dual-interaction publisher cave overflow")
    process.write(DUAL_INTERACTION_PUBLISHER_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_PUBLISHER_SITE, patch)


def build_native_leader_bridge_cave() -> bytes:
    cave = bytearray()

    short_fixups = []

    def short_jump(opcode: int, label: str) -> None:
        if label in {
            "leader_done",
            "leader_validation_failed",
            "leader_world_stale",
        }:
            if opcode == 0xEB:
                cave.extend(b"\xe9\x00\x00\x00\x00")
            else:
                cave.extend(bytes((0x0F, opcode + 0x10)) + b"\x00\x00\x00\x00")
            short_fixups.append((len(cave) - 4, label, 4))
            return
        cave.extend(bytes((opcode, 0)))
        short_fixups.append((len(cave) - 1, label, 1))

    labels = {}

    cave += b"\x9c\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += b"\x41\x8b\x43" + bytes((NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET,))
    cave += b"\x41\x3b\x43" + bytes((NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,))
    short_jump(0x74, "leader_done")
    # Ordinary KO/area repair requests retain the established immediate path.
    # A role transfer, however, cannot call the stock setter until movement has
    # staged every changed follower mailbox for this exact request sequence.
    cave += b"\x41\x83\x7b" + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET, NATIVE_LEADER_ROLE_ACTIVE))
    short_jump(0x74, "leader_role_checks")
    cave += b"\x41\x83\x7b" + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET, 0))
    short_jump(0x75, "leader_done")
    cave += b"\x41\x83\x7b" + bytes((NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET, NATIVE_LEADER_HANDOFF_TRANSIENT))
    short_jump(0x75, "leader_done")
    cave += b"\x31\xd2"  # EDX=0: ordinary transient request
    short_jump(0xEB, "leader_ready")
    labels["leader_role_checks"] = len(cave)
    # Movement closes role admission before any shutdown cleanup.  Ordinary
    # transient KO/area repair remains independent and keeps its established
    # path above; only the cross-worker role permutation needs this fence.
    cave += b"\x49\xba" + struct.pack("<Q", NATIVE_LEADER_ROLE_SHUTDOWN_FENCE)
    cave += b"\x41\x83\x3a\x00"
    short_jump(0x75, "leader_done")
    cave += b"\x41\x3b\x43" + bytes((NATIVE_LEADER_ROLE_SEQUENCE_OFFSET,))
    short_jump(0x75, "leader_done")
    cave += b"\x41\x3b\x43" + bytes((NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,))
    short_jump(0x75, "leader_done")
    cave += b"\x41\x8b\x4b" + bytes((NATIVE_LEADER_MEMBER_OFFSET,))
    cave += b"\x41\x3b\x4b" + bytes((NATIVE_LEADER_ROLE_P1_OFFSET,))
    short_jump(0x75, "leader_done")
    cave += b"\xba\x01\x00\x00\x00"  # EDX=1: journalled role transfer

    labels["leader_ready"] = len(cave)
    cave += b"\x48\x83\xec\x28"
    # The captured request sequence survives both calls in unused shadow space.
    cave += b"\x89\x44\x24\x20"
    cave += b"\x89\x54\x24\x24"
    cave += b"\x85\xd2"
    short_jump(0x74, "call_setter")
    cave += b"\x89\xc1"
    cave += b"\x48\xb8" + struct.pack("<Q", NATIVE_LEADER_ROLE_COMMIT_CAVE)
    cave += b"\xff\xd0\x83\xf8\x01"
    short_jump(0x74, "leader_validation_succeeded")
    cave += b"\x83\xf8\x02"
    short_jump(0x74, "leader_world_stale")
    short_jump(0xEB, "leader_validation_failed")
    labels["leader_validation_succeeded"] = len(cave)
    cave += b"\x8b\x4c\x24\x20"
    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += b"\x41\x89\x4b" + bytes((NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,))

    labels["call_setter"] = len(cave)
    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += b"\x41\x8b\x4b" + bytes((NATIVE_LEADER_MEMBER_OFFSET,))
    cave += b"\x48\xb8" + struct.pack("<Q", NATIVE_LEADER_SETTER)
    cave += b"\xff\xd0"
    cave += b"\x83\x7c\x24\x24\x00"
    short_jump(0x74, "leader_publish_done")
    cave += b"\x8b\x4c\x24\x20"
    cave += b"\x48\xb8" + struct.pack(
        "<Q",
        NATIVE_LEADER_ROLE_COMMIT_CAVE + NATIVE_LEADER_ROLE_PUBLISH_ENTRY_OFFSET,
    )
    cave += b"\xff\xd0"

    labels["leader_publish_done"] = len(cave)
    cave += b"\x8b\x4c\x24\x20"
    cave += b"\x48\x83\xc4\x28"

    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += b"\x41\x89\x4b" + bytes((NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,))
    short_jump(0xEB, "leader_done")

    labels["leader_world_stale"] = len(cave)
    cave += b"\x8b\x4c\x24\x20"
    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += (
        b"\x41\xc7\x43"
        + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET,))
        + struct.pack("<I", NATIVE_LEADER_ROLE_CANCELLING)
    )
    # Returning status 2 transfers cancellation authority to this game-thread
    # callback.  Withdraw PREPARED before publishing the tombstone so a
    # completed cancellation can never be rediscovered as an inactive commit
    # after both cancellation ACKs have released admission.
    cave += (
        b"\x41\xc7\x43"
        + bytes((NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,))
        + struct.pack("<I", 0)
    )
    cave += b"\x49\xba" + struct.pack(
        "<Q", NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE
    )
    cave += b"\x41\x89\x0a"
    cave += b"\x41\x89\x4b" + bytes((NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,))
    cave += (
        b"\x41\xc7\x43"
        + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET,))
        + struct.pack("<I", NATIVE_LEADER_ROLE_CANCELLED)
    )
    cave += b"\x48\x83\xc4\x28"
    short_jump(0xEB, "leader_done")

    labels["leader_validation_failed"] = len(cave)
    cave += b"\x48\x83\xc4\x28"

    labels["leader_done"] = len(cave)
    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += b"\x41\x8b\x43" + bytes((NATIVE_AREA_REQUEST_SEQUENCE_OFFSET,))
    cave += b"\x41\x3b\x43" + bytes((NATIVE_AREA_APPLIED_SEQUENCE_OFFSET,))
    short_jump(0x74, "restore")

    cave += b"\x41\x8b\x4b" + bytes((NATIVE_AREA_TARGET_OFFSET,))
    cave += b"\x41\x8b\x53" + bytes((NATIVE_AREA_SPAWN_OFFSET,))
    cave += b"\x45\x8b\x43" + bytes((NATIVE_AREA_OPTIONS_OFFSET,))
    cave += b"\x45\x8b\x4b" + bytes((NATIVE_AREA_FLAG_OFFSET,))
    cave += b"\x48\x83\xec\x28"
    cave += b"\x48\xb8" + struct.pack("<Q", NATIVE_AREA_REQUEST_WRAPPER)
    cave += b"\xff\xd0"
    cave += b"\x48\x83\xc4\x28"

    cave += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    cave += b"\x41\x89\x43" + bytes((NATIVE_AREA_RESULT_OFFSET,))
    cave += b"\x41\x8b\x43" + bytes((NATIVE_AREA_REQUEST_SEQUENCE_OFFSET,))
    cave += b"\x41\x89\x43" + bytes((NATIVE_AREA_APPLIED_SEQUENCE_OFFSET,))

    labels["restore"] = len(cave)
    cave += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x58\x9d\xc3"
    for displacement_at, label, size in short_fixups:
        displacement = labels[label] - (displacement_at + size)
        if size == 4:
            struct.pack_into("<i", cave, displacement_at, displacement)
        else:
            if not -0x80 <= displacement <= 0x7F:
                raise RuntimeError(f"native leader bridge {label} branch overflow")
            cave[displacement_at] = displacement & 0xFF
    if len(cave) > NATIVE_LEADER_BRIDGE_CAVE_SIZE:
        raise RuntimeError(f"native leader bridge overflow: {len(cave):#x}")
    return bytes(cave)


def build_native_leader_role_commit_cave() -> bytes:
    """Build validation entry + no-fail game-thread publication entry.

    Validation (base + 0x00) runs before the native setter and returns EAX=1
    only when the immutable journal, live mailbox generations and role payload
    all agree with ECX.  Publication (base + 0x80) then has no rejection path:
    after the setter crosses the boundary it copies both complete payloads under
    their main seqlocks and publishes all HUD slots before APPLIED is written.
    """

    cave = bytearray()
    validation_fixups = []
    world_stale_fixups = []

    def validation_jump(opcode: int) -> None:
        cave.extend(bytes((0x0F, opcode + 0x10)) + b"\x00\x00\x00\x00")
        validation_fixups.append(len(cave) - 4)

    def world_stale_jump(opcode: int) -> None:
        cave.extend(bytes((0x0F, opcode + 0x10)) + b"\x00\x00\x00\x00")
        world_stale_fixups.append(len(cave) - 4)

    cave.extend(b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE))
    cave.extend(
        b"\x49\xba" + struct.pack("<Q", NATIVE_LEADER_ROLE_SHUTDOWN_FENCE)
    )
    cave.extend(b"\x41\x83\x3a\x00")
    validation_jump(0x75)
    cave.extend(
        b"\x41\x83\x7b"
        + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET, NATIVE_LEADER_ROLE_ACTIVE))
    )
    validation_jump(0x75)
    cave.extend(
        b"\x41\x83\x7b"
        + bytes((NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET, 0))
    )
    validation_jump(0x75)
    for offset in (
        NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET,
        NATIVE_LEADER_ROLE_SEQUENCE_OFFSET,
        NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
    ):
        cave.extend(b"\x41\x39\x4b" + bytes((offset,)))
        validation_jump(0x75)
    cave.extend(
        b"\x41\x8b\x43" + bytes((NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,))
    )
    cave.extend(b"\x39\xc8")
    validation_jump(0x74)
    cave.extend(
        b"\x41\x3b\x43" + bytes((NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET,))
    )
    validation_jump(0x75)
    # Authenticate the complete desired role permutation and native target.
    cave.extend(b"\x41\x8b\x53" + bytes((NATIVE_LEADER_ROLE_P1_OFFSET,)))
    cave.extend(b"\x41\x39\x53" + bytes((NATIVE_LEADER_MEMBER_OFFSET,)))
    validation_jump(0x75)
    cave.extend(b"\x83\xfa\x05")
    validation_jump(0x77)
    cave.extend(b"\x45\x8b\x43" + bytes((NATIVE_LEADER_ROLE_P2_OFFSET,)))
    cave.extend(b"\x41\x83\xf8\xff")
    p2_absent = len(cave)
    cave.extend(b"\x74\x00")
    cave.extend(b"\x41\x83\xf8\x05")
    validation_jump(0x77)
    cave.extend(b"\x44\x39\xc2")
    validation_jump(0x74)
    p2_valid = len(cave)
    cave[p2_absent + 1] = (p2_valid - (p2_absent + 2)) & 0xFF
    cave.extend(b"\x45\x8b\x4b" + bytes((NATIVE_LEADER_ROLE_P3_OFFSET,)))
    cave.extend(b"\x41\x83\xf9\xfe")
    p3_absent = len(cave)
    cave.extend(b"\x73\x00")
    cave.extend(b"\x41\x83\xf9\x05")
    validation_jump(0x77)
    cave.extend(b"\x44\x39\xca")
    validation_jump(0x74)
    cave.extend(b"\x45\x39\xc8")
    validation_jump(0x74)
    roles_valid = len(cave)
    cave[p3_absent + 1] = (roles_valid - (p3_absent + 2)) & 0xFF
    # Movement resolves the requested P1 actor immediately before PREPARED.
    # A missing handle is a malformed transaction, not a transient world
    # change, and therefore fails closed without invoking the native setter.
    cave.extend(
        b"\x41\x83\xbb"
        + struct.pack("<I", NATIVE_LEADER_ROLE_P1_NATIVE_HANDLE_OFFSET)
        + b"\x00"
    )
    validation_jump(0x74)
    cave.extend(b"\x41\x8b\x43" + bytes((NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,)))
    cave.extend(b"\x85\xc0")
    validation_jump(0x74)
    cave.extend(b"\xa8\x01")
    validation_jump(0x75)
    cave.extend(b"\x49\xba" + struct.pack("<Q", NATIVE_LEADER_ROLE_JOURNAL))
    cave.extend(b"\x41\x3b\x02")
    validation_jump(0x75)
    cave.extend(
        b"\x41\x39\x4a" + bytes((NATIVE_LEADER_JOURNAL_REQUEST_OFFSET,))
    )
    validation_jump(0x75)
    for state_offset, journal_offset in (
        (NATIVE_LEADER_ROLE_P1_OFFSET, NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET),
        (NATIVE_LEADER_ROLE_P2_OFFSET, NATIVE_LEADER_JOURNAL_P2_ROLE_OFFSET),
        (NATIVE_LEADER_ROLE_P3_OFFSET, NATIVE_LEADER_JOURNAL_P3_ROLE_OFFSET),
        (
            NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET,
        ),
        (
            NATIVE_LEADER_ROLE_P3_MOVEMENT_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET,
        ),
        (NATIVE_LEADER_ROLE_WORLD_EPOCH_OFFSET, NATIVE_LEADER_JOURNAL_WORLD_EPOCH_OFFSET),
        (
            NATIVE_LEADER_ROLE_LAYOUT_GENERATION_OFFSET,
            NATIVE_LEADER_JOURNAL_LAYOUT_GENERATION_OFFSET,
        ),
        (
            NATIVE_LEADER_ROLE_P1_HUD_SLOT_OFFSET,
            NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET,
        ),
        (
            NATIVE_LEADER_ROLE_P2_HUD_SLOT_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_HUD_SLOT_OFFSET,
        ),
        (
            NATIVE_LEADER_ROLE_P3_HUD_SLOT_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_HUD_SLOT_OFFSET,
        ),
        (
            NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET,
        ),
        (
            NATIVE_LEADER_ROLE_P3_NATIVE_HANDLE_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_NATIVE_HANDLE_OFFSET,
        ),
    ):
        if state_offset <= 0x7F:
            cave.extend(b"\x41\x8b\x53" + bytes((state_offset,)))
        else:
            cave.extend(b"\x41\x8b\x93" + struct.pack("<I", state_offset))
        if journal_offset <= 0x7F:
            cave.extend(b"\x41\x3b\x52" + bytes((journal_offset,)))
        else:
            cave.extend(b"\x41\x3b\x92" + struct.pack("<I", journal_offset))
        validation_jump(0x75)
    for state_offset, allow_inactive in (
        (NATIVE_LEADER_ROLE_P1_HUD_SLOT_OFFSET, False),
        (NATIVE_LEADER_ROLE_P2_HUD_SLOT_OFFSET, True),
        (NATIVE_LEADER_ROLE_P3_HUD_SLOT_OFFSET, True),
    ):
        cave.extend(b"\x41\x8b\x53" + bytes((state_offset,)))
        if allow_inactive:
            cave.extend(b"\x83\xfa\xff")
            hud_inactive = len(cave)
            cave.extend(b"\x74\x00")
        cave.extend(b"\x83\xfa\x03")
        validation_jump(0x77)
        if allow_inactive:
            hud_valid = len(cave)
            cave[hud_inactive + 1] = (hud_valid - (hud_inactive + 2)) & 0xFF
    # A reservation freezes ordinary Python publication.  Prove that every
    # non-zero journal sequence is the exact next even generation of the still-
    # committed live mailbox; an odd, stale or skipped generation cannot move
    # native leadership.
    for journal_offset, payload_offset, role_offset, handle_offset, mailbox in (
        (
            NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET,
            NATIVE_LEADER_ROLE_P2_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET,
            P2_MOVEMENT_MAILBOX,
        ),
        (
            NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET,
            NATIVE_LEADER_ROLE_P3_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_NATIVE_HANDLE_OFFSET,
            P3_MOVEMENT_MAILBOX,
        ),
    ):
        cave.extend(b"\x41\x8b\x52" + bytes((journal_offset,)))
        cave.extend(b"\x85\xd2")
        absent_branch = len(cave)
        cave.extend(b"\x0f\x84\x00\x00\x00\x00")
        cave.extend(b"\xf6\xc2\x01")
        validation_jump(0x75)
        cave.extend(b"\x49\xb9" + struct.pack("<Q", mailbox))
        cave.extend(b"\x45\x8b\x09\x41\xf6\xc1\x01")
        validation_jump(0x75)
        cave.extend(b"\x41\x83\xc1\x02\x41\x83\xe1\xfe")
        normalized_branch = len(cave)
        cave.extend(b"\x75\x00")
        cave.extend(b"\x41\xb9\x02\x00\x00\x00")
        normalized = len(cave)
        cave[normalized_branch + 1] = (normalized - (normalized_branch + 2)) & 0xFF
        cave.extend(b"\x44\x39\xca")
        validation_jump(0x75)
        # The immutable payload must describe exactly this desired role and
        # prepared world.  Active records require a materialized actor/phys and
        # non-zero generation; inactive (KO/manual-off) records require the
        # canonical zero/FFFFFFFF form.  Thus corruption cannot move native
        # leadership and publish an unrelated or ownerless command.
        cave.extend(b"\x45\x8b\x42" + bytes((payload_offset,)))
        cave.extend(b"\x45\x85\xc0")
        payload_inactive = len(cave)
        cave.extend(b"\x74\x00")
        cave.extend(b"\x41\x83\xf8\x01")
        validation_jump(0x75)
        cave.extend(b"\x41\x83\xba" + struct.pack("<I", handle_offset) + b"\x00")
        validation_jump(0x74)
        for field_offset in (
            payload_offset + movement_mailbox.ACTOR_OFFSET - 4,
            payload_offset + movement_mailbox.PHYS_OFFSET - 4,
        ):
            cave.extend(b"\x49\x83\x7a" + bytes((field_offset, 0)))
            validation_jump(0x74)
        cave.extend(
            b"\x45\x8b\x42"
            + bytes((payload_offset + movement_mailbox.MEMBER_ID_OFFSET - 4,))
        )
        cave.extend(b"\x45\x3b\x43" + bytes((role_offset,)))
        validation_jump(0x75)
        cave.extend(
            b"\x41\x83\x7a"
            + bytes(
                (
                    payload_offset
                    + movement_mailbox.ACTOR_GENERATION_OFFSET
                    - 4,
                    0,
                )
            )
        )
        validation_jump(0x74)
        active_payload_done = len(cave)
        cave.extend(b"\xeb\x00")
        inactive_payload = len(cave)
        cave[payload_inactive + 1] = (
            inactive_payload - (payload_inactive + 2)
        ) & 0xFF
        cave.extend(b"\x41\x83\xba" + struct.pack("<I", handle_offset) + b"\x00")
        validation_jump(0x75)
        for field_offset in (
            payload_offset + movement_mailbox.ACTOR_OFFSET - 4,
            payload_offset + movement_mailbox.PHYS_OFFSET - 4,
        ):
            cave.extend(b"\x49\x83\x7a" + bytes((field_offset, 0)))
            validation_jump(0x75)
        cave.extend(
            b"\x41\x83\x7a"
            + bytes(
                (
                    payload_offset + movement_mailbox.MEMBER_ID_OFFSET - 4,
                    0xFF,
                )
            )
        )
        validation_jump(0x75)
        cave.extend(
            b"\x41\x83\x7a"
            + bytes(
                (
                    payload_offset
                    + movement_mailbox.ACTOR_GENERATION_OFFSET
                    - 4,
                    0,
                )
            )
        )
        validation_jump(0x75)
        payload_done = len(cave)
        cave[active_payload_done + 1] = (
            payload_done - (active_payload_done + 2)
        ) & 0xFF
        cave.extend(
            b"\x45\x8b\x42"
            + bytes((payload_offset + movement_mailbox.WORLD_EPOCH_OFFSET - 4,))
        )
        cave.extend(
            b"\x45\x3b\x43" + bytes((NATIVE_LEADER_ROLE_WORLD_EPOCH_OFFSET,))
        )
        validation_jump(0x75)
        role_valid = len(cave)
        struct.pack_into("<i", cave, absent_branch + 2, role_valid - (absent_branch + 6))
    # Finish static authentication before classifying a mismatch as a stale
    # world. Corrupt/torn state always fails closed (EAX=0); only an otherwise
    # exact PREPARED journal may take the clean game-thread cancellation path.
    cave.extend(b"\x41\x3b\x02")
    validation_jump(0x75)

    # Capture roots were journalled immediately before PREPARED. Re-read the
    # native roots on the same game thread that will invoke the setter, prove
    # them non-null/equal, then compare the coherent location and the first four
    # Current/UI party slots byte-for-byte. This prevents an old actor/phys
    # payload from crossing an area teardown or party-generation transition.
    cave.extend(b"\x49\xb9" + struct.pack("<Q", session_state.BATTLE_INFO_GLOBAL))
    cave.extend(b"\x4d\x8b\x09\x4d\x85\xc9")
    world_stale_jump(0x74)
    cave.extend(
        b"\x4d\x3b\x8a"
        + struct.pack("<I", NATIVE_LEADER_JOURNAL_BATTLE_INFO_OFFSET)
    )
    world_stale_jump(0x75)
    cave.extend(
        b"\x49\xb8" + struct.pack("<Q", session_state.LOCATION_SAVE_GLOBAL)
    )
    cave.extend(b"\x4d\x8b\x00\x4d\x85\xc0")
    world_stale_jump(0x74)
    cave.extend(
        b"\x4d\x3b\x82"
        + struct.pack("<I", NATIVE_LEADER_JOURNAL_LOCATION_BASE_OFFSET)
    )
    world_stale_jump(0x75)
    for live_offset in (
        session_state.LOCATION_CURRENT_OFFSET,
        session_state.LOCATION_UI_OFFSET,
    ):
        cave.extend(b"\x41\x8b\x90" + struct.pack("<I", live_offset))
        cave.extend(
            b"\x41\x3b\x92"
            + struct.pack("<I", NATIVE_LEADER_JOURNAL_LOCATION_ID_OFFSET)
        )
        world_stale_jump(0x75)
    for layout_index, journal_offset in (
        (
            session_state.BATTLE_CURRENT_LAYOUT,
            NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET,
        ),
        (
            session_state.BATTLE_UI_LAYOUT,
            NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET,
        ),
    ):
        live_layout_offset = (
            session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + session_state.BATTLE_PARTY_LAYOUT_STRIDE * layout_index
        )
        cave.extend(b"\x49\x8b\x91" + struct.pack("<I", live_layout_offset))
        cave.extend(b"\x49\x3b\x92" + struct.pack("<I", journal_offset))
        world_stale_jump(0x75)
    # Bracket the dereferences with a second root read. A root swap during the
    # live sample is stale, never an authenticated cancellation of corruption.
    cave.extend(b"\x48\xba" + struct.pack("<Q", session_state.BATTLE_INFO_GLOBAL))
    cave.extend(b"\x4c\x3b\x0a")
    world_stale_jump(0x75)
    cave.extend(b"\x48\xba" + struct.pack("<Q", session_state.LOCATION_SAVE_GLOBAL))
    cave.extend(b"\x4c\x3b\x02")
    world_stale_jump(0x75)
    # Active staged commands must still resolve through the live actor manager
    # without dereferencing the journalled actor first.  The native handle's
    # low word selects the bounded BWEI slot; that slot authenticates actor,
    # party slot and the actor-root physics chain before the setter can run.
    cave.extend(b"\x49\xb9" + struct.pack("<Q", ACTOR_MANAGER_GLOBAL))
    cave.extend(b"\x4d\x8b\x09\x4d\x85\xc9")
    world_stale_jump(0x74)
    cave.extend(
        b"\x4d\x3b\x8a"
        + struct.pack("<I", NATIVE_LEADER_JOURNAL_ACTOR_MANAGER_OFFSET)
    )
    world_stale_jump(0x75)
    cave.extend(b"\x41\x8b\x01\x85\xc0")
    world_stale_jump(0x74)
    cave.extend(b"\x3d\x00\x10\x00\x00")
    world_stale_jump(0x77)
    cave.extend(
        b"\x41\x3b\x82"
        + struct.pack("<I", NATIVE_LEADER_JOURNAL_ACTOR_COUNT_OFFSET)
    )
    world_stale_jump(0x75)
    # Resolve the requested P1 through the live actor manager, then validate
    # both identity and liveness on the game thread immediately before the
    # setter.  A KO that lands after Python prepared the transaction becomes
    # an authenticated stale-world cancellation instead of a dead leader.
    cave.extend(
        b"\x41\x8b\x93"
        + struct.pack("<I", NATIVE_LEADER_ROLE_P1_NATIVE_HANDLE_OFFSET)
    )
    cave.extend(b"\x89\x54\x24\x08")
    cave.extend(b"\x89\xd0\x25\xff\xff\x00\x00")
    cave.extend(b"\x41\x3b\x01")
    world_stale_jump(0x73)
    cave.extend(b"\x4d\x8b\x44\xc1\x08\x4d\x85\xc0")
    world_stale_jump(0x74)
    cave.extend(b"\x4d\x8b\x40\x30\x4d\x85\xc0")
    world_stale_jump(0x74)
    cave.extend(b"\x41\x3b\x50\x08")
    world_stale_jump(0x75)
    cave.extend(b"\x49\x8b\x80\x98\x06\x00\x00\x48\x85\xc0")
    world_stale_jump(0x74)
    cave.extend(b"\x83\x78\x48\x00")
    world_stale_jump(0x74)
    cave.extend(b"\x8b\x50\x64\x44\x8b\x40\x64\x44\x39\xc2")
    world_stale_jump(0x75)
    cave.extend(b"\x0b\x50\x3c\xf6\xc2\x01")
    world_stale_jump(0x75)
    cave.extend(
        b"\x41\x8b\x93"
        + struct.pack("<I", NATIVE_LEADER_ROLE_P1_NATIVE_HANDLE_OFFSET)
    )
    cave.extend(b"\x3b\x54\x24\x08")
    world_stale_jump(0x75)
    for sequence_offset, payload_offset, handle_offset, hud_offset in (
        (
            NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_HUD_SLOT_OFFSET,
        ),
        (
            NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_NATIVE_HANDLE_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_HUD_SLOT_OFFSET,
        ),
    ):
        role_done_fixups = []
        cave.extend(b"\x41\x8b\x92" + struct.pack("<I", sequence_offset))
        cave.extend(b"\x85\xd2")
        cave.extend(b"\x0f\x84\x00\x00\x00\x00")
        role_done_fixups.append(len(cave) - 4)
        cave.extend(b"\x41\x8b\x82" + struct.pack("<I", payload_offset))
        cave.extend(b"\x85\xc0")
        cave.extend(b"\x0f\x84\x00\x00\x00\x00")
        role_done_fixups.append(len(cave) - 4)
        cave.extend(b"\x41\x8b\x92" + struct.pack("<I", handle_offset))
        cave.extend(b"\x85\xd2")
        world_stale_jump(0x74)
        cave.extend(b"\x89\xd0\x25\xff\xff\x00\x00")
        cave.extend(b"\x41\x3b\x01")
        world_stale_jump(0x73)
        cave.extend(b"\x4d\x8b\x44\xc1\x08\x4d\x85\xc0")
        world_stale_jump(0x74)
        cave.extend(b"\x49\x8b\x40\x30\x48\x85\xc0")
        world_stale_jump(0x74)
        cave.extend(
            b"\x49\x3b\x82"
            + struct.pack(
                "<I", payload_offset + movement_mailbox.ACTOR_OFFSET - 4
            )
        )
        world_stale_jump(0x75)
        cave.extend(b"\x41\x0f\xb6\x50\x12")
        cave.extend(b"\x41\x3b\x92" + struct.pack("<I", hud_offset))
        world_stale_jump(0x75)
        cave.extend(b"\x49\x89\xc0")
        cave.extend(b"\x41\x8b\x92" + struct.pack("<I", handle_offset))
        cave.extend(b"\x41\x3b\x50\x08")
        world_stale_jump(0x75)
        cave.extend(b"\x4d\x8b\x40\x10\x4d\x85\xc0")
        world_stale_jump(0x74)
        cave.extend(b"\x4d\x8b\x80\xb8\x00\x00\x00\x4d\x85\xc0")
        world_stale_jump(0x74)
        cave.extend(
            b"\x4d\x3b\x82"
            + struct.pack(
                "<I", payload_offset + movement_mailbox.PHYS_OFFSET - 4
            )
        )
        world_stale_jump(0x75)
        role_done = len(cave)
        for displacement_at in role_done_fixups:
            struct.pack_into(
                "<i", cave, displacement_at, role_done - (displacement_at + 4)
            )
    cave.extend(b"\x48\xb8" + struct.pack("<Q", ACTOR_MANAGER_GLOBAL))
    cave.extend(b"\x4c\x3b\x08")
    world_stale_jump(0x75)
    cave.extend(b"\x41\x8b\x01")
    cave.extend(
        b"\x41\x3b\x82"
        + struct.pack("<I", NATIVE_LEADER_JOURNAL_ACTOR_COUNT_OFFSET)
    )
    world_stale_jump(0x75)
    # Re-bracket the immutable journal and the request/mode/fence commit words
    # immediately before returning success to the bridge.
    cave.extend(b"\x41\x8b\x43" + bytes((NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,)))
    cave.extend(b"\x41\x3b\x02")
    validation_jump(0x75)
    cave.extend(
        b"\x41\x83\x7b"
        + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET, NATIVE_LEADER_ROLE_ACTIVE))
    )
    validation_jump(0x75)
    cave.extend(b"\x49\xba" + struct.pack("<Q", NATIVE_LEADER_ROLE_SHUTDOWN_FENCE))
    cave.extend(b"\x41\x83\x3a\x00")
    validation_jump(0x75)
    cave.extend(b"\x41\x39\x4b" + bytes((NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET,)))
    validation_jump(0x75)
    cave.extend(b"\xb8\x01\x00\x00\x00\xc3")
    world_stale = len(cave)
    # Live drift is cancellable only while the fully authenticated static
    # transaction still belongs to this exact request. Re-bracket every commit
    # word before returning status 2; a concurrent CLAIMED/APPLIED/fence/state
    # change converts the result to ordinary fail-closed rejection.
    cave.extend(b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE))
    for offset in (
        NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET,
        NATIVE_LEADER_ROLE_SEQUENCE_OFFSET,
        NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
    ):
        cave.extend(b"\x41\x39\x4b" + bytes((offset,)))
        validation_jump(0x75)
    cave.extend(
        b"\x41\x39\x4b" + bytes((NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,))
    )
    validation_jump(0x74)
    cave.extend(
        b"\x41\x83\x7b"
        + bytes((NATIVE_LEADER_ROLE_ACTIVE_OFFSET, NATIVE_LEADER_ROLE_ACTIVE))
    )
    validation_jump(0x75)
    cave.extend(
        b"\x41\x83\x7b" + bytes((NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET, 0))
    )
    validation_jump(0x75)
    cave.extend(
        b"\x41\x8b\x43" + bytes((NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,))
    )
    cave.extend(b"\x39\xc8")
    validation_jump(0x74)
    cave.extend(
        b"\x41\x3b\x43" + bytes((NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET,))
    )
    validation_jump(0x75)
    cave.extend(
        b"\x41\x8b\x43"
        + bytes((NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,))
    )
    cave.extend(b"\x85\xc0")
    validation_jump(0x74)
    cave.extend(b"\xa8\x01")
    validation_jump(0x75)
    cave.extend(b"\x49\xba" + struct.pack("<Q", NATIVE_LEADER_ROLE_JOURNAL))
    cave.extend(b"\x41\x3b\x02")
    validation_jump(0x75)
    cave.extend(
        b"\x41\x39\x4a" + bytes((NATIVE_LEADER_JOURNAL_REQUEST_OFFSET,))
    )
    validation_jump(0x75)
    cave.extend(
        b"\x49\xba" + struct.pack("<Q", NATIVE_LEADER_ROLE_SHUTDOWN_FENCE)
    )
    cave.extend(b"\x41\x83\x3a\x00")
    validation_jump(0x75)
    cave.extend(b"\xb8\x02\x00\x00\x00\xc3")
    validation_failed = len(cave)
    cave.extend(b"\x31\xc0\xc3")
    for displacement_at in world_stale_fixups:
        displacement = world_stale - (displacement_at + 4)
        struct.pack_into("<i", cave, displacement_at, displacement)
    for displacement_at in validation_fixups:
        displacement = validation_failed - (displacement_at + 4)
        struct.pack_into("<i", cave, displacement_at, displacement)
    if len(cave) > NATIVE_LEADER_ROLE_PUBLISH_ENTRY_OFFSET:
        raise RuntimeError(f"native leader validation overflow: {len(cave):#x}")
    cave.extend(b"\x90" * (NATIVE_LEADER_ROLE_PUBLISH_ENTRY_OFFSET - len(cave)))

    publish_fixups = []
    publish_labels = {}

    def publish_jump(opcode: int, label: str) -> None:
        cave.extend(bytes((opcode, 0)))
        publish_fixups.append((len(cave) - 1, label))

    cave.extend(b"\x56\x57\xfc")  # preserve RSI/RDI; outer bridge restores flags
    cave.extend(b"\x49\xbb" + struct.pack("<Q", NATIVE_LEADER_ROLE_JOURNAL))
    for role, journal_sequence, journal_payload, mailbox in (
        (
            "p2",
            NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET,
            P2_MOVEMENT_MAILBOX,
        ),
        (
            "p3",
            NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET,
            NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET,
            P3_MOVEMENT_MAILBOX,
        ),
    ):
        cave.extend(b"\x41\x8b\x43" + bytes((journal_sequence,)))
        cave.extend(b"\x85\xc0")
        publish_jump(0x74, f"{role}_done")
        cave.extend(b"\x49\xba" + struct.pack("<Q", mailbox))
        cave.extend(b"\x89\xc2\x83\xca\x01\x41\x89\x12")
        cave.extend(
            b"\x48\xbe"
            + struct.pack("<Q", NATIVE_LEADER_ROLE_JOURNAL + journal_payload)
        )
        cave.extend(b"\x49\x8d\x7a\x04")
        cave.extend(b"\xb9" + struct.pack("<I", movement_mailbox.MAILBOX_SIZE - 4))
        cave.extend(b"\xf3\xa4")
        # EAX is preserved by REP MOVSB and is the even commit word.
        cave.extend(b"\x41\x89\x02")
        publish_labels[f"{role}_done"] = len(cave)

    cave.extend(b"\x49\xbb" + struct.pack("<Q", NATIVE_LEADER_ROLE_JOURNAL))
    for source_offset, target in (
        (
            NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET,
            COMMAND_BLOCK + P1_HUD_PARTY_SLOT_OFFSET,
        ),
        (
            NATIVE_LEADER_JOURNAL_P2_HUD_SLOT_OFFSET,
            COMMAND_BLOCK + P2_HUD_PARTY_SLOT_OFFSET,
        ),
        (
            NATIVE_LEADER_JOURNAL_P3_HUD_SLOT_OFFSET,
            third_player_block() + P3_HUD_PARTY_SLOT_OFFSET,
        ),
    ):
        cave.extend(b"\x41\x8b\x43" + bytes((source_offset,)))
        cave.extend(b"\x49\xba" + struct.pack("<Q", target))
        cave.extend(b"\x41\x89\x02")
    cave.extend(b"\x5f\x5e\xc3")
    for displacement_at, label in publish_fixups:
        displacement = publish_labels[label] - (displacement_at + 1)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError(f"native leader publish {label} branch overflow")
        cave[displacement_at] = displacement & 0xFF
    if len(cave) > NATIVE_LEADER_ROLE_COMMIT_CAVE_SIZE:
        raise RuntimeError(f"native leader role commit overflow: {len(cave):#x}")
    return bytes(cave)


def build_native_party_leader_guard_cave(cave_address=None) -> bytes:
    address = (
        NATIVE_PARTY_LEADER_GUARD_CAVE if cave_address is None else int(cave_address)
    )
    if not address:
        raise RuntimeError("native party leader guard cave is not initialized")

    cave = bytearray(b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK))
    cave += b"\x83\x78" + bytes((COOP_ACTIVE_OFFSET,)) + b"\x00"
    inactive = len(cave)
    cave += b"\x74\x00"
    # Co-op leadership changes exclusively through the lower game-thread
    # bridge at NATIVE_LEADER_SETTER.  The stock UI setter is therefore always
    # blocked while co-op is active—even if a mailbox happens to target the
    # same member—so a rapid native menu cannot impersonate a valid request.
    cave += b"\xc3"

    native = len(cave)
    cave += NATIVE_PARTY_LEADER_SETTER_ORIGINAL
    jump_site = address + len(cave)
    cave += b"\xe9" + rel32(
        jump_site,
        5,
        NATIVE_PARTY_LEADER_SETTER_RETURN,
    )

    displacement = native - (inactive + 2)
    if not 0 <= displacement <= 0x7F:
        raise RuntimeError("native party leader guard allow branch overflow")
    cave[inactive + 1] = displacement

    if len(cave) > 0x40:
        raise RuntimeError(f"native party leader guard overflow: {len(cave):#x}")
    return bytes(cave)


def install_native_party_leader_guard(process) -> None:
    patch = (
        b"\xe9"
        + rel32(
            NATIVE_PARTY_LEADER_SETTER,
            5,
            NATIVE_PARTY_LEADER_GUARD_CAVE,
        )
        + b"\x90"
    )
    current = process.read(
        NATIVE_PARTY_LEADER_SETTER,
        len(NATIVE_PARTY_LEADER_SETTER_ORIGINAL),
    )
    already_installed = current == patch
    if not already_installed and current != NATIVE_PARTY_LEADER_SETTER_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at native party leader setter: {current.hex(' ')}"
        )
    expected_cave = build_native_party_leader_guard_cave()
    if already_installed:
        resident = process.read(NATIVE_PARTY_LEADER_GUARD_CAVE, len(expected_cave))
        if resident != expected_cave:
            raise RuntimeError(
                "native party leader guard cave changed while its root is live; "
                "restart the game"
            )
        return
    process.write(NATIVE_PARTY_LEADER_GUARD_CAVE, expected_cave)
    process.patch_code(NATIVE_PARTY_LEADER_SETTER, patch)


def install_dual_interaction_scan(process):
    patch = b"\xe8" + rel32(
        INTERACTION_DUAL_SCAN_CALL_SITE, 5, DUAL_INTERACTION_SCAN_CAVE
    )
    current = process.read(
        INTERACTION_DUAL_SCAN_CALL_SITE,
        len(INTERACTION_DUAL_SCAN_CALL_ORIGINAL),
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_DUAL_SCAN_CALL_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at dual interaction scan: {current.hex(' ')}"
        )

    state = DUAL_INTERACTION_STATE
    p2_snapshot = state + DUAL_INTERACTION_P2_SNAPSHOT_OFFSET
    p3_snapshot = state + DUAL_INTERACTION_P3_SNAPSHOT_OFFSET
    p3_block = third_player_block()
    active_entry = interaction_scan_entry_address()

    bridge_cave = build_native_leader_bridge_cave()
    role_commit_cave = build_native_leader_role_commit_cave()

    cave = bytearray(b"\x48\x83\xec\x38")
    bridge_call = DUAL_INTERACTION_SCAN_CAVE + len(cave)
    cave += b"\xe8" + rel32(
        bridge_call,
        5,
        NATIVE_LEADER_BRIDGE_CAVE,
    )
    cave += b"\x48\x89\x4c\x24\x30"
    cave += b"\x48\x89\x7c\x24\x28"
    cave += b"\x48\x89\x74\x24\x20"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78" + bytes((INTERACTION_ROUTING_ACTIVE_OFFSET,)) + b"\x00"
    cave += b"\x75\x05"
    stock_only_jump = len(cave)
    cave += b"\xe9\x00\x00\x00\x00"

    def call_native(target: int) -> None:
        call_site = DUAL_INTERACTION_SCAN_CAVE + len(cave)
        cave.extend(b"\xe8" + rel32(call_site, 5, target))

    def select_owner(flag: int, handle_address: int) -> None:
        cave.extend(b"\x49\xbb" + struct.pack("<Q", state))
        cave.extend(b"\x41\xc7\x03" + struct.pack("<I", flag))
        cave.extend(b"\x49\xba" + struct.pack("<Q", active_entry))
        cave.extend(b"\x49\xc7\x02\x00\x00\x00\x00")
        cave.extend(b"\x48\xb8" + struct.pack("<Q", handle_address))
        cave.extend(b"\x8b\x08\x85\xc9")
        absent_handle = len(cave)
        cave.extend(b"\x74\x00")
        call_native(ENTITY_ENTRY_RESOLVER)
        cave.extend(b"\x48\x85\xc0")
        absent_entry = len(cave)
        cave.extend(b"\x74\x00")
        cave.extend(b"\x49\xba" + struct.pack("<Q", active_entry))
        cave.extend(b"\x49\x89\x02")
        for absent in (absent_handle, absent_entry):
            displacement = len(cave) - (absent + 2)
            if not 0 <= displacement <= 0x7F:
                raise RuntimeError("dual-interaction owner-resolution branch overflow")
            cave[absent + 1] = displacement

    def clear_private_slots() -> None:
        cave.extend(b"\x49\xbb" + struct.pack("<Q", state))
        cave.extend(b"\x49\x8d\x7b\x40")
        cave.extend(b"\x31\xc0")
        cave.extend(b"\xb9\x1c\x00\x00\x00")
        cave.extend(b"\xf3\x48\xab")

    def run_private_owner_scan() -> None:
        cave.extend(b"\x48\x8b\x4c\x24\x30")
        call_native(INTERACTION_NATIVE_SCAN)

    def copy_snapshot(destination: int) -> None:
        # Publish only what a pass actually found. This copy formerly ran
        # unconditionally, so an empty pass overwrote a populated snapshot with
        # the cleared buffer. In a live process with three characters at a chest,
        # the aggregate slot held a candidate for 0.013 ms of each 16.67 ms frame:
        # 0.08% of the time at 55 Hz. A 120 Hz reader saw it once every 650 frames,
        # making the prompt flash for one frame instead of remaining visible.
        #
        # The snapshot now retains the last populated pass until another replaces
        # it, with two counters describing its lifetime. The cave is capped at
        # 0x200 bytes, so the encoding is tight: R10 retains the destination, and
        # the eight 32-bit slots are copied four qwords at a time from a biased
        # array base, keeping every displacement to one byte.
        cave.extend(b"\x49\xba" + struct.pack("<Q", destination))

        cave.extend(
            b"\x49\xbb" + struct.pack("<Q", state + DUAL_INTERACTION_STATE_SLOTS_OFFSET)
        )
        cave.extend(b"\x31\xc9")
        for pair in range(DUAL_INTERACTION_STATE_SLOT_COUNT // 2):
            cave.extend(b"\x49\x0b\x4b" + bytes((pair * 8,)))
        cave.extend(b"\x48\x85\xc9")
        empty_pass = len(cave)
        cave.extend(b"\x0f\x84\x00\x00\x00\x00")

        cave.extend(b"\x48\xbe" + struct.pack("<Q", state))
        cave.extend(b"\x48\xbf" + struct.pack("<Q", destination))
        cave.extend(b"\xb9\x24\x00\x00\x00")
        cave.extend(b"\xf3\x48\xa5")

        cave.extend(
            b"\x48\xbf"
            + struct.pack("<Q", destination + DUAL_PUBLISH_EXACT_DESCRIPTOR_OFFSET)
        )
        cave.extend(b"\x31\xc0")
        cave.extend(b"\xb9\x04\x00\x00\x00")
        cave.extend(b"\xf3\x48\xab")

        # Stamp this publication tick and place the empty-pass branch immediately
        # afterward so a pass with nothing to say leaves snapshot and stamp as the
        # last real publication left them. R10 still holds the destination: the
        # copy above touches only RSI, RDI, RCX and RAX, while reloading R10 would
        # cost ten bytes unavailable in the 0x200-byte cave.
        cave.extend(
            b"\x41\x8b\x8a" + struct.pack("<I", DUAL_INTERACTION_PUBLISH_TICK_OFFSET)
        )
        cave.extend(
            b"\x41\x89\x8a" + struct.pack("<I", DUAL_INTERACTION_PUBLISH_MARK_OFFSET)
        )
        struct.pack_into("<i", cave, empty_pass + 2, len(cave) - (empty_pass + 6))

    def run_private_owner(flag: int, handle_address: int, destination: int) -> None:
        select_owner(flag, handle_address)
        # Resolution failure is still a completed owner pass.  Advance its
        # clock before the guard so an old prompt expires after despawn rather
        # than remaining current forever while the handle is absent.
        cave.extend(b"\x49\xba" + struct.pack("<Q", destination))
        cave.extend(
            b"\x41\xff\x82" + struct.pack("<I", DUAL_INTERACTION_PUBLISH_TICK_OFFSET)
        )
        cave.extend(b"\x49\xba" + struct.pack("<Q", active_entry))
        cave.extend(b"\x49\x83\x3a\x00")
        unresolved = len(cave)
        cave.extend(b"\x0f\x84\x00\x00\x00\x00")
        clear_private_slots()
        run_private_owner_scan()
        copy_snapshot(destination)
        struct.pack_into("<i", cave, unresolved + 2, len(cave) - (unresolved + 6))

    run_private_owner(1, COMMAND_BLOCK + P2_LOOT_HANDLE_OFFSET, p2_snapshot)
    run_private_owner(2, p3_block + P3_LOOT_HANDLE_OFFSET, p3_snapshot)

    stock_only = len(cave)
    struct.pack_into(
        "<i",
        cave,
        stock_only_jump + 1,
        stock_only - (stock_only_jump + 5),
    )
    cave += b"\x49\xbb" + struct.pack("<Q", state)
    cave += b"\x41\xc7\x03\x00\x00\x00\x00"
    cave += b"\x49\xba" + struct.pack("<Q", active_entry)
    cave += b"\x49\xc7\x02\x00\x00\x00\x00"
    cave += b"\x48\x8b\x4c\x24\x30"
    call_native(INTERACTION_NATIVE_SCAN)
    cave += b"\x48\x8b\x74\x24\x20"
    cave += b"\x48\x8b\x7c\x24\x28"
    cave += b"\x48\x83\xc4\x38\xc3"

    if len(cave) > DUAL_INTERACTION_SCAN_CAVE_LIMIT:
        raise RuntimeError("dual-interaction scan cave overflow")
    scan_cave = bytes(cave)
    live_caves = (
        ("native leader bridge", NATIVE_LEADER_BRIDGE_CAVE, bridge_cave),
        (
            "native leader role commit",
            NATIVE_LEADER_ROLE_COMMIT_CAVE,
            role_commit_cave,
        ),
        ("dual interaction scan", DUAL_INTERACTION_SCAN_CAVE, scan_cave),
    )
    if already_installed:
        for label, address, expected in live_caves:
            resident = process.read(address, len(expected))
            if resident != expected:
                raise RuntimeError(
                    f"{label} cave changed while its root is live; restart the game"
                )
        return
    for _label, address, expected in live_caves:
        process.write(address, expected)
    process.patch_code(INTERACTION_DUAL_SCAN_CALL_SITE, patch)


def install_p2_interaction_broadphase_filter(process):
    patch = (
        b"\xe9"
        + rel32(
            INTERACTION_BROADPHASE_FILTER_SITE,
            5,
            INTERACTION_BROADPHASE_FILTER_CAVE,
        )
        + b"\x90" * (len(INTERACTION_BROADPHASE_FILTER_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_BROADPHASE_FILTER_SITE,
        len(INTERACTION_BROADPHASE_FILTER_ORIGINAL),
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_BROADPHASE_FILTER_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction broadphase filter: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x44\x85\xf8"
    native_hit = len(cave)
    cave += b"\x75\x00"
    cave += b"\x50"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x83\x78\x08\x00"
    inactive = len(cave)
    cave += b"\x74\x00"
    cave += b"\x48\xb8" + struct.pack("<Q", interaction_scan_entry_address())
    cave += b"\x48\x3b\x18"
    not_p2 = len(cave)
    cave += b"\x75\x00"
    cave += b"\x58"
    accept_jump = INTERACTION_BROADPHASE_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(accept_jump, 5, INTERACTION_BROADPHASE_FILTER_ACCEPT)
    reject = len(cave)
    cave += b"\x58"
    reject_jump = INTERACTION_BROADPHASE_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(reject_jump, 5, INTERACTION_BROADPHASE_FILTER_REJECT)
    native_accept = len(cave)
    direct_accept_jump = INTERACTION_BROADPHASE_FILTER_CAVE + len(cave)
    cave += b"\xe9" + rel32(direct_accept_jump, 5, INTERACTION_BROADPHASE_FILTER_ACCEPT)

    for branch, destination in (
        (native_hit, native_accept),
        (inactive, reject),
        (not_p2, reject),
    ):
        displacement = destination - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("interaction-broadphase short branch overflow")
        cave[branch + 1] = displacement & 0xFF

    if len(cave) > 0x100:
        raise RuntimeError(f"interaction-broadphase cave overflow: {len(cave):#x}")
    process.write(INTERACTION_BROADPHASE_FILTER_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_BROADPHASE_FILTER_SITE, patch)


def install_interaction_geometry_logger(process):
    patch = (
        b"\xe9"
        + rel32(INTERACTION_GEOMETRY_ENTRY_SITE, 5, INTERACTION_GEOMETRY_ENTRY_CAVE)
        + b"\x90" * (len(INTERACTION_GEOMETRY_ENTRY_ORIGINAL) - 5)
    )
    current = process.read(
        INTERACTION_GEOMETRY_ENTRY_SITE, len(INTERACTION_GEOMETRY_ENTRY_ORIGINAL)
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_GEOMETRY_ENTRY_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction geometry entry: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x41\x52\x41\x53"
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x49\x89\x9b" + struct.pack("<I", INTERACTION_GEOMETRY_LAST_ENTRY_OFFSET)
    cave += b"\x49\xff\x83" + struct.pack("<I", INTERACTION_GEOMETRY_TOTAL_OFFSET)
    cave += b"\x49\x3b\x9b" + struct.pack("<I", P2_INTERACTION_ENTRY_OFFSET)
    not_p2 = len(cave)
    cave += b"\x75\x00"
    cave += b"\x49\xff\x83" + struct.pack("<I", INTERACTION_GEOMETRY_P2_MATCH_OFFSET)
    logged = len(cave)
    displacement = logged - (not_p2 + 2)
    if not 0 <= displacement <= 0x7F:
        raise RuntimeError("interaction-geometry logger branch overflow")
    cave[not_p2 + 1] = displacement
    cave += b"\x48\x8b\xcb"
    call_site = INTERACTION_GEOMETRY_ENTRY_CAVE + len(cave)
    cave += b"\xe8" + rel32(call_site, 5, INTERACTION_GEOMETRY_FIRST_CHECK)
    cave += b"\x41\x5b\x41\x5a"
    jump_site = INTERACTION_GEOMETRY_ENTRY_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, INTERACTION_GEOMETRY_ENTRY_RETURN)

    if len(cave) > 0x100:
        raise RuntimeError(f"interaction-geometry cave overflow: {len(cave):#x}")
    process.write(INTERACTION_GEOMETRY_ENTRY_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_GEOMETRY_ENTRY_SITE, patch)


def install_interaction_update_logger(process):
    patch = b"\xe9" + rel32(
        INTERACTION_UPDATE_ENTRY_SITE, 5, INTERACTION_UPDATE_ENTRY_CAVE
    )
    current = process.read(
        INTERACTION_UPDATE_ENTRY_SITE, len(INTERACTION_UPDATE_ENTRY_ORIGINAL)
    )
    already_installed = current == patch
    if not already_installed and current != INTERACTION_UPDATE_ENTRY_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at interaction update entry: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x50"
    cave += b"\x48\xb8" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x48\xff\x80" + struct.pack("<I", INTERACTION_UPDATE_COUNT_OFFSET)
    cave += b"\x58"
    cave += INTERACTION_UPDATE_ENTRY_ORIGINAL
    jump_site = INTERACTION_UPDATE_ENTRY_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, INTERACTION_UPDATE_ENTRY_RETURN)
    if len(cave) > 0x100:
        raise RuntimeError(f"interaction-update cave overflow: {len(cave):#x}")
    process.write(INTERACTION_UPDATE_ENTRY_CAVE, cave)
    if not already_installed:
        process.patch_code(INTERACTION_UPDATE_ENTRY_SITE, patch)


def install_steering_heading_override(process):
    original = b"\xf3\x0f\x11\x87\xa4\x00\x00\x00"
    patch = (
        b"\xe9"
        + rel32(STEERING_HEADING_STORE_SITE, 5, STEERING_HEADING_CAVE)
        + b"\x90\x90\x90"
    )
    # The hook is in the middle of native steering. Preserve flags and every
    # volatile register used to validate either the current or prior complete
    # mailbox record.
    cave = bytearray(b"\x9c\x41\x53\x41\x52\x41\x51\x50")
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    done_branches = []
    p3_branches = []

    def near_jcc(opcode, destinations):
        position = len(cave)
        cave.extend(b"\x0f" + bytes((opcode,)) + b"\x00\x00\x00\x00")
        destinations.append(position)

    def short_jcc(opcode, destinations):
        position = len(cave)
        cave.extend(bytes((opcode, 0)))
        destinations.append(position)

    def resolve_short(branches, destination, description):
        for branch in branches:
            displacement = destination - (branch + 2)
            if not -0x80 <= displacement <= 0x7F:
                raise RuntimeError(f"steering-heading {description} branch overflow")
            cave[branch + 1] = displacement & 0xFF

    cave += b"\x41\x83\x7b" + bytes((MOVEMENT_ACTIVE_OFFSET,)) + b"\x00"
    near_jcc(0x84, done_branches)

    def select_mailbox(mailbox, miss_branches):
        fallback_branches = []
        # A local mailbox base keeps this two-role consumer inside its audited
        # 0x100-byte cave.  Identity is the stable published physics pointer
        # compared with the hook's live RDI; never dereference an actor pointer
        # supplied by a mailbox merely to rediscover that same physics object.
        cave.extend(b"\x4d\x8d\x8b" + struct.pack("<i", mailbox))
        cave.extend(b"\x45\x8b\x11")
        cave.extend(b"\x45\x85\xd2")
        short_jcc(0x74, miss_branches)
        cave.extend(b"\x41\xf6\xc2\x01")
        short_jcc(0x75, fallback_branches)
        cave.extend(b"\x41\x83\x79" + bytes((movement_mailbox.ACTIVE_OFFSET, 0)))
        short_jcc(0x74, miss_branches)
        cave.extend(b"\x49\x3b\x79" + bytes((movement_mailbox.PHYS_OFFSET,)))
        short_jcc(0x75, miss_branches)
        cave.extend(b"\x41\x8b\x41" + bytes((movement_mailbox.HEADING_OFFSET,)))
        cave.extend(b"\x45\x3b\x11")
        short_jcc(0x75, fallback_branches)
        cave.extend(b"\x66\x0f\x6e\xc0")
        short_jcc(0xEB, done_branches)

        fallback = len(cave)
        resolve_short(fallback_branches, fallback, "fallback")
        call_site = STEERING_HEADING_CAVE + len(cave)
        cave.extend(b"\xe8" + rel32(call_site, 5, STEERING_FALLBACK_HELPER))
        # CF means a complete fallback exists but belongs to another actor/phys
        # pair. ZF clear with CF clear means its local sequence raced.  In both
        # cases fail open for this frame: retrying either record here can pin the
        # game's native thread forever if a publisher dies with an odd sequence.
        short_jcc(0x72, miss_branches)
        short_jcc(0x75, miss_branches)
        cave.extend(b"\x66\x0f\x6e\xc0")
        short_jcc(0xEB, done_branches)

    p2_mailbox = P2_MOVEMENT_MAILBOX_OFFSET - 0x200
    p3_mailbox = P3_MOVEMENT_MAILBOX_OFFSET - 0x200
    select_mailbox(p2_mailbox, p3_branches)
    p3_start = len(cave)
    resolve_short(p3_branches, p3_start, "P2")
    select_mailbox(p3_mailbox, done_branches)

    done = len(cave)
    for branch in done_branches:
        if cave[branch] == 0x0F:
            struct.pack_into("<i", cave, branch + 2, done - (branch + 6))
            continue
        displacement = done - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError("steering-heading completion branch overflow")
        cave[branch + 1] = displacement & 0xFF
    cave += b"\x58\x41\x59\x41\x5a\x41\x5b\x9d"
    cave += original
    jump_site = STEERING_HEADING_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, STEERING_HEADING_STORE_RETURN)

    if len(cave) > 0x100:
        raise RuntimeError(f"steering-heading cave overflow: {len(cave):#x}")

    helper = bytearray()
    transient_branches = []
    miss_branches = []

    def helper_short_jcc(opcode, destinations):
        position = len(helper)
        helper.extend(bytes((opcode, 0)))
        destinations.append(position)

    # Input: R9 -> selected mailbox, RDI -> hook physics object.
    # Output: EAX = heading when ZF=1/CF=0; transient invalid sample when
    # ZF=0/CF=0; identity miss when CF=1.  Both invalid results fail open in the
    # caller. The caller owns/saves all touched registers.
    helper += b"\x45\x8b\x51" + bytes((movement_mailbox.FALLBACK_SEQUENCE_OFFSET,))
    helper += b"\x45\x85\xd2"
    helper_short_jcc(0x74, miss_branches)
    helper += b"\x41\xf6\xc2\x01"
    helper_short_jcc(0x75, transient_branches)
    helper += b"\x41\x83\x79" + bytes((movement_mailbox.FALLBACK_ACTIVE_OFFSET, 0))
    helper_short_jcc(0x74, miss_branches)
    # Validate only the scalar pointer identity; the hook owns RDI, while the
    # fallback actor field is publisher-controlled and must not be dereferenced.
    helper += b"\x49\x3b\x79" + bytes((movement_mailbox.FALLBACK_PHYS_OFFSET,))
    helper_short_jcc(0x75, miss_branches)
    helper += b"\x41\x8b\x41" + bytes((movement_mailbox.FALLBACK_HEADING_OFFSET,))
    helper += b"\x45\x3b\x51" + bytes((movement_mailbox.FALLBACK_SEQUENCE_OFFSET,))
    helper += b"\xf8\xc3"  # clc; ret (ZF is the sequence-validation result)

    transient_return = len(helper)
    helper += b"\xf8\xc3"
    miss_return = len(helper)
    helper += b"\xf9\xc3"
    for branches, destination in (
        (transient_branches, transient_return),
        (miss_branches, miss_return),
    ):
        for branch in branches:
            displacement = destination - (branch + 2)
            if not 0 <= displacement <= 0x7F:
                raise RuntimeError("steering fallback helper branch overflow")
            helper[branch + 1] = displacement
    if len(helper) > STEERING_FALLBACK_HELPER_SIZE:
        raise RuntimeError(f"steering fallback helper overflow: {len(helper):#x}")

    actor_helper = build_actor_physics_helper()
    mode, pending = _preflight_process_lifetime_detour(
        process,
        label="steering-heading",
        site=STEERING_HEADING_STORE_SITE,
        original=original,
        patch=patch,
        regions=(
            (
                "actor-physics helper",
                ACTOR_PHYSICS_HELPER,
                ACTOR_PHYSICS_HELPER_SIZE,
                actor_helper,
                True,
            ),
            (
                "fallback helper",
                STEERING_FALLBACK_HELPER,
                STEERING_FALLBACK_HELPER_SIZE,
                helper,
                False,
            ),
            ("cave", STEERING_HEADING_CAVE, 0x100, cave, False),
        ),
    )
    return _publish_process_lifetime_detour(
        process,
        site=STEERING_HEADING_STORE_SITE,
        patch=patch,
        mode=mode,
        pending=pending,
    )


def build_p1_sdl_xinput_cave() -> bytes:
    """Build the bridge that substitutes one native XInput poll with SDL P1."""

    code = bytearray()
    native_fixups: list[int] = []

    def branch_native() -> None:
        code.extend(b"\x74\x00")
        native_fixups.append(len(code) - 1)

    # Preserve the two volatile scratch registers because this hook sits in the
    # middle of the game's controller-update function. EAX deliberately remains
    # live: zero is the native XInput success result for a published virtual pad.
    code += b"\x41\x53\x41\x52"  # push r11; push r10
    code += b"\x49\xbb" + struct.pack("<Q", P1_SDL_XINPUT_STATE)
    code += b"\x41\x83\x3b\x00"  # cmp dword ptr [r11], 0
    branch_native()
    code += b"\x44\x8b\x93\x58\x04\x00\x00"  # mov r10d, [rbx+458h]
    code += b"\x45\x3b\x53" + bytes((P1_SDL_XINPUT_SLOT_OFFSET,))
    code += b"\x75\x00"
    native_fixups.append(len(code) - 1)
    code += b"\x45\x8b\x53" + bytes((P1_SDL_XINPUT_ACTIVE_BUFFER_OFFSET,))
    code += b"\x41\x83\xe2\x01"  # and r10d, 1
    code += b"\x49\xc1\xe2\x04"  # shl r10, 4
    # Both buffers are aligned 16-byte XINPUT_STATE records. The two pushes move
    # the original caller's [rsp+28h] output buffer to [rsp+38h].
    code += b"\x43\x0f\x10\x44\x1a" + bytes((P1_SDL_XINPUT_BUFFER_0_OFFSET,))
    code += b"\x0f\x11\x44\x24\x38"
    code += b"\x33\xc0"  # xor eax, eax

    native_offset = len(code)
    code += b"\x41\x5a\x41\x5b"  # pop r10; pop r11
    code += XINPUT_STATE_RESULT_ORIGINAL
    jump_site = P1_SDL_XINPUT_CAVE + len(code)
    code += b"\xe9" + rel32(jump_site, 5, XINPUT_STATE_RESULT_RETURN)

    for displacement_at in native_fixups:
        displacement = native_offset - (displacement_at + 1)
        if not 0 <= displacement <= 0x7F:
            raise RuntimeError("P1 SDL XInput bridge branch overflow")
        code[displacement_at] = displacement
    if len(code) > P1_SDL_XINPUT_CAVE_SIZE:
        raise RuntimeError(f"P1 SDL XInput cave overflow: {len(code):#x}")
    return bytes(code)


def install_p1_sdl_xinput_bridge(process) -> bool:
    patch = (
        b"\xe9" + rel32(XINPUT_STATE_RESULT_SITE, 5, P1_SDL_XINPUT_CAVE) + b"\x90\x90"
    )
    cave = build_p1_sdl_xinput_cave()
    mode = _preflight_static_detour(
        process,
        label="P1 SDL XInput bridge",
        site=XINPUT_STATE_RESULT_SITE,
        original=XINPUT_STATE_RESULT_ORIGINAL,
        patch=patch,
        cave_address=P1_SDL_XINPUT_CAVE,
        cave_size=P1_SDL_XINPUT_CAVE_SIZE,
        cave=cave,
    )
    if mode == "adopt":
        return False

    # The dynamic publication is disarmed before code becomes reachable.  The
    # executable slot and root were both proven pristine above.
    process.write(P1_SDL_XINPUT_STATE, b"\x00" * P1_SDL_XINPUT_STATE_SIZE)
    process.write(P1_SDL_XINPUT_CAVE, cave)
    process.patch_code(XINPUT_STATE_RESULT_SITE, patch)
    return True


def publish_p1_sdl_xinput_state(
    process,
    gamepad: bytes,
    packet_number: int,
    active_buffer: int,
) -> int:
    """Publish a tear-free XINPUT_STATE and return the newly active buffer."""

    payload = bytes(gamepad)
    if len(payload) != 12:
        raise ValueError("XINPUT_GAMEPAD payload must contain exactly 12 bytes")
    next_buffer = 1 - (int(active_buffer) & 1)
    buffer_offset = (
        P1_SDL_XINPUT_BUFFER_0_OFFSET
        if next_buffer == 0
        else P1_SDL_XINPUT_BUFFER_1_OFFSET
    )
    process.write(
        P1_SDL_XINPUT_STATE + buffer_offset,
        struct.pack("<I", int(packet_number) & 0xFFFFFFFF) + payload,
    )
    # A single aligned DWORD selects a completely written buffer. Enable is
    # committed last so the native hook can never observe an uninitialized slot.
    process.write(
        P1_SDL_XINPUT_STATE + P1_SDL_XINPUT_SLOT_OFFSET,
        struct.pack("<I", P1_SDL_XINPUT_VIRTUAL_SLOT),
    )
    process.write(
        P1_SDL_XINPUT_STATE + P1_SDL_XINPUT_ACTIVE_BUFFER_OFFSET,
        struct.pack("<I", next_buffer),
    )
    process.write(
        P1_SDL_XINPUT_STATE + P1_SDL_XINPUT_ENABLED_OFFSET,
        struct.pack("<I", 1),
    )
    return next_buffer


def disable_p1_sdl_xinput(process) -> None:
    process.write(
        P1_SDL_XINPUT_STATE + P1_SDL_XINPUT_ENABLED_OFFSET,
        struct.pack("<I", 0),
    )


def install_p2_device_isolation(process):
    patch = b"\xe9" + rel32(XINPUT_DEVICE_FINALIZE_SITE, 5, P2_DEVICE_FILTER_CAVE)

    code = bytearray()
    pass_fixups = []

    def branch_pass(opcode: bytes) -> None:
        code.extend(opcode)
        pass_fixups.append(len(code))
        code.extend(b"\x00" * 4)

    code += b"\x50\x51\x41\x53"
    code += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    code += b"\x41\x8b\x03"
    code += b"\x85\xc0"
    branch_pass(b"\x0f\x84")
    code += b"\x8b\x8b\x58\x04\x00\x00"
    code += b"\x0f\xa3\xc8"
    branch_pass(b"\x0f\x83")
    for offset in (0x414, 0x418, 0x420, 0x424, 0x45C):
        code += b"\xc7\x83" + struct.pack("<I", offset) + b"\x00" * 4

    for offset in (0x43C, 0x444):
        code += b"\x48\xc7\x83" + struct.pack("<I", offset) + b"\x00" * 4

    pass_offset = len(code)

    # Mirror flee state into the mapped device before the finalizer. P1's
    # physical RT is combined with the request published by co-op.

    code += b"\x8b\x83\x58\x04\x00\x00"
    slot_compare_site = P2_DEVICE_FILTER_CAVE + len(code)
    code += b"\x3b\x05" + rel32(
        slot_compare_site,
        6,
        P2_DEVICE_FILTER_STATE + P1_NATIVE_CONTROLLER_SLOT_OFFSET,
    )
    flee_done_fixups: list[int] = []
    code += b"\x75\x00"
    flee_done_fixups.append(len(code) - 1)
    code += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    code += b"\x41\x83\x7b" + bytes((COOP_FLEE_ACTIVE_OFFSET,)) + b"\x00"
    code += b"\x74\x00"
    flee_done_fixups.append(len(code) - 1)
    code += b"\x41\x8b\x43" + bytes((COOP_FLEE_REQUEST_OFFSET,))
    code += b"\x41\x0b\x43" + bytes((COOP_FLEE_P1_PHYSICAL_OFFSET,))
    code += b"\x85\xc0"
    code += b"\x0f\x95\xc0"
    code += b"\x88\x83\x43\x04\x00\x00"
    flee_done = len(code)
    for displacement_at in flee_done_fixups:
        displacement = flee_done - (displacement_at + 1)
        if not 0 <= displacement <= 0x7F:
            raise RuntimeError("co-op flee branch overflow")
        code[displacement_at] = displacement
    code += b"\x41\x5b\x59\x58"
    call_site = P2_DEVICE_FILTER_CAVE + len(code)
    code += b"\xe8" + rel32(call_site, 5, XINPUT_DEVICE_FINALIZER)

    code += b"\x9c"
    code += b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
    code += b"\x48\x81\xec\x80\x00\x00\x00"
    for register, offset in enumerate(range(0x20, 0x80, 0x10)):
        code += b"\xf3\x0f\x7f"
        code += bytes((0x44 | (register << 3), 0x24, offset))

    lifecycle_fixups: list[tuple[int, str]] = []
    lifecycle_labels: dict[str, int] = {}

    def lifecycle_label(name: str) -> None:
        lifecycle_labels[name] = len(code)

    def lifecycle_branch(opcode: bytes, name: str) -> None:
        code.extend(opcode)
        lifecycle_fixups.append((len(code), name))
        code.extend(b"\x00" * 4)

    code += b"\x8b\x83\x58\x04\x00\x00"
    slot_compare_site = P2_DEVICE_FILTER_CAVE + len(code)
    code += b"\x3b\x05" + rel32(
        slot_compare_site,
        6,
        P2_DEVICE_FILTER_STATE + P1_NATIVE_CONTROLLER_SLOT_OFFSET,
    )
    lifecycle_branch(b"\x0f\x85", "lifecycle_restore")
    code += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    code += b"\x41\x0f\xb6\x43" + bytes((WAIT_MENU_DESIRED_OFFSET,))
    code += b"\x41\x3a\x43" + bytes((WAIT_MENU_APPLIED_OFFSET,))
    lifecycle_branch(b"\x0f\x84", "lifecycle_restore")
    code += b"\x84\xc0"
    lifecycle_branch(b"\x0f\x84", "lifecycle_close")

    code += b"\x49\xba" + struct.pack("<Q", NATIVE_COMMAND_ACTOR_GLOBAL)
    code += b"\x41\x83\x3a\x00"
    lifecycle_branch(b"\x0f\x85", "lifecycle_restore")
    code += b"\x49\xba" + struct.pack("<Q", NATIVE_COMMAND_FLAG_GLOBAL)
    code += b"\x41\x83\x3a\x00"
    lifecycle_branch(b"\x0f\x85", "lifecycle_restore")
    code += b"\xb9\x01\x00\x00\x00"
    code += b"\xba\x01\x00\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_COMMAND_MENU_OPEN)
    code += b"\xff\xd0"
    code += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    code += b"\x41\xc6\x43" + bytes((WAIT_MENU_APPLIED_OFFSET, 1))
    lifecycle_branch(b"\xe9", "lifecycle_restore")

    lifecycle_label("lifecycle_close")
    code += b"\x49\xba" + struct.pack("<Q", NATIVE_COMMAND_ACTOR_GLOBAL)
    code += b"\x41\x8b\x02"
    code += b"\x85\xc0"
    lifecycle_branch(b"\x0f\x84", "lifecycle_mark_closed")

    code += b"\x83\xf8\x01"
    lifecycle_branch(b"\x0f\x85", "lifecycle_mark_closed")
    code += b"\x49\xba" + struct.pack("<Q", NATIVE_COMMAND_FLAG_GLOBAL)
    code += b"\x41\x83\x3a\x01"
    lifecycle_branch(b"\x0f\x85", "lifecycle_mark_closed")
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_COMMAND_MENU_CLOSE)
    code += b"\xff\xd0"

    lifecycle_label("lifecycle_mark_closed")
    code += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    code += b"\x41\xc6\x43" + bytes((WAIT_MENU_APPLIED_OFFSET, 0))

    lifecycle_label("lifecycle_restore")
    for register, offset in enumerate(range(0x20, 0x80, 0x10)):
        code += b"\xf3\x0f\x6f"
        code += bytes((0x44 | (register << 3), 0x24, offset))
    code += b"\x48\x81\xc4\x80\x00\x00\x00"
    code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x58\x9d"

    jump_site = P2_DEVICE_FILTER_CAVE + len(code)
    code += b"\xe9" + rel32(jump_site, 5, XINPUT_DEVICE_FINALIZE_RETURN)
    for displacement_at in pass_fixups:
        struct.pack_into(
            "<i",
            code,
            displacement_at,
            P2_DEVICE_FILTER_CAVE
            + pass_offset
            - (P2_DEVICE_FILTER_CAVE + displacement_at + 4),
        )
    for displacement_at, target in lifecycle_fixups:
        struct.pack_into(
            "<i",
            code,
            displacement_at,
            P2_DEVICE_FILTER_CAVE
            + lifecycle_labels[target]
            - (P2_DEVICE_FILTER_CAVE + displacement_at + 4),
        )
    if len(code) > P2_DEVICE_FILTER_CAVE_SIZE:
        raise RuntimeError(f"P2 device-filter cave overflow: {len(code):#x}")
    initial_state = bytearray(P2_DEVICE_FILTER_STATE_SIZE)
    struct.pack_into(
        "<I",
        initial_state,
        P1_NATIVE_CONTROLLER_SLOT_OFFSET,
        INVALID_NATIVE_CONTROLLER_SLOT,
    )
    mode = _preflight_static_detour(
        process,
        label="P2/P3 XInput device isolation",
        site=XINPUT_DEVICE_FINALIZE_SITE,
        original=XINPUT_DEVICE_FINALIZE_ORIGINAL,
        patch=patch,
        cave_address=P2_DEVICE_FILTER_CAVE,
        cave_size=P2_DEVICE_FILTER_CAVE_SIZE,
        cave=code,
    )
    if mode == "adopt":
        return False

    process.write(P2_DEVICE_FILTER_STATE, initial_state)
    process.write(P2_DEVICE_FILTER_CAVE, code)
    process.patch_code(XINPUT_DEVICE_FINALIZE_SITE, patch)
    return True


def build_native_gamepad_filter_cave(cave_address: int, return_address: int) -> bytes:
    """Clear m_platformData and run the game's transition/filter finalizer."""

    code = bytearray()
    code += b"\x49\xbb" + struct.pack("<Q", P2_DEVICE_FILTER_STATE)
    code += b"\x41\x83\x3b\x00"  # cmp dword ptr [r11],0 (isolation mask)
    inactive_jump = len(code)
    code += b"\x74\x00"
    for offset in (0x414, 0x418, 0x420, 0x424):
        code += b"\xc7\x83" + struct.pack("<I", offset) + b"\x00" * 4
    for offset in (0x43C, 0x444):
        code += b"\x48\xc7\x83" + struct.pack("<I", offset) + b"\x00" * 4

    finalize_offset = len(code)
    displacement = finalize_offset - (inactive_jump + 2)
    if not 0 <= displacement <= 0x7F:
        raise RuntimeError("native gamepad-filter branch overflow")
    code[inactive_jump + 1] = displacement
    code += b"\x48\x8b\xcb"  # mov rcx,rbx
    call_site = cave_address + len(code)
    code += b"\xe8" + rel32(call_site, 5, XINPUT_DEVICE_FINALIZER)
    jump_site = cave_address + len(code)
    code += b"\xe9" + rel32(jump_site, 5, return_address)
    return bytes(code)


def _install_native_gamepad_filter(
    process,
    *,
    label: str,
    site: int,
    original: bytes,
    cave_address: int,
    cave_size: int,
    return_address: int,
) -> bool:
    patch = b"\xe9" + rel32(site, 5, cave_address) + b"\x90" * (len(original) - 5)
    cave = build_native_gamepad_filter_cave(cave_address, return_address)
    if len(cave) > cave_size:
        raise RuntimeError(f"{label} gamepad-filter cave overflow: {len(cave):#x}")
    mode = _preflight_static_detour(
        process,
        label=f"{label} gamepad finalizer",
        site=site,
        original=original,
        patch=patch,
        cave_address=cave_address,
        cave_size=cave_size,
        cave=cave,
    )
    if mode == "adopt":
        return False

    process.write(cave_address, cave)
    process.patch_code(site, patch)
    return True


def install_dinput_gamepad_filter(process) -> bool:
    return _install_native_gamepad_filter(
        process,
        label="DirectInput",
        site=DINPUT_GAMEPAD_FINALIZE_SITE,
        original=DINPUT_GAMEPAD_FINALIZE_ORIGINAL,
        cave_address=DINPUT_GAMEPAD_FILTER_CAVE,
        cave_size=DINPUT_GAMEPAD_FILTER_CAVE_SIZE,
        return_address=DINPUT_GAMEPAD_FINALIZE_RETURN,
    )


def install_steam_gamepad_filter(process) -> bool:
    return _install_native_gamepad_filter(
        process,
        label="Steam",
        site=STEAM_GAMEPAD_FINALIZE_SITE,
        original=STEAM_GAMEPAD_FINALIZE_ORIGINAL,
        cave_address=STEAM_GAMEPAD_FILTER_CAVE,
        cave_size=STEAM_GAMEPAD_FILTER_CAVE_SIZE,
        return_address=STEAM_GAMEPAD_FINALIZE_RETURN,
    )


def controller_isolation_mask(controller_indices) -> int:
    mask = 0
    for controller_index in controller_indices:
        if controller_index is None:
            continue
        mask |= 1 << (int(controller_index) & 3)
    return mask


def native_controller_isolation_indices(primary_controller_index):
    """Leave only the selected P1 controller available to native game input."""
    primary_slot = native_controller_slot(primary_controller_index)
    if 0 <= primary_slot <= 3:
        return tuple(index for index in range(4) if index != primary_slot)
    return tuple(range(4))


def native_controller_slot(primary_controller_index) -> int:
    """Resolve the P1 assignment to the slot observed by native game input."""
    if isinstance(primary_controller_index, int) and 0 <= primary_controller_index <= 3:
        return primary_controller_index
    if (
        isinstance(primary_controller_index, dict)
        and primary_controller_index.get("backend") == "sdl"
    ):
        return P1_SDL_XINPUT_VIRTUAL_SLOT
    return INVALID_NATIVE_CONTROLLER_SLOT


def publish_coop_device_isolation(process, controller_indices):
    process.write(
        P2_DEVICE_FILTER_STATE,
        struct.pack("<I", controller_isolation_mask(controller_indices)),
    )


def publish_native_controller_slot(process, primary_controller_index) -> int:
    """Publish the slot after normalizing XInput and the P1 SDL bridge."""
    slot = native_controller_slot(primary_controller_index)
    process.write(
        P2_DEVICE_FILTER_STATE + P1_NATIVE_CONTROLLER_SLOT_OFFSET,
        struct.pack("<I", slot),
    )
    return slot


def request_native_leader_refresh(process, party_member_id) -> int:
    party_member_id = int(party_member_id)
    if not 0 <= party_member_id <= 0xFF:
        raise ValueError("party member id must fit the native CL argument")
    role_active_address = (
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )
    transient_address = (
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
    )
    if (
        process.u32(role_active_address) != 0
        or process.u32(transient_address) != NATIVE_LEADER_HANDOFF_TRANSIENT
    ):
        raise RuntimeError("native leader refresh is not transient owner")
    request_address = P2_DEVICE_FILTER_STATE + NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    sequence = (process.u32(request_address) + 1) & 0xFFFFFFFF
    if sequence == 0:
        sequence = 1

    process.write(
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_MEMBER_OFFSET,
        struct.pack("<I", party_member_id),
    )
    if (
        process.u32(role_active_address) != 0
        or process.u32(transient_address) != NATIVE_LEADER_HANDOFF_TRANSIENT
    ):
        raise RuntimeError("role transfer claimed native leader publication")
    process.write(request_address, struct.pack("<I", sequence))
    return sequence


class NativeLeaderRoleAdmissionClosed(RuntimeError):
    """A role request was rejected before publishing any remote write."""


@dataclass(frozen=True)
class NativeLeaderRoleTransfer:
    sequence: int
    target_member_id: int
    identity: session_state.SessionIdentity
    prepared_sequence: int
    p2_movement_sequence: int
    p3_movement_sequence: int
    p1_hud_slot: int
    p2_hud_slot: int
    p3_hud_slot: int
    committed_sequence: int
    world_epoch: int
    layout_generation: int
    applied_sequence: int

    @property
    def prepared(self) -> bool:
        return self.prepared_sequence == self.sequence

    @property
    def native_applied(self) -> bool:
        return self.applied_sequence == self.sequence

    @property
    def committed(self) -> bool:
        return self.committed_sequence == self.sequence


def leader_role_layout_generation(current_layout, ui_layout) -> int:
    """Return a deterministic non-zero generation for one coherent roster."""

    value = 0x811C9DC5
    for member_id in tuple(current_layout[:4]) + tuple(ui_layout[:4]):
        value ^= int(member_id) & 0xFFFF
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value or 1


def _role_identity_members(identity: session_state.SessionIdentity) -> tuple[int, ...]:
    return tuple(
        int(member_id)
        for member_id in (
            identity.p1_party_member_id,
            identity.p2_party_member_id,
            identity.p3_party_member_id,
        )
        if member_id is not None
    )


def _native_role_member(member_id: int | None) -> int:
    return NATIVE_LEADER_ROLE_NONE if member_id is None else int(member_id)


def _native_p3_role_member(identity: session_state.SessionIdentity) -> int:
    if identity.p3_party_member_id is not None:
        return int(identity.p3_party_member_id)
    if identity.player_count >= 3:
        return NATIVE_LEADER_ROLE_EMPTY_P3
    return NATIVE_LEADER_ROLE_NONE


def native_leader_role_permutation(
    current_identity: session_state.SessionIdentity,
    target_member_id: int,
) -> session_state.SessionIdentity:
    """Move the target role to P1 by swapping only the two affected owners."""

    target_member_id = int(target_member_id)
    if target_member_id == current_identity.p1_party_member_id:
        return current_identity
    if target_member_id == current_identity.p2_party_member_id:
        return session_state.SessionIdentity(
            current_identity.p2_party_member_id,
            current_identity.p1_party_member_id,
            current_identity.p3_party_member_id,
            current_identity.player_count,
        )
    if target_member_id == current_identity.p3_party_member_id:
        return session_state.SessionIdentity(
            int(current_identity.p3_party_member_id),
            current_identity.p2_party_member_id,
            current_identity.p1_party_member_id,
            current_identity.player_count,
        )
    raise ValueError("native leader target is not owned by a player role")


def request_native_leader_role_transfer(
    process,
    target_member_id,
    current_identity,
    current_layout,
    ui_layout,
    *,
    manual_control_mask,
) -> tuple[int, session_state.SessionIdentity]:
    """Publish a complete role permutation, with request sequence last."""

    target_member_id = int(target_member_id)
    replacement = native_leader_role_permutation(current_identity, target_member_id)
    current_members = _role_identity_members(current_identity)
    replacement_members = () if replacement is None else _role_identity_members(replacement)
    changed_members = tuple(
        member_id
        for current_member, member_id in zip(
            (
                current_identity.p1_party_member_id,
                current_identity.p2_party_member_id,
                current_identity.p3_party_member_id,
            ),
            (
                replacement.p1_party_member_id,
                replacement.p2_party_member_id,
                replacement.p3_party_member_id,
            ),
        )
        if member_id is not None and member_id != current_member
    )
    if (
        replacement is None
        or replacement.p1_party_member_id != target_member_id
        or len(replacement_members) != len(set(replacement_members))
        or set(replacement_members) != set(current_members)
        or any(member_id not in session_state.MAIN_PARTY_MEMBER_IDS for member_id in replacement_members)
        or any(
            not role_member_layout_coherent(member_id, current_layout, ui_layout)
            for member_id in changed_members
        )
    ):
        raise ValueError("leader transfer must be a coherent permutation of player roles")

    base = P2_DEVICE_FILTER_STATE
    request_address = base + NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    applied_address = base + NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    transient_address = base + NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
    role_active_address = base + NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    if process.u32(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE):
        raise NativeLeaderRoleAdmissionClosed(
            "movement is fenced for shutdown or restart"
        )
    claimed = process.u32(
        base + NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
    )
    committed = process.u32(
        base + NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET
    )
    if claimed and claimed != committed:
        raise RuntimeError("a claimed native leader transfer is still irreversible")
    if process.u32(role_active_address):
        raise RuntimeError("a native leader role transfer is already published")
    if process.u32(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE) != process.u32(
        NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE
    ):
        raise RuntimeError("a native leader cancellation awaits producer acknowledgement")
    if process.u32(transient_address):
        raise RuntimeError("a transient native leader handoff is active")
    manual_control_mask = int(manual_control_mask) & DEFAULT_MANUAL_CONTROL_MASK

    def hud_slot(member_id, bit=None):
        if member_id is None or (bit is not None and not manual_control_mask & bit):
            return INVALID_HUD_PARTY_SLOT
        try:
            return tuple(current_layout[:4]).index(int(member_id))
        except ValueError:
            # A configured role can remain reserved while its character is out
            # of the active party.  It is not part of this two-seat swap and has
            # no HUD row until it materializes again.
            return INVALID_HUD_PARTY_SLOT

    sequence = None
    try:
        # The first claim publication is itself ambiguous: WriteProcessMemory
        # can make the dword visible and still report failure. Keep it inside
        # the same rollback/recovery protocol as every later publication.
        process.write(
            role_active_address,
            struct.pack("<I", NATIVE_LEADER_ROLE_CLAIMING),
        )
        # ROLE_CLAIMING blocks both native consumers and all transient
        # producers.  Re-read the competing gate after claiming it; if both
        # producers raced, neither is allowed to publish shared request words.
        if process.u32(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE):
            raise RuntimeError("movement shutdown fence won the claim")
        if process.u32(transient_address):
            raise RuntimeError("a transient native leader handoff won the claim")
        if process.u32(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE) != process.u32(
            NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE
        ):
            raise RuntimeError("a native leader cancellation won the claim")
        current_request = process.u32(request_address)
        if current_request != process.u32(applied_address):
            raise RuntimeError("a native leader request is still pending")
        if process.u32(role_active_address) != NATIVE_LEADER_ROLE_CLAIMING:
            raise RuntimeError("native leader role claim changed")
        sequence = (current_request + 1) & 0xFFFFFFFF
        if sequence == 0:
            sequence = 1
        role_payload = struct.pack(
            "<13I",
            int(replacement.p1_party_member_id),
            _native_role_member(replacement.p2_party_member_id),
            _native_p3_role_member(replacement),
            sequence,
            0,  # prepared sequence -- movement is the sole producer
            0,  # P2 reserved movement sequence
            0,  # P3 reserved movement sequence
            hud_slot(replacement.p1_party_member_id),
            hud_slot(replacement.p2_party_member_id, P2_MANUAL_CONTROL_BIT),
            hud_slot(replacement.p3_party_member_id, P3_MANUAL_CONTROL_BIT),
            committed,  # preserve the preceding terminal CLAIMED/COMMITTED pair
            0,  # prepared world epoch
            leader_role_layout_generation(current_layout, ui_layout),
        )
        process.write(base + NATIVE_LEADER_ROLE_P1_OFFSET, role_payload)
        process.write(
            base + NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET,
            struct.pack("<3I", 0, 0, 0),
        )
        process.write(
            base + NATIVE_LEADER_MEMBER_OFFSET,
            struct.pack("<I", target_member_id),
        )
        process.write(request_address, struct.pack("<I", sequence))
        # ACTIVE is the final publication word.  CLAIMING is deliberately not
        # consumable by the bridge, even if the request write became visible.
        process.write(
            role_active_address,
            struct.pack("<I", NATIVE_LEADER_ROLE_ACTIVE),
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        # Clear only a provably unpublished claim.  If the request word landed,
        # leave CLAIMING for authenticated startup/frame recovery to promote;
        # clearing it asynchronously could race the game-thread bridge.
        try:
            live_request = process.u32(request_address)
            live_applied = process.u32(applied_address)
            live_claimed = process.u32(
                base + NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
            )
            live_committed = process.u32(
                base + NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET
            )
            request_published = bool(
                sequence is not None and live_request == sequence
            )
            if (
                not request_published
                and live_request == live_applied
                and live_claimed == live_committed
                and process.u32(role_active_address)
                == NATIVE_LEADER_ROLE_CLAIMING
            ):
                process.write(role_active_address, struct.pack("<I", 0))
        except (OSError, RuntimeError, struct.error, ValueError):
            pass
        raise
    return sequence, replacement


def read_native_leader_role_transfer(
    process,
    *,
    include_inactive=False,
    include_claiming=False,
) -> NativeLeaderRoleTransfer | None:
    """Read one authenticated role-transfer payload with request bracketing."""

    base = P2_DEVICE_FILTER_STATE
    first = process.u32(base + NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET)
    if not first:
        return None
    first_cancelled = process.u32(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE)
    raw = process.read(base, P2_DEVICE_FILTER_STATE_SIZE)
    confirm = process.read(base, P2_DEVICE_FILTER_STATE_SIZE)
    final = process.u32(base + NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET)
    final_cancelled = process.u32(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE)
    if (
        first != final
        or first_cancelled != final_cancelled
        or first_cancelled == first
        or raw[
            NATIVE_LEADER_MEMBER_OFFSET : NATIVE_LEADER_ROLE_ACTIVE_OFFSET + 4
        ]
        != confirm[
            NATIVE_LEADER_MEMBER_OFFSET : NATIVE_LEADER_ROLE_ACTIVE_OFFSET + 4
        ]
    ):
        return None
    mode = struct.unpack_from("<I", raw, NATIVE_LEADER_ROLE_ACTIVE_OFFSET)[0]
    role_values = struct.unpack_from("<13I", raw, NATIVE_LEADER_ROLE_P1_OFFSET)
    (
        p1,
        p2,
        p3,
        role_sequence,
        prepared_sequence,
        p2_movement_sequence,
        p3_movement_sequence,
        p1_hud_slot,
        p2_hud_slot,
        p3_hud_slot,
        committed_sequence,
        world_epoch,
        layout_generation,
    ) = role_values
    target = struct.unpack_from("<I", raw, NATIVE_LEADER_MEMBER_OFFSET)[0]
    applied = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    )[0]
    p2_member_id = None if p2 == NATIVE_LEADER_ROLE_NONE else p2
    p3_member_id = (
        None
        if p3 in (NATIVE_LEADER_ROLE_NONE, NATIVE_LEADER_ROLE_EMPTY_P3)
        else p3
    )
    player_count = 2 if p3 == NATIVE_LEADER_ROLE_NONE else 3
    identity = session_state.SessionIdentity(
        p1,
        p2_member_id,
        p3_member_id,
        player_count,
    )
    members = _role_identity_members(identity)
    accepted_modes = [NATIVE_LEADER_ROLE_ACTIVE]
    if include_inactive:
        accepted_modes.append(0)
    if include_claiming:
        accepted_modes.append(NATIVE_LEADER_ROLE_CLAIMING)
    if (
        mode not in accepted_modes
        or role_sequence != first
        or target != p1
        or len(members) != len(set(members))
        or any(member_id not in session_state.MAIN_PARTY_MEMBER_IDS for member_id in members)
    ):
        return None
    return NativeLeaderRoleTransfer(
        first,
        target,
        identity,
        prepared_sequence,
        p2_movement_sequence,
        p3_movement_sequence,
        p1_hud_slot,
        p2_hud_slot,
        p3_hud_slot,
        committed_sequence,
        world_epoch,
        layout_generation,
        applied,
    )


def snapshot_with_native_leader_role_transfer(snapshot, transfer):
    """Adopt a committed native leader without waiting for config-file polling."""

    if (
        transfer is None
        or not (
            transfer.native_applied
            or (
                transfer.prepared
                and snapshot.leader_party_member_id == transfer.target_member_id
            )
        )
        or leader_role_layout_generation(
            snapshot.current_layout, snapshot.ui_layout
        )
        != transfer.layout_generation
        or set(_role_identity_members(snapshot.identity))
        != set(_role_identity_members(transfer.identity))
    ):
        return snapshot
    return replace(snapshot, identity=transfer.identity)


def read_native_leader_role_journal(process, transfer):
    """Return a complete immutable journal record for ``transfer``."""

    base = NATIVE_LEADER_ROLE_JOURNAL
    first = process.u32(base + NATIVE_LEADER_JOURNAL_SEQUENCE_OFFSET)
    if not first or first & 1:
        return None
    raw = process.read(base, NATIVE_LEADER_JOURNAL_RECORD_SIZE)
    final = process.u32(base + NATIVE_LEADER_JOURNAL_SEQUENCE_OFFSET)
    if first != final:
        return None
    request, journal_world, layout_generation = struct.unpack_from("<3I", raw, 4)
    p2_sequence = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET
    )[0]
    p3_sequence = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET
    )[0]
    hud_slots = struct.unpack_from(
        "<3I", raw, NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET
    )
    journal_roles = struct.unpack_from(
        "<3I", raw, NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET
    )
    battle_info, location_base, location_id = struct.unpack_from(
        "<QQI", raw, NATIVE_LEADER_JOURNAL_BATTLE_INFO_OFFSET
    )
    current_layout = struct.unpack_from(
        "<4H", raw, NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET
    )
    ui_layout = struct.unpack_from(
        "<4H", raw, NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET
    )
    p2_native_handle, p3_native_handle = struct.unpack_from(
        "<2I", raw, NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET
    )
    actor_manager, actor_count = struct.unpack_from(
        "<QI", raw, NATIVE_LEADER_JOURNAL_ACTOR_MANAGER_OFFSET
    )
    p2_command = movement_mailbox.MovementCommand.from_payload(
        raw[
            NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET:
            NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET
            + movement_mailbox.MAILBOX_SIZE
            - 4
        ]
    )
    p3_command = movement_mailbox.MovementCommand.from_payload(
        raw[
            NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET:
            NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET
            + movement_mailbox.MAILBOX_SIZE
            - 4
        ]
    )
    expected_roles = (
        int(transfer.identity.p1_party_member_id),
        _native_role_member(transfer.identity.p2_party_member_id),
        _native_p3_role_member(transfer.identity),
    )
    if (
        request != transfer.sequence
        or layout_generation != transfer.layout_generation
        or first
        != process.u32(
            P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET
        )
        or p2_sequence != transfer.p2_movement_sequence
        or p3_sequence != transfer.p3_movement_sequence
        or hud_slots
        != (transfer.p1_hud_slot, transfer.p2_hud_slot, transfer.p3_hud_slot)
        or journal_roles != expected_roles
        or not battle_info
        or not location_base
        or not actor_manager
        or not 0 < actor_count <= 4096
        or location_id == 0xFFFFFFFF
        or leader_role_layout_generation(current_layout, ui_layout)
        != transfer.layout_generation
        or bool(p2_native_handle) != bool(p2_command.active)
        or bool(p3_native_handle) != bool(p3_command.active)
    ):
        return None
    return {
        "sequence": first,
        "world_epoch": journal_world,
        "battle_info": battle_info,
        "location_base": location_base,
        "location_id": location_id,
        "current_layout": current_layout,
        "ui_layout": ui_layout,
        "p2_sequence": p2_sequence,
        "p3_sequence": p3_sequence,
        "p2_command": p2_command,
        "p3_command": p3_command,
        "p2_native_handle": p2_native_handle,
        "p3_native_handle": p3_native_handle,
        "actor_manager": actor_manager,
        "actor_count": actor_count,
    }


def _native_leader_role_live_world_record(process, snapshot):
    """Capture one exact native world/layout generation for the journal."""

    battle_info = process.u64(session_state.BATTLE_INFO_GLOBAL)
    location_base = process.u64(session_state.LOCATION_SAVE_GLOBAL)
    actor_manager = process.u64(ACTOR_MANAGER_GLOBAL)
    actor_count = process.u32(actor_manager) if actor_manager else 0
    current_location = process.u32(
        location_base + session_state.LOCATION_CURRENT_OFFSET
    ) if location_base else 0xFFFFFFFF
    ui_location = process.u32(
        location_base + session_state.LOCATION_UI_OFFSET
    ) if location_base else 0xFFFFFFFF
    current_layout = struct.pack("<4H", *tuple(snapshot.current_layout[:4]))
    ui_layout = struct.pack("<4H", *tuple(snapshot.ui_layout[:4]))
    live_current_layout = (
        process.read(
            battle_info
            + session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + session_state.BATTLE_PARTY_LAYOUT_STRIDE
            * session_state.BATTLE_CURRENT_LAYOUT,
            8,
        )
        if battle_info
        else b""
    )
    live_ui_layout = (
        process.read(
            battle_info
            + session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + session_state.BATTLE_PARTY_LAYOUT_STRIDE
            * session_state.BATTLE_UI_LAYOUT,
            8,
        )
        if battle_info
        else b""
    )
    if (
        not battle_info
        or int(getattr(snapshot, "battle_info", 0)) != battle_info
        or not location_base
        or not actor_manager
        or not 0 < actor_count <= 4096
        or current_location == 0xFFFFFFFF
        or current_location != ui_location
        or current_location != int(getattr(snapshot, "current_location", -1))
        or ui_location != int(getattr(snapshot, "ui_location", -1))
        or bytes(live_current_layout) != current_layout
        or bytes(live_ui_layout) != ui_layout
    ):
        raise RuntimeError("native leader world changed during preparation")
    return (
        int(battle_info),
        int(location_base),
        int(current_location),
        current_layout,
        ui_layout,
        int(actor_manager),
        int(actor_count),
    )


def prepare_native_leader_role_transfer(
    process,
    transfer,
    snapshot,
    party_actor_slots,
    actor_generations,
    *,
    world_epoch,
    manual_control_mask,
) -> bool:
    """Journal every changed follower role, publishing PREPARED last."""

    if transfer is None or transfer.native_applied:
        return False
    if (
        not world_epoch
        or snapshot.loading
        or snapshot.cutscene
        or snapshot.leader_party_member_id != snapshot.identity.p1_party_member_id
        or leader_role_layout_generation(snapshot.current_layout, snapshot.ui_layout)
        != transfer.layout_generation
        or set(_role_identity_members(snapshot.identity))
        != set(_role_identity_members(transfer.identity))
    ):
        return False

    mapped = session_state.map_party_slot_actors(snapshot, party_actor_slots)
    p1_entry = mapped.get(transfer.identity.p1_party_member_id)
    p1_mapping = fresh_role_actor_mapping(p1_entry)
    if not p1_mapping:
        return False
    p1_actor, p1_phys = p1_mapping
    if not p1_actor or not p1_phys:
        return False
    p1_alive = actor_is_alive(process, p1_actor)
    if p1_alive is not True:
        return False
    p1_native_handle = actor_u32_or_zero(process, p1_actor, 0x08)
    if not p1_native_handle:
        return False
    publishers = {
        "p2": movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX),
        "p3": movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX),
    }
    if transfer.prepared:
        journal = read_native_leader_role_journal(process, transfer)
        if journal is None or journal["world_epoch"] != int(world_epoch):
            return False
        try:
            for role in ("p2", "p3"):
                sequence = journal[f"{role}_sequence"]
                if not sequence:
                    continue
                reservation = publishers[role].reserve(journal[f"{role}_command"])
                if reservation.sequence != sequence:
                    raise RuntimeError("journal no longer owns the next generation")
        except (OSError, RuntimeError, struct.error, ValueError):
            for publisher in publishers.values():
                publisher.cancel_reserved()
            return False
        return True

    reserved_sequences = {"p2": 0, "p3": 0}
    journal_commands = {
        "p2": movement_mailbox.MovementCommand.inactive(world_epoch=world_epoch),
        "p3": movement_mailbox.MovementCommand.inactive(world_epoch=world_epoch),
    }
    journal_native_handles = {"p2": 0, "p3": 0}
    reserved_now = []
    prepared_publication_attempted = False
    role_specs = (
        (
            "p2",
            transfer.identity.p2_party_member_id,
            snapshot.identity.p2_party_member_id,
            P2_MANUAL_CONTROL_BIT,
        ),
        (
            "p3",
            transfer.identity.p3_party_member_id,
            snapshot.identity.p3_party_member_id,
            P3_MANUAL_CONTROL_BIT,
        ),
    )
    try:
        for role, desired_member, current_member, control_bit in role_specs:
            if desired_member is None or desired_member == current_member:
                continue
            publisher = publishers[role]
            if not int(manual_control_mask) & control_bit:
                command = movement_mailbox.MovementCommand.inactive(
                    world_epoch=world_epoch
                )
                reservation = publisher.reserve(command)
                reserved_sequences[role] = reservation.sequence
                journal_commands[role] = command
                reserved_now.append(publisher)
                continue
            entry = mapped.get(desired_member)
            actor, phys = fresh_role_actor_mapping(entry)
            if not actor or not phys:
                raise RuntimeError(f"{role} replacement actor is not materialized")
            alive = actor_is_alive(process, actor)
            if alive is None:
                raise RuntimeError(f"{role} replacement liveness is unknown")
            native_handle = actor_u32_or_zero(process, actor, 0x08)
            token = actor_generations.observe(
                role,
                desired_member,
                actor,
                phys,
                native_handle=native_handle,
                world_epoch=world_epoch,
                alive=alive,
                structurally_present=role_member_layout_coherent(
                    desired_member,
                    snapshot.current_layout,
                    snapshot.ui_layout,
                ),
            )
            if token is None or not token.native_handle:
                raise RuntimeError(f"{role} replacement identity is not stable")
            # The native handle exists only to authenticate a pointer-bearing
            # active command in the game-thread callback.  KO/Stone/manual-off
            # records are canonical inactive payloads and must carry zero so
            # no stale actor identity is accidentally treated as live.
            journal_native_handles[role] = (
                int(token.native_handle) & 0xFFFFFFFF if alive else 0
            )
            previous = publisher.last_command
            try:
                leader_heading, leader_visual = leader_heading_pair(process, phys)
            except ACTOR_SAMPLE_ERRORS:
                leader_heading = leader_visual = 0.0
            command = movement_mailbox.MovementCommand(
                bool(alive),
                actor if alive else 0,
                phys if alive else 0,
                desired_member if alive else movement_mailbox.INACTIVE_MEMBER_ID,
                token.actor_generation if alive else 0,
                world_epoch,
                previous.heading if previous is not None else leader_heading,
                previous.visual_heading if previous is not None else leader_visual,
                previous.magnitude if previous is not None and alive else 0.0,
            )
            reservation = publisher.reserve(command)
            reserved_sequences[role] = reservation.sequence
            journal_commands[role] = command
            reserved_now.append(publisher)

        live_world_record = _native_leader_role_live_world_record(
            process, snapshot
        )
        journal_base = NATIVE_LEADER_ROLE_JOURNAL
        previous_journal_sequence = process.u32(journal_base)
        journal_sequence = (previous_journal_sequence + 2) & 0xFFFFFFFE
        if journal_sequence == 0:
            journal_sequence = 2
        journal = bytearray(NATIVE_LEADER_JOURNAL_RECORD_SIZE)
        struct.pack_into("<I", journal, 0, journal_sequence | 1)
        struct.pack_into(
            "<3I",
            journal,
            NATIVE_LEADER_JOURNAL_REQUEST_OFFSET,
            transfer.sequence,
            int(world_epoch),
            transfer.layout_generation,
        )
        struct.pack_into(
            "<I",
            journal,
            NATIVE_LEADER_JOURNAL_P2_SEQUENCE_OFFSET,
            reserved_sequences["p2"],
        )
        journal[
            NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET : NATIVE_LEADER_JOURNAL_P2_PAYLOAD_OFFSET
            + movement_mailbox.MAILBOX_SIZE
            - 4
        ] = journal_commands["p2"].payload()
        struct.pack_into(
            "<I",
            journal,
            NATIVE_LEADER_JOURNAL_P3_SEQUENCE_OFFSET,
            reserved_sequences["p3"],
        )
        journal[
            NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET : NATIVE_LEADER_JOURNAL_P3_PAYLOAD_OFFSET
            + movement_mailbox.MAILBOX_SIZE
            - 4
        ] = journal_commands["p3"].payload()
        struct.pack_into(
            "<3I",
            journal,
            NATIVE_LEADER_JOURNAL_P1_HUD_SLOT_OFFSET,
            transfer.p1_hud_slot,
            transfer.p2_hud_slot,
            transfer.p3_hud_slot,
        )
        struct.pack_into(
            "<3I",
            journal,
            NATIVE_LEADER_JOURNAL_P1_ROLE_OFFSET,
            int(transfer.identity.p1_party_member_id),
            _native_role_member(transfer.identity.p2_party_member_id),
            _native_p3_role_member(transfer.identity),
        )
        (
            battle_info,
            location_base,
            location_id,
            live_current,
            live_ui,
            actor_manager,
            actor_count,
        ) = live_world_record
        struct.pack_into(
            "<QQI",
            journal,
            NATIVE_LEADER_JOURNAL_BATTLE_INFO_OFFSET,
            battle_info,
            location_base,
            location_id,
        )
        journal[
            NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET:
            NATIVE_LEADER_JOURNAL_CURRENT_LAYOUT_OFFSET + 8
        ] = live_current
        journal[
            NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET:
            NATIVE_LEADER_JOURNAL_UI_LAYOUT_OFFSET + 8
        ] = live_ui
        struct.pack_into(
            "<2I",
            journal,
            NATIVE_LEADER_JOURNAL_P2_NATIVE_HANDLE_OFFSET,
            journal_native_handles["p2"],
            journal_native_handles["p3"],
        )
        struct.pack_into(
            "<QI",
            journal,
            NATIVE_LEADER_JOURNAL_ACTOR_MANAGER_OFFSET,
            actor_manager,
            actor_count,
        )
        # Publish the odd commit word in its own aligned scalar write before
        # touching any payload byte.  A large cross-process copy is not an
        # atomic substitute: partial visibility could otherwise leave the old
        # even word bracketing a mixture of old/new payload.
        process.write(journal_base, struct.pack("<I", journal_sequence | 1))
        process.write(journal_base + 4, journal[4:])
        process.write(journal_base, struct.pack("<I", journal_sequence))

        base = P2_DEVICE_FILTER_STATE
        process.write(
            base + NATIVE_LEADER_ROLE_P2_MOVEMENT_SEQUENCE_OFFSET,
            struct.pack(
                "<2I",
                reserved_sequences["p2"],
                reserved_sequences["p3"],
            ),
        )
        process.write(
            base + NATIVE_LEADER_ROLE_WORLD_EPOCH_OFFSET,
            struct.pack("<I", int(world_epoch)),
        )
        process.write(
            base + NATIVE_LEADER_ROLE_P2_NATIVE_HANDLE_OFFSET,
            struct.pack(
                "<3I",
                journal_native_handles["p2"],
                journal_native_handles["p3"],
                p1_native_handle,
            ),
        )
        process.write(
            base + NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,
            struct.pack("<I", journal_sequence),
        )
        if (
            _native_leader_role_live_world_record(process, snapshot)
            != live_world_record
            or process.u32(base + NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET)
            != transfer.sequence
            or process.u32(base + NATIVE_LEADER_ROLE_ACTIVE_OFFSET)
            != NATIVE_LEADER_ROLE_ACTIVE
            or process.u32(base + NATIVE_LEADER_ROLE_LAYOUT_GENERATION_OFFSET)
            != leader_role_layout_generation(
                snapshot.current_layout, snapshot.ui_layout
            )
            or process.u32(journal_base) != journal_sequence
        ):
            raise RuntimeError("leader role transfer changed during preparation")
        prepared_publication_attempted = True
        process.write(
            base + NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
            struct.pack("<I", transfer.sequence),
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        preserve_reservations = False
        if prepared_publication_attempted:
            try:
                preserve_reservations = bool(
                    process.u32(
                        base + NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
                    )
                    == transfer.sequence
                    or process.u32(
                        base + NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
                    )
                    == transfer.sequence
                    or process.u32(base + NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET)
                    == transfer.sequence
                )
            except (OSError, RuntimeError, struct.error, ValueError):
                # An ambiguous remote write is not proof of rollback.  Keep
                # the reservation/write freeze for restart recovery.
                preserve_reservations = True
        if not preserve_reservations:
            for publisher in reversed(reserved_now):
                publisher.cancel_reserved()
        return preserve_reservations
    return True


def settle_native_leader_role_transfer_publishers(process, transfer) -> bool:
    """Adopt a native journal commit, or release an unclaimed reservation."""

    publishers = (
        (
            "p2",
            movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX),
            transfer.p2_movement_sequence if transfer is not None else 0,
        ),
        (
            "p3",
            movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX),
            transfer.p3_movement_sequence if transfer is not None else 0,
        ),
    )
    if transfer is not None and transfer.native_applied:
        complete = True
        committed = []
        for role, publisher, sequence in publishers:
            if not sequence:
                continue
            if publisher.reserved is not None:
                complete = publisher.finalize_reserved(sequence) and complete
            elif publisher.sequence != sequence:
                complete = publisher.adopt_committed(sequence) and complete
            if publisher.sequence != sequence or publisher.last_command is None:
                complete = False
            committed.append((role, publisher, sequence))
        if complete:
            mirror_generations = getattr(
                process,
                "_native_leader_legacy_mirror_generations",
                {},
            )
            for role, publisher, sequence in committed:
                generation = (int(transfer.sequence), int(sequence))
                if mirror_generations.get(role) == generation:
                    continue
                if role == "p2":
                    synchronize_second_player_legacy_movement_mirror(
                        process,
                        publisher.last_command,
                    )
                else:
                    synchronize_third_player_legacy_movement_mirror(
                        process,
                        publisher.last_command,
                    )
                mirror_generations[role] = generation
            process._native_leader_legacy_mirror_generations = mirror_generations
        return complete
    if transfer is not None:
        claimed = process.u32(
            P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
        )
        if claimed == transfer.sequence:
            return False
    for _role, publisher, _sequence in publishers:
        publisher.cancel_reserved()
    return False


@dataclass
class NativeLeaderRolePrepareCancellationTracker:
    """Require a stable coherent roster mismatch before pre-PREPARED cancel."""

    required_samples: int = 3
    signature: tuple | None = None
    samples: int = 0

    def reset(self) -> None:
        self.signature = None
        self.samples = 0

    def observe_signature(self, signature) -> bool:
        if signature is None:
            self.reset()
            return False
        if signature == self.signature:
            self.samples += 1
        else:
            self.signature = signature
            self.samples = 1
        return self.samples >= max(1, int(self.required_samples))

    def observe(self, transfer, snapshot) -> bool:
        return self.observe_signature(
            native_leader_role_prepare_mismatch_signature(
                transfer,
                snapshot,
            )
        )


def native_leader_role_target_unavailable_signature(
    process,
    transfer,
    snapshot,
    party_actor_slots,
):
    """Describe a stable pre-PREPARED target that cannot become P1."""

    if transfer is None or transfer.prepared or transfer.native_applied:
        return None
    if (
        snapshot.loading
        or snapshot.cutscene
        or not snapshot.battle_info
        or not snapshot.user_control_enabled
        or snapshot.native_menu_open
        or snapshot.native_modal_active
    ):
        return None
    if (
        leader_role_layout_generation(snapshot.current_layout, snapshot.ui_layout)
        != transfer.layout_generation
        or set(_role_identity_members(snapshot.identity))
        != set(_role_identity_members(transfer.identity))
    ):
        return None
    target_member_id = transfer.identity.p1_party_member_id
    mapped = session_state.map_party_slot_actors(snapshot, party_actor_slots)
    target_mapping = fresh_role_actor_mapping(mapped.get(target_member_id))
    if not target_mapping:
        actor = phys = 0
    else:
        actor, phys = target_mapping
    if not actor or not phys:
        return (
            int(transfer.sequence),
            "target-not-materialized",
            int(target_member_id),
            int(transfer.layout_generation),
        )
    alive = actor_is_alive(process, actor)
    if alive is False:
        return (
            int(transfer.sequence),
            "target-dead",
            int(target_member_id),
            int(actor),
            int(actor_u32_or_zero(process, actor, 0x08)),
            int(transfer.layout_generation),
        )
    if alive is None:
        return None
    available = actor_available_for_native_leader(process, actor)
    if available is not False:
        return None
    return (
        int(transfer.sequence),
        "target-command-blocked",
        int(target_member_id),
        int(actor),
        int(actor_u32_or_zero(process, actor, 0x08)),
        int(transfer.layout_generation),
    )


def native_leader_role_prepare_mismatch_signature(transfer, snapshot):
    """Authenticate a converged pre-PREPARED roster mismatch.

    ``SessionSnapshot.loading`` is identity-relative: removing the old P1 role
    makes it true even after Current/UI have converged on a perfectly coherent
    new roster.  Cancellation therefore uses only live structural facts.  A
    genuine area/menu/control transition remains ineligible and resets the
    debounce; a stable coherent mismatch is quarantined from its first sample.
    """

    if transfer is None or transfer.prepared or transfer.native_applied:
        return None
    if (
        not snapshot.battle_info
        or not snapshot.user_control_enabled
        or snapshot.native_menu_open
        or snapshot.native_modal_active
        or snapshot.cutscene
    ):
        return None
    current_location = snapshot.current_location
    ui_location = snapshot.ui_location
    if (
        current_location is None
        or ui_location is None
        or int(current_location) == 0xFFFFFFFF
        or int(ui_location) == 0xFFFFFFFF
        or int(current_location) != int(ui_location)
    ):
        return None
    current_layout = tuple(int(value) for value in snapshot.current_layout[:4])
    ui_layout = tuple(int(value) for value in snapshot.ui_layout[:4])
    current_members = tuple(
        value for value in current_layout if value != session_state.EMPTY_PARTY_ID
    )
    ui_members = tuple(
        value for value in ui_layout if value != session_state.EMPTY_PARTY_ID
    )
    if (
        not current_members
        or len(set(current_members)) != len(current_members)
        or len(set(ui_members)) != len(ui_members)
        or set(current_members) != set(ui_members)
    ):
        return None
    native_leader = snapshot.leader_party_member_id
    if (
        native_leader is not None
        and native_leader not in session_state.MAIN_PARTY_MEMBER_IDS
    ):
        return None
    observed_generation = leader_role_layout_generation(
        current_layout,
        ui_layout,
    )
    observed_members = set(current_members)
    # The identity also retains configured roles which are currently in
    # reserve.  They are intentionally absent from Current/UI and must not turn
    # an otherwise unchanged two-character layout into a cancellation.  The
    # request already proved that every role changed by this permutation is in
    # both layouts; the exact layout generation is therefore the authoritative
    # pre-PREPARED roster check.
    if observed_generation == transfer.layout_generation:
        return None
    return (
        int(transfer.sequence),
        int(snapshot.battle_info),
        int(current_location),
        int(observed_generation),
        tuple(sorted(observed_members)),
        current_layout,
        ui_layout,
    )


def cancel_native_leader_role_transfer_before_prepared(process, transfer) -> bool:
    """Rollback a stable invalid request while movement still owns authority."""

    if transfer is None or transfer.prepared or transfer.native_applied:
        return False
    fence_raw = process.read(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
    if not isinstance(fence_raw, (bytes, bytearray, memoryview)) or len(
        fence_raw
    ) != 4:
        raise struct.error("native leader cancellation fence is unreadable")
    if struct.unpack("<I", fence_raw)[0] != 0:
        return False
    first = _read_native_leader_role_transaction_words(process)
    confirm = _read_native_leader_role_transaction_words(process)
    sequence = int(transfer.sequence)
    if first != confirm:
        raise RuntimeError("native leader pre-PREPARED cancel sample changed")
    if (
        first["mode"] != NATIVE_LEADER_ROLE_ACTIVE
        or first["request"] != sequence
        or first["role_sequence"] != sequence
        or first["applied"] == sequence
        or first["prepared"] == sequence
        or first["claimed"] == sequence
        or first["claimed"] != first["committed"]
    ):
        return False

    _write_native_leader_role_word_confirmed(
        process,
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        NATIVE_LEADER_ROLE_CANCELLING,
        label="native leader pre-PREPARED cancellation claim",
    )
    fence_raw = process.read(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
    if not isinstance(fence_raw, (bytes, bytearray, memoryview)) or len(
        fence_raw
    ) != 4:
        raise struct.error("native leader cancellation fence is unreadable")
    if struct.unpack("<I", fence_raw)[0] != 0:
        # CANCELLING is a durable recovery marker. Never roll it back to
        # ACTIVE when shutdown wins after movement has claimed cancellation.
        raise RuntimeError("movement shutdown fenced an in-flight cancellation")
    publishers = (
        movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX),
        movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX),
    )
    for publisher in publishers:
        publisher.cancel_reserved()
    if any(publisher.reserved is not None for publisher in publishers):
        raise RuntimeError("leader cancellation retained a movement reservation")
    if not _recover_native_leader_role_cancellation(process):
        raise RuntimeError("native leader cancellation marker disappeared")
    if settle_native_leader_role_cancellation(process) != sequence:
        raise RuntimeError("native leader cancellation reservations did not settle")
    return True


def native_leader_role_transfer_write_authority_crossed(
    transfer,
    *,
    prepare_reported=False,
    claimed_sequence=0,
) -> bool:
    """Decide whether the current Python frame has lost write authority."""

    return bool(
        transfer is None
        or prepare_reported
        or transfer.prepared
        or transfer.native_applied
        or int(claimed_sequence) == transfer.sequence
    )


def acknowledge_native_leader_role_transfer(process, transfer) -> None:
    process.write(
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET,
        struct.pack("<I", transfer.sequence),
    )


def native_leader_role_transfer_committed(process, sequence) -> bool:
    transfer = read_native_leader_role_transfer(process)
    return bool(
        transfer is not None
        and transfer.sequence == int(sequence)
        and transfer.native_applied
        and transfer.committed
    )


def native_leader_role_transfer_cancelled(process, sequence) -> bool:
    """Observe movement's terminal pre-PREPARED cancellation state."""

    sequence = int(sequence) & 0xFFFFFFFF
    if not sequence:
        return False
    first = _read_native_leader_role_transaction_words(process)
    confirm = _read_native_leader_role_transaction_words(process)
    if first != confirm:
        return False
    return bool(first["cancelled"] == sequence)


def native_leader_role_transfer_outcome(process, sequence) -> str:
    """Read one coherent terminal outcome, with CANCELLED taking precedence."""

    sequence = int(sequence) & 0xFFFFFFFF
    if not sequence:
        return "pending"
    first = _read_native_leader_role_transaction_words(process)
    confirm = _read_native_leader_role_transaction_words(process)
    if first != confirm:
        return "pending"
    if first["cancelled"] == sequence:
        return "cancelled"
    if first["applied"] == sequence:
        return "applied"
    return "pending"


def native_leader_refresh_applied(process, sequence) -> bool:
    return process.u32(
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    ) == int(sequence)


def request_native_area_transition(
    process,
    target_area,
    spawn_point,
    options=0,
    flag=1,
) -> int:
    values = tuple(
        int(value)
        for value in (
            target_area,
            spawn_point,
            options,
            flag,
        )
    )
    if values[0] < 0 or values[0] == 0xFFFFFFFF:
        return 0
    if values[1] < 0:
        return 0
    if any(not 0 <= value <= 0xFFFFFFFF for value in values):
        return 0

    request_address = P2_DEVICE_FILTER_STATE + NATIVE_AREA_REQUEST_SEQUENCE_OFFSET
    sequence = (process.u32(request_address) + 1) & 0xFFFFFFFF
    if sequence == 0:
        sequence = 1
    process.write(
        P2_DEVICE_FILTER_STATE + NATIVE_AREA_TARGET_OFFSET,
        struct.pack("<4I", *values),
    )
    process.write(request_address, struct.pack("<I", sequence))
    return sequence


def publish_native_leader_handoff_active(process, active) -> None:
    process.write(
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET,
        struct.pack(
            "<I",
            NATIVE_LEADER_HANDOFF_TRANSIENT if bool(active) else 0,
        ),
    )


def claim_native_leader_transient_handoff(process) -> bool:
    """Claim the transient request lane without racing a role transaction."""

    base = P2_DEVICE_FILTER_STATE
    role_active_address = base + NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    transient_address = base + NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
    if process.u32(role_active_address):
        return False
    if process.u32(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE) != process.u32(
        NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE
    ):
        return False
    process.write(
        transient_address,
        struct.pack("<I", NATIVE_LEADER_HANDOFF_TRANSIENT),
    )
    if process.u32(role_active_address) or process.u32(
        NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE
    ) != process.u32(NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE):
        # The role producer crossed its CLAIMING word concurrently.  Neither
        # lane publishes shared member/request words in this tie.  A terminal
        # cancellation marker has the same precedence until its consumer ACK.
        if process.u32(transient_address) == NATIVE_LEADER_HANDOFF_TRANSIENT:
            process.write(transient_address, struct.pack("<I", 0))
        return False
    return True


def publish_native_leader_role_transfer_active(process, active) -> None:
    """Publish only the role-transfer gate; transient KO/area owns 0x1C."""

    process.write(
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", NATIVE_LEADER_ROLE_ACTIVE if bool(active) else 0),
    )


def publish_native_leader_role_shutdown_fence(process, fenced) -> None:
    """Close/open role admission around movement lifetime transitions."""

    process.write(
        NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        struct.pack("<I", 1 if bool(fenced) else 0),
    )


def native_leader_role_transfer_recovery_required(process) -> bool:
    """Return whether fixed memory contains a role transaction to preserve."""

    base = P2_DEVICE_FILTER_STATE
    raw = process.read(base, P2_DEVICE_FILTER_STATE_SIZE)
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != (
        P2_DEVICE_FILTER_STATE_SIZE
    ):
        raise struct.error("native leader role state is unreadable")
    _member, request, applied, transient_mode = struct.unpack_from(
        "<4I", raw, NATIVE_LEADER_MEMBER_OFFSET
    )
    role_sequence = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_ROLE_SEQUENCE_OFFSET
    )[0]
    prepared = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
    )[0]
    committed = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET
    )[0]
    claimed = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
    )[0]
    role_active = struct.unpack_from(
        "<I", raw, NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )[0]
    cancellation_raw = process.read(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE, 8)
    if not isinstance(
        cancellation_raw, (bytes, bytearray, memoryview)
    ) or len(cancellation_raw) != 8:
        raise struct.error("native leader cancellation state is unreadable")
    cancelled, _fence_word = struct.unpack("<2I", cancellation_raw)
    movement_ack_raw = process.read(
        NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE, 4
    )
    if not isinstance(
        movement_ack_raw, (bytes, bytearray, memoryview)
    ) or len(movement_ack_raw) != 4:
        raise struct.error("native leader movement cancellation acknowledgement is unreadable")
    movement_cancel_ack = struct.unpack("<I", movement_ack_raw)[0]
    cancel_ack_raw = process.read(NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE, 4)
    if not isinstance(
        cancel_ack_raw, (bytes, bytearray, memoryview)
    ) or len(cancel_ack_raw) != 4:
        raise struct.error("native leader cancellation acknowledgement is unreadable")
    cancel_ack = struct.unpack("<I", cancel_ack_raw)[0]
    legacy_role_mode = transient_mode == NATIVE_LEADER_HANDOFF_ROLE_TRANSFER
    authenticated_inactive = bool(
        request
        and request != committed
        and role_sequence == request
        and (prepared == request or claimed == request)
    )
    cancellation_pending = bool(
        cancelled
        and (
            movement_cancel_ack != cancelled
            or cancel_ack != cancelled
            or role_active == NATIVE_LEADER_ROLE_CANCELLED
        )
        and role_active == NATIVE_LEADER_ROLE_CANCELLED
        and request == cancelled
        and applied == cancelled
        and role_sequence == cancelled
        and claimed != cancelled
    )
    return bool(
        role_active
        or legacy_role_mode
        or authenticated_inactive
        or cancellation_pending
    )


def _read_native_leader_role_transaction_words(process) -> dict[str, int]:
    raw = process.read(P2_DEVICE_FILTER_STATE, P2_DEVICE_FILTER_STATE_SIZE)
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != (
        P2_DEVICE_FILTER_STATE_SIZE
    ):
        raise struct.error("native leader role state is unreadable")
    cancelled_raw = process.read(NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE, 4)
    if not isinstance(
        cancelled_raw, (bytes, bytearray, memoryview)
    ) or len(cancelled_raw) != 4:
        raise struct.error("native leader cancellation state is unreadable")
    movement_ack_raw = process.read(
        NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE, 4
    )
    if not isinstance(
        movement_ack_raw, (bytes, bytearray, memoryview)
    ) or len(movement_ack_raw) != 4:
        raise struct.error(
            "native leader movement cancellation acknowledgement is unreadable"
        )
    cancel_ack_raw = process.read(NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE, 4)
    if not isinstance(
        cancel_ack_raw, (bytes, bytearray, memoryview)
    ) or len(cancel_ack_raw) != 4:
        raise struct.error("native leader cancellation acknowledgement is unreadable")
    return {
        "request": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
        )[0],
        "applied": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
        )[0],
        "role_sequence": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_SEQUENCE_OFFSET
        )[0],
        "prepared": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
        )[0],
        "committed": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET
        )[0],
        "claimed": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
        )[0],
        "mode": struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_ACTIVE_OFFSET
        )[0],
        "cancelled": struct.unpack("<I", cancelled_raw)[0],
        "movement_cancel_ack": struct.unpack("<I", movement_ack_raw)[0],
        "cancel_ack": struct.unpack("<I", cancel_ack_raw)[0],
    }


def _write_native_leader_role_word_confirmed(
    process,
    address,
    value,
    *,
    label,
) -> None:
    payload = struct.pack("<I", int(value) & 0xFFFFFFFF)
    write_error = None
    try:
        process.write(address, payload)
    except (OSError, RuntimeError, struct.error, ValueError) as error:
        write_error = error
    raw = process.read(address, 4)
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != 4:
        raise struct.error(f"{label} readback is unavailable")
    if bytes(raw) != payload:
        if write_error is not None:
            raise RuntimeError(f"{label} publication is ambiguous") from write_error
        raise RuntimeError(f"{label} publication did not persist")


def native_leader_role_cancellation_pending(process) -> bool:
    """Return whether headless still owes ACK for a terminal cancellation."""

    first = _read_native_leader_role_transaction_words(process)
    confirm = _read_native_leader_role_transaction_words(process)
    if first != confirm:
        return True
    sequence = first["cancelled"]
    return bool(
        sequence
        and first["cancel_ack"] != sequence
        and first["mode"] == NATIVE_LEADER_ROLE_CANCELLED
        and first["request"] == sequence
        and first["applied"] == sequence
        and first["role_sequence"] == sequence
        and first["prepared"] == 0
        and first["claimed"] != sequence
        and first["claimed"] == first["committed"]
    )


def acknowledge_native_leader_role_cancellation(process, sequence=None) -> int:
    """ACK and release one terminal cancel without opening admission early."""

    first = _read_native_leader_role_transaction_words(process)
    confirm = _read_native_leader_role_transaction_words(process)
    if first != confirm:
        return 0
    cancelled = first["cancelled"]
    if sequence is not None and cancelled != (int(sequence) & 0xFFFFFFFF):
        return 0
    terminal_fields_valid = bool(
        cancelled
        and first["request"] == cancelled
        and first["applied"] == cancelled
        and first["role_sequence"] == cancelled
        and first["prepared"] == 0
        and first["claimed"] != cancelled
    )
    if not terminal_fields_valid:
        return 0
    if first["mode"] == 0 and first["cancel_ack"] == cancelled:
        return cancelled
    if first["mode"] != NATIVE_LEADER_ROLE_CANCELLED:
        return 0
    # Only movement owns the in-process reservation objects.  Headless may
    # observe the tombstone first, but must not reopen either producer lane
    # until movement has released every staged mailbox generation.
    if first["movement_cancel_ack"] != cancelled:
        return 0
    if first["cancel_ack"] != cancelled:
        _write_native_leader_role_word_confirmed(
            process,
            NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE,
            cancelled,
            label="native leader cancellation consumer acknowledgement",
        )
    # The terminal mode blocks both role and transient producers until the
    # consumer has durably acknowledged the cancellation.  Clear it last;
    # land-and-raise is accepted only through exact readback.
    _write_native_leader_role_word_confirmed(
        process,
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        0,
        label="native leader cancellation admission release",
    )
    return cancelled


def settle_native_leader_role_cancellation(process) -> int:
    """Release staged mailbox reservations and ACK them from movement."""

    first = _read_native_leader_role_transaction_words(process)
    confirm = _read_native_leader_role_transaction_words(process)
    if first != confirm:
        return 0
    sequence = first["cancelled"]
    if not (
        sequence
        and first["mode"] == NATIVE_LEADER_ROLE_CANCELLED
        and first["request"] == sequence
        and first["applied"] == sequence
        and first["role_sequence"] == sequence
        and first["prepared"] == 0
        and first["claimed"] != sequence
        and first["claimed"] == first["committed"]
    ):
        return 0
    publishers = (
        movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX),
        movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX),
    )
    for publisher in publishers:
        publisher.cancel_reserved()
    if any(publisher.reserved is not None for publisher in publishers):
        raise RuntimeError("leader cancellation retained a movement reservation")
    if first["movement_cancel_ack"] != sequence:
        _write_native_leader_role_word_confirmed(
            process,
            NATIVE_LEADER_ROLE_CANCEL_MOVEMENT_ACK_SEQUENCE,
            sequence,
            label="native leader movement cancellation acknowledgement",
        )
    return sequence


def _recover_native_leader_role_cancellation(process) -> bool:
    """Finish movement-owned rollback after the CANCELLING marker lands."""

    first = _read_native_leader_role_transaction_words(process)
    if first["mode"] != NATIVE_LEADER_ROLE_CANCELLING:
        return False
    confirm = _read_native_leader_role_transaction_words(process)
    if confirm != first:
        raise RuntimeError("native leader cancellation state changed")
    sequence = first["role_sequence"]
    if (
        not sequence
        or first["request"] != sequence
        or (
            first["applied"] == sequence
            and first["cancelled"] != sequence
        )
        or first["prepared"] not in (0, sequence)
        or first["claimed"] == sequence
        or first["claimed"] != first["committed"]
    ):
        raise RuntimeError("native leader cancellation crossed commit authority")

    # A native world-stale result first publishes CANCELLING while PREPARED is
    # still visible, then withdraws PREPARED before its tombstone.  A hard stop
    # may occur between either scalar store.  Movement may finish that exact
    # prefix idempotently, but no other PREPARED value is ever discarded.
    if first["prepared"] == sequence:
        _write_native_leader_role_word_confirmed(
            process,
            P2_DEVICE_FILTER_STATE
            + NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
            0,
            label="native leader cancellation prepared withdrawal",
        )
    _write_native_leader_role_word_confirmed(
        process,
        NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE,
        sequence,
        label="native leader cancellation marker",
    )
    _write_native_leader_role_word_confirmed(
        process,
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
        sequence,
        label="native leader cancellation acknowledgement",
    )
    _write_native_leader_role_word_confirmed(
        process,
        P2_DEVICE_FILTER_STATE + NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        NATIVE_LEADER_ROLE_CANCELLED,
        label="native leader cancellation terminal mode",
    )
    final = _read_native_leader_role_transaction_words(process)
    if (
        final["mode"] != NATIVE_LEADER_ROLE_CANCELLED
        or final["request"] != sequence
        or final["applied"] != sequence
        or final["cancelled"] != sequence
        or final["prepared"] != 0
        or final["claimed"] == sequence
    ):
        raise RuntimeError("native leader cancellation final state is torn")
    return True


def native_leader_role_claim_in_progress(process) -> bool:
    """Conservatively detect a producer between CLAIMING and ACTIVE."""

    base = P2_DEVICE_FILTER_STATE

    def read_mode() -> int:
        raw = process.read(base, P2_DEVICE_FILTER_STATE_SIZE)
        if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != (
            P2_DEVICE_FILTER_STATE_SIZE
        ):
            raise struct.error("native leader role state is unreadable")
        return struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_ACTIVE_OFFSET
        )[0]

    # If either side of the sample observes CLAIMING, freeze this frame. A
    # transition to ACTIVE is consumed on the next frame; a stalled producer
    # is repaired only by fenced startup with explicit quiescence authority.
    return NATIVE_LEADER_ROLE_CLAIMING in (read_mode(), read_mode())


def _recover_abandoned_native_leader_role_claim(process) -> bool:
    """Clear one proven pre-request CLAIMING publication under a closed fence.

    A helper may die, or WriteProcessMemory may land and raise, immediately
    after publishing CLAIMING. No role payload is authenticated at that
    point. The claim is abortable only while admission is durably closed and
    both request and irreversible-commit pairs prove that nothing advanced.
    Every read is repeated around the clear; an ambiguous clear is accepted
    only when its exact postcondition can be read back.
    """

    base = P2_DEVICE_FILTER_STATE

    def read_fence() -> int:
        raw = process.read(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
        if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != 4:
            raise struct.error("native leader shutdown fence is unreadable")
        return struct.unpack("<I", raw)[0]

    def read_claim_state() -> tuple[int, int, int, int, int]:
        raw = process.read(base, P2_DEVICE_FILTER_STATE_SIZE)
        if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != (
            P2_DEVICE_FILTER_STATE_SIZE
        ):
            raise struct.error("native leader role state is unreadable")
        request = struct.unpack_from(
            "<I", raw, NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
        )[0]
        applied = struct.unpack_from(
            "<I", raw, NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
        )[0]
        committed = struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET
        )[0]
        claimed = struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
        )[0]
        role_active = struct.unpack_from(
            "<I", raw, NATIVE_LEADER_ROLE_ACTIVE_OFFSET
        )[0]
        return request, applied, claimed, committed, role_active

    if read_fence() != 1:
        return False
    first = read_claim_state()
    request, applied, claimed, committed, role_active = first
    if role_active != NATIVE_LEADER_ROLE_CLAIMING:
        return False
    if request != applied or claimed != committed:
        return False
    if read_claim_state() != first or read_fence() != 1:
        raise RuntimeError("native leader role claim changed during recovery")

    clear_error = None
    try:
        process.write(
            base + NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
            struct.pack("<I", 0),
        )
    except (OSError, RuntimeError, struct.error, ValueError) as error:
        clear_error = error

    final = read_claim_state()
    final_fence = read_fence()
    expected = (request, applied, claimed, committed, 0)
    if final != expected or final_fence != 1:
        if clear_error is not None:
            raise RuntimeError(
                "abandoned native leader role claim clear is ambiguous"
            ) from clear_error
        raise RuntimeError("abandoned native leader role claim did not stay clear")
    return True


def repair_abandoned_native_leader_role_claim_for_restart(process) -> bool:
    """Repair only a quiesced pre-request CLAIMING prefix under CLOSED fence.

    The supervisor has no ownership of movement's process-local mailbox
    reservations, so it must not run the general transfer adoption path.  This
    narrow operation is the sole repair safe between a proven producer exit
    and the replacement consumer startup.
    """

    first_fence = process.read(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
    if not isinstance(
        first_fence, (bytes, bytearray, memoryview)
    ) or len(first_fence) != 4:
        raise struct.error("native leader shutdown fence is unreadable")
    if struct.unpack("<I", first_fence)[0] != 1:
        raise RuntimeError("abandoned role claim repair requires closed admission")
    if not native_leader_role_claim_in_progress(process):
        return False
    repaired = _recover_abandoned_native_leader_role_claim(process)
    final_fence = process.read(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
    if not isinstance(
        final_fence, (bytes, bytearray, memoryview)
    ) or len(final_fence) != 4:
        raise struct.error("native leader shutdown fence readback is unavailable")
    if struct.unpack("<I", final_fence)[0] != 1:
        raise RuntimeError("role admission reopened during abandoned claim repair")
    return repaired


def native_party_leader(process):
    battle_info = process.u64(session_state.BATTLE_INFO_GLOBAL)
    if not battle_info:
        return None
    member_id = process.u32(battle_info + session_state.BATTLE_LEADER_OFFSET)
    return None if member_id == session_state.EMPTY_PARTY_ID else member_id


def area_transition_signature(process):
    try:
        return struct.unpack("<4I", process.read(AREA_ID_TABLE, 16))
    except (OSError, struct.error):
        return None


class FollowerGatewayStallDetector:
    def __init__(
        self,
        moving_magnitude=FOLLOWER_GATEWAY_MOVING_MAGNITUDE,
        progress_distance=FOLLOWER_GATEWAY_PROGRESS_DISTANCE,
        stall_seconds=FOLLOWER_GATEWAY_STALL_SECONDS,
    ):
        self.moving_magnitude = float(moving_magnitude)
        self.progress_distance_squared = float(progress_distance) ** 2
        self.stall_seconds = float(stall_seconds)
        self.states = {}

    def observe(
        self,
        player,
        position,
        magnitude,
        eligible,
        now,
        heading=None,
    ):
        player = int(player)
        moving = bool(
            eligible
            and position is not None
            and math.isfinite(float(position[0]))
            and math.isfinite(float(position[2]))
            and float(magnitude) >= self.moving_magnitude
        )
        if not moving:
            self.states.pop(player, None)
            return False

        x = float(position[0])
        z = float(position[2])
        state = self.states.get(player)
        finite_heading = heading is not None and math.isfinite(float(heading))
        current_heading = float(heading) if finite_heading else None
        if state is None:
            self.states[player] = [
                x,
                z,
                float(now),
                False,
                current_heading,
            ]
            return False
        if state[3]:
            return False

        dx = x - state[0]
        dz = z - state[1]
        anchor_heading = state[4] if len(state) > 4 else None
        if current_heading is not None and anchor_heading is not None:
            heading_delta = math.atan2(
                math.sin(current_heading - anchor_heading),
                math.cos(current_heading - anchor_heading),
            )

            if abs(heading_delta) >= 0.35:
                state[0] = x
                state[1] = z
                state[2] = float(now)
                state[4] = current_heading
                return False
            forward_progress = dx * math.sin(current_heading) + dz * math.cos(
                current_heading
            )
            made_progress = (
                forward_progress * forward_progress >= self.progress_distance_squared
                and forward_progress > 0.0
            )
        else:
            made_progress = dx * dx + dz * dz >= self.progress_distance_squared

        if made_progress:
            state[0] = x
            state[1] = z
            state[2] = float(now)
            state[4] = current_heading
            return False
        if float(now) - state[2] < self.stall_seconds:
            return False

        state[3] = True
        return True


class NativeAreaHandoffController:
    def __init__(self, detector=None):
        self.detector = detector or FollowerGatewayStallDetector()
        self.phase = "idle"
        self.player = None
        self.original_member_id = None
        self.follower_member_id = None
        self.original_area = None
        self.sequence = 0
        self.deadline = 0.0
        self.area_changed = False

    @property
    def active(self):
        return self.phase != "idle"

    def consider(
        self,
        process,
        player,
        position,
        magnitude,
        eligible,
        original_member_id,
        follower_member_id,
        now,
        heading=None,
    ):
        if self.active:
            return False
        triggered = self.detector.observe(
            player,
            position,
            magnitude,
            eligible,
            now,
            heading,
        )
        if not triggered:
            return False
        if (
            original_member_id is None
            or follower_member_id is None
            or int(original_member_id) == int(follower_member_id)
        ):
            return False
        try:
            if native_party_leader(process) != int(original_member_id):
                return False
            original_area = area_transition_signature(process)
            if original_area is None:
                return False

            if not claim_native_leader_transient_handoff(process):
                return False
            sequence = request_native_leader_refresh(process, follower_member_id)
        except (OSError, RuntimeError):
            try:
                publish_native_leader_handoff_active(process, False)
            except OSError:
                pass
            return False

        self.phase = "await_follower"
        self.player = int(player)
        self.original_member_id = int(original_member_id)
        self.follower_member_id = int(follower_member_id)
        self.original_area = original_area
        self.sequence = sequence
        self.deadline = float(now) + FOLLOWER_GATEWAY_REQUEST_TIMEOUT_SECONDS
        self.area_changed = False
        print(
            "FOLLOWER_AREA_HANDOFF_START "
            f"p={self.player} follower={self.follower_member_id} "
            f"area={self.original_area}",
            flush=True,
        )
        return True

    def _request_restore(self, process, now):
        self.sequence = request_native_leader_refresh(process, self.original_member_id)
        self.phase = "await_restore"
        self.deadline = float(now) + FOLLOWER_GATEWAY_RESTORE_RETRY_SECONDS

    def _finish(self, process):
        publish_native_leader_handoff_active(process, False)
        print(
            "FOLLOWER_AREA_HANDOFF_RESTORED "
            f"p={self.player} changed={self.area_changed}",
            flush=True,
        )
        self.phase = "idle"
        self.player = None
        self.original_member_id = None
        self.follower_member_id = None
        self.original_area = None
        self.sequence = 0
        self.deadline = 0.0
        self.area_changed = False

    def tick(self, process, now):
        if not self.active:
            return
        try:
            current_leader = native_party_leader(process)
            current_area = area_transition_signature(process)

            if self.phase == "await_follower":
                if native_leader_refresh_applied(process, self.sequence):
                    if current_leader == self.follower_member_id:
                        self.phase = "hold"
                        self.deadline = (
                            float(now) + FOLLOWER_GATEWAY_NATIVE_PULSE_SECONDS
                        )
                        print(
                            f"FOLLOWER_AREA_HANDOFF_PROMOTED p={self.player}",
                            flush=True,
                        )
                    else:
                        self._request_restore(process, now)
                elif float(now) >= self.deadline:
                    self._request_restore(process, now)
                return

            if self.phase == "hold":
                if current_leader == self.original_member_id:
                    self._finish(process)
                    return
                if (
                    current_area is not None
                    and self.original_area is not None
                    and current_area != self.original_area
                ):
                    self.area_changed = True
                if self.area_changed or float(now) >= self.deadline:
                    self._request_restore(process, now)
                return

            if self.phase == "await_restore":
                if current_leader == self.original_member_id:
                    self._finish(process)
                elif float(now) >= self.deadline:
                    self._request_restore(process, now)
        except (OSError, RuntimeError):
            return

    def shutdown(self, process, timeout_seconds=4.0):
        if not self.active:
            try:
                publish_native_leader_handoff_active(process, False)
            except OSError:
                pass
            return True
        try:
            self._request_restore(process, time.monotonic())
        except (OSError, RuntimeError):
            return False
        deadline = time.monotonic() + float(timeout_seconds)
        while self.active and time.monotonic() < deadline:
            self.tick(process, time.monotonic())
            if self.active:
                time.sleep(POLL_SECONDS)
        return not self.active


class NativeKoLeaderOwnershipController:
    """Own native-leader handoff while configured P1 is unavailable.

    The historical class name predates the status-aware native action gate.  Its
    tri-state input deliberately represents leader availability, not lifecycle
    liveness: non-KO statuses can leave the co-op session alive while making an
    actor ineligible for native leader ownership.
    """

    def __init__(self):
        self.original_member_id = None
        self.promoted_role = None
        self.promoted_member_id = None
        self.sequence = 0
        self.promotion_retry_at = 0.0
        self.restore_retry_at = 0.0
        self.unknown_promotion_role = None
        self.unknown_promotion_since = 0.0

    @property
    def active(self):
        return self.original_member_id is not None

    def _clear(self, process, *, publish=True):
        if publish:
            try:
                publish_native_leader_handoff_active(process, False)
            except OSError:
                pass
        if self.original_member_id is not None:
            print(
                "KO_LEADER_OWNERSHIP_END "
                f"p={self.promoted_role or 'none'} "
                f"leader={self.original_member_id}",
                flush=True,
            )
        self.original_member_id = None
        self.promoted_role = None
        self.promoted_member_id = None
        self.sequence = 0
        self.promotion_retry_at = 0.0
        self.restore_retry_at = 0.0
        self.unknown_promotion_role = None
        self.unknown_promotion_since = 0.0

    def _unknown_promotion_settled(self, role, now):
        """Debounce a follower leader while configured P1 cannot be sampled.

        X-Zone can remove P1's actor before the status word is readable.  The
        native game may already have promoted P2/P3 at that point.  One torn
        frame is not evidence of a handoff, but a stable follower leader plus a
        usable follower is enough to adopt the native promotion without ever
        changing the persisted role identity.
        """

        if role not in ("p2", "p3"):
            self.unknown_promotion_role = None
            self.unknown_promotion_since = 0.0
            return False
        if self.unknown_promotion_role != role:
            self.unknown_promotion_role = role
            self.unknown_promotion_since = float(now)
            return False
        return (
            float(now) - self.unknown_promotion_since
            >= KO_LEADER_UNKNOWN_PROMOTION_SETTLE_SECONDS
        )

    @staticmethod
    def _replacement(identity, p2_available, p3_available):
        if p2_available is True and identity.p2_party_member_id is not None:
            return "p2", int(identity.p2_party_member_id)
        if p3_available is True and identity.p3_party_member_id is not None:
            return "p3", int(identity.p3_party_member_id)
        return None, None

    @staticmethod
    def _role_availability(role, p2_available, p3_available):
        if role == "p2":
            return p2_available
        if role == "p3":
            return p3_available
        return False

    def _request_promotion(self, process, now):
        if self.promoted_member_id is None or float(now) < self.promotion_retry_at:
            return
        try:
            self.sequence = request_native_leader_refresh(
                process, self.promoted_member_id
            )
        except (OSError, RuntimeError):
            self.promotion_retry_at = float(now) + KO_LEADER_REQUEST_RETRY_SECONDS
            return
        self.promotion_retry_at = float(now) + KO_LEADER_REQUEST_RETRY_SECONDS
        print(
            "KO_LEADER_OWNERSHIP_PROMOTE_REQUEST "
            f"p={self.promoted_role} member={self.promoted_member_id} "
            f"seq={self.sequence}",
            flush=True,
        )

    def tick(
        self,
        process,
        identity,
        native_leader_member_id,
        configured_p1_available,
        *,
        p1_present,
        p2_available=None,
        p3_available=None,
        now,
        transient_area_handoff=False,
        native_leader_is_known_guest=False,
    ):
        current_promoted_role = session_state.ko_promoted_follower_role(
            identity,
            native_leader_member_id,
            False,
            p1_present=p1_present,
        )
        replacement_role, replacement_member_id = self._replacement(
            identity,
            p2_available,
            p3_available,
        )
        usable_promoted_role = current_promoted_role
        if (
            self._role_availability(
                current_promoted_role,
                p2_available,
                p3_available,
            )
            is False
        ):
            usable_promoted_role = None
        if not self.active:
            if transient_area_handoff or configured_p1_available is True:
                self._unknown_promotion_settled(None, now)
                return None
            if configured_p1_available is None and not self._unknown_promotion_settled(
                usable_promoted_role,
                now,
            ):
                return None
            if configured_p1_available is False:
                self._unknown_promotion_settled(None, now)
            if usable_promoted_role is None and (
                replacement_role is None
                or (
                    native_leader_member_id != identity.p1_party_member_id
                    and current_promoted_role is None
                )
            ):
                return None
            try:
                if not claim_native_leader_transient_handoff(process):
                    return None
            except (OSError, RuntimeError):
                return None
            self.original_member_id = int(identity.p1_party_member_id)
            self.promoted_role = usable_promoted_role or replacement_role
            self.promoted_member_id = int(
                native_leader_member_id
                if usable_promoted_role is not None
                else replacement_member_id
            )
            print(
                "KO_LEADER_OWNERSHIP_START "
                f"p={self.promoted_role} "
                f"member={self.promoted_member_id} "
                f"original={self.original_member_id}",
                flush=True,
            )
            if usable_promoted_role is None:
                self._request_promotion(process, now)
            return self.promoted_role

        if (
            int(identity.p1_party_member_id) != self.original_member_id
            or not p1_present
        ):
            self._clear(process)
            return None
        if configured_p1_available is True:
            if native_leader_member_id == self.original_member_id:
                self._clear(process)
                return None
            if float(now) >= self.restore_retry_at:
                try:
                    self.sequence = request_native_leader_refresh(
                        process, self.original_member_id
                    )
                except (OSError, RuntimeError):
                    self.restore_retry_at = float(now) + KO_LEADER_REQUEST_RETRY_SECONDS
                    return current_promoted_role
                self.restore_retry_at = float(now) + KO_LEADER_REQUEST_RETRY_SECONDS
                print(
                    "KO_LEADER_OWNERSHIP_RESTORE_REQUEST "
                    f"member={self.original_member_id} seq={self.sequence}",
                    flush=True,
                )
            return current_promoted_role

        if native_leader_member_id == self.original_member_id:
            if replacement_role is not None:
                role_changed = self.promoted_role != replacement_role
                self.promoted_role = replacement_role
                self.promoted_member_id = replacement_member_id
                if role_changed:
                    self.promotion_retry_at = 0.0
                self._request_promotion(process, now)
                return replacement_role
            self.promoted_role = None
            self.promoted_member_id = None
            return None
        if current_promoted_role is None:
            if native_leader_is_known_guest:
                self.promoted_member_id = int(native_leader_member_id)
                return None
            self._clear(process)
            return None

        current_promoted_available = self._role_availability(
            current_promoted_role,
            p2_available,
            p3_available,
        )
        if current_promoted_available is not False:
            self.promoted_role = current_promoted_role
            self.promoted_member_id = int(native_leader_member_id)
            return current_promoted_role

        if replacement_role is None:
            self.promoted_role = None
            self.promoted_member_id = None
            return None

        previous_role = current_promoted_role
        self.promoted_role = replacement_role
        self.promoted_member_id = replacement_member_id
        self.promotion_retry_at = 0.0
        print(
            "KO_LEADER_OWNERSHIP_FAILOVER "
            f"from={previous_role} to={replacement_role} "
            f"member={replacement_member_id}",
            flush=True,
        )
        self._request_promotion(process, now)
        return replacement_role

    def shutdown(self, process):
        if not self.active:
            return
        self._clear(process)


def install_keyboard_filter(process):
    original = b"\x48\x8b\xce\xe8\x3e\xfe\xff\xff"
    patch = b"\xe9" + rel32(KEYBOARD_HOOK_SITE, 5, KEYBOARD_CAVE) + b"\x90" * 3

    cave = bytearray()
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)

    cave += b"\x41\x83\x7b" + bytes((KEYBOARD_FILTER_ACTIVE_OFFSET,)) + b"\x00"
    inactive_jump = len(cave)
    cave += b"\x0f\x84\x00\x00\x00\x00"
    # The budget prevents the filter from outliving its helper if the helper exits
    # without clearing the flag. Without renewal, input returns to the game in
    # approximately one second.
    cave += b"\x49\xba" + struct.pack("<Q", KEYBOARD_FILTER_BUDGET)
    cave += b"\x41\x83\x3a\x00"
    expired_jump = len(cave)
    cave += b"\x0f\x8e\x00\x00\x00\x00"
    cave += b"\x41\xff\x0a"
    for state_offset in (0x30, 0x130, 0x231):
        cave += b"\x48\x8d\x8e" + struct.pack("<I", state_offset)
        cave += b"\x33\xd2"
        cave += b"\x41\xb8\x00\x01\x00\x00"
        memset_call = KEYBOARD_CAVE + len(cave)
        cave += b"\xe8" + rel32(memset_call, 5, MEMSET_FUNCTION)

    # The raw 256-byte DirectInput sample remains at rsp + 0x20 here.  Rebuild
    # only the verified shared-game keys in the normalized +0x231 state.  The
    # stock loop at 0x7A1730 uses the same signed-byte rule: a raw value with
    # its high bit set becomes 1, every other value becomes 0.
    cave += b"\x49\xba" + b"\x00" * 8
    passthrough_table_pointer = len(cave) - 8
    cave += b"\x49\xbb" + struct.pack("<Q", KEYBOARD_NATIVE_KEYMAP)
    cave += b"\x41\xb9" + struct.pack(
        "<I", len(KEYBOARD_NATIVE_PASSTHROUGH_INDICES)
    )
    passthrough_loop = len(cave)
    cave += b"\x41\x0f\xb6\x02"  # movzx eax, byte ptr [r10]
    cave += b"\x49\xff\xc2"  # inc r10
    cave += b"\x41\x0f\xb6\x14\x03"  # movzx edx, byte ptr [r11+rax]
    cave += b"\x80\x7c\x14\x20\x00"  # cmp byte ptr [rsp+rdx+0x20], 0
    cave += b"\x0f\x9c\xc2"  # setl dl
    cave += b"\x88\x94\x06\x31\x02\x00\x00"  # mov [rsi+rax+0x231], dl
    cave += b"\x41\xff\xc9"  # dec r9d
    loop_delta = passthrough_loop - (len(cave) + 2)
    if not -0x80 <= loop_delta <= 0x7F:
        raise RuntimeError(f"keyboard-passthrough loop overflow: {loop_delta:#x}")
    cave += b"\x75" + struct.pack("<b", loop_delta)

    passthrough_end = len(cave)
    for branch in (inactive_jump, expired_jump):
        skip = passthrough_end - (branch + 6)
        struct.pack_into("<i", cave, branch + 2, skip)
    cave += b"\x48\x8b\xce"
    call_site = KEYBOARD_CAVE + len(cave)
    cave += b"\xe8" + rel32(call_site, 5, KEYBOARD_FINALIZER)
    jump_site = KEYBOARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, KEYBOARD_HOOK_RETURN)

    passthrough_table = len(cave)
    struct.pack_into(
        "<Q",
        cave,
        passthrough_table_pointer,
        KEYBOARD_CAVE + passthrough_table,
    )
    cave += bytes(KEYBOARD_NATIVE_PASSTHROUGH_INDICES)
    if len(cave) > KEYBOARD_FILTER_BUDGET_CAVE_OFFSET:
        raise RuntimeError(
            "keyboard filter overlaps its lifetime budget: "
            f"{len(cave):#x} > {KEYBOARD_FILTER_BUDGET_CAVE_OFFSET:#x}"
        )

    mode, pending = _preflight_process_lifetime_detour(
        process,
        label="keyboard filter",
        site=KEYBOARD_HOOK_SITE,
        original=original,
        patch=patch,
        regions=(
            (
                "cave",
                KEYBOARD_CAVE,
                KEYBOARD_FILTER_BUDGET_CAVE_OFFSET,
                cave,
                False,
            ),
        ),
    )
    return _publish_process_lifetime_detour(
        process,
        site=KEYBOARD_HOOK_SITE,
        patch=patch,
        mode=mode,
        pending=pending,
    )


def install_facing_override(process):
    original = b"\xf3\x0f\x11\x83\x5c\x01\x00\x00"
    patch = b"\xe9" + rel32(FACING_HOOK_SITE, 5, FACING_CAVE) + b"\x90" * 3

    p2_mailbox = P2_MOVEMENT_MAILBOX_OFFSET - 0x200
    p3_mailbox = P3_MOVEMENT_MAILBOX_OFFSET - 0x200
    p1_berserk = P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
    cave = bytearray()
    fixups: list[tuple[int, str]] = []
    short_fixups: list[tuple[int, str]] = []
    labels: dict[str, int] = {}

    def jump(opcode: bytes, label: str) -> None:
        near = {
            b"\x72": b"\x0f\x82",
            b"\x74": b"\x0f\x84",
            b"\x75": b"\x0f\x85",
            b"\xeb": b"\xe9",
        }[opcode]
        cave.extend(near)
        fixups.append((len(cave), label))
        cave.extend(b"\x00\x00\x00\x00")

    def short_jump(opcode: bytes, label: str) -> None:
        cave.extend(opcode)
        short_fixups.append((len(cave), label))
        cave.extend(b"\x00")

    cave += b"\x50\x41\x52\x41\x51"
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    # P1 Berserk deliberately owns locomotion through its private mailbox even
    # when there is no follower, so the follower master flag is zero in a solo
    # party.  Facing must use the same OR gate as the movement cave or P1 can
    # translate while the native Berserk facing remains frozen.
    cave += (
        b"\x41\x83\xbb"
        + struct.pack("<i", p1_berserk + P1_BERSERK_ACTIVE_OFFSET)
        + b"\x00"
    )
    jump(b"\x75", "select_actor")
    cave += b"\x41\x83\x7b" + bytes((MOVEMENT_ACTIVE_OFFSET,)) + b"\x00"
    jump(b"\x74", "store")
    labels["select_actor"] = len(cave)

    def release_check(magnitude_disp: bytes) -> None:
        if FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE:
            cave.extend(b"\x41\x8b\x93" + magnitude_disp)
            cave.extend(b"\x50\x48\x8b\xc3")
            call_site = FACING_CAVE + len(cave)
            cave.extend(
                b"\xe8"
                + rel32(
                    call_site,
                    5,
                    FOLLOWER_ACTION_NATIVE_APPROACH_HELPER,
                )
            )
            cave.extend(b"\x84\xc0\x58")
            jump(b"\x75", "store")
        if not FOLLOWER_ACTION_FACING_RELEASE:
            return
        cave.extend(b"\x80\xbb\xb4\x06\x00\x00\x04")
        keep = len(cave)
        cave.extend(b"\x75\x00")
        cave.extend(b"\x83\xbb\x10\x07\x00\x00\x00")
        keep_no_target = len(cave)
        cave.extend(b"\x74\x00")
        cave.extend(b"\x41\x83\xbb" + magnitude_disp + b"\x00")
        jump(b"\x74", "store")
        for position in (keep, keep_no_target):
            cave[position + 1] = len(cave) - (position + 2)

    def select_follower(mailbox, next_label, role):
        sequence = mailbox + movement_mailbox.SEQUENCE_OFFSET
        fallback_label = f"{role}_fallback"
        cave.extend(b"\x45\x8b\x93" + struct.pack("<i", sequence))
        cave.extend(b"\x45\x85\xd2")
        jump(b"\x74", next_label)
        cave.extend(b"\x41\xf6\xc2\x01")
        jump(b"\x75", fallback_label)
        cave.extend(
            b"\x41\x83\xbb"
            + struct.pack("<i", mailbox + movement_mailbox.ACTIVE_OFFSET)
            + b"\x00"
        )
        jump(b"\x74", next_label)
        cave.extend(
            b"\x49\x3b\x9b" + struct.pack("<i", mailbox + movement_mailbox.ACTOR_OFFSET)
        )
        jump(b"\x75", next_label)
        cave.extend(b"\x53\x58")
        call_site = FACING_CAVE + len(cave)
        cave.extend(b"\xe8" + rel32(call_site, 5, ACTOR_PHYSICS_HELPER))
        jump(b"\x72", next_label)
        cave.extend(
            b"\x49\x3b\x83" + struct.pack("<i", mailbox + movement_mailbox.PHYS_OFFSET)
        )
        jump(b"\x75", next_label)
        cave.extend(
            b"\x45\x8b\x8b"
            + struct.pack("<i", mailbox + movement_mailbox.VISUAL_HEADING_OFFSET)
        )
        cave.extend(b"\x45\x3b\x93" + struct.pack("<i", sequence))
        jump(b"\x75", fallback_label)
        cave.extend(b"\x66\x41\x0f\x6e\xc1")
        jump(b"\xeb", "store")

        # The producer commits this prior scalar while the main sequence is
        # still stable, then marks the main record odd.  Thus both an odd first
        # read and a changed final read can safely keep the old complete visual
        # heading instead of snapping to the native follower target.  If that
        # fallback is itself in flight, fail open to native facing for this
        # frame; a retry loop could freeze the game after an interrupted write.
        labels[fallback_label] = len(cave)
        cave.extend(
            b"\x45\x8b\x93"
            + struct.pack("<i", mailbox + movement_mailbox.FALLBACK_SEQUENCE_OFFSET)
        )
        cave.extend(b"\x45\x85\xd2")
        short_jump(b"\x74", next_label)
        cave.extend(b"\x41\xf6\xc2\x01")
        jump(b"\x75", next_label)
        cave.extend(
            b"\x41\x83\xbb"
            + struct.pack("<i", mailbox + movement_mailbox.FALLBACK_ACTIVE_OFFSET)
            + b"\x00"
        )
        short_jump(b"\x74", next_label)
        cave.extend(
            b"\x49\x3b\x9b"
            + struct.pack("<i", mailbox + movement_mailbox.FALLBACK_ACTOR_OFFSET)
        )
        short_jump(b"\x75", next_label)
        cave.extend(b"\x53\x58")
        call_site = FACING_CAVE + len(cave)
        cave.extend(b"\xe8" + rel32(call_site, 5, ACTOR_PHYSICS_HELPER))
        short_jump(b"\x72", next_label)
        cave.extend(
            b"\x49\x3b\x83"
            + struct.pack("<i", mailbox + movement_mailbox.FALLBACK_PHYS_OFFSET)
        )
        short_jump(b"\x75", next_label)
        cave.extend(
            b"\x45\x8b\x8b"
            + struct.pack(
                "<i", mailbox + movement_mailbox.FALLBACK_VISUAL_HEADING_OFFSET
            )
        )
        cave.extend(
            b"\x45\x3b\x93"
            + struct.pack("<i", mailbox + movement_mailbox.FALLBACK_SEQUENCE_OFFSET)
        )
        jump(b"\x75", next_label)
        cave.extend(b"\x66\x41\x0f\x6e\xc1")
        jump(b"\xeb", "store")

    select_follower(p2_mailbox, "check_p3", "p2")
    labels["check_p3"] = len(cave)
    select_follower(p3_mailbox, "check_p1_berserk", "p3")
    labels["check_p1_berserk"] = len(cave)
    cave += (
        b"\x41\x83\xbb"
        + struct.pack("<i", p1_berserk + P1_BERSERK_ACTIVE_OFFSET)
        + b"\x00"
    )
    jump(b"\x74", "store")
    cave += (
        b"\x49\x83\xbb"
        + struct.pack("<i", p1_berserk + P1_BERSERK_ACTOR_OFFSET)
        + b"\x00"
    )
    jump(b"\x74", "store")
    cave += b"\x49\x3b\x9b" + struct.pack("<i", p1_berserk + P1_BERSERK_ACTOR_OFFSET)
    jump(b"\x75", "store")
    cave += b"\xf3\x41\x0f\x10\x83" + struct.pack(
        "<i", p1_berserk + P1_BERSERK_VISUAL_HEADING_OFFSET
    )
    jump(b"\xeb", "store")
    labels["store"] = len(cave)
    for position, label in fixups:
        struct.pack_into("<i", cave, position, labels[label] - (position + 4))
    for position, label in short_fixups:
        displacement = labels[label] - (position + 1)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError(f"facing {label} short branch overflow")
        cave[position] = displacement & 0xFF
    cave += b"\x41\x59\x41\x5a\x58"
    cave += original
    jump_site = FACING_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FACING_HOOK_RETURN)

    if len(cave) > 0x200:
        raise RuntimeError(f"facing cave overflow: {len(cave):#x} bytes")

    actor_helper = build_actor_physics_helper()
    mode, pending = _preflight_process_lifetime_detour(
        process,
        label="facing",
        site=FACING_HOOK_SITE,
        original=original,
        patch=patch,
        regions=(
            (
                "actor-physics helper",
                ACTOR_PHYSICS_HELPER,
                ACTOR_PHYSICS_HELPER_SIZE,
                actor_helper,
                True,
            ),
            ("cave", FACING_CAVE, 0x200, cave, False),
        ),
    )
    return _publish_process_lifetime_detour(
        process,
        site=FACING_HOOK_SITE,
        patch=patch,
        mode=mode,
        pending=pending,
    )


def install_visual_facing_update(process):
    original = b"\xe8\x98\xd9\x02\x00"
    patch = b"\xe8" + rel32(VISUAL_FACING_CALL_SITE, 5, VISUAL_FACING_CAVE)

    cave = bytearray()
    original_heading_branches = []
    skip_cancel_branches = []
    p1_selection_branches = []
    p2_miss_branches = []
    p2_selected_jumps = []
    movement_enabled_branches = []

    def emit_near_jcc(opcode, destinations):
        position = len(cave)
        cave.extend(b"\x0f" + bytes([opcode]) + b"\x00\x00\x00\x00")
        destinations.append(position)

    def resolve_near_jcc(position, destination):
        struct.pack_into("<i", cave, position + 2, destination - (position + 6))

    p1_berserk = P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    cave += (
        b"\x41\x83\xbb"
        + struct.pack("<i", p1_berserk + P1_BERSERK_ACTIVE_OFFSET)
        + b"\x00"
    )
    emit_near_jcc(0x85, movement_enabled_branches)
    cave += b"\x41\x83\x7b" + bytes((MOVEMENT_ACTIVE_OFFSET,)) + b"\x00"
    emit_near_jcc(0x84, original_heading_branches)
    movement_enabled = len(cave)
    for branch in movement_enabled_branches:
        resolve_near_jcc(branch, movement_enabled)

    # At 0x23631A the native caller loads RCX from [RBX+10h].  The displaced
    # callee accepts a null RCX and only follows RCX+B8h for native type tags
    # 20h and 60h.  Mirror that contract before our extra RCX+30h actor read;
    # lifecycle transitions must tail-call the original instead of faulting in
    # the hook.
    valid_native_type_branches = []
    cave += b"\x48\x85\xc9"
    emit_near_jcc(0x84, original_heading_branches)
    cave += b"\x0f\xb6\x51\x03"
    cave += b"\xc1\xea\x05"
    cave += b"\x83\xea\x01"
    emit_near_jcc(0x84, valid_native_type_branches)
    cave += b"\x83\xfa\x02"
    emit_near_jcc(0x85, original_heading_branches)
    valid_native_type = len(cave)
    for branch in valid_native_type_branches:
        resolve_near_jcc(branch, valid_native_type)
    cave += b"\x48\x8b\x41\x30"
    cave += b"\x48\x85\xc0"
    emit_near_jcc(0x84, original_heading_branches)

    def release_check(magnitude_disp: bytes) -> None:
        if FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE:
            cave.extend(b"\x50")
            cave.extend(b"\x41\x8b\x93" + magnitude_disp)
            call_site = VISUAL_FACING_CAVE + len(cave)
            cave.extend(
                b"\xe8"
                + rel32(
                    call_site,
                    5,
                    FOLLOWER_ACTION_NATIVE_APPROACH_HELPER,
                )
            )
            cave.extend(b"\x84\xc0\x58")
            emit_near_jcc(0x85, original_heading_branches)
        if not FOLLOWER_ACTION_FACING_RELEASE:
            return
        cave.extend(b"\x80\xb8\xb4\x06\x00\x00\x04")
        keep = len(cave)
        cave.extend(b"\x75\x00")
        cave.extend(b"\x83\xb8\x10\x07\x00\x00\x00")
        keep_no_target = len(cave)
        cave.extend(b"\x74\x00")
        cave.extend(b"\x41\x83\xbb" + magnitude_disp + b"\x00")
        emit_near_jcc(0x84, original_heading_branches)
        for position in (keep, keep_no_target):
            cave[position + 1] = len(cave) - (position + 2)

    p2_mailbox = P2_MOVEMENT_MAILBOX_OFFSET - 0x200
    p3_mailbox = P3_MOVEMENT_MAILBOX_OFFSET - 0x200

    def select_visual_mailbox(mailbox, miss_branches):
        cave.extend(b"\x4d\x8d\x8b" + struct.pack("<i", mailbox))
        call_site = VISUAL_FACING_CAVE + len(cave)
        cave.extend(b"\xe8" + rel32(call_site, 5, VISUAL_FALLBACK_HELPER))
        # The helper returns the sequence-validated scalar in R10D and preserves
        # RAX, whose call-site contract is the selected native actor.  Returning
        # the scalar in EAX used to turn arbitrary heading bits into a pointer
        # consumed by the optional P2 action-cancel path.
        # Any invalid fallback fails open with CF set, including a permanently
        # odd or continuously changing sequence.
        emit_near_jcc(0x82, miss_branches)
        scratch = mailbox + movement_mailbox.NATIVE_SELECTED_VISUAL_OFFSET
        cave.extend(b"\x45\x89\x93" + struct.pack("<i", scratch))
        cave.extend(b"\x4d\x8d\x93" + struct.pack("<i", scratch))

    select_visual_mailbox(p2_mailbox, p2_miss_branches)
    p2_selected_jumps.append(len(cave))
    cave += b"\xe9\x00\x00\x00\x00"

    p3_selection_check = len(cave)
    for branch in p2_miss_branches:
        resolve_near_jcc(branch, p3_selection_check)
    select_visual_mailbox(p3_mailbox, p1_selection_branches)

    p3_common_jump = len(cave)
    cave += b"\xe9\x00\x00\x00\x00"

    p1_selection = len(cave)
    for branch in p1_selection_branches:
        resolve_near_jcc(branch, p1_selection)
    cave += (
        b"\x41\x83\xbb"
        + struct.pack("<i", p1_berserk + P1_BERSERK_ACTIVE_OFFSET)
        + b"\x00"
    )
    emit_near_jcc(0x84, original_heading_branches)
    cave += (
        b"\x49\x83\xbb"
        + struct.pack("<i", p1_berserk + P1_BERSERK_ACTOR_OFFSET)
        + b"\x00"
    )
    emit_near_jcc(0x84, original_heading_branches)
    cave += b"\x49\x3b\x83" + struct.pack("<i", p1_berserk + P1_BERSERK_ACTOR_OFFSET)
    emit_near_jcc(0x85, original_heading_branches)
    cave += b"\x4d\x8d\x93" + struct.pack(
        "<i", p1_berserk + P1_BERSERK_VISUAL_HEADING_OFFSET
    )
    p1_common_jump = len(cave)
    cave += b"\xe9\x00\x00\x00\x00"

    p2_selection = len(cave)
    for branch in p2_selected_jumps:
        struct.pack_into("<i", cave, branch + 1, p2_selection - (branch + 5))

    # The native controller tries to turn a follower toward the leader. During
    # co-op ownership this competes with visual facing and can pin animation in
    # place. Cancellation lasts one frame and applies only to idle, targetless
    # action.  When disabled, emit none of its actor/skeleton dereferences: dead
    # probes are still capable of faulting and provide no gameplay effect.
    if ENABLE_IDLE_MOTION_CANCEL:
        cave += b"\x41\x83\x7b\x04\x00"
        emit_near_jcc(0x85, skip_cancel_branches)
        cave += b"\x80\xb8\xb4\x06\x00\x00\x01"
        emit_near_jcc(0x85, skip_cancel_branches)
        cave += b"\xf7\x00\x00\x40\x00\x00"
        emit_near_jcc(0x85, skip_cancel_branches)
        cave += b"\x66\x81\xb8\x14\x07\x00\x00" + struct.pack(
            "<H", FOLLOWER_IDLE_ACTION
        )
        emit_near_jcc(0x85, skip_cancel_branches)
        cave += b"\x48\x8b\x91\xc0\x00\x00\x00"
        cave += b"\x48\x85\xd2"
        emit_near_jcc(0x84, skip_cancel_branches)
        cave += b"\x48\x63\x02"
        cave += b"\x83\xf8\xff"
        emit_near_jcc(0x84, skip_cancel_branches)
        cave += b"\x48\x69\xc0\x40\x07\x00\x00"
        cave += b"\x48\xba" + struct.pack("<Q", SKELETON_MAIN_BASE_GLOBAL)
        cave += b"\x48\x8b\x12"
        cave += b"\x48\x03\xc2"
        cave += b"\x81\xb8\xa0\x02\x00\x00\x06\x00\x00\x00"
        emit_near_jcc(0x85, skip_cancel_branches)
        cave += b"\x66\x81\xb8\xdc\x06\x00\x00\x06\x00"
        emit_near_jcc(0x85, skip_cancel_branches)
        cave += b"\x49\xff\x83\x98\x00\x00\x00"
        cave += b"\x80\x88\x94\x02\x00\x00\x04"

    skip_cancel = len(cave)
    for branch in skip_cancel_branches:
        resolve_near_jcc(branch, skip_cancel)
    struct.pack_into("<i", cave, p3_common_jump + 1, skip_cancel - (p3_common_jump + 5))
    struct.pack_into("<i", cave, p1_common_jump + 1, skip_cancel - (p1_common_jump + 5))

    cave += b"\x48\x8b\x81\xb8\x00\x00\x00"
    cave += b"\xf3\x41\x0f\x10\x02"
    cave += b"\x0f\x2e\x80\xa8\x00\x00\x00"
    skip_visual_setter_branch = len(cave)
    cave += b"\x74\x00"

    cave += b"\x48\x83\xec\x38"
    cave += b"\x48\x89\x4c\x24\x28"
    cave += b"\x48\x8b\x81\xb8\x00\x00\x00"
    cave += b"\xf3\x41\x0f\x10\x0a"
    cave += b"\xf3\x0f\x10\x90\xac\x00\x00\x00"
    cave += b"\x45\x33\xc9"
    call_site = VISUAL_FACING_CAVE + len(cave)
    cave += b"\xe8" + rel32(call_site, 5, SET_VISUAL_FACING)
    cave += b"\x48\x8b\x4c\x24\x28"
    cave += b"\x48\x83\xc4\x38"
    skip_visual_setter = len(cave)
    setter_skip = skip_visual_setter - (skip_visual_setter_branch + 2)
    if not 0 <= setter_skip <= 0x7F:
        raise RuntimeError("visual-facing setter branch overflow")
    cave[skip_visual_setter_branch + 1] = setter_skip
    read_original_heading = len(cave)
    for branch in original_heading_branches:
        resolve_near_jcc(branch, read_original_heading)
    jump_site = VISUAL_FACING_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, READ_PHYS_HEADING)

    if len(cave) > 0x200:
        raise RuntimeError(f"visual-facing cave overflow: {len(cave):#x} bytes")

    helper = bytearray()
    helper_fixups: list[tuple[int, str]] = []
    helper_labels: dict[str, int] = {}

    def helper_jump(opcode: int, label: str) -> None:
        helper.extend(b"\x0f" + bytes((opcode,)))
        helper_fixups.append((len(helper), label))
        helper.extend(b"\x00\x00\x00\x00")

    def helper_identity_checks(*, fallback: bool) -> None:
        active = (
            movement_mailbox.FALLBACK_ACTIVE_OFFSET
            if fallback
            else movement_mailbox.ACTIVE_OFFSET
        )
        actor = (
            movement_mailbox.FALLBACK_ACTOR_OFFSET
            if fallback
            else movement_mailbox.ACTOR_OFFSET
        )
        phys = (
            movement_mailbox.FALLBACK_PHYS_OFFSET
            if fallback
            else movement_mailbox.PHYS_OFFSET
        )
        visual = (
            movement_mailbox.FALLBACK_VISUAL_HEADING_OFFSET
            if fallback
            else movement_mailbox.VISUAL_HEADING_OFFSET
        )
        helper.extend(b"\x41\x83\x79" + bytes((active, 0)))
        helper_jump(0x84, "miss")
        helper.extend(b"\x49\x3b\x41" + bytes((actor,)))
        helper_jump(0x85, "miss")
        helper.extend(b"\x50")
        call_site = VISUAL_FALLBACK_HELPER + len(helper)
        helper.extend(b"\xe8" + rel32(call_site, 5, ACTOR_PHYSICS_HELPER))
        helper.extend(b"\x4c\x8b\xd0\x58")
        helper_jump(0x82, "miss")
        helper.extend(b"\x4d\x3b\x51" + bytes((phys,)))
        helper_jump(0x85, "miss")
        helper.extend(b"\x45\x8b\x51" + bytes((visual,)))

    # R9 points at one role mailbox and RAX is the actor at this native call.
    # On success R10D carries the scalar and RAX remains that actor; on miss RAX
    # is also preserved.  The caller must never reinterpret heading bits as an
    # actor pointer.
    # Main may fall back once, but an odd/changed fallback fails open to native
    # facing for this frame.  Never spin on publisher-owned sequence words from
    # the game's native thread: an interrupted publisher can leave them odd.
    helper += b"\x41\x8b\x11"
    helper += b"\x85\xd2"
    helper_jump(0x84, "miss")
    helper += b"\xf6\xc2\x01"
    helper_jump(0x85, "fallback")
    helper_identity_checks(fallback=False)
    helper += b"\x41\x3b\x11"
    helper_jump(0x85, "fallback")
    helper += b"\xf8\xc3"

    helper_labels["fallback"] = len(helper)
    helper += b"\x41\x8b\x51" + bytes((movement_mailbox.FALLBACK_SEQUENCE_OFFSET,))
    helper += b"\x85\xd2"
    helper_jump(0x84, "miss")
    helper += b"\xf6\xc2\x01"
    helper_jump(0x85, "miss")
    helper_identity_checks(fallback=True)
    helper += b"\x41\x3b\x51" + bytes((movement_mailbox.FALLBACK_SEQUENCE_OFFSET,))
    helper_jump(0x85, "miss")
    helper += b"\xf8\xc3"

    helper_labels["miss"] = len(helper)
    helper += b"\xf9\xc3"
    for position, label in helper_fixups:
        struct.pack_into("<i", helper, position, helper_labels[label] - (position + 4))
    if len(helper) > VISUAL_FALLBACK_HELPER_SIZE:
        raise RuntimeError(f"visual fallback helper overflow: {len(helper):#x}")

    actor_helper = build_actor_physics_helper()
    mode, pending = _preflight_process_lifetime_detour(
        process,
        label="visual facing",
        site=VISUAL_FACING_CALL_SITE,
        original=original,
        patch=patch,
        regions=(
            (
                "actor-physics helper",
                ACTOR_PHYSICS_HELPER,
                ACTOR_PHYSICS_HELPER_SIZE,
                actor_helper,
                True,
            ),
            (
                "fallback helper",
                VISUAL_FALLBACK_HELPER,
                VISUAL_FALLBACK_HELPER_SIZE,
                helper,
                False,
            ),
            ("cave", VISUAL_FACING_CAVE, 0x200, cave, False),
        ),
    )
    return _publish_process_lifetime_detour(
        process,
        site=VISUAL_FACING_CALL_SITE,
        patch=patch,
        mode=mode,
        pending=pending,
    )


def restore_phys_facing_guard(process):
    original = b"\xf3\x0f\x11\xb6\xa8\x00\x00\x00"
    patch = (
        b"\xe9"
        + rel32(PHYS_FACING_STORE_SITE, 5, PHYS_FACING_GUARD_CAVE)
        + b"\x90\x90\x90"
    )
    current = process.read(PHYS_FACING_STORE_SITE, len(original))
    if current == original:
        return
    if current != patch:
        raise RuntimeError(
            f"unexpected bytes while restoring phys-facing store: {current.hex(' ')}"
        )
    process.patch_code(PHYS_FACING_STORE_SITE, original)


def follower_action_approach_gate_patch(cave_address):
    return (
        b"\xe9"
        + rel32(FOLLOWER_ACTION_APPROACH_GATE_SITE, 5, cave_address)
        + b"\x90\x90"
    )


def build_follower_action_approach_gate_cave(cave_address, command_block):
    code = bytearray()
    # Preserve the native leader comparison while controlled-actor identity is
    # resolved.  A surviving follower becomes FF12's temporary native leader
    # when P1 is KO; allowing that comparison to return first lets the native
    # target-approach controller fight the follower movement mailbox.  Resolve
    # manual ownership first, then replay the exact native flags for every
    # actor that is not controlled by co-op.
    code += b"\x3b\xc3"
    code += b"\x9c\x41\x53"
    code += b"\x49\xbb" + struct.pack("<Q", command_block)
    code += b"\x41\x83\x7b" + bytes((COOP_ACTIVE_OFFSET,)) + b"\x00"
    inactive = len(code)
    code += b"\x74\x00"

    p1_state = P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
    p1_active = p1_state + P1_BERSERK_ACTIVE_OFFSET
    p1_actor = p1_state + P1_BERSERK_ACTOR_OFFSET
    code += b"\x41\x83\xbb" + struct.pack("<i", p1_active) + b"\x00"
    p1_inactive = len(code)
    code += b"\x74\x00"
    code += b"\x49\x3b\xbb" + struct.pack("<i", p1_actor)
    p1_match = len(code)
    code += b"\x74\x00"

    p2_check = len(code)
    code += b"\x49\x3b\x7b\x10"
    p2_match = len(code)
    code += b"\x74\x00"
    p3_actor = P3_COMMAND_BLOCK_OFFSET - 0x200 + P3_ACTOR_OFFSET
    code += b"\x49\x3b\xbb" + struct.pack("<i", p3_actor)
    p3_match = len(code)
    code += b"\x74\x00"

    native_follower = len(code)
    code += b"\x41\x5b\x9d"
    code += b"\x48\x8b\x5c\x24\x30"
    jump_site = cave_address + len(code)
    code += b"\xe9" + rel32(jump_site, 5, FOLLOWER_ACTION_APPROACH_GATE_RETURN)

    controlled = len(code)
    code += b"\x41\x5b\x9d"
    code += b"\x48\x8b\x5c\x24\x30"
    jump_site = cave_address + len(code)
    code += b"\xe9" + rel32(jump_site, 5, FOLLOWER_ACTION_APPROACH_NO_APPROACH)

    for branch, target in (
        (inactive, native_follower),
        (p1_inactive, p2_check),
        (p1_match, controlled),
        (p2_match, controlled),
        (p3_match, controlled),
    ):
        displacement = target - (branch + 2)
        if not -0x80 <= displacement <= 0x7F:
            raise RuntimeError(
                f"follower action-approach branch overflow: {displacement:#x}"
            )
        code[branch + 1] = displacement & 0xFF

    return code


def install_follower_action_approach_gate(process):
    original = FOLLOWER_ACTION_APPROACH_GATE_ORIGINAL
    patch = follower_action_approach_gate_patch(FOLLOWER_ACTION_APPROACH_GATE_CAVE)
    current = process.read(FOLLOWER_ACTION_APPROACH_GATE_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            "unexpected bytes at follower action-approach gate: " + current.hex(" ")
        )

    cave = build_follower_action_approach_gate_cave(
        FOLLOWER_ACTION_APPROACH_GATE_CAVE,
        COMMAND_BLOCK,
    )
    if len(cave) > 0x100:
        raise RuntimeError(f"follower action-approach cave overflow: {len(cave):#x}")
    process.write(FOLLOWER_ACTION_APPROACH_GATE_CAVE, cave)
    if not already_installed:
        process.patch_code(FOLLOWER_ACTION_APPROACH_GATE_SITE, patch)


def restore_follower_action_approach_gate(process):
    original = FOLLOWER_ACTION_APPROACH_GATE_ORIGINAL
    patch = follower_action_approach_gate_patch(FOLLOWER_ACTION_APPROACH_GATE_CAVE)
    current = process.read(FOLLOWER_ACTION_APPROACH_GATE_SITE, len(original))
    if current == patch:
        process.patch_code(FOLLOWER_ACTION_APPROACH_GATE_SITE, original)
    elif current != original:
        raise RuntimeError(
            "unexpected bytes while restoring follower action-approach gate: "
            + current.hex(" ")
        )


def install_follower_action_chase_guard(process):
    original = FOLLOWER_ACTION_CHASE_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(
            FOLLOWER_ACTION_CHASE_SITE,
            5,
            FOLLOWER_ARBITRATOR_GUARD_CAVE,
        )
        + b"\x90\x90\x90"
    )
    current = process.read(FOLLOWER_ACTION_CHASE_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            "unexpected bytes at follower action-chase consumer: " + current.hex(" ")
        )

    cave = bytearray()
    cave += b"\x9c\x41\x53"
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    native_branches = []

    def emit_native(opcode):
        position = len(cave)
        cave.extend(b"\x0f" + bytes((opcode + 0x10,)) + b"\x00\x00\x00\x00")
        native_branches.append(position)

    p3 = P3_COMMAND_BLOCK_OFFSET - 0x200
    cave += b"\x41\x83\x7b" + bytes((COOP_ACTIVE_OFFSET,)) + b"\x00"
    emit_native(0x74)

    cave += b"\x49\x3b\x5b\x10"
    p2_jump = len(cave)
    cave += b"\x74\x00"
    cave += b"\x49\x3b\x9b" + struct.pack("<i", p3 + P3_ACTOR_OFFSET)
    emit_native(0x75)
    cave += (
        b"\x41\x83\xbb" + struct.pack("<i", p3 + P3_MOVEMENT_ACTIVE_OFFSET) + b"\x00"
    )
    emit_native(0x74)
    cave += b"\x41\x83\xbb" + struct.pack("<i", p3 + P3_MAGNITUDE_OFFSET) + b"\x00"
    emit_native(0x75)
    p3_done = len(cave)
    cave += b"\xeb\x00"

    p2_path = len(cave)
    p2_delta = p2_path - (p2_jump + 2)
    if not 0 <= p2_delta <= 0x7F:
        raise RuntimeError(f"action-chase P2 branch overflow: {p2_delta:#x}")
    cave[p2_jump + 1] = p2_delta
    cave += b"\x41\x83\x7b\x04\x00"
    emit_native(0x75)

    common = len(cave)
    p3_delta = common - (p3_done + 2)
    if not 0 <= p3_delta <= 0x7F:
        raise RuntimeError(f"action-chase P3 branch overflow: {p3_delta:#x}")
    cave[p3_done + 1] = p3_delta
    cave += b"\x48\x0f\xba\x23\x2e"
    emit_native(0x72)
    cave += b"\x80\xbb\xb4\x06\x00\x00\x04"
    emit_native(0x75)
    cave += b"\x83\xbb\x10\x07\x00\x00\x00"
    emit_native(0x74)

    cave += b"\x41\x5b\x9d"
    cave += b"\xc7\x83\x58\x01\x00\x00\x00\x00\x00\x00"
    cave += b"\x0f\x57\xc9"
    jump_site = FOLLOWER_ARBITRATOR_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FOLLOWER_ACTION_CHASE_RETURN)

    native = len(cave)
    for position in native_branches:
        struct.pack_into("<i", cave, position + 2, native - (position + 6))
    cave += b"\x41\x5b\x9d"
    cave += original
    jump_site = FOLLOWER_ARBITRATOR_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FOLLOWER_ACTION_CHASE_RETURN)

    if len(cave) > 0x100:
        raise RuntimeError(
            f"follower action-chase guard overflow: {len(cave):#x} bytes"
        )
    process.write(FOLLOWER_ARBITRATOR_GUARD_CAVE, cave)
    if not already_installed:
        process.patch_code(FOLLOWER_ACTION_CHASE_SITE, patch)


def restore_follower_action_chase_guard(process):
    original = FOLLOWER_ACTION_CHASE_ORIGINAL
    known_patches = (
        b"\xe9"
        + rel32(
            FOLLOWER_ACTION_CHASE_SITE,
            5,
            FOLLOWER_ARBITRATOR_GUARD_CAVE,
        )
        + b"\x90\x90\x90",
        b"\xe9"
        + rel32(
            FOLLOWER_ACTION_CHASE_SITE,
            5,
            FOLLOWER_LOCOMOTION_GUARD_CAVE,
        )
        + b"\x90\x90\x90",
    )
    current = process.read(FOLLOWER_ACTION_CHASE_SITE, len(original))
    if current in known_patches:
        process.patch_code(FOLLOWER_ACTION_CHASE_SITE, original)
    elif current != original:
        raise RuntimeError(
            "unexpected bytes while restoring follower action-chase: "
            + current.hex(" ")
        )


def restore_follower_arbitrator_guard(process):
    original = b"\x48\x89\x5c\x24\x20"
    patch = b"\xe9" + rel32(FOLLOWER_ARBITRATOR_SITE, 5, FOLLOWER_ARBITRATOR_GUARD_CAVE)
    current = process.read(FOLLOWER_ARBITRATOR_SITE, len(original))
    if current == patch:
        process.patch_code(FOLLOWER_ARBITRATOR_SITE, original)
    elif current != original:
        raise RuntimeError(
            "unexpected bytes while restoring follower arbitrator: " + current.hex(" ")
        )


def restore_follower_turn_guard(process):
    original = b"\xe8\x4a\x00\x00\x00"
    current_patch = b"\xe8" + rel32(
        FOLLOWER_TURN_CALL_SITE, 5, FOLLOWER_TURN_GUARD_CAVE
    )
    legacy_cave = FOLLOWER_TURN_GUARD_CAVE - 0xF000 + 0x1D00
    legacy_patch = b"\xe8" + rel32(FOLLOWER_TURN_CALL_SITE, 5, legacy_cave)
    current = process.read(FOLLOWER_TURN_CALL_SITE, len(original))
    if current == original:
        return
    if current not in (current_patch, legacy_patch):
        raise RuntimeError(
            "unexpected bytes while restoring old follower-turn guard: "
            + current.hex(" ")
        )
    process.patch_code(FOLLOWER_TURN_CALL_SITE, original)
    if (
        current == legacy_patch
        and process.read(legacy_cave, 2) == b"\x49\xbb"
        and process.u64(legacy_cave + 2) == COMMAND_BLOCK
    ):
        process.write(legacy_cave, b"\x00" * 0x100)


def build_follower_action_transition_helper(
    helper_address,
    mailbox_address,
    *,
    entity_resolver=ENTITY_ENTRY_RESOLVER,
    physics_helper=None,
):
    """Build the bounded game-thread consumer for follower action repairs.

    The follower-list evaluator supplies a live actor in R13 and the list being
    evaluated in R10.  Published actor/physics values are identity comparisons
    only: the helper resolves the generation-coded handle natively and derives
    physics from that live actor before touching its action packet.  It updates
    both the current action at +714h and the setter's shadow at +72Ch.

    This is a transparent custom-call ABI.  Every GPR, RFLAGS and XMM0..XMM5 is
    restored.  No publisher-owned sequence is ever polled in a loop.
    """

    helper_address = int(helper_address)
    mailbox_address = int(mailbox_address)
    physics_helper = ACTOR_PHYSICS_HELPER if physics_helper is None else int(
        physics_helper
    )
    if not helper_address or not mailbox_address or not physics_helper:
        raise ValueError("follower action helper addresses must be non-zero")

    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(name: str) -> None:
        labels[name] = len(code)

    def jump(opcode: bytes, label: str) -> None:
        if opcode == b"\xe9":
            code.extend(opcode)
        elif len(opcode) == 1:
            code.extend(b"\x0f" + opcode)
        else:
            raise ValueError("invalid follower action branch opcode")
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    def call(target: int) -> None:
        site = helper_address + len(code)
        code.extend(b"\xe8" + rel32(site, 5, int(target)))

    def sequence_still_current(label: str = "next") -> None:
        code.extend(b"\x41\x39\x1f")  # cmp [r15], ebx
        jump(b"\x85", label)

    def resolve_identity() -> None:
        code.extend(b"\x41\x8b\x4f\x0c")  # mov ecx,[r15+0Ch]
        code.extend(b"\x85\xc9")
        jump(b"\x84", "reject_identity")
        call(entity_resolver)
        code.extend(b"\x48\x85\xc0")
        jump(b"\x84", "reject_identity")
        code.extend(b"\x48\x8b\x40\x30")  # registry entry -> live actor
        code.extend(b"\x4c\x39\xe8")  # cmp rax,r13
        jump(b"\x85", "reject_identity")
        sequence_still_current()

    def resolve_physics() -> None:
        code.extend(b"\x49\x83\x7f\x18\x00")
        jump(b"\x84", "reject_physics")
        code.extend(b"\x4c\x89\xe8")  # mov rax,r13
        call(physics_helper)
        jump(b"\x82", "reject_physics")  # helper reports a broken chain in CF
        code.extend(b"\x49\x3b\x47\x18")
        jump(b"\x85", "reject_physics")
        sequence_still_current()

    def validate_signature() -> None:
        code.extend(b"\x41\x80\xbd\xb4\x06\x00\x00\x01")
        jump(b"\x85", "reject_signature")
        code.extend(b"\x41\x83\xbd\x10\x07\x00\x00\x00")
        jump(b"\x85", "reject_signature")
        # FUN_0030f760 mirrors target/action +710h..+717h at +728h..+72Fh.
        code.extend(b"\x41\x83\xbd\x28\x07\x00\x00\x00")
        jump(b"\x85", "reject_signature")
        code.extend(b"\x49\x8b\x85\x08\x07\x00\x00")
        code.extend(b"\x48\x85\xc0")
        jump(b"\x84", "reject_signature")
        code.extend(b"\x66\x81\x38" + struct.pack("<H", FOLLOWER_IDLE_ACTION))
        jump(b"\x85", "reject_signature")

    # Entry RSP is the standard Win64 8 mod 16.  Sixteen pushes retain that
    # alignment; 88h bytes provide shadow space, XMM saves, and call alignment.
    code.extend(b"\x9c")
    code.extend(b"\x50\x51\x52\x53\x55\x56\x57")
    code.extend(b"\x41\x50\x41\x51\x41\x52\x41\x53")
    code.extend(b"\x41\x54\x41\x55\x41\x56\x41\x57")
    code.extend(b"\x48\x81\xec\x88\x00\x00\x00")
    for register, offset in enumerate(range(0x20, 0x80, 0x10)):
        modrm = 0x44 | (register << 3)
        code.extend(b"\xf3\x0f\x7f" + bytes((modrm, 0x24, offset)))

    code.extend(b"\x4d\x89\xd4")  # mov r12,r10 (native list)
    code.extend(b"\x49\xbf" + struct.pack("<Q", mailbox_address))
    code.extend(b"\x41\xbe" + struct.pack("<I", FOLLOWER_ACTION_MAILBOX_ROLE_COUNT))

    mark("loop")
    code.extend(b"\x41\x8b\x1f")  # mov ebx,[r15+sequence]
    code.extend(b"\x85\xdb")
    jump(b"\x84", "next")
    code.extend(b"\xf6\xc3\x01")
    jump(b"\x85", "next")
    # expected_actor is comparison-only; R13 remains the only actor dereferenced.
    code.extend(b"\x4d\x39\x6f\x10")
    jump(b"\x85", "next")

    # Only the two intentional transitions are legal, even if mailbox memory is
    # corrupted.  Arbitrary action IDs can never become a native write primitive.
    code.extend(b"\x41\x0f\xb7\x6f\x20")  # ebp=expected action
    code.extend(b"\x41\x0f\xb7\x77\x22")  # esi=desired action
    code.extend(b"\x81\xfd" + struct.pack("<I", FOLLOWER_IDLE_ACTION))
    jump(b"\x85", "check_resume_pair")
    code.extend(b"\x81\xfe" + struct.pack("<I", NATIVE_IDLE_ACTION))
    jump(b"\x84", "pair_ok")
    jump(b"\xe9", "reject_signature")
    mark("check_resume_pair")
    code.extend(b"\x81\xfd" + struct.pack("<I", NATIVE_IDLE_ACTION))
    jump(b"\x85", "reject_signature")
    code.extend(b"\x81\xfe" + struct.pack("<I", FOLLOWER_IDLE_ACTION))
    jump(b"\x85", "reject_signature")

    mark("pair_ok")
    # The hook can be reached for several actor lists.  A non-follow list is a
    # miss, not a rejection; the matching pass can consume the request later.
    code.extend(b"\x48\xb8" + struct.pack("<Q", FOLLOWER_LIST_PRIMARY))
    code.extend(b"\x49\x39\xc4")
    jump(b"\x84", "list_ok")
    code.extend(b"\x48\xb8" + struct.pack("<Q", FOLLOWER_LIST_SECONDARY))
    code.extend(b"\x49\x39\xc4")
    jump(b"\x85", "next")

    mark("list_ok")
    # Classify the complete live/shadow pair before expensive native resolution.
    code.extend(b"\x66\x41\x39\xad\x14\x07\x00\x00")
    jump(b"\x85", "check_desired")
    code.extend(b"\x66\x41\x39\xad\x2c\x07\x00\x00")
    jump(b"\x85", "reject_action")
    code.extend(b"\xbf" + struct.pack("<I", FOLLOWER_ACTION_RESULT_APPLIED))
    jump(b"\xe9", "validate")

    mark("check_desired")
    code.extend(b"\x66\x41\x39\xb5\x14\x07\x00\x00")
    jump(b"\x85", "dormant_or_reject")
    code.extend(b"\x66\x41\x39\xb5\x2c\x07\x00\x00")
    jump(b"\x85", "dormant_or_reject")
    # An acknowledged persistent sanitize request remains armed, but must not
    # emit a fresh result until the residual expected action actually reappears.
    code.extend(b"\x41\x39\x5f\x04")
    jump(b"\x84", "next")
    code.extend(
        b"\xbf" + struct.pack("<I", FOLLOWER_ACTION_RESULT_ALREADY_DESIRED)
    )
    jump(b"\xe9", "validate")

    mark("dormant_or_reject")
    code.extend(b"\x41\x39\x5f\x04")
    jump(b"\x84", "next")
    jump(b"\xe9", "reject_action")

    mark("validate")
    resolve_identity()
    resolve_physics()
    validate_signature()
    sequence_still_current()

    # Re-resolve identity and physics immediately before the mutation.  A
    # despawn/root rebuild that overlaps validation therefore fails closed.
    resolve_identity()
    resolve_physics()
    validate_signature()
    sequence_still_current()

    code.extend(b"\x83\xff" + bytes((FOLLOWER_ACTION_RESULT_APPLIED,)))
    jump(b"\x85", "verify_desired")
    code.extend(b"\x66\x41\x39\xad\x14\x07\x00\x00")
    jump(b"\x85", "reject_action")
    code.extend(b"\x66\x41\x39\xad\x2c\x07\x00\x00")
    jump(b"\x85", "reject_action")
    sequence_still_current()
    code.extend(b"\x66\x41\x89\xb5\x14\x07\x00\x00")
    code.extend(b"\x66\x41\x89\xb5\x2c\x07\x00\x00")
    jump(b"\xe9", "ack_success")

    mark("verify_desired")
    code.extend(b"\x66\x41\x39\xb5\x14\x07\x00\x00")
    jump(b"\x85", "reject_action")
    code.extend(b"\x66\x41\x39\xb5\x2c\x07\x00\x00")
    jump(b"\x85", "reject_action")
    sequence_still_current()

    mark("ack_success")
    code.extend(b"\x41\x89\x7f\x08")  # result before acknowledgement
    code.extend(b"\x41\x89\x5f\x04")
    jump(b"\xe9", "next")

    for label, result in (
        ("reject_identity", FOLLOWER_ACTION_RESULT_STALE_IDENTITY),
        ("reject_physics", FOLLOWER_ACTION_RESULT_STALE_PHYSICS),
        ("reject_signature", FOLLOWER_ACTION_RESULT_INVALID_SIGNATURE),
        ("reject_action", FOLLOWER_ACTION_RESULT_ACTION_CHANGED),
    ):
        mark(label)
        code.extend(b"\xb8" + struct.pack("<I", result))
        jump(b"\xe9", "ack_reject")

    mark("ack_reject")
    # A persistent request that was already acknowledged is dormant.  Transient
    # later state cannot turn it into a stream of negative acknowledgements.
    code.extend(b"\x41\x39\x5f\x04")
    jump(b"\x84", "next")
    sequence_still_current()
    code.extend(b"\x41\x89\x47\x08")
    code.extend(b"\x41\x89\x5f\x04")

    mark("next")
    code.extend(
        b"\x49\x83\xc7" + bytes((FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE,))
    )
    code.extend(b"\x41\xff\xce")
    jump(b"\x85", "loop")

    for register, offset in reversed(tuple(enumerate(range(0x20, 0x80, 0x10)))):
        modrm = 0x44 | (register << 3)
        code.extend(b"\xf3\x0f\x6f" + bytes((modrm, 0x24, offset)))
    code.extend(b"\x48\x81\xc4\x88\x00\x00\x00")
    code.extend(b"\x41\x5f\x41\x5e\x41\x5d\x41\x5c")
    code.extend(b"\x41\x5b\x41\x5a\x41\x59\x41\x58")
    code.extend(b"\x5f\x5e\x5d\x5b\x5a\x59\x58\x9d\xc3")

    for position, label in fixups:
        if label not in labels:
            raise RuntimeError(f"unknown follower action helper label: {label}")
        struct.pack_into("<i", code, position, labels[label] - (position + 4))
    if len(code) > FOLLOWER_ACTION_TRANSITION_HELPER_SIZE:
        raise RuntimeError(f"follower action helper overflow: {len(code):#x}")
    return code


def install_follower_list_guard(process):
    original = b"\x48\x89\x9c\x24\x80\x00\x00\x00"
    patch = (
        b"\xe9"
        + rel32(FOLLOWER_LIST_EVALUATOR_SITE, 5, FOLLOWER_LIST_GUARD_CAVE)
        + b"\x90\x90\x90"
    )
    current = process.read(FOLLOWER_LIST_EVALUATOR_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            f"unexpected bytes at follower-list evaluator: {current.hex(' ')}"
        )

    cave = bytearray()
    cave += b"\x9c\x41\x53"
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    if FOLLOWER_ACTION_TRANSITION_HELPER and P2_FOLLOWER_ACTION_MAILBOX:
        install_actor_physics_helper(process)
        helper = build_follower_action_transition_helper(
            FOLLOWER_ACTION_TRANSITION_HELPER,
            P2_FOLLOWER_ACTION_MAILBOX,
        )
        process.write(FOLLOWER_ACTION_TRANSITION_HELPER, helper)
        call_site = FOLLOWER_LIST_GUARD_CAVE + len(cave)
        cave += b"\xe8" + rel32(
            call_site,
            5,
            FOLLOWER_ACTION_TRANSITION_HELPER,
        )
    native_branches = []
    reject_branches = []

    def emit_branch(branches, opcode):
        position = len(cave)
        cave.extend(b"\x0f" + bytes([opcode]) + b"\x00\x00\x00\x00")
        branches.append(position)

    cave += b"\x41\x83\x7b" + bytes((COOP_ACTIVE_OFFSET,)) + b"\x00"
    emit_branch(native_branches, 0x84)

    controlled_jumps = []

    cave += b"\x49\x83\x7b\x10\x00"
    p2_missing_jump = len(cave)
    cave += b"\x74\x00"
    cave += b"\x4d\x3b\x6b\x10"
    controlled_jumps.append(len(cave))
    cave += b"\x74\x00"
    after_p2 = len(cave)
    p2_missing_delta = after_p2 - (p2_missing_jump + 2)
    if not 0 <= p2_missing_delta <= 0x7F:
        raise RuntimeError("follower-list P2 empty branch overflow")
    cave[p2_missing_jump + 1] = p2_missing_delta
    if FOLLOWER_LIST_GUARD_INCLUDES_LEADER:
        cave += b"\x4d\x3b\x6b\x18"
        controlled_jumps.append(len(cave))
        cave += b"\x74\x00"
    cave += (
        b"\x49\x83\xbb"
        + struct.pack("<i", P3_COMMAND_BLOCK_OFFSET - 0x200 + P3_ACTOR_OFFSET)
        + b"\x00"
    )
    emit_branch(native_branches, 0x84)
    cave += b"\x4d\x3b\xab" + struct.pack(
        "<i", P3_COMMAND_BLOCK_OFFSET - 0x200 + P3_ACTOR_OFFSET
    )
    emit_branch(native_branches, 0x85)
    cave += (
        b"\x41\x83\xbb"
        + struct.pack("<i", P3_COMMAND_BLOCK_OFFSET - 0x200 + P3_MOVEMENT_ACTIVE_OFFSET)
        + b"\x00"
    )
    emit_branch(native_branches, 0x84)
    controlled = len(cave)
    for position in controlled_jumps:
        delta = controlled - (position + 2)
        if not 0 <= delta <= 0x7F:
            raise RuntimeError(f"follower-list guard branch overflow: {delta:#x}")
        cave[position + 1] = delta
    if FOLLOWER_LIST_GUARD_REJECTS_EVERY_LIST:
        cave += b"\x4d\x39\xd2"
        emit_branch(reject_branches, 0x84)
    else:
        cave += b"\x49\xbb" + struct.pack("<Q", FOLLOWER_LIST_PRIMARY)
        cave += b"\x4d\x3b\xd3"
        emit_branch(reject_branches, 0x84)
        cave += b"\x49\xbb" + struct.pack("<Q", FOLLOWER_LIST_SECONDARY)
        cave += b"\x4d\x3b\xd3"
        emit_branch(reject_branches, 0x84)

    native = len(cave)
    for position in native_branches:
        struct.pack_into("<i", cave, position + 2, native - (position + 6))
    cave += b"\x41\x5b\x9d"
    cave += original
    jump_site = FOLLOWER_LIST_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FOLLOWER_LIST_EVALUATOR_RETURN)

    reject = len(cave)
    for position in reject_branches:
        struct.pack_into("<i", cave, position + 2, reject - (position + 6))
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x49\xff\x83\x90\x00\x00\x00"
    cave += b"\x41\x5b\x9d"
    jump_site = FOLLOWER_LIST_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FOLLOWER_LIST_EMPTY_RETURN)

    if len(cave) > 0x180:
        raise RuntimeError(f"follower-list guard overflow: {len(cave):#x} bytes")
    process.write(FOLLOWER_LIST_GUARD_CAVE, cave)
    if not already_installed:
        process.patch_code(FOLLOWER_LIST_EVALUATOR_SITE, patch)


def restore_follower_list_guard(process):
    original = b"\x48\x89\x9c\x24\x80\x00\x00\x00"
    patch = (
        b"\xe9"
        + rel32(FOLLOWER_LIST_EVALUATOR_SITE, 5, FOLLOWER_LIST_GUARD_CAVE)
        + b"\x90\x90\x90"
    )
    current = process.read(FOLLOWER_LIST_EVALUATOR_SITE, len(original))
    if current == original:
        return
    if current != patch:
        raise RuntimeError(
            "unexpected bytes while restoring follower-list evaluator: "
            + current.hex(" ")
        )
    process.patch_code(FOLLOWER_LIST_EVALUATOR_SITE, original)


def restore_visual_facing_update(process):
    original = bytes((0xE8, 0x98, 0xD9, 0x02, 0x00))
    patch = bytes((0xE8,)) + rel32(VISUAL_FACING_CALL_SITE, 5, VISUAL_FACING_CAVE)
    current = process.read(VISUAL_FACING_CALL_SITE, len(original))
    if current == original:
        return
    if current != patch:
        raise RuntimeError(
            "unexpected bytes while restoring visual-facing call: " + current.hex(" ")
        )
    process.patch_code(VISUAL_FACING_CALL_SITE, original)


def build_follower_gateway_collision_filter_cave(
    cave_address,
    command_block,
    third_block,
):
    cave = bytearray()
    labels = {}
    fixups = []

    def label(name):
        labels[name] = len(cave)

    def branch(opcode, target):
        cave.extend((opcode, 0))
        fixups.append((len(cave) - 1, target))

    p3_context_displacement = (
        int(third_block) + P3_SKELETON_KEEP_OFFSET - int(command_block)
    )
    p1_berserk_state = int(command_block) + P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200

    cave += b"\x9c\x41\x52\x41\x53"
    cave += b"\x49\xbb" + struct.pack("<Q", int(command_block))
    cave += b"\x41\x83\x7b" + bytes((COOP_ACTIVE_OFFSET,)) + b"\x00"
    branch(0x74, "done")
    cave += b"\x66\x83\xfb\x01"
    branch(0x75, "done")

    cave += b"\x49\xba" + struct.pack("<Q", p1_berserk_state)
    cave += b"\x41\x83\x7a" + bytes((P1_BERSERK_ACTIVE_OFFSET,)) + b"\x00"
    branch(0x74, "check_p2")
    cave += b"\x4d\x8b\x52" + bytes((P1_BERSERK_SKELETON_KEEP_OFFSET,))
    cave += b"\x4d\x85\xd2"
    branch(0x74, "check_p2")
    cave += b"\x49\x3b\xca"
    branch(0x74, "leader_class")

    label("check_p2")
    cave += b"\x4d\x8b\x53" + bytes((P2_SKELETON_KEEP_OFFSET,))
    cave += b"\x4d\x85\xd2"
    branch(0x74, "check_p3")
    cave += b"\x49\x3b\xca"
    branch(0x74, "leader_class")

    label("check_p3")
    cave += b"\x4d\x8b\x93" + struct.pack("<i", p3_context_displacement)
    cave += b"\x4d\x85\xd2"
    branch(0x74, "done")
    cave += b"\x49\x3b\xca"
    branch(0x75, "done")

    label("leader_class")
    cave += b"\x31\xdb"

    label("done")
    cave += b"\x41\x5b\x41\x5a\x9d"
    cave += FOLLOWER_GATEWAY_COLLISION_FILTER_ORIGINAL
    jump_site = int(cave_address) + len(cave)
    cave += b"\xe9" + rel32(
        jump_site,
        5,
        FOLLOWER_GATEWAY_COLLISION_FILTER_RETURN,
    )

    for position, target in fixups:
        displacement = labels[target] - (position + 1)
        if not -128 <= displacement <= 127:
            raise RuntimeError(
                "follower gateway collision filter branch overflow: "
                f"{target}={displacement:#x}"
            )
        cave[position] = displacement & 0xFF
    if len(cave) > 0x100:
        raise RuntimeError(
            f"follower gateway collision filter overflow: {len(cave):#x}"
        )
    return bytes(cave)


def follower_gateway_collision_filter_patch(cave_address):
    return (
        b"\xe9"
        + rel32(
            FOLLOWER_GATEWAY_COLLISION_FILTER_SITE,
            5,
            int(cave_address),
        )
        + b"\x90\x90"
    )


def follower_gateway_trigger_patch(cave):
    return b"\xe9" + rel32(GATEWAY_TRIGGER_SITE, 5, cave)


def build_follower_gateway_trigger_cave(cave, scratch, command_block, p3_block):
    code = bytearray()
    fixups = []
    labels = {}

    def emit(data):
        code.extend(data)

    def rip(opcode, target, trailing=0):
        emit(opcode)
        fixups.append((len(code), target, trailing))
        emit(b"\x00\x00\x00\x00")

    def jump(opcode, label):
        emit(opcode)
        fixups.append((len(code), label, 0))
        emit(b"\x00\x00\x00\x00")

    def mark(label):
        labels[label] = len(code)

    def call(target):
        emit(b"\xe8")
        fixups.append((len(code), target, 0))
        emit(b"\x00\x00\x00\x00")

    saved_handle = scratch
    # These counters identify which guard stopped the follower pass.
    count_active = scratch + GATEWAY_TRIGGER_COUNT_ACTIVE
    count_entity = scratch + GATEWAY_TRIGGER_COUNT_ENTITY
    count_handle = scratch + GATEWAY_TRIGGER_COUNT_HANDLE
    count_pass = scratch + GATEWAY_TRIGGER_COUNT_PASS

    rip(b"\x8b\x05", CONTROLLED_HANDLE_GLOBAL)
    rip(b"\x89\x05", saved_handle)

    # FUN_0025B600 obtains CONTROLLED_HANDLE_GLOBAL and resolves the live
    # interaction entity itself through the actor manager.  Feeding it an
    # externally published entry pointer was both redundant and unsafe: an
    # actor can despawn after publication, leaving that pointer unmapped before
    # ENTITY_HANDLE_RESOLVER dereferences it.  Keep the native P1 pass intact
    # and name each follower only by its generation-checked, stable handle.
    call(GATEWAY_TRIGGER_NATIVE)
    rip(b"\x83\x3d", command_block + 8, trailing=1)
    emit(b"\x00")
    jump(b"\x0f\x84", "restore")
    rip(b"\xff\x05", count_active)

    for index, handle in enumerate(
        (
            command_block + P2_LOOT_HANDLE_OFFSET,
            p3_block + P3_LOOT_HANDLE_OFFSET,
        )
    ):
        skip = f"skip{index}"
        rip(b"\x8b\x05", handle)
        emit(b"\x85\xc0")
        jump(b"\x0f\x84", skip)
        rip(b"\xff\x05", count_entity)
        rip(b"\x3b\x05", saved_handle)
        jump(b"\x0f\x84", skip)
        rip(b"\xff\x05", count_handle)
        rip(b"\x89\x05", CONTROLLED_HANDLE_GLOBAL)
        rip(b"\xff\x05", count_pass)
        call(GATEWAY_TRIGGER_NATIVE)
        mark(skip)

    mark("restore")
    rip(b"\x8b\x05", saved_handle)
    rip(b"\x89\x05", CONTROLLED_HANDLE_GLOBAL)

    mark("done")
    jump(b"\xe9", GATEWAY_TRIGGER_RETURN)

    for position, target, trailing in fixups:
        end = position + 4 + trailing
        absolute = labels[target] + cave if isinstance(target, str) else target
        displacement = absolute - (cave + end)
        if not -0x80000000 <= displacement <= 0x7FFFFFFF:
            raise RuntimeError("follower-gateway-trigger displacement overflow")
        struct.pack_into("<i", code, position, displacement)

    limit = FOLLOWER_GATEWAY_TRIGGER_SCRATCH_OFFSET - (
        FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET
    )
    if len(code) > limit:
        raise RuntimeError(f"follower-gateway-trigger cave overflow: {len(code):#x}")
    return bytes(code)


def install_follower_gateway_trigger(process):
    patch = follower_gateway_trigger_patch(FOLLOWER_GATEWAY_TRIGGER_CAVE)
    current = process.read(GATEWAY_TRIGGER_SITE, len(GATEWAY_TRIGGER_ORIGINAL))
    already_installed = current == patch
    if not already_installed and current != GATEWAY_TRIGGER_ORIGINAL:
        raise RuntimeError(
            "unexpected bytes at gateway trigger call: " + current.hex(" ")
        )

    process.write(
        FOLLOWER_GATEWAY_TRIGGER_CAVE,
        build_follower_gateway_trigger_cave(
            FOLLOWER_GATEWAY_TRIGGER_CAVE,
            FOLLOWER_GATEWAY_TRIGGER_SCRATCH,
            COMMAND_BLOCK,
            third_player_block(),
        ),
    )
    process.write(
        FOLLOWER_GATEWAY_TRIGGER_SCRATCH,
        b"\x00" * FOLLOWER_GATEWAY_TRIGGER_SCRATCH_SIZE,
    )
    if not already_installed:
        process.patch_code(GATEWAY_TRIGGER_SITE, patch)


def restore_follower_gateway_trigger(process):
    patch = follower_gateway_trigger_patch(FOLLOWER_GATEWAY_TRIGGER_CAVE)
    current = process.read(GATEWAY_TRIGGER_SITE, len(GATEWAY_TRIGGER_ORIGINAL))
    if current == GATEWAY_TRIGGER_ORIGINAL:
        return
    if current != patch:
        raise RuntimeError(
            "unexpected bytes while restoring gateway trigger: " + current.hex(" ")
        )
    process.patch_code(GATEWAY_TRIGGER_SITE, GATEWAY_TRIGGER_ORIGINAL)


def install_follower_gateway_collision_filter(process):
    original = FOLLOWER_GATEWAY_COLLISION_FILTER_ORIGINAL
    patch = follower_gateway_collision_filter_patch(
        FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE
    )
    current = process.read(
        FOLLOWER_GATEWAY_COLLISION_FILTER_SITE,
        len(original),
    )
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            "unexpected bytes at follower gateway collision filter: " + current.hex(" ")
        )

    process.write(
        FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE,
        build_follower_gateway_collision_filter_cave(
            FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE,
            COMMAND_BLOCK,
            third_player_block(),
        ),
    )
    if not already_installed:
        process.patch_code(FOLLOWER_GATEWAY_COLLISION_FILTER_SITE, patch)


def restore_follower_gateway_collision_filter(process):
    original = FOLLOWER_GATEWAY_COLLISION_FILTER_ORIGINAL
    patch = follower_gateway_collision_filter_patch(
        FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE
    )
    current = process.read(
        FOLLOWER_GATEWAY_COLLISION_FILTER_SITE,
        len(original),
    )
    if current == original:
        return
    if current != patch:
        raise RuntimeError(
            "unexpected bytes while restoring follower gateway collision "
            "filter: " + current.hex(" ")
        )
    process.patch_code(FOLLOWER_GATEWAY_COLLISION_FILTER_SITE, original)


def install_follower_locomotion_animation_guard(process):
    original = FOLLOWER_LOCOMOTION_STORE_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(
            FOLLOWER_LOCOMOTION_STORE_SITE,
            5,
            FOLLOWER_LOCOMOTION_GUARD_CAVE,
        )
        + b"\x90\x90"
    )
    current = process.read(FOLLOWER_LOCOMOTION_STORE_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            "unexpected bytes at follower locomotion store: " + current.hex(" ")
        )

    cave = bytearray()
    cave += b"\x9c\x41\x53"
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    native_branches = []

    def emit_native(opcode):
        position = len(cave)
        cave.extend(b"\x0f" + bytes((opcode + 0x10,)) + b"\x00\x00\x00\x00")
        native_branches.append(position)

    p3 = P3_COMMAND_BLOCK_OFFSET - 0x200
    cave += b"\x41\x83\x7b" + bytes((COOP_ACTIVE_OFFSET,)) + b"\x00"
    emit_native(0x74)

    cave += b"\x49\x3b\x4b" + bytes((P2_SKELETON_KEEP_OFFSET,))
    p2_skeleton_jump = len(cave)
    cave += b"\x74\x00"
    cave += b"\x49\x3b\x8b" + struct.pack("<i", p3 + P3_SKELETON_KEEP_OFFSET)
    emit_native(0x75)
    cave += b"\x41\x83\xbb" + struct.pack("<i", p3 + P3_MAGNITUDE_OFFSET) + b"\x00"
    emit_native(0x75)
    cave += b"\x4d\x8b\x9b" + struct.pack("<i", p3 + P3_ACTOR_OFFSET)
    p3_done_jump = len(cave)
    cave += b"\xeb\x00"

    p2_path = len(cave)
    p2_skeleton_delta = p2_path - (p2_skeleton_jump + 2)
    if not 0 <= p2_skeleton_delta <= 0x7F:
        raise RuntimeError(
            f"follower locomotion guard P2 branch overflow: {p2_skeleton_delta:#x}"
        )
    cave[p2_skeleton_jump + 1] = p2_skeleton_delta
    cave += b"\x41\x83\x7b\x04\x00"
    emit_native(0x75)
    cave += b"\x4d\x8b\x5b\x10"

    common = len(cave)
    p3_done_delta = common - (p3_done_jump + 2)
    if not 0 <= p3_done_delta <= 0x7F:
        raise RuntimeError(
            f"follower locomotion guard P3 branch overflow: {p3_done_delta:#x}"
        )
    cave[p3_done_jump + 1] = p3_done_delta
    cave += b"\x4d\x85\xdb"
    emit_native(0x74)
    cave += b"\x49\x0f\xba\x23\x2e"
    emit_native(0x72)
    cave += b"\x41\x80\xbb\xb4\x06\x00\x00\x04"
    emit_native(0x75)
    cave += b"\x66\x41\x81\xbb\x14\x07\x00\x00\x96\x00"
    emit_native(0x75)
    cave += b"\x41\x83\xbb\x10\x07\x00\x00\x00"
    emit_native(0x74)

    # Locomotion mode is published in two copies. Clearing BX before the original
    # store keeps both copies synchronized for the controlled follower.
    cave += b"\x41\x5b\x9d"
    cave += b"\x31\xdb"
    cave += original
    jump_site = FOLLOWER_LOCOMOTION_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FOLLOWER_LOCOMOTION_STORE_RETURN)

    native = len(cave)
    for position in native_branches:
        struct.pack_into("<i", cave, position + 2, native - (position + 6))
    cave += b"\x41\x5b\x9d"
    cave += original
    jump_site = FOLLOWER_LOCOMOTION_GUARD_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, FOLLOWER_LOCOMOTION_STORE_RETURN)
    if len(cave) > 0x180:
        raise RuntimeError(f"follower locomotion guard overflow: {len(cave):#x}")
    process.write(FOLLOWER_LOCOMOTION_GUARD_CAVE, cave)
    if not already_installed:
        process.patch_code(FOLLOWER_LOCOMOTION_STORE_SITE, patch)


def restore_follower_locomotion_animation_guard(process):
    original = FOLLOWER_LOCOMOTION_STORE_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(
            FOLLOWER_LOCOMOTION_STORE_SITE,
            5,
            FOLLOWER_LOCOMOTION_GUARD_CAVE,
        )
        + b"\x90\x90"
    )
    current = process.read(FOLLOWER_LOCOMOTION_STORE_SITE, len(original))
    gateway_patch = follower_gateway_collision_filter_patch(
        FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE
    )
    if current == gateway_patch:
        return
    if current == patch:
        process.patch_code(FOLLOWER_LOCOMOTION_STORE_SITE, original)
    elif current != original:
        raise RuntimeError(
            "unexpected bytes while restoring follower locomotion store: "
            + current.hex(" ")
        )


def follower_action_mailbox_address(role):
    if role == "p2":
        return P2_FOLLOWER_ACTION_MAILBOX
    if role == "p3":
        return P3_FOLLOWER_ACTION_MAILBOX
    return 0


def clear_follower_action_mailbox(process, role):
    address = follower_action_mailbox_address(role)
    if not address:
        return False
    process.write(address, b"\x00" * FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE)
    return True


def _next_follower_action_sequence(current):
    committed = ((int(current) & 0xFFFFFFFE) + 2) & 0xFFFFFFFE
    return committed or 2


def _publish_follower_action_transition(
    process,
    *,
    role,
    handle,
    actor,
    expected_phys,
    expected_action,
    desired_action,
    persistent,
):
    """Publish or observe one generation-checked native action transition.

    Python never dereferences or writes the actor action packet.  A stable-even
    sequence commits comparison-only identity data; the follower-list hook owns
    all validation and the two coherent game-thread stores.
    """

    address = follower_action_mailbox_address(role)
    try:
        handle = int(handle)
        actor = int(actor)
        expected_phys = int(expected_phys)
        expected_action = int(expected_action)
        desired_action = int(desired_action)
    except (TypeError, ValueError):
        return False
    if (
        not address
        or not 0 < handle <= 0xFFFFFFFF
        or not 0 < actor <= 0xFFFFFFFFFFFFFFFF
        or not 0 < expected_phys <= 0xFFFFFFFFFFFFFFFF
        or not 0 <= expected_action <= 0xFFFF
        or not 0 <= desired_action <= 0xFFFF
        or (expected_action, desired_action)
        not in (
            (FOLLOWER_IDLE_ACTION, NATIVE_IDLE_ACTION),
            (NATIVE_IDLE_ACTION, FOLLOWER_IDLE_ACTION),
        )
    ):
        return False

    try:
        raw = process.read(address, FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE)
        (
            sequence,
            ack_sequence,
            result,
            published_handle,
            published_actor,
            published_phys,
            published_expected,
            published_desired,
            _reserved,
        ) = FOLLOWER_ACTION_MAILBOX_STRUCT.unpack(raw)
    except (OSError, RuntimeError, struct.error):
        return False

    same_request = bool(
        sequence
        and not sequence & 1
        and published_handle == handle
        and published_actor == actor
        and published_phys == expected_phys
        and published_expected == expected_action
        and published_desired == desired_action
    )
    if same_request:
        if ack_sequence != sequence:
            return False
        if result in (
            FOLLOWER_ACTION_RESULT_APPLIED,
            FOLLOWER_ACTION_RESULT_ALREADY_DESIRED,
        ):
            if persistent:
                # Leave the immutable request armed.  Clearing only the result
                # lets the helper report a genuinely recurring residual action
                # without producing one log line per native list evaluation.
                process.write(
                    address + FOLLOWER_ACTION_RESULT_OFFSET,
                    struct.pack("<I", FOLLOWER_ACTION_RESULT_PENDING),
                )
            else:
                clear_follower_action_mailbox(process, role)
            return True
        if result != FOLLOWER_ACTION_RESULT_PENDING:
            clear_follower_action_mailbox(process, role)
        return False

    committed = _next_follower_action_sequence(sequence)
    in_progress = committed | 1
    payload = FOLLOWER_ACTION_MAILBOX_STRUCT.pack(
        in_progress,
        0,
        FOLLOWER_ACTION_RESULT_PENDING,
        handle,
        actor,
        expected_phys,
        expected_action,
        desired_action,
        0,
    )
    try:
        process.write(
            address + FOLLOWER_ACTION_REQUEST_SEQUENCE_OFFSET,
            struct.pack("<I", in_progress),
        )
        process.write(address + 4, payload[4:])
        process.write(
            address + FOLLOWER_ACTION_REQUEST_SEQUENCE_OFFSET,
            struct.pack("<I", committed),
        )
    except (OSError, RuntimeError, struct.error):
        return False
    return False


def sanitize_residual_follower_action(
    process,
    actor,
    coop_active,
    *,
    role=None,
    handle=0,
    expected_phys=0,
):
    if not coop_active:
        return False
    return _publish_follower_action_transition(
        process,
        role=role,
        handle=handle,
        actor=actor,
        expected_phys=expected_phys,
        expected_action=FOLLOWER_IDLE_ACTION,
        desired_action=NATIVE_IDLE_ACTION,
        persistent=True,
    )


def actor_physics_pointer(process, actor):
    """Resolve an actor's live physics object through its native root."""

    if not actor:
        return 0
    try:
        root = process.u64(int(actor) + 0x10)
        if not root:
            return 0
        phys = process.u64(int(root) + 0xB8)
        return int(phys) if phys else 0
    except ACTOR_SAMPLE_ERRORS:
        return 0


def resume_sanitized_follower_action(
    process,
    actor,
    *,
    role=None,
    handle=0,
    expected_phys=0,
):
    """Resume a native follow request suppressed while the actor was controlled."""

    return _publish_follower_action_transition(
        process,
        role=role,
        handle=handle,
        actor=actor,
        expected_phys=expected_phys,
        expected_action=NATIVE_IDLE_ACTION,
        desired_action=FOLLOWER_IDLE_ACTION,
        persistent=False,
    )


def restore_follower_action_filter(process):
    original = b"\x66\x89\xbb\x14\x07\x00\x00"
    patch = (
        b"\xe9"
        + rel32(FOLLOWER_ACTION_STORE_SITE, 5, FOLLOWER_ACTION_FILTER_CAVE)
        + b"\x90\x90"
    )
    current = process.read(FOLLOWER_ACTION_STORE_SITE, len(original))
    if current == patch:
        process.patch_code(FOLLOWER_ACTION_STORE_SITE, original)
    elif current != original:
        raise RuntimeError(
            f"unexpected bytes while restoring follower action store: "
            f"{current.hex(' ')}"
        )


def build_camera_snapshot_helper():
    """Build the native seqlock reader for one complete renderer snapshot.

    RCX points to a private 0x20-byte destination.  The helper returns one in
    EAX only after lifecycle-active and the atomic output commit stayed stable
    around the whole copy.  All other GPRs and every XMM register are preserved.
    """

    code = bytearray()
    reject_branches = []

    def reject(opcode):
        position = len(code)
        code.extend(b"\x0f" + bytes((opcode,)) + b"\x00\x00\x00\x00")
        reject_branches.append(position)

    code += b"\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
    code += b"\x49\xba" + struct.pack("<Q", COMMAND_BLOCK)
    code += b"\x41\x83\x7a" + bytes((CAMERA_ACTIVE_OFFSET,)) + b"\x01"
    reject(0x85)

    code += b"\x49\xba" + struct.pack("<Q", CAMERA_FRAME_STATE)
    code += b"\x41\x81\xba" + struct.pack(
        "<I", camera_frame_sync.CAMERA_OUTPUT_HEADER_OFFSET
    ) + struct.pack("<I", camera_frame_sync.CAMERA_FRAME_MAGIC)
    reject(0x85)
    code += b"\x41\x81\xba" + struct.pack(
        "<I", camera_frame_sync.CAMERA_OUTPUT_HEADER_OFFSET + 4
    ) + struct.pack("<I", camera_frame_sync.CAMERA_FRAME_VERSION)
    reject(0x85)

    code += b"\x4d\x8b\x9a" + struct.pack(
        "<I", camera_frame_sync.CAMERA_OUTPUT_COMMIT_OFFSET
    )
    code += b"\x45\x85\xdb"
    reject(0x84)
    code += b"\x4c\x89\xda\x48\xc1\xea\x20\x83\xfa\x01"
    reject(0x85)

    code += b"\x4d\x8d\x92" + struct.pack(
        "<I", camera_frame_sync.CAMERA_OUTPUT_SLOT_OFFSETS[0]
    )
    code += b"\x41\xf6\xc3\x01\x74\x04\x49\x83\xc2\x20"
    for offset in range(0, camera_frame_sync.CAMERA_OUTPUT_SLOT_SIZE, 8):
        if offset:
            code += b"\x49\x8b\x52" + bytes((offset,))
            code += b"\x48\x89\x51" + bytes((offset,))
        else:
            code += b"\x49\x8b\x12\x48\x89\x11"

    code += b"\x49\xba" + struct.pack("<Q", CAMERA_FRAME_STATE)
    code += b"\x4d\x3b\x9a" + struct.pack(
        "<I", camera_frame_sync.CAMERA_OUTPUT_COMMIT_OFFSET
    )
    reject(0x85)
    code += b"\x49\xba" + struct.pack("<Q", COMMAND_BLOCK)
    code += b"\x41\x83\x7a" + bytes((CAMERA_ACTIVE_OFFSET,)) + b"\x01"
    reject(0x85)
    code += b"\xb8\x01\x00\x00\x00"
    success_jump = len(code)
    code += b"\xe9\x00\x00\x00\x00"

    reject_path = len(code)
    code += b"\x31\xc0"
    restore = len(code)
    code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\xc3"

    for branch in reject_branches:
        struct.pack_into("<i", code, branch + 2, reject_path - (branch + 6))
    struct.pack_into("<i", code, success_jump + 1, restore - (success_jump + 5))
    if len(code) > CAMERA_SNAPSHOT_HELPER_SIZE:
        raise RuntimeError("camera snapshot helper overflow")
    return bytes(code)


def install_camera_snapshot_helper(process):
    process.write(CAMERA_SNAPSHOT_HELPER, build_camera_snapshot_helper())


def install_camera_midpoint(process):
    original = b"\x0f\x11\x4a\x40\x48\x8b\xcb"
    patch = b"\xe9" + rel32(CAMERA_HOOK_SITE, 5, CAMERA_CAVE) + b"\x90" * 2

    current = process.read(CAMERA_HOOK_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(f"unexpected bytes at camera hook site: {current.hex(' ')}")

    install_camera_snapshot_helper(process)
    cave = bytearray(original)
    cave += b"\x9c\x50\x51\x48\x83\xec\x30"
    cave += b"\x0f\x11\x44\x24\x20"
    cave += b"\x48\xb8" + struct.pack("<Q", CAMERA_MASTER_STATE)
    cave += b"\x48\x39\xc2"
    restore_branches = []

    def restore_if(opcode):
        position = len(cave)
        cave.extend(b"\x0f" + bytes((opcode,)) + b"\x00\x00\x00\x00")
        restore_branches.append(position)

    restore_if(0x85)
    cave += b"\x48\x8d\x0c\x24"
    call_site = CAMERA_CAVE + len(cave)
    cave += b"\xe8" + rel32(call_site, 5, CAMERA_SNAPSHOT_HELPER)
    cave += b"\x85\xc0"
    restore_if(0x84)

    for eye_offset, focus_offset, snapshot_offset in (
        (0x10, 0x20, 0x00),
        (0x14, 0x24, 0x04),
        (0x18, 0x28, 0x08),
    ):
        cave += b"\xf3\x0f\x10\x44\x24" + bytes((snapshot_offset,))
        cave += b"\xf3\x0f\x5c\x42" + bytes((focus_offset,))
        cave += b"\xf3\x0f\x58\x42" + bytes((eye_offset,))
        cave += b"\xf3\x0f\x11\x42" + bytes((eye_offset,))
        cave += b"\xf3\x0f\x10\x44\x24" + bytes((snapshot_offset,))
        cave += b"\xf3\x0f\x11\x42" + bytes((focus_offset,))

    restore = len(cave)
    for branch in restore_branches:
        struct.pack_into("<i", cave, branch + 2, restore - (branch + 6))
    cave += b"\x0f\x10\x44\x24\x20"
    cave += b"\x48\x83\xc4\x30\x59\x58\x9d"
    jump_site = CAMERA_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, CAMERA_HOOK_RETURN)
    if len(cave) > 0x100:
        raise RuntimeError("camera midpoint cave overflow")
    process.write(CAMERA_CAVE, cave)
    if not already_installed:
        process.patch_code(CAMERA_HOOK_SITE, patch)


def restore_camera_midpoint(process):
    original = b"\x0f\x11\x4a\x40\x48\x8b\xcb"
    patch = b"\xe9" + rel32(CAMERA_HOOK_SITE, 5, CAMERA_CAVE) + b"\x90" * 2
    current = process.read(CAMERA_HOOK_SITE, len(original))
    smoothing_patch = (
        b"\xe9" + rel32(CAMERA_HOOK_SITE, 5, CAMERA_SMOOTH_CAVE) + b"\x90" * 2
    )
    if current == smoothing_patch:
        return
    if current == patch:
        process.patch_code(CAMERA_HOOK_SITE, original)
    elif current != original:
        raise RuntimeError(
            f"unexpected bytes restoring camera midpoint: {current.hex(' ')}"
        )


def install_controller_shared_focus(process):
    original = CAMERA_CONTROLLER_FOCUS_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(
            CAMERA_CONTROLLER_FOCUS_HOOK_SITE,
            5,
            CAMERA_FOCUS_V2_CAVE,
        )
        + b"\x90" * (len(original) - 5)
    )
    current = process.read(CAMERA_CONTROLLER_FOCUS_HOOK_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(
            f"unexpected bytes at camera-controller focus site: {current.hex(' ')}"
        )

    install_camera_snapshot_helper(process)
    cave = bytearray(original)
    # The hook may execute more than once in a camera pass.  It only asks the
    # shared helper for one committed Present snapshot and never advances time.
    cave += b"\x9c\x50\x51\x52\x48\x83\xec\x20"
    cave += b"\x48\x8d\x0c\x24"
    call_site = CAMERA_FOCUS_V2_CAVE + len(cave)
    cave += b"\xe8" + rel32(call_site, 5, CAMERA_SNAPSHOT_HELPER)
    cave += b"\x85\xc0"
    inactive_jump = len(cave)
    cave += b"\x0f\x84\x00\x00\x00\x00"
    cave += b"\x8b\x04\x24\x8b\x4c\x24\x04\x8b\x54\x24\x08"
    cave += b"\x89\x47\x60\x89\x4f\x64\x89\x57\x68"
    cave += b"\xc7\x47\x6c\x00\x00\x80\x3f"
    restore = len(cave)
    struct.pack_into("<i", cave, inactive_jump + 2, restore - (inactive_jump + 6))
    cave += b"\x48\x83\xc4\x20\x5a\x59\x58\x9d"
    jump_site = CAMERA_FOCUS_V2_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, CAMERA_CONTROLLER_FOCUS_HOOK_RETURN)
    if len(cave) > 0x100:
        raise RuntimeError("camera-controller focus cave overflow")
    process.write(CAMERA_FOCUS_V2_CAVE, cave)
    if not already_installed:
        process.patch_code(CAMERA_CONTROLLER_FOCUS_HOOK_SITE, patch)


def restore_upstream_camera_focus_v2(process):
    original = CAMERA_FOCUS_V2_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(CAMERA_FOCUS_V2_HOOK_SITE, 5, CAMERA_FOCUS_V2_CAVE)
        + b"\x90" * (len(original) - 5)
    )
    current = process.read(CAMERA_FOCUS_V2_HOOK_SITE, len(original))
    if current == patch:
        process.patch_code(CAMERA_FOCUS_V2_HOOK_SITE, original)
    elif current != original:
        raise RuntimeError(
            f"unexpected bytes restoring camera V2 focus: {current.hex(' ')}"
        )


def restore_controller_shared_focus(process):
    original = CAMERA_CONTROLLER_FOCUS_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(
            CAMERA_CONTROLLER_FOCUS_HOOK_SITE,
            5,
            CAMERA_FOCUS_V2_CAVE,
        )
        + b"\x90" * (len(original) - 5)
    )
    current = process.read(CAMERA_CONTROLLER_FOCUS_HOOK_SITE, len(original))
    if current == patch:
        process.patch_code(CAMERA_CONTROLLER_FOCUS_HOOK_SITE, original)
    elif current != original:
        raise RuntimeError(
            f"unexpected bytes restoring camera-controller focus: {current.hex(' ')}"
        )


def _native_camera_collision_call(site):
    return b"\xe8" + rel32(site, 5, CAMERA_COLLISION_QUERY)


def _shared_camera_collision_call(site):
    return b"\xe8" + rel32(site, 5, CAMERA_COLLISION_FILTER_CAVE)


def _build_shared_camera_collision_filter():
    template_path = Path(__file__).with_name("native_camera_collision_filter_v2.bin")
    cave = bytearray(template_path.read_bytes())
    replacements = (
        (
            struct.pack("<Q", 0x1111111122222222),
            struct.pack("<Q", COMMAND_BLOCK),
            1,
        ),
        (
            struct.pack("<Q", 0x3333333344444444),
            struct.pack("<Q", CAMERA_COLLISION_FILTER_SCRATCH),
            4,
        ),
        (
            struct.pack("<Q", 0x5555555566666666),
            struct.pack("<Q", CAMERA_COLLISION_QUERY),
            2,
        ),
    )
    for marker, replacement, expected_count in replacements:
        actual_count = cave.count(marker)
        if actual_count != expected_count:
            raise RuntimeError(
                "camera collision V2 marker layout changed: "
                f"expected {expected_count}, found {actual_count}"
            )
        cave = bytearray(bytes(cave).replace(marker, replacement))
    if len(cave) > (CAMERA_COLLISION_FILTER_SCRATCH - CAMERA_COLLISION_FILTER_CAVE):
        raise RuntimeError("camera collision V2 wrapper overlaps its scratch state")
    return bytes(cave)


def camera_collision_formation_payload(
    p1_position,
    p2_position,
    tuning,
    *,
    enabled,
    native_boom=None,
    previously_active=False,
):
    try:
        p1 = tuple(float(value) for value in p1_position[:3])
        p2 = tuple(float(value) for value in p2_position[:3])
    except (TypeError, ValueError):
        p1 = ()
        p2 = ()
    valid = bool(
        enabled
        and len(p1) == 3
        and len(p2) == 3
        and all(math.isfinite(value) for value in (*p1, *p2))
    )
    if not valid:
        return b"\x00" * 0x24

    if native_boom is not None and math.isfinite(native_boom):
        limite = float(
            tuning.internal_collision_boom_release
            if previously_active
            else tuning.internal_collision_min_boom
        )
        if native_boom < limite:
            return b"\x00" * 0x24

    if math.hypot(p2[0] - p1[0], p2[2] - p1[2]) < float(
        tuning.internal_collision_min_separation
    ):
        return b"\x00" * 0x24
    height = float(tuning.target_height)

    p1, p2 = clamp_formation_span(
        p1, p2, float(tuning.internal_collision_corridor_length)
    )
    horizontal_radius = formation_corridor_radius((p1, p2), tuning)

    if native_boom is not None and math.isfinite(native_boom) and native_boom > 0.0:
        horizontal_radius = min(
            horizontal_radius,
            max(
                0.5,
                float(native_boom) * float(tuning.internal_collision_radius_boom_ratio),
            ),
        )
    vertical_radius = max(0.1, float(tuning.internal_collision_vertical_radius))
    return struct.pack(
        "<3f3fffI",
        p1[0],
        p1[1] + height,
        p1[2],
        p2[0],
        p2[1] + height,
        p2[2],
        horizontal_radius * horizontal_radius,
        vertical_radius * vertical_radius,
        1,
    )


def camera_collision_focus_escape_payload(
    positions,
    camera_focus,
    tuning,
    *,
    enabled,
):
    try:
        party = [
            tuple(float(value) for value in position[:3]) for position in positions
        ]
        focus = tuple(float(value) for value in camera_focus[:3])
    except (TypeError, ValueError):
        return b"\x00" * 8
    values = [*focus]
    for member in party:
        values.extend(member)
    valid = bool(
        enabled
        and len(party) >= 2
        and len(focus) == 3
        and all(len(member) == 3 for member in party)
        and all(math.isfinite(value) for value in values)
    )
    if not valid:
        return b"\x00" * 8

    actor_height = float(tuning.target_height)
    nearest_actor = min(
        math.sqrt(
            (focus[0] - member[0]) ** 2
            + (focus[1] - (member[1] + actor_height)) ** 2
            + (focus[2] - member[2]) ** 2
        )
        for member in party
    )
    if nearest_actor < float(tuning.focus_collision_escape_min_actor_distance):
        return b"\x00" * 8
    maximum_hit = max(0.0, float(tuning.focus_collision_escape_max_hit_distance))
    if maximum_hit <= 0.0:
        return b"\x00" * 8
    return struct.pack("<If", 1, maximum_hit * maximum_hit)


def clamp_formation_span(first, second, limit):
    if limit <= 0.0:
        return first, second
    dx = second[0] - first[0]
    dz = second[2] - first[2]
    span = math.hypot(dx, dz)
    if span <= limit or span <= 0.0:
        return first, second
    keep = (limit / span) / 2.0
    middle = ((first[0] + second[0]) / 2.0, (first[2] + second[2]) / 2.0)
    return (
        (middle[0] - dx * keep, first[1], middle[1] - dz * keep),
        (middle[0] + dx * keep, second[1], middle[1] + dz * keep),
    )


def publish_camera_collision_formation(
    process,
    p1_position,
    p2_position,
    tuning,
    *,
    enabled,
    native_boom=None,
    previously_active=False,
):
    payload = camera_collision_formation_payload(
        p1_position,
        p2_position,
        tuning,
        enabled=enabled,
        native_boom=native_boom,
        previously_active=previously_active,
    )
    process.write(
        CAMERA_COLLISION_FILTER_SCRATCH + CAMERA_COLLISION_FORMATION_OFFSET,
        payload,
    )
    return payload != b"\x00" * 0x24


def publish_camera_collision_focus_escape(
    process,
    positions,
    camera_focus,
    tuning,
    *,
    enabled,
):
    payload = camera_collision_focus_escape_payload(
        positions,
        camera_focus,
        tuning,
        enabled=enabled,
    )
    process.write(
        CAMERA_COLLISION_FILTER_SCRATCH + CAMERA_COLLISION_FOCUS_ESCAPE_ACTIVE_OFFSET,
        payload,
    )
    return payload != b"\x00" * 8


def widest_party_pair(positions):
    party = [tuple(position[:3]) for position in positions]
    if len(party) < 2:
        return (), ()
    return max(
        (
            (party[first], party[second])
            for first in range(len(party))
            for second in range(first + 1, len(party))
        ),
        key=lambda pair: (
            (pair[1][0] - pair[0][0]) ** 2 + (pair[1][2] - pair[0][2]) ** 2
        ),
    )


def install_shared_camera_internal_collision_filter(process):
    patches = []
    for site in CAMERA_COLLISION_CALL_SITES:
        original = _native_camera_collision_call(site)
        patch = _shared_camera_collision_call(site)
        current = process.read(site, len(original))
        if current not in (original, patch):
            raise RuntimeError(
                f"unexpected bytes at camera collision call {site:#x}: "
                f"{current.hex(' ')}"
            )
        patches.append((site, current == patch, patch))

    process.write(
        CAMERA_COLLISION_FILTER_CAVE,
        _build_shared_camera_collision_filter(),
    )
    process.write(
        COMMAND_BLOCK + CAMERA_COLLISION_EXCLUSION_START_OFFSET,
        struct.pack("<2f", 0.0, 0.0),
    )
    process.write(
        CAMERA_COLLISION_FILTER_SCRATCH,
        b"\x00" * 0x50,
    )
    for site, already_installed, patch in patches:
        if not already_installed:
            process.patch_code(site, patch)


def restore_shared_camera_internal_collision_filter(process):
    for site in CAMERA_COLLISION_CALL_SITES:
        original = _native_camera_collision_call(site)
        patch = _shared_camera_collision_call(site)
        current = process.read(site, len(original))
        if current == patch:
            process.patch_code(site, original)
        elif current != original:
            raise RuntimeError(
                f"unexpected bytes restoring camera collision call {site:#x}: "
                f"{current.hex(' ')}"
            )


def install_collision_aware_camera_zoom(process):
    # Validate both detours before writing the shared helper.  A foreign or
    # legacy patch must leave the live installation byte-for-byte untouched.
    for site, cave_address, original in (
        (CAMERA_DISTANCE_MUL1_SITE, CAMERA_DISTANCE_CAVE1, CAMERA_RIG_BUILD1_ORIGINAL),
        (CAMERA_DISTANCE_MUL2_SITE, CAMERA_DISTANCE_CAVE2, CAMERA_RIG_BUILD2_ORIGINAL),
    ):
        patch = b"\xe9" + rel32(site, 5, cave_address) + b"\x90" * (
            len(original) - 5
        )
        current = process.read(site, len(original))
        if current not in (original, patch):
            raise RuntimeError(
                f"unexpected bytes at camera rig site {site:#x}: "
                f"{current.hex(' ')}"
            )
    install_camera_snapshot_helper(process)

    def install_site(
        site,
        return_address,
        cave_address,
        original,
        extended_path,
        *,
        keep_extended_rcx=False,
    ):
        patch = b"\xe9" + rel32(site, 5, cave_address) + b"\x90" * (len(original) - 5)
        current = process.read(site, len(original))
        already_installed = current == patch
        if not already_installed and current != original:
            raise RuntimeError(
                f"unexpected bytes at camera rig site {site:#x}: {current.hex(' ')}"
            )

        cave = bytearray(b"\x9c\x50\x51\x48\x83\xec\x20")
        cave += b"\x48\xb8" + struct.pack("<Q", CAMERA_PRIMARY_RIG)
        cave += b"\x48\x39\xc6"
        fallback_branches = []

        def fallback_if(opcode):
            position = len(cave)
            cave.extend(b"\x0f" + bytes((opcode,)) + b"\x00\x00\x00\x00")
            fallback_branches.append(position)

        fallback_if(0x85)
        if CAMERA_DISTANCE_YIELDS_TO_WALL:
            cave += b"\xf7\x40\x40" + struct.pack("<I", CAMERA_BACK_ON_WALL_MASK)
            fallback_if(0x85)
        cave += b"\x48\x8d\x0c\x24"
        call_site = cave_address + len(cave)
        cave += b"\xe8" + rel32(call_site, 5, CAMERA_SNAPSHOT_HELPER)
        cave += b"\x85\xc0"
        fallback_if(0x84)
        cave += extended_path
        if keep_extended_rcx:
            # Site 2's original instructions deliberately publish RSI+0x20 in
            # RCX. Discard the saved input RCX while restoring RAX and flags.
            cave += b"\x48\x83\xc4\x28\x58\x9d"
        else:
            cave += b"\x48\x83\xc4\x20\x59\x58\x9d"
        extended_jump_site = cave_address + len(cave)
        cave += b"\xe9" + rel32(extended_jump_site, 5, return_address)

        original_path = len(cave)
        cave += b"\x48\x83\xc4\x20\x59\x58\x9d"
        cave += original
        original_jump_site = cave_address + len(cave)
        cave += b"\xe9" + rel32(original_jump_site, 5, return_address)
        for branch in fallback_branches:
            struct.pack_into("<i", cave, branch + 2, original_path - (branch + 6))
        if len(cave) > 0x100:
            raise RuntimeError("camera rig cave overflow")
        process.write(cave_address, cave)
        if not already_installed:
            process.patch_code(site, patch)

    install_site(
        CAMERA_DISTANCE_MUL1_SITE,
        CAMERA_RIG_BUILD1_RETURN,
        CAMERA_DISTANCE_CAVE1,
        CAMERA_RIG_BUILD1_ORIGINAL,
        bytes.fromhex(
            "F3 0F 10 46 38 F3 0F 58 44 24 0C F3 0F 59 D8 "
            "F3 0F 10 56 34 F3 0F 58 54 24 14 "
            "F3 0F 10 4E 30 F3 0F 58 4C 24 10"
        ),
    )
    install_site(
        CAMERA_DISTANCE_MUL2_SITE,
        CAMERA_RIG_BUILD2_RETURN,
        CAMERA_DISTANCE_CAVE2,
        CAMERA_RIG_BUILD2_ORIGINAL,
        bytes.fromhex(
            "F3 0F 10 46 38 F3 0F 58 44 24 0C F3 0F 59 F0 "
            "48 8D 7E 10 48 8D 4E 20 48 89 7C 24 58 "
            "F3 0F 10 56 34 F3 0F 58 54 24 14 "
            "F3 0F 10 4E 30 F3 0F 58 4C 24 10 0F 28 DE"
        ),
        keep_extended_rcx=True,
    )


def reset_camera_eye_smoothing(process):
    # This legacy reservation now owns both camera-frame double buffers.  Clear
    # the complete ABI so neither a restarted worker nor the renderer can adopt
    # a goal/output committed by an earlier lifecycle.
    process.write(
        CAMERA_FRAME_STATE,
        b"\x00" * camera_frame_sync.CAMERA_FRAME_STATE_SIZE,
    )


def restore_camera_eye_smoothing(process):
    original = b"\x0f\x11\x4a\x40\x48\x8b\xcb"
    patch = b"\xe9" + rel32(CAMERA_HOOK_SITE, 5, CAMERA_SMOOTH_CAVE) + b"\x90" * 2
    current = process.read(CAMERA_HOOK_SITE, len(original))
    if current == patch:
        process.patch_code(CAMERA_HOOK_SITE, original)
    elif current != original:
        raise RuntimeError(
            f"unexpected bytes restoring native camera eye: {current.hex(' ')}"
        )
    reset_camera_eye_smoothing(process)


def install_p2_gauge_color(process):
    original = b"\x85\xc9\x0f\x84\x35\x01\x00\x00"
    patch = b"\xe9" + rel32(GAUGE_COLOR_HOOK_SITE, 5, GAUGE_COLOR_CAVE) + b"\x90" * 3

    current = process.read(GAUGE_COLOR_HOOK_SITE, len(original))
    already_installed = current == patch
    if not already_installed and current != original:
        raise RuntimeError(f"unexpected bytes at gauge-color hook: {current.hex(' ')}")

    cave = bytearray()
    native_dispatch_branches = []
    nonzero_branches = []

    def emit_near_jcc(opcode, destinations):
        position = len(cave)
        cave.extend(b"\x0f" + bytes([opcode]) + b"\x00\x00\x00\x00")
        destinations.append(position)

    def resolve_near_jcc(position, destination):
        struct.pack_into("<i", cave, position + 2, destination - (position + 6))

    cave += b"\x48\x85\xff"
    emit_near_jcc(0x84, native_dispatch_branches)
    cave += b"\x49\xbb" + struct.pack("<Q", COMMAND_BLOCK)
    cave += b"\x41\x83\x7b\x08\x00"
    emit_near_jcc(0x84, native_dispatch_branches)
    cave += b"\x49\x8b\x43\x10"
    cave += b"\x48\x85\xc0"
    p2_missing = []
    emit_near_jcc(0x84, p2_missing)
    cave += b"\x48\x3b\x47\x30"
    p2_mismatch = []
    emit_near_jcc(0x85, p2_mismatch)
    custom_jump_site = GAUGE_COLOR_CAVE + len(cave)
    cave += b"\xe9" + rel32(custom_jump_site, 5, GAUGE_CUSTOM_COLOR_CAVE)

    check_p3 = len(cave)
    for branch in p2_missing + p2_mismatch:
        resolve_near_jcc(branch, check_p3)

    cave += b"\x49\x8b\x83" + struct.pack(
        "<i", P3_COMMAND_BLOCK_OFFSET - 0x200 + P3_ACTOR_OFFSET
    )
    cave += b"\x48\x85\xc0"
    p3_missing = []
    emit_near_jcc(0x84, p3_missing)
    cave += b"\x48\x3b\x47\x30"
    p3_mismatch = []
    emit_near_jcc(0x85, p3_mismatch)
    p3_jump_site = GAUGE_COLOR_CAVE + len(cave)
    cave += b"\xe9" + rel32(p3_jump_site, 5, GAUGE_P3_COLOR_CAVE)

    check_p1 = len(cave)
    for branch in p3_missing + p3_mismatch:
        resolve_near_jcc(branch, check_p1)
    cave += b"\x49\x8b\x43\x18"
    cave += b"\x48\x85\xc0"
    emit_near_jcc(0x84, native_dispatch_branches)
    cave += b"\x48\x3b\x47\x30"
    emit_near_jcc(0x85, native_dispatch_branches)
    p1_jump_site = GAUGE_COLOR_CAVE + len(cave)
    cave += b"\xe9" + rel32(p1_jump_site, 5, GAUGE_P1_COLOR_CAVE)

    native_dispatch = len(cave)
    for branch in native_dispatch_branches:
        resolve_near_jcc(branch, native_dispatch)
    cave += b"\x85\xc9"
    emit_near_jcc(0x85, nonzero_branches)

    jump_site = GAUGE_COLOR_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, GAUGE_COLOR_SIDE_PATH)

    native_nonzero = len(cave)
    for branch in nonzero_branches:
        resolve_near_jcc(branch, native_nonzero)
    cave += b"\x83\xe9\x01"
    jump_site = GAUGE_COLOR_CAVE + len(cave)
    cave += b"\xe9" + rel32(jump_site, 5, GAUGE_COLOR_NONZERO_PATH)

    selection = menu_themes.load_selection()
    identity = session_state.SessionIdentity.from_config(session_state.load_config())
    role_themes = {
        1: selection.for_player(1, identity.p1_party_member_id),
        2: selection.for_player(2, identity.p2_party_member_id),
        3: selection.for_player(3, identity.p3_party_member_id),
    }
    for palette_address, player in (
        (GAUGE_P1_PALETTE, 1),
        (GAUGE_CUSTOM_PALETTE, 2),
        (GAUGE_P3_PALETTE, 3),
    ):
        process.write(
            palette_address,
            struct.pack("<8i", *menu_themes.gauge_palette(role_themes[player])),
        )

    for text_address, menu_address, player in (
        (HUD_P1_TEXT_COLOR, P1_MENU_THEME_COLOR, 1),
        (HUD_P2_TEXT_COLOR, P2_MENU_THEME_COLOR, 2),
        (HUD_P3_TEXT_COLOR, P3_MENU_THEME_COLOR, 3),
    ):
        color = struct.pack("<3i", *menu_themes.hud_text_color(role_themes[player]))
        process.write(text_address, color)
        process.write(menu_address, color)

    for damage_address, player in (
        (P1_DAMAGE_NUMBER_COLOR, 1),
        (P2_DAMAGE_NUMBER_COLOR, 2),
        (P3_DAMAGE_NUMBER_COLOR, 3),
    ):
        process.write(
            damage_address,
            struct.pack("<4i", *menu_themes.damage_number_style(role_themes[player])),
        )

    custom = bytearray.fromhex(
        "49 BB 88 77 66 55 44 33 22 11 "
        "49 BA 11 22 33 44 55 66 77 88 "
        "41 8B 4B 08 8D 42 01 66 0F 6E C8 41 8B 43 1C "
        "41 2B 43 0C 0F 5B C9 66 0F 6E C0 F3 41 0F 5E 0A "
        "41 8B 43 18 29 C8 0F 5B C0 F3 0F 59 C1 F3 0F 2C D0 "
        "66 0F 6E C0 41 03 53 0C 0F 5B C0 C1 E2 08 8D 04 11 "
        "41 8B 4B 04 F3 0F 59 C1 F3 44 0F 2C C0 41 01 C0 "
        "41 8B 43 14 29 C8 41 C1 E0 08 66 0F 6E C0 42 8D 04 01 "
        "41 8B 0B 0F 5B C0 F3 0F 59 C1 F3 0F 2C D0 01 C2 "
        "41 8B 43 10 29 C8 C1 E2 08 01 D1 66 0F 6E C0 0F 5B C0 "
        "F3 0F 59 C1 F3 0F 2C C0 01 C8 C3"
    )
    custom[2:10] = struct.pack("<Q", GAUGE_CUSTOM_PALETTE)
    custom[12:20] = struct.pack("<Q", 0x916484)
    process.write(GAUGE_CUSTOM_COLOR_CAVE, custom)
    p1_custom = bytearray(custom)
    p1_custom[2:10] = struct.pack("<Q", GAUGE_P1_PALETTE)
    process.write(GAUGE_P1_COLOR_CAVE, p1_custom)
    p3_custom = bytearray(custom)
    p3_custom[2:10] = struct.pack("<Q", GAUGE_P3_PALETTE)
    process.write(GAUGE_P3_COLOR_CAVE, p3_custom)
    if len(cave) > 0x100:
        raise RuntimeError(f"gauge-colour cave overflow ({len(cave)})")
    process.write(GAUGE_COLOR_CAVE, cave)
    if not already_installed:
        process.patch_code(GAUGE_COLOR_HOOK_SITE, patch)


def restore_p2_hud_text_color(process):
    original = HUD_TEXT_COLOR_ORIGINAL
    patch = (
        b"\xe9"
        + rel32(HUD_TEXT_COLOR_HOOK_SITE, 5, HUD_TEXT_COLOR_CAVE)
        + b"\x90" * (len(original) - 5)
    )
    current = process.read(HUD_TEXT_COLOR_HOOK_SITE, len(original))
    if current == patch:
        process.patch_code(HUD_TEXT_COLOR_HOOK_SITE, original)
    elif current != original:
        raise RuntimeError(
            "unexpected bytes restoring HUD-text builder: " + current.hex(" ")
        )


def build_damage_number_source_wrapper(
    wrapper_address,
    source_state_address,
):
    code = bytearray()
    code += b"\x49\xbb" + struct.pack("<Q", source_state_address)
    code += b"\x49\x89\x2b"
    code += b"\x48\x83\xec\x28"
    call_site = wrapper_address + len(code)
    code += b"\xe8" + rel32(call_site, 5, DAMAGE_NUMBER_FACTORY)
    code += b"\x49\xbb" + struct.pack("<Q", source_state_address)
    code += b"\x49\xc7\x03\x00\x00\x00\x00"
    code += b"\x48\x83\xc4\x28"
    code += b"\xc3"
    return code


def build_damage_number_init_wrapper(
    wrapper_address,
    source_state_address,
    command_block,
    p3_block,
    p1_theme_color,
    p2_theme_color,
    p3_theme_color,
    brightness_state_address,
):
    code = bytearray()
    labels = {}
    fixups = []

    def near_jcc(opcode, label):
        code.extend(b"\x0f" + bytes((opcode,)))
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    def near_jmp(label):
        code.append(0xE9)
        fixups.append((len(code), label))
        code.extend(b"\x00\x00\x00\x00")

    code += b"\x53"
    code += b"\x56"
    code += b"\x48\x83\xec\x28"
    code += b"\x48\x89\xcb"
    code += b"\x48\x89\xd6"
    call_site = wrapper_address + len(code)
    code += b"\xe8" + rel32(call_site, 5, DAMAGE_NUMBER_INIT)

    code += b"\x80\x7b" + bytes((DAMAGE_NUMBER_OBJECT_KIND_OFFSET, 0))
    near_jcc(0x85, "done")
    code += b"\x80\x7e" + bytes(
        (DAMAGE_NUMBER_DESCRIPTOR_PALETTE_OFFSET, DAMAGE_NUMBER_DESCRIPTOR_PALETTE_MAX)
    )
    near_jcc(0x87, "done")
    # The native factory encodes recovery number palettes as 1/3/5/7 and
    # damage palettes as 2/4/6/8 in descriptor+0x0D. Preserve FF12's green
    # recovery colour before applying the attacking player's theme.
    code += b"\xf6\x46" + bytes(
        (DAMAGE_NUMBER_DESCRIPTOR_PALETTE_OFFSET, DAMAGE_NUMBER_RECOVERY_PALETTE_BIT)
    )
    near_jcc(0x85, "done")
    code += b"\x49\xbb" + struct.pack("<Q", source_state_address)
    code += b"\x49\x8b\x03"
    code += b"\x48\x85\xc0"
    near_jcc(0x84, "done")
    code += b"\x49\xba" + struct.pack("<Q", command_block)
    code += b"\x41\x83\x7a\x08\x00"
    near_jcc(0x84, "done")

    code += b"\x49\x3b\x42\x18"
    near_jcc(0x84, "theme_p1")
    code += b"\x49\x3b\x42\x10"
    near_jcc(0x84, "theme_p2")
    code += b"\x49\xb9" + struct.pack("<Q", p3_block)
    code += b"\x49\x3b\x41\x08"
    near_jcc(0x85, "done")

    labels["theme_p3"] = len(code)
    code += b"\x49\xb8" + struct.pack("<Q", p3_theme_color)
    near_jmp("classify_receiver")
    labels["theme_p2"] = len(code)
    code += b"\x49\xb8" + struct.pack("<Q", p2_theme_color)
    near_jmp("classify_receiver")
    labels["theme_p1"] = len(code)
    code += b"\x49\xb8" + struct.pack("<Q", p1_theme_color)

    labels["classify_receiver"] = len(code)
    code += b"\x8b\x4b\x08"
    # Receiver identity is already available as a stable native handle.  Never
    # reload one of the asynchronously published actor pointers merely to read
    # actor+0x08: despawn/KO/Stone can invalidate that allocation between the
    # publication and this render-thread wrapper.  A racing handle can at worst
    # leave the number on the themed path; it cannot become a memory base.
    code += b"\x49\xbb" + struct.pack("<Q", CONTROLLED_HANDLE_GLOBAL)
    code += b"\x41\x3b\x0b"
    near_jcc(0x84, "done")
    code += b"\x41\x3b\x4a" + bytes((P2_LOOT_HANDLE_OFFSET,))
    near_jcc(0x84, "done")
    code += b"\x49\xb9" + struct.pack("<Q", p3_block)
    code += b"\x41\x3b\x49" + bytes((P3_LOOT_HANDLE_OFFSET,))
    near_jcc(0x84, "done")

    labels["apply_enemy"] = len(code)

    # Validate the complete style before modifying the object.  battle_4_c has
    # exactly 108 extracted palette entries (0..107); bad or stale shared-memory
    # data leaves the native RGB/palette pair intact.
    code += b"\x41\x8b\x40\x0c"
    code += b"\x83\xf8" + bytes((DAMAGE_NUMBER_PALETTE_MAX,))
    near_jcc(0x87, "done")

    code += b"\x49\xba" + struct.pack("<Q", brightness_state_address)
    code += b"\x45\x8b\x12"
    code += b"\x41\x81\xfa" + struct.pack("<I", DAMAGE_NUMBER_BRIGHTNESS_UNCHANGED)
    code += b"\x76\x06"
    code += b"\x41\xba" + struct.pack("<I", DAMAGE_NUMBER_BRIGHTNESS_UNCHANGED)
    neutral = struct.pack("<I", DAMAGE_NUMBER_NEUTRAL_CHANNEL)
    ceiling = struct.pack("<I", DAMAGE_NUMBER_CHANNEL_CEILING)
    for theme_offset, object_offset in ((0, 0x0C), (4, 0x0D), (8, 0x0E)):
        if theme_offset:
            code += b"\x41\x8b\x40" + bytes((theme_offset,))
        else:
            code += b"\x41\x8b\x00"

        code += b"\x3d" + ceiling
        code += b"\x76\x05"
        code += b"\xb8" + ceiling
        code += b"\xb9" + neutral
        code += b"\x29\xc1"
        code += b"\x41\x0f\xaf\xca"
        code += b"\xc1\xf9\x08"
        code += b"\xb8" + neutral
        code += b"\x29\xc8"
        code += b"\x88\x43" + bytes((object_offset,))

    code += b"\x41\x8b\x40\x0c"
    code += b"\x88\x43\x3d"

    labels["done"] = len(code)
    code += b"\x48\x83\xc4\x28\x5e\x5b\xc3"

    for offset, label in fixups:
        source_after = wrapper_address + offset + 4
        target = wrapper_address + labels[label]
        struct.pack_into("<i", code, offset, target - source_after)
    return code


def detach_damage_meter_proxy(process, current_source: bytes) -> bytes:
    """Describe a verified meter proxy without ever detaching live code.

    The historical implementation restored the proxy's previous CALL target.
    That is not safe: another game thread may already have entered the proxy or
    its heap wrapper, so changing the root creates a split topology and makes a
    later ``VirtualFreeEx`` a use-after-free candidate.  The v2 damage hook is
    process-lifetime resident.  Treat its signed previous call as a *virtual*
    view for theme preflight while leaving every executable byte untouched.
    """

    proxy_base = DAMAGE_NUMBER_THEME_BASE + DAMAGE_METER_PROXY_WORKSPACE_OFFSET
    proxy_entry = proxy_base + DAMAGE_METER_PROXY_CODE_OFFSET
    proxy_patch = b"\xe8" + rel32(
        DAMAGE_NUMBER_SOURCE_CALL_SITE,
        5,
        proxy_entry,
    )
    if current_source != proxy_patch:
        return current_source

    # Lazy import avoids the coop_runtime <-> damage_meter module cycle.  Keep
    # one canonical signature parser: DamageHook.install and global launcher
    # preflight use this exact inspector as well.
    import damage_meter

    resident = damage_meter.inspect_resident_damage_hook(
        process,
        DAMAGE_NUMBER_THEME_BASE,
        current_call=current_source,
    )
    if resident is None:
        raise RuntimeError("battle-number meter proxy was not recognized")
    return resident.previous_call


def install_damage_number_source_themes(process):
    source_patch = b"\xe8" + rel32(
        DAMAGE_NUMBER_SOURCE_CALL_SITE,
        5,
        DAMAGE_NUMBER_SOURCE_WRAPPER,
    )
    init_slots = (
        DAMAGE_NUMBER_INIT_WRAPPER,
        DAMAGE_NUMBER_INIT_WRAPPER_ALT,
    )
    init_patches = tuple(
        b"\xe8" + rel32(DAMAGE_NUMBER_INIT_CALL_SITE, 5, slot) for slot in init_slots
    )
    actual_source = process.read(
        DAMAGE_NUMBER_SOURCE_CALL_SITE,
        len(DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL),
    )
    current_source = detach_damage_meter_proxy(process, actual_source)
    current_init = process.read(
        DAMAGE_NUMBER_INIT_CALL_SITE,
        len(DAMAGE_NUMBER_INIT_CALL_ORIGINAL),
    )
    if current_source not in (
        DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL,
        source_patch,
    ):
        raise RuntimeError(
            "unexpected bytes at battle-number source call: " + current_source.hex(" ")
        )
    if current_init not in (DAMAGE_NUMBER_INIT_CALL_ORIGINAL,) + init_patches:
        raise RuntimeError(
            "unexpected bytes at battle-number init call: " + current_init.hex(" ")
        )

    live_index = (
        init_patches.index(current_init) if current_init in init_patches else None
    )
    source_wrapper = build_damage_number_source_wrapper(
        DAMAGE_NUMBER_SOURCE_WRAPPER,
        DAMAGE_NUMBER_SOURCE_STATE,
    )
    init_wrappers = tuple(
        build_damage_number_init_wrapper(
            slot,
            DAMAGE_NUMBER_SOURCE_STATE,
            COMMAND_BLOCK,
            third_player_block(),
            P1_DAMAGE_NUMBER_COLOR,
            P2_DAMAGE_NUMBER_COLOR,
            P3_DAMAGE_NUMBER_COLOR,
            DAMAGE_NUMBER_BRIGHTNESS_STATE,
        )
        for slot in init_slots
    )
    if len(source_wrapper) > 0x100 or any(
        len(wrapper) > 0x200 for wrapper in init_wrappers
    ):
        raise RuntimeError("battle-number theme wrapper overflow")

    source_active = current_source == source_patch
    init_active = live_index is not None
    if source_active != init_active:
        raise RuntimeError("battle-number theme topology is only partially installed")

    if source_active:
        if process.read(DAMAGE_NUMBER_SOURCE_WRAPPER, len(source_wrapper)) != source_wrapper:
            raise RuntimeError("battle-number source wrapper mismatch")
        if (
            process.read(init_slots[live_index], len(init_wrappers[live_index]))
            != init_wrappers[live_index]
        ):
            raise RuntimeError("battle-number initializer wrapper mismatch")
        recorder = getattr(process, "record_executable_region", None)
        if recorder is not None:
            recorder(DAMAGE_NUMBER_SOURCE_WRAPPER, len(source_wrapper))
            recorder(init_slots[live_index], len(init_wrappers[live_index]))
        # An active proxy remains the root owner.  Exact adoption is deliberately
        # read-only, including dynamic state, so startup can never split a live
        # hook from the theme wrapper it chains.
        return

    brightness = struct.unpack("<I", process.read(DAMAGE_NUMBER_BRIGHTNESS_STATE, 4))[0]
    if (
        brightness in DAMAGE_NUMBER_BRIGHTNESS_LEGACY_DEFAULTS
        or not 0 < brightness <= DAMAGE_NUMBER_BRIGHTNESS_UNCHANGED
    ):
        process.write(
            DAMAGE_NUMBER_BRIGHTNESS_STATE,
            struct.pack("<I", DAMAGE_NUMBER_BRIGHTNESS_DEFAULT),
        )

    target_index = 0
    process.write(DAMAGE_NUMBER_SOURCE_STATE, struct.pack("<Q", 0))
    process.write(DAMAGE_NUMBER_SOURCE_WRAPPER, source_wrapper)
    process.write(init_slots[target_index], init_wrappers[target_index])

    process.patch_code(DAMAGE_NUMBER_INIT_CALL_SITE, init_patches[target_index])
    process.patch_code(DAMAGE_NUMBER_SOURCE_CALL_SITE, source_patch)


def install_background_unpause(process):
    classify_original = b"\x66\x41\x3b\xfd\x75\x35"
    broad_test_patch = b"\x90" * len(classify_original)
    current_classify = process.read(FOCUS_CLASSIFY_SITE, len(classify_original))
    if current_classify == broad_test_patch:
        raise RuntimeError(
            "legacy broad focus classifier is resident; restart the game"
        )
    elif current_classify != classify_original:
        raise RuntimeError(
            f"unexpected bytes at focus classifier: {current_classify.hex(' ')}"
        )

    original_call = b"\xe8\x75\xa6\x00\x00"
    patched_call = b"\xe8" + rel32(FOCUS_INACTIVE_DISPATCH_CALL, 5, FOCUS_PAUSE_CAVE)
    current_call = process.read(FOCUS_INACTIVE_DISPATCH_CALL, len(original_call))
    already_installed = current_call == patched_call
    if not already_installed and current_call != original_call:
        raise RuntimeError(
            f"unexpected bytes at inactive-focus dispatch: {current_call.hex(' ')}"
        )

    cave = b"\xc7\x42\x04\x01\x00\x00\x00"
    cave += b"\xe9" + rel32(FOCUS_PAUSE_CAVE + len(cave), 5, FOCUS_EVENT_DISPATCH)
    process.write(FOCUS_PAUSE_CAVE, cave)
    if not already_installed:
        process.patch_code(FOCUS_INACTIVE_DISPATCH_CALL, patched_call)


def build_pad_background_input_cave(cave: int) -> bytes:
    """Run the unfocused controller path without falsifying the shared flag."""

    code = bytearray()
    code += b"\x74\x00"  # je focused
    focused_fixup = 1

    # mov byte [rip+disp], 0 -- displacement is relative to the instruction end.
    store = bytearray(b"\xc6\x05\x00\x00\x00\x00\x00")
    store_end = cave + len(code) + len(store)
    delta = INPUT_FOCUS_FLAG_GLOBAL - store_end
    if not -(1 << 31) <= delta < (1 << 31):
        raise RuntimeError("input focus flag is outside +/-2 GB of the cave")
    struct.pack_into("<i", store, 2, delta)
    code += store

    resume_site = cave + len(code)
    code += b"\xe9" + rel32(resume_site, 5, PAD_BACKGROUND_INPUT_RESUME_ENTRY)

    focused = len(code)
    focused_site = cave + focused
    code += b"\xe9" + rel32(focused_site, 5, PAD_BACKGROUND_INPUT_FOCUSED_ENTRY)

    skip = focused - (focused_fixup + 1)
    if not 0 <= skip <= 0x7F:
        raise RuntimeError(f"pad background branch overflow: {skip:#x}")
    code[focused_fixup] = skip

    if len(code) > PAD_BACKGROUND_INPUT_CAVE_SIZE:
        raise RuntimeError(f"pad background cave overflow: {len(code):#x}")
    return bytes(code)


def install_pad_background_input(process) -> bool:
    """Keep controller input active in the background without mouse or keyboard."""

    patch = (
        b"\xe9"
        + rel32(PAD_BACKGROUND_INPUT_SITE, 5, PAD_BACKGROUND_INPUT_CAVE)
        + b"\x90"
    )
    cave = build_pad_background_input_cave(PAD_BACKGROUND_INPUT_CAVE)
    mode = _preflight_static_detour(
        process,
        label="pad background input",
        site=PAD_BACKGROUND_INPUT_SITE,
        original=PAD_BACKGROUND_INPUT_ORIGINAL,
        patch=patch,
        cave_address=PAD_BACKGROUND_INPUT_CAVE,
        cave_size=PAD_BACKGROUND_INPUT_CAVE_SIZE,
        cave=cave,
    )
    if mode == "adopt":
        return False

    # The complete reserved slot and root were proven pristine above.  Publish
    # immutable code first and make it reachable only with the final root write.
    process.write(PAD_BACKGROUND_INPUT_CAVE, cave)
    process.patch_code(PAD_BACKGROUND_INPUT_SITE, patch)
    return True


def restore_pad_background_input(process) -> bool:
    patch = (
        b"\xe9"
        + rel32(PAD_BACKGROUND_INPUT_SITE, 5, PAD_BACKGROUND_INPUT_CAVE)
        + b"\x90"
    )
    current = process.read(
        PAD_BACKGROUND_INPUT_SITE, len(PAD_BACKGROUND_INPUT_ORIGINAL)
    )
    if current == PAD_BACKGROUND_INPUT_ORIGINAL:
        return False
    if current != patch:
        raise RuntimeError(f"unexpected bytes at pad focus gate: {current.hex(' ')}")
    process.patch_code(PAD_BACKGROUND_INPUT_SITE, PAD_BACKGROUND_INPUT_ORIGINAL)
    return True


def _write_manual_command_status_gate(process, payload):
    size = len(MANUAL_COMMAND_STATUS_GATE_ORIGINAL)
    current = process.read(MANUAL_COMMAND_STATUS_GATE_SITE, size)
    if current == payload:
        return
    if current not in (
        MANUAL_COMMAND_STATUS_GATE_ORIGINAL,
        MANUAL_COMMAND_STATUS_GATE_PATCHED,
    ):
        raise RuntimeError(
            f"unexpected bytes at manual command status gate: {current.hex(' ')}"
        )
    process.patch_code(MANUAL_COMMAND_STATUS_GATE_SITE, payload)


def install_berserk_manual_command_gate(process):
    """Allow manual commands under Berserk without changing other status gates."""

    _write_manual_command_status_gate(process, MANUAL_COMMAND_STATUS_GATE_PATCHED)


def restore_berserk_manual_command_gate(process):
    current = process.read(
        MANUAL_COMMAND_STATUS_GATE_SITE,
        len(MANUAL_COMMAND_STATUS_GATE_ORIGINAL),
    )
    if current == MANUAL_COMMAND_STATUS_GATE_ORIGINAL:
        return False
    if current == MANUAL_COMMAND_STATUS_GATE_PATCHED:
        raise RuntimeError(
            "resident manual-command status gate is process-lifetime; "
            "restart the game"
        )
    raise RuntimeError(
        f"unexpected bytes at manual command status gate: {current.hex(' ')}"
    )


def install_menu_side_arrow_suppression(process):
    current = process.read(
        MENU_SIDE_ARROW_ENABLE_SITE, len(MENU_SIDE_ARROW_ENABLE_ORIGINAL)
    )
    if current == MENU_SIDE_ARROW_ENABLE_PATCH:
        return
    if current != MENU_SIDE_ARROW_ENABLE_ORIGINAL:
        raise RuntimeError(
            f"unexpected bytes at menu side-arrow writer: {current.hex(' ')}"
        )
    process.patch_code(MENU_SIDE_ARROW_ENABLE_SITE, MENU_SIDE_ARROW_ENABLE_PATCH)


def restore_native_cursor(process):
    suppressed = (
        b"\xe9"
        + rel32(NATIVE_CURSOR_DRAW_SITE, 5, NATIVE_CURSOR_FILTER_CAVE)
        + b"\x90" * (len(NATIVE_CURSOR_DRAW_ORIGINAL) - 5)
    )
    current = process.read(NATIVE_CURSOR_DRAW_SITE, len(NATIVE_CURSOR_DRAW_ORIGINAL))
    if current == NATIVE_CURSOR_DRAW_ORIGINAL:
        return
    if current != suppressed:
        raise RuntimeError(
            "unexpected bytes at native cursor sprite draw while restoring: "
            f"{current.hex(' ')}"
        )
    process.patch_code(NATIVE_CURSOR_DRAW_SITE, NATIVE_CURSOR_DRAW_ORIGINAL)


def finite_position(values):
    return all(math.isfinite(value) and abs(value) < 1.0e8 for value in values)


def resolve_skeleton_keep(process, actor_id):
    try:
        manager = process.u64(ACTOR_MANAGER_GLOBAL)
        bwei = process.u64(manager + 8 + actor_id * 8) if manager else 0
        swbi = process.u64(bwei + 0xC0) if bwei else 0
        return process.u64(swbi + 0x138) if swbi else 0
    except OSError:
        return 0


def resolve_active_actors(process):
    try:
        manager = process.u64(ACTOR_MANAGER_GLOBAL)
        if not manager:
            return []
        count = process.u32(manager)
    except OSError:
        return []
    if count == 0 or count > 4096:
        return []

    actors = []
    for actor_id in range(count):
        try:
            bwei = process.u64(manager + 8 + actor_id * 8)
            if not bwei:
                continue
            actor = process.u64(bwei + 0x30)
            if not actor:
                continue
            root = process.u64(actor + 0x10)
            phys = process.u64(root + 0xB8) if root else 0
            if not phys:
                continue
            actor_pos = struct.unpack("<3f", process.read(actor + 0xD0, 12))
            phys_pos = struct.unpack("<3f", process.read(phys, 12))
            if not finite_position(actor_pos) or not finite_position(phys_pos):
                continue
            error = math.sqrt(
                sum((actor_pos[index] - phys_pos[index]) ** 2 for index in range(3))
            )
            if error > 1.0:
                continue
            actors.append((actor_id, actor, phys, actor_pos))
        except OSError:
            continue
    return actors


def resolve_active_party_actor_slots(process, actors=None):
    if actors is None:
        actors = resolve_active_actors(process)
    try:
        manager = process.u64(ACTOR_MANAGER_GLOBAL)
    except OSError:
        return {}
    if not manager:
        return {}
    by_party_slot = {}
    for entry in actors:
        actor_id, actor, _phys, _position = entry
        try:
            bwei = process.u64(manager + 8 + actor_id * 8)
            if not bwei or process.u64(bwei + 0x30) != actor:
                continue
            handle = process.u32(actor + 0x08)
            battle_keep = process.u64(actor + 0x698)
            party_slot = process.read(bwei + 0x12, 1)[0]
        except OSError:
            continue
        if (
            not handle
            or (handle & 0xFFFF) != actor_id
            or not battle_keep
            or party_slot > 3
        ):
            continue
        by_party_slot.setdefault(party_slot, []).append(entry)

    return {
        party_slot: entries[0]
        for party_slot, entries in sorted(by_party_slot.items())
        if len(entries) == 1
    }


def party_actor_validity(process, actor):
    if not actor:
        return False
    try:
        manager = process.u64(ACTOR_MANAGER_GLOBAL)
        if not manager:
            return None
        count = process.u32(manager)
    except OSError:
        return None
    if count == 0 or count > 4096:
        return None

    matched = False
    scan_incomplete = False
    for actor_id in range(count):
        try:
            bwei = process.u64(manager + 8 + actor_id * 8)
            if not bwei or process.u64(bwei + 0x30) != actor:
                continue
            matched = True
            handle = process.u32(actor + 0x08)
            if not handle or (handle & 0xFFFF) != actor_id:
                continue
            party_slot = process.read(bwei + 0x12, 1)[0]
            if party_slot > 3:
                return False
            root = process.u64(actor + 0x10)
            phys = process.u64(root + 0xB8) if root else 0
            battle_keep = process.u64(actor + 0x698)
            if not root or not phys or not battle_keep:
                return None
            return True
        except OSError:
            scan_incomplete = True
            if matched:
                return None
            continue
    return None if scan_incomplete else False


def battle_status_effects(process, battle_keep):
    """Return one coherent temporary|permanent native status sample.

    Each aligned u32 is atomic, but the two fields are published independently.
    Bracket the permanent read with the temporary word: when both temporary
    samples agree, the combined value existed at the instant permanent was
    read.  A racing writer or failure stays unknown instead of inventing an
    alive/dead transition.
    """

    if not battle_keep:
        return None
    for _attempt in range(STATUS_SNAPSHOT_ATTEMPTS):
        try:
            temporary_before = process.u32(
                battle_keep + TEMPORARY_STATUS_EFFECTS_OFFSET
            )
            permanent = process.u32(battle_keep + PERMANENT_STATUS_EFFECTS_OFFSET)
            temporary_after = process.u32(
                battle_keep + TEMPORARY_STATUS_EFFECTS_OFFSET
            )
            if temporary_before == temporary_after:
                return temporary_before | permanent
        except ACTOR_SAMPLE_ERRORS:
            # The attempt budget also covers a one-read transport fault.  A
            # persistent failure remains UNKNOWN after the same fixed bound.
            continue
    return None


def actor_is_alive(process, actor):
    if not actor:
        return None
    try:
        battle_keep = process.u64(actor + 0x698)
        if not battle_keep:
            return None
        hp = process.u32(battle_keep + 0x48)
    except ACTOR_SAMPLE_ERRORS:
        return None
    status = battle_status_effects(process, battle_keep)
    if status is None:
        return None
    return bool(hp and not (status & 1))


def actor_u32_or_zero(process, actor, offset):
    if not actor:
        return 0
    try:
        return process.u32(actor + int(offset))
    except ACTOR_SAMPLE_ERRORS:
        return 0


def actor_u64_or_zero(process, actor, offset):
    if not actor:
        return 0
    try:
        return process.u64(actor + int(offset))
    except ACTOR_SAMPLE_ERRORS:
        return 0


def actor_accepts_manual_movement(process, actor):
    if not actor:
        return None
    try:
        battle_keep = process.u64(actor + 0x698)
        if not battle_keep:
            return None
    except ACTOR_SAMPLE_ERRORS:
        return None
    status = battle_status_effects(process, battle_keep)
    if status is None:
        return None
    return not bool(status & MOVEMENT_BLOCKING_STATUS_MASK)


def actor_has_berserk(process, actor):
    if not actor:
        return None
    try:
        battle_keep = process.u64(actor + 0x698)
        if not battle_keep:
            return None
    except ACTOR_SAMPLE_ERRORS:
        return None
    status = battle_status_effects(process, battle_keep)
    if status is None:
        return None
    return bool(status & BERSERK_STATUS_MASK)


def actor_follow_request_blocked(process, actor):
    """Classify the native action gate without inventing a clear status."""

    if not actor:
        return None
    try:
        battle_keep = process.u64(actor + 0x698)
        if not battle_keep:
            return None
    except ACTOR_SAMPLE_ERRORS:
        return None
    status = battle_status_effects(process, battle_keep)
    if status is None:
        return None
    return bool(status & FOLLOW_REQUEST_BLOCKING_STATUS_MASK)


def _actor_available_for_leader(process, actor, blocking_status_mask):
    """Return one coherent tri-state native-leader availability sample.

    HP, status and the actor's battle object are published independently.  A
    pair of otherwise valid helper calls could therefore combine an old alive
    sample with a new clear-status sample.  Bracket the status transaction with
    both HP and battle-object identity and retry a fixed number of times; an
    unstable actor is UNKNOWN and is never promoted speculatively.
    """

    if not actor:
        return None
    for _attempt in range(STATUS_SNAPSHOT_ATTEMPTS):
        try:
            battle_keep_before = process.u64(actor + 0x698)
            if not battle_keep_before:
                return None
            hp_before = process.u32(battle_keep_before + 0x48)
            status = battle_status_effects(process, battle_keep_before)
            hp_after = process.u32(battle_keep_before + 0x48)
            battle_keep_after = process.u64(actor + 0x698)
        except ACTOR_SAMPLE_ERRORS:
            continue
        if (
            status is not None
            and battle_keep_before == battle_keep_after
            and hp_before == hp_after
        ):
            return bool(
                hp_before and not (status & int(blocking_status_mask))
            )
    return None


def actor_available_for_native_leader(process, actor):
    """Classify configured P1 against the effective patched manual-action gate."""

    return _actor_available_for_leader(
        process,
        actor,
        CONFIGURED_P1_LEADER_BLOCKING_STATUS_MASK,
    )


def actor_available_for_ko_leader(process, actor):
    """Classify a follower candidate for the native promotion request."""

    return _actor_available_for_leader(
        process,
        actor,
        FOLLOW_REQUEST_BLOCKING_STATUS_MASK,
    )


def actor_fighting_stance(process, actor):
    if not actor:
        return False
    try:
        return bool(
            process.read(actor + FIGHTING_STANCE_OFFSET, 1)[0] & FIGHTING_STANCE_MASK
        )
    except ACTOR_SAMPLE_ERRORS:
        return False


def actor_execution_animation_active(process, actor, actor_alive):
    if not actor or actor_alive is not True:
        return False
    try:
        return bool(process.u64(actor) & EXECUTION_ANIMATION_FLAG)
    except ACTOR_SAMPLE_ERRORS:
        return False


def actor_execution_target_heading(
    process,
    actor,
    actor_position,
    actor_by_handle,
):
    if not actor or actor_position is None:
        return None
    try:
        target_handle = process.u32(actor + 0x710)
        target_actor = actor_by_handle.get(target_handle)
        if not target_actor:
            return None
        target_position = struct.unpack("<3f", process.read(target_actor + 0xD0, 12))
        target_x = target_position[0] - actor_position[0]
        target_z = target_position[2] - actor_position[2]
        if target_x * target_x + target_z * target_z <= 1.0e-6:
            return None
        heading = math.atan2(target_x, target_z)
    except ACTOR_SAMPLE_ERRORS:
        return None
    if not math.isfinite(heading):
        return None
    return heading


def custom_wait_shadow_preserves_movement(process):
    try:
        return bool(
            process.read(COMMAND_BLOCK + WAIT_MENU_APPLIED_OFFSET, 1)[0] == 1
            and process.u32(NATIVE_COMMAND_FLAG_GLOBAL) == 1
            and process.u32(NATIVE_COMMAND_ACTOR_GLOBAL) != 0
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        return False


def native_battle_mode_is_active(process):
    try:
        settings = process.u64(SETTINGS_POINTER_GLOBAL)
        return bool(settings and process.u32(settings) & BATTLE_MODE_ACTIVE_MASK)
    except (OSError, RuntimeError, struct.error, ValueError):
        return False


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def startup_safe_foreground_edge(
    current: bool,
    previous: bool | None,
    foreground: bool,
) -> bool:
    """Return a press edge without inventing one at startup/focus handoff."""

    return bool(foreground and previous is not None and current and not previous)


def process_is_foreground(pid):
    window = user32.GetForegroundWindow()
    if not window:
        return False
    foreground_pid = wt.DWORD()
    user32.GetWindowThreadProcessId(window, ct.byref(foreground_pid))
    return foreground_pid.value == pid


def horizontal_camera_basis(process, fallback):
    if not CAMERA_VIEW_MATRIX:
        return (0.0, 1.0), (-1.0, 0.0)

    matrix = struct.unpack("<16f", process.read(CAMERA_VIEW_MATRIX, 64))
    rows = (matrix[0:3], matrix[4:7], matrix[8:11])
    if not all(math.isfinite(value) for value in matrix):
        return fallback
    norms = tuple(sum(value * value for value in row) for row in rows)
    dots = (
        sum(rows[0][i] * rows[1][i] for i in range(3)),
        sum(rows[0][i] * rows[2][i] for i in range(3)),
        sum(rows[1][i] * rows[2][i] for i in range(3)),
    )
    if any(abs(norm - 1.0) > 0.02 for norm in norms) or any(
        abs(dot) > 0.02 for dot in dots
    ):
        return fallback

    matrix_yaw = math.atan2(matrix[8], matrix[10])
    forward_heading = math.pi - matrix_yaw
    forward = (math.sin(forward_heading), math.cos(forward_heading))

    right = (-forward[1], forward[0])

    fallback_forward = fallback[0]
    fallback_heading = math.atan2(fallback_forward[0], fallback_forward[1])
    yaw_step = math.atan2(
        math.sin(forward_heading - fallback_heading),
        math.cos(forward_heading - fallback_heading),
    )
    if abs(yaw_step) <= CAMERA_YAW_MAX_STEP:
        horizontal_camera_basis.pending_heading = None
        horizontal_camera_basis.pending_count = 0
        return forward, right

    pending_heading = getattr(horizontal_camera_basis, "pending_heading", None)
    pending_count = getattr(horizontal_camera_basis, "pending_count", 0)
    if pending_heading is not None:
        pending_step = math.atan2(
            math.sin(forward_heading - pending_heading),
            math.cos(forward_heading - pending_heading),
        )
    else:
        pending_step = math.inf
    if abs(pending_step) <= CAMERA_YAW_MAX_STEP:
        pending_count += 1
    else:
        pending_heading = forward_heading
        pending_count = 1
    horizontal_camera_basis.pending_heading = pending_heading
    horizontal_camera_basis.pending_count = pending_count
    if pending_count >= CAMERA_YAW_CONFIRM_SAMPLES:
        horizontal_camera_basis.pending_heading = None
        horizontal_camera_basis.pending_count = 0
        return forward, right
    return fallback


def action_facing_release_heading(process, actor, visual_heading):
    if not FOLLOWER_ACTION_FACING_RELEASE or not actor:
        return visual_heading
    try:
        if process.read(actor + 0x6B4, 1)[0] != 4:
            return visual_heading
        if not process.u32(actor + 0x710):
            return visual_heading
        return struct.unpack("<f", process.read(actor + 0x15C, 4))[0]
    except (OSError, RuntimeError, struct.error):
        return visual_heading


def guard_p2_publication_generation(process, desired_actor, owned):
    publisher = movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX)
    published_actor = (
        publisher.last_command.actor
        if publisher.last_command is not None and publisher.last_command.active
        else 0
    )
    expected_actor = int(desired_actor) if owned else 0
    if published_actor != expected_actor:
        write_command(process, 0, 0.0, 0.0, 0.0, False)


def guard_p3_publication_generation(process, desired_actor, owned):
    publisher = movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX)
    published_actor = (
        publisher.last_command.actor
        if publisher.last_command is not None and publisher.last_command.active
        else 0
    )
    expected_actor = int(desired_actor) if owned else 0
    if published_actor != expected_actor:
        write_third_player_command(process, 0, 0.0, 0.0, False)


def synchronize_second_player_legacy_movement_mirror(process, command):
    """Mirror one committed P2 mailbox generation for legacy HUD consumers."""

    active = bool(command.active and command.actor and command.phys)
    actor = int(command.actor) if active else 0
    phys = int(command.phys) if active else 0
    current_actor = struct.unpack(
        "<Q",
        process.read(COMMAND_BLOCK + 0x10, 8),
    )[0]
    if current_actor != actor:
        process.write(COMMAND_BLOCK + 0x10, struct.pack("<Q", 0))
        process.write(COMMAND_BLOCK + 0x78, struct.pack("<Q", 0))
    process.write(COMMAND_BLOCK + 0x00, struct.pack("<f", command.heading))
    process.write(
        COMMAND_BLOCK + 0x0C,
        struct.pack("<f", command.visual_heading),
    )
    process.write(COMMAND_BLOCK + 0x04, struct.pack("<f", command.magnitude))
    process.write(COMMAND_BLOCK + 0x78, struct.pack("<Q", phys))
    process.write(COMMAND_BLOCK + 0x10, struct.pack("<Q", actor))


def synchronize_third_player_legacy_movement_mirror(process, command):
    """Mirror one committed P3 mailbox generation for legacy HUD consumers."""

    block = third_player_block()
    active = bool(command.active and command.actor and command.phys)
    actor = int(command.actor) if active else 0
    phys = int(command.phys) if active else 0
    current_actor = struct.unpack(
        "<Q",
        process.read(block + P3_ACTOR_OFFSET, 8),
    )[0]
    if not active or current_actor != actor:
        process.write(block + P3_MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 0))
        process.write(block + P3_ACTOR_OFFSET, struct.pack("<Q", 0))
        process.write(block + P3_PHYS_OFFSET, struct.pack("<Q", 0))
    process.write(block + P3_HEADING_OFFSET, struct.pack("<f", command.heading))
    process.write(block + P3_MAGNITUDE_OFFSET, struct.pack("<f", command.magnitude))
    process.write(
        block + P3_VISUAL_HEADING_OFFSET,
        struct.pack("<f", command.visual_heading),
    )
    process.write(block + P3_PHYS_OFFSET, struct.pack("<Q", phys))
    process.write(block + P3_ACTOR_OFFSET, struct.pack("<Q", actor))
    if active:
        process.write(block + P3_MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 1))


def write_command(
    process,
    actor,
    heading,
    visual_heading,
    magnitude,
    active,
    *,
    phys=0,
    member_id=movement_mailbox.INACTIVE_MEMBER_ID,
    actor_generation=0,
    world_epoch=0,
):
    active = bool(active and actor and phys)
    actor = int(actor) if active else 0
    phys = int(phys) if active else 0
    command = movement_mailbox.MovementCommand(
        active,
        actor,
        phys,
        int(member_id) if active else movement_mailbox.INACTIVE_MEMBER_ID,
        int(actor_generation) if active else 0,
        int(world_epoch),
        float(heading),
        float(visual_heading),
        float(magnitude) if active else 0.0,
    )
    publisher = movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX)
    previous = publisher.last_command
    if not publisher.publish(command):
        return False

    previous_actor = previous.actor if previous is not None and previous.active else 0
    if previous_actor != actor:
        process.write(COMMAND_BLOCK + 0x10, struct.pack("<Q", 0))
        process.write(COMMAND_BLOCK + 0x78, struct.pack("<Q", 0))
    process.write(COMMAND_BLOCK + 0x00, struct.pack("<f", heading))
    process.write(COMMAND_BLOCK + 0x0C, struct.pack("<f", visual_heading))
    process.write(COMMAND_BLOCK + 0x04, struct.pack("<f", command.magnitude))
    process.write(
        COMMAND_BLOCK + 0x78,
        struct.pack("<Q", phys),
    )
    process.write(COMMAND_BLOCK + 0x10, struct.pack("<Q", actor))
    return True


def publish_follower_movement_master(process, active):
    process.write(
        COMMAND_BLOCK + MOVEMENT_ACTIVE_OFFSET,
        struct.pack("<I", int(bool(active))),
    )


def third_player_block() -> int:
    return COMMAND_BLOCK - 0x200 + P3_COMMAND_BLOCK_OFFSET


def movement_helper_liveness_address(base: int | None = None) -> int:
    if base is None:
        return third_player_block() + MOVEMENT_HELPER_LIVENESS_OFFSET
    return int(base) + P3_COMMAND_BLOCK_OFFSET + MOVEMENT_HELPER_LIVENESS_OFFSET


def clear_movement_helper_liveness(process, *, base: int | None = None) -> None:
    process.write(
        movement_helper_liveness_address(base),
        b"\x00" * MOVEMENT_HELPER_LIVENESS_SIZE,
    )


def publish_movement_helper_liveness(
    process,
    sequence: int,
    *,
    owner_pid: int,
    heartbeat_ms: int | None = None,
    flags: int = 0,
) -> int:
    """Publish one coherent worker heartbeat and return its even sequence."""

    address = movement_helper_liveness_address()
    odd = (int(sequence) + 1) & 0xFFFFFFFF
    if not odd & 1:
        odd = (odd + 1) & 0xFFFFFFFF
    if odd == 0:
        odd = 1
    even = (odd + 1) & 0xFFFFFFFF
    if even == 0:
        even = 2
    if heartbeat_ms is None:
        heartbeat_ms = max(1, int(time.monotonic() * 1000.0))

    process.write(address, struct.pack("<I", odd))
    process.write(
        address + 4,
        struct.pack(
            "<IIIQII",
            MOVEMENT_HELPER_LIVENESS_MAGIC,
            MOVEMENT_HELPER_LIVENESS_VERSION,
            int(owner_pid) & 0xFFFFFFFF,
            int(heartbeat_ms) & 0xFFFFFFFFFFFFFFFF,
            int(flags) & 0xFFFFFFFF,
            0,
        ),
    )
    process.write(address, struct.pack("<I", even))
    return even


def read_movement_helper_liveness(
    process,
    *,
    base: int | None = None,
    attempts: int = 3,
) -> tuple[bool, int, int, int, int, int]:
    """Return coherent, sequence, magic, version, owner PID and heartbeat."""

    address = movement_helper_liveness_address(base)
    last = (False, 0, 0, 0, 0, 0)
    for _attempt in range(max(1, int(attempts))):
        first = struct.unpack("<I", process.read(address, 4))[0]
        if first == 0 or first & 1:
            last = (False, first, 0, 0, 0, 0)
            continue
        raw = process.read(address, MOVEMENT_HELPER_LIVENESS_SIZE)
        final = struct.unpack("<I", process.read(address, 4))[0]
        (
            sequence,
            magic,
            version,
            owner_pid,
            heartbeat_ms,
            _flags,
            _reserved,
        ) = MOVEMENT_HELPER_LIVENESS_STRUCT.unpack(raw)
        coherent = bool(
            sequence == first
            and final == first
            and not final & 1
            and magic == MOVEMENT_HELPER_LIVENESS_MAGIC
            and version == MOVEMENT_HELPER_LIVENESS_VERSION
            and owner_pid
            and heartbeat_ms
        )
        last = (
            coherent,
            sequence,
            magic,
            version,
            owner_pid,
            heartbeat_ms,
        )
        if coherent:
            break
    return last


def publish_manual_control_mask(process, mask: int) -> int:
    mask = int(mask) & DEFAULT_MANUAL_CONTROL_MASK
    process.write(
        third_player_block() + MANUAL_CONTROL_STATE_OFFSET,
        struct.pack("<2I", MANUAL_CONTROL_STATE_MAGIC, mask),
    )
    return mask


def configured_follower_control_mask(bindings) -> int:
    """Enable manual ownership only for roles that have an input source.

    A configured gamepad remains capable while temporarily disconnected so it
    can recover on hotplug without changing player ownership.  A role with no
    gamepad and no desktop ownership must stay AI-controlled instead of being
    published as manual and becoming inert.
    """

    mask = 0
    for player, bit in (
        ("p2", P2_MANUAL_CONTROL_BIT),
        ("p3", P3_MANUAL_CONTROL_BIT),
    ):
        if bindings.owns_desktop(player) or bindings.assigned_gamepad(player) is not None:
            mask |= bit
    return mask


def reconcile_follower_control_sources(
    manual_mask: int,
    previous_source_mask: int,
    current_source_mask: int,
) -> int:
    """Follow configuration changes without reviving explicitly disabled roles."""

    manual_mask = int(manual_mask) & DEFAULT_MANUAL_CONTROL_MASK
    previous_source_mask = int(previous_source_mask) & DEFAULT_MANUAL_CONTROL_MASK
    current_source_mask = int(current_source_mask) & DEFAULT_MANUAL_CONTROL_MASK
    newly_capable = current_source_mask & ~previous_source_mask
    return (manual_mask & current_source_mask) | newly_capable


def read_manual_control_mask(process) -> int:
    try:
        magic, mask = struct.unpack(
            "<2I",
            process.read(
                third_player_block() + MANUAL_CONTROL_STATE_OFFSET,
                8,
            ),
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        return DEFAULT_MANUAL_CONTROL_MASK
    if magic != MANUAL_CONTROL_STATE_MAGIC:
        return DEFAULT_MANUAL_CONTROL_MASK
    return int(mask) & DEFAULT_MANUAL_CONTROL_MASK


def interaction_scan_entry_address() -> int:
    return third_player_block() + P3_INTERACTION_SCAN_ENTRY_OFFSET


def player_steering(
    bindings,
    player,
    camera_basis,
    walk_mode,
    allowed,
    *,
    native_leader_pad=False,
):
    if not allowed:
        return None, 0.0, False
    if native_leader_pad:
        forward_input, right_input = bindings.native_left_stick_axes(player)
    else:
        forward_input = bindings.signed_axis(player, "move_up", "move_down")
        right_input = bindings.signed_axis(player, "move_right", "move_left")
    if not forward_input and not right_input:
        return None, 0.0, False
    strength = min(1.0, math.hypot(forward_input, right_input))
    forward, right = camera_basis
    world_x = forward_input * forward[0] + right_input * right[0]
    world_z = forward_input * forward[1] + right_input * right[1]
    heading = math.atan2(world_x, world_z)
    share = native_stick_fraction(strength)
    magnitude = (WALK_STEP if walk_mode else RUN_STEP) * share
    return heading, magnitude, True


def third_player_steering(bindings, camera_basis, walk_mode, allowed):
    return player_steering(
        bindings,
        "p3",
        camera_basis,
        walk_mode,
        allowed,
    )


def write_p1_berserk_command(
    process,
    actor,
    phys,
    heading,
    magnitude,
    active,
    visual_heading=None,
    interaction_entry=0,
    skeleton_keep=0,
):
    state = P1_BERSERK_MOVEMENT_STATE
    actor = int(actor)
    phys = int(phys)
    active = bool(active and actor and phys)
    try:
        previous_actor = process.u64(state + P1_BERSERK_ACTOR_OFFSET)
        previous_phys = process.u64(state + P1_BERSERK_PHYS_OFFSET)
    except (AttributeError, OSError, RuntimeError, struct.error, ValueError):
        previous_actor = previous_phys = None
    if not active or previous_actor != actor or previous_phys != phys:
        process.write(
            state + P1_BERSERK_ACTIVE_OFFSET,
            struct.pack("<I", 0),
        )
        process.write(state + P1_BERSERK_ACTOR_OFFSET, struct.pack("<Q", 0))
        process.write(state + P1_BERSERK_PHYS_OFFSET, struct.pack("<Q", 0))
        process.write(
            state + P1_BERSERK_INTERACTION_ENTRY_OFFSET,
            struct.pack("<Q", 0),
        )
        process.write(
            state + P1_BERSERK_SKELETON_KEEP_OFFSET,
            struct.pack("<Q", 0),
        )
    process.write(
        state + P1_BERSERK_HEADING_OFFSET,
        struct.pack("<f", float(heading)),
    )
    process.write(
        state + P1_BERSERK_MAGNITUDE_OFFSET,
        struct.pack("<f", float(magnitude) if active else 0.0),
    )
    process.write(
        state + P1_BERSERK_VISUAL_HEADING_OFFSET,
        struct.pack(
            "<f",
            float(heading if visual_heading is None else visual_heading),
        ),
    )
    process.write(
        state + P1_BERSERK_PHYS_OFFSET,
        struct.pack("<Q", phys if active else 0),
    )
    process.write(
        state + P1_BERSERK_ACTOR_OFFSET,
        struct.pack("<Q", actor if active else 0),
    )
    process.write(
        state + P1_BERSERK_INTERACTION_ENTRY_OFFSET,
        struct.pack("<Q", int(interaction_entry) if active else 0),
    )
    process.write(
        state + P1_BERSERK_SKELETON_KEEP_OFFSET,
        struct.pack("<Q", int(skeleton_keep) if active else 0),
    )
    if active:
        process.write(
            state + P1_BERSERK_ACTIVE_OFFSET,
            struct.pack("<I", 1),
        )


def clear_p1_berserk_command(process):
    write_p1_berserk_command(process, 0, 0, 0.0, 0.0, False)


def write_third_player_command(
    process,
    actor,
    heading,
    magnitude,
    active,
    visual_heading=None,
    *,
    phys=0,
    member_id=movement_mailbox.INACTIVE_MEMBER_ID,
    actor_generation=0,
    world_epoch=0,
):
    block = third_player_block()
    active = bool(active and actor and phys)
    actor = int(actor) if active else 0
    phys = int(phys) if active else 0
    if visual_heading is None:
        visual_heading = heading
    command = movement_mailbox.MovementCommand(
        active,
        actor,
        phys,
        int(member_id) if active else movement_mailbox.INACTIVE_MEMBER_ID,
        int(actor_generation) if active else 0,
        int(world_epoch),
        float(heading),
        float(visual_heading),
        float(magnitude) if active else 0.0,
    )
    publisher = movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX)
    previous = publisher.last_command
    if not publisher.publish(command):
        return False
    previous_actor = previous.actor if previous is not None and previous.active else 0
    if not active or previous_actor != actor:
        process.write(block + P3_MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 0))
        process.write(block + P3_ACTOR_OFFSET, struct.pack("<Q", 0))
        process.write(block + P3_PHYS_OFFSET, struct.pack("<Q", 0))
    process.write(block + P3_HEADING_OFFSET, struct.pack("<f", heading))
    process.write(block + P3_MAGNITUDE_OFFSET, struct.pack("<f", command.magnitude))
    process.write(
        block + P3_VISUAL_HEADING_OFFSET,
        struct.pack("<f", visual_heading),
    )
    process.write(
        block + P3_PHYS_OFFSET,
        struct.pack("<Q", phys),
    )
    process.write(block + P3_ACTOR_OFFSET, struct.pack("<Q", actor))
    if active:
        process.write(block + P3_MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 1))
    return True


def clear_third_player_command(process, *, world_epoch=0):
    disarm_third_player_command(process, world_epoch=world_epoch)
    clear_third_player_metadata(process)


def disarm_third_player_command(process, *, world_epoch=0):
    published = write_third_player_command(
        process,
        0,
        0.0,
        0.0,
        False,
        world_epoch=world_epoch,
    )
    clear_follower_action_mailbox(process, "p3")
    return published


def clear_third_player_metadata(process):
    block = third_player_block()
    process.write(block + P3_LOOT_HANDLE_OFFSET, struct.pack("<I", 0))
    process.write(block + P3_SKELETON_KEEP_OFFSET, struct.pack("<Q", 0))
    process.write(block + P3_INTERACTION_ENTRY_OFFSET, struct.pack("<Q", 0))
    process.write(
        block + P3_HUD_PARTY_SLOT_OFFSET,
        struct.pack("<I", INVALID_HUD_PARTY_SLOT),
    )


def clear_second_player_command(process, *, world_epoch=0):
    disarm_second_player_command(process, world_epoch=world_epoch)
    clear_second_player_metadata(process)


def disarm_second_player_command(process, *, world_epoch=0):
    published = write_command(
        process,
        0,
        0.0,
        0.0,
        0.0,
        False,
        world_epoch=world_epoch,
    )
    clear_follower_action_mailbox(process, "p2")
    return published


def clear_second_player_metadata(process):
    process.write(COMMAND_BLOCK + P2_LOOT_HANDLE_OFFSET, struct.pack("<I", 0))
    process.write(COMMAND_BLOCK + P2_SKELETON_KEEP_OFFSET, struct.pack("<Q", 0))
    process.write(COMMAND_BLOCK + P2_INTERACTION_ENTRY_OFFSET, struct.pack("<Q", 0))
    process.write(
        COMMAND_BLOCK + P2_HUD_PARTY_SLOT_OFFSET,
        struct.pack("<I", INVALID_HUD_PARTY_SLOT),
    )


def clear_follower_role_metadata(process, role):
    if role == "p2":
        clear_second_player_metadata(process)
        return
    if role == "p3":
        clear_third_player_metadata(process)
        return
    raise ValueError(f"unknown follower role: {role!r}")


def observe_follower_handoff(
    process,
    token,
    snapshot,
    party_actor_slots,
    sample,
):
    member_id = token.member_id
    try:
        layout_coherent = bool(
            snapshot is not None
            and role_member_layout_coherent(
                member_id,
                snapshot.current_layout,
                snapshot.ui_layout,
            )
        )
        entries_by_member = (
            session_state.map_party_slot_actors(snapshot, party_actor_slots)
            if snapshot is not None and party_actor_slots
            else {}
        )
        entry = entries_by_member.get(member_id)
        entry_matches = bool(
            entry is not None
            and int(entry[1]) == token.actor
            and int(entry[2]) == token.phys
        )
        leader_member_id = (
            snapshot.leader_party_member_id if snapshot is not None else None
        )
        session_blocked = bool(
            snapshot is None or snapshot.cutscene or snapshot.loading
        )
    except ACTOR_SAMPLE_ERRORS:
        return follow_handoff.FollowerObservation(
            token=token,
            sample=int(sample),
            available=False,
            alive=None,
            materialized=False,
            layout_coherent=False,
            leader_member_id=None,
            cutscene=True,
            status_blocked=None,
        )
    # A coherent member with no scan entry is transiently UNKNOWN.  A concrete
    # different entry proves reincarnation and must cancel the stale token.
    materialized = bool(entry_matches or (entry is None and layout_coherent))
    base = dict(
        token=token,
        sample=int(sample),
        alive=(actor_is_alive(process, token.actor) if materialized else None),
        materialized=materialized,
        layout_coherent=layout_coherent,
        leader_member_id=leader_member_id,
        cutscene=session_blocked,
        status_blocked=(
            actor_follow_request_blocked(process, token.actor)
            if materialized
            else None
        ),
    )
    if not entry_matches or not layout_coherent:
        return follow_handoff.FollowerObservation(available=False, **base)
    if not token.native_handle:
        # Pointer equality without the native generation handle cannot prove an
        # incarnation. Keep a read-only UNKNOWN observation; neither a follow
        # request nor a false confirmation may consume this repair.
        return follow_handoff.FollowerObservation(available=False, **base)
    try:
        current_handle = process.u32(token.actor + 0x08)
        if not current_handle:
            return follow_handoff.FollowerObservation(available=False, **base)
        if current_handle != token.native_handle:
            return follow_handoff.FollowerObservation(
                available=False,
                materialized=False,
                **{
                    key: value
                    for key, value in base.items()
                    if key != "materialized"
                },
            )
        if actor_physics_pointer(process, token.actor) != token.phys:
            return follow_handoff.FollowerObservation(
                available=False,
                materialized=False,
                **{key: value for key, value in base.items() if key != "materialized"},
            )
        state = process.read(token.actor + 0x6B4, 1)[0]
        descriptor = process.u64(token.actor + 0x708)
        target = process.u32(token.actor + 0x710)
        action = struct.unpack("<H", process.read(token.actor + 0x714, 2))[0]
        descriptor_action = (
            struct.unpack("<H", process.read(descriptor, 2))[0] if descriptor else None
        )
        lists = (
            process.u64(token.actor + 0x6E8),
            process.u64(token.actor + 0x6F0),
        )
    except ACTOR_SAMPLE_ERRORS:
        return follow_handoff.FollowerObservation(available=False, **base)
    return follow_handoff.FollowerObservation(
        available=True,
        action_state=state,
        target=target,
        action=action,
        descriptor_action=descriptor_action,
        follows_native_list=any(
            item in (FOLLOWER_LIST_PRIMARY, FOLLOWER_LIST_SECONDARY) for item in lists
        ),
        **base,
    )


def apply_follower_handoff_effects(
    process,
    effects,
    observations,
    *,
    current_world_epoch=None,
):
    """Execute ordered effects after validating their complete owner token."""

    def acquisition_identity_current(token):
        if not token.native_handle:
            return False
        try:
            if process.u32(token.actor + 0x08) != token.native_handle:
                return False
        except ACTOR_SAMPLE_ERRORS:
            return False
        return actor_physics_pointer(process, token.actor) == token.phys

    acquired_headings = {}
    rejected_acquisitions = []
    acquisition_tokens = {
        effect.token.role: effect.token
        for effect in effects
        if effect.kind == follow_handoff.EffectKind.ACQUIRE_NEW
    }
    for role, token in acquisition_tokens.items():
        execution_world_epoch = (
            token.world_epoch
            if current_world_epoch is None
            else int(current_world_epoch)
        )
        if token.world_epoch != execution_world_epoch or not acquisition_identity_current(
            token
        ):
            rejected_acquisitions.append(token)
            continue
        try:
            pair = leader_heading_pair(process, token.phys)
        except ACTOR_SAMPLE_ERRORS:
            pair = None
        if pair is None or not acquisition_identity_current(token):
            rejected_acquisitions.append(token)
            continue
        acquired_headings[role] = pair

    seamless_roles = set(acquired_headings)
    for effect in effects:
        token = effect.token
        execution_world_epoch = (
            token.world_epoch
            if current_world_epoch is None
            else int(current_world_epoch)
        )
        stale_world = token.world_epoch != execution_world_epoch
        if effect.kind == follow_handoff.EffectKind.DISARM_OLD:
            if token.role in seamless_roles:
                continue
            if token.role == "p2":
                disarm_second_player_command(
                    process,
                    world_epoch=execution_world_epoch,
                )
            else:
                disarm_third_player_command(
                    process,
                    world_epoch=execution_world_epoch,
                )
        elif effect.kind == follow_handoff.EffectKind.CLEAR_OLD_METADATA:
            if token.role in seamless_roles:
                continue
            clear_follower_role_metadata(process, token.role)
        elif effect.kind == follow_handoff.EffectKind.REQUEST_NATIVE_FOLLOW:
            if stale_world:
                continue
            observation = observations.get(token.incarnation_key)
            if (
                observation is not None
                and observation.token == token
                and observation.safe_to_request
            ):
                try:
                    resume_sanitized_follower_action(
                        process,
                        token.actor,
                        role=token.role,
                        handle=token.native_handle,
                        expected_phys=token.phys,
                    )
                except ACTOR_SAMPLE_ERRORS:
                    # A later bounded attempt can republish.  A volatile
                    # acknowledgement cleanup must not terminate the worker.
                    continue
        elif effect.kind == follow_handoff.EffectKind.ACQUIRE_NEW:
            # Acquisition was fully revalidated before any destructive effect.
            # This lets a same-role replacement skip DISARM/CLEAR atomically.
            continue
        elif effect.kind in (
            follow_handoff.EffectKind.CONFIRM_NATIVE_FOLLOW,
            follow_handoff.EffectKind.CANCEL_NATIVE_FOLLOW,
        ):
            # Pure-state terminal effects require no process mutation.
            continue
        else:
            raise RuntimeError(f"unhandled follower handoff effect: {effect.kind}")
    return acquired_headings, tuple(rejected_acquisitions)


def follower_handoff_requires_camera_reset(
    effects, acquired_headings, rejected_acquisitions
) -> bool:
    """Keep camera history across a complete same-role ownership replacement."""

    disarmed_roles = {
        effect.token.role
        for effect in effects
        if effect.kind == follow_handoff.EffectKind.DISARM_OLD
    }
    acquired_roles = set(acquired_headings)
    return bool(
        rejected_acquisitions
        or disarmed_roles != acquired_roles
        or any(
            effect.kind == follow_handoff.EffectKind.ACQUIRE_NEW
            and effect.token.role not in disarmed_roles
            for effect in effects
        )
    )


def publish_follower_role_metadata(
    process,
    *,
    active,
    p2_manual_control,
    p3_manual_control,
    p2_actor,
    p3_actor,
    p2_skeleton_keep,
    p3_skeleton_keep,
    p2_hud_slot=None,
    p3_hud_slot=None,
):
    p2_owned = bool(active and p2_manual_control and p2_actor)
    p3_owned = bool(active and p3_manual_control and p3_actor)
    p2_handle = actor_u32_or_zero(process, p2_actor, 0x08) if p2_owned else 0
    p3_handle = actor_u32_or_zero(process, p3_actor, 0x08) if p3_owned else 0
    p2_entry = actor_u64_or_zero(process, p2_actor, 0x10) if p2_owned else 0
    p3_entry = actor_u64_or_zero(process, p3_actor, 0x10) if p3_owned else 0

    process.write(
        COMMAND_BLOCK + P2_LOOT_HANDLE_OFFSET,
        struct.pack("<I", p2_handle),
    )
    process.write(
        third_player_block() + P3_LOOT_HANDLE_OFFSET,
        struct.pack("<I", p3_handle),
    )
    process.write(
        COMMAND_BLOCK + P2_SKELETON_KEEP_OFFSET,
        struct.pack("<Q", int(p2_skeleton_keep) if p2_owned else 0),
    )
    process.write(
        third_player_block() + P3_SKELETON_KEEP_OFFSET,
        struct.pack("<Q", int(p3_skeleton_keep) if p3_owned else 0),
    )
    process.write(
        COMMAND_BLOCK + P2_INTERACTION_ENTRY_OFFSET,
        struct.pack("<Q", p2_entry),
    )
    process.write(
        third_player_block() + P3_INTERACTION_ENTRY_OFFSET,
        struct.pack("<Q", p3_entry),
    )
    process.write(
        COMMAND_BLOCK + P2_HUD_PARTY_SLOT_OFFSET,
        struct.pack(
            "<I",
            int(p2_hud_slot)
            if p2_owned and p2_hud_slot is not None
            else INVALID_HUD_PARTY_SLOT,
        ),
    )
    process.write(
        third_player_block() + P3_HUD_PARTY_SLOT_OFFSET,
        struct.pack(
            "<I",
            int(p3_hud_slot)
            if p3_owned and p3_hud_slot is not None
            else INVALID_HUD_PARTY_SLOT,
        ),
    )


def publish_lifecycle_flags(
    process,
    logical_active,
    camera_active,
    interaction_routing_active,
):
    process.write(
        COMMAND_BLOCK + COOP_ACTIVE_OFFSET,
        struct.pack("<I", int(logical_active)),
    )
    process.write(
        COMMAND_BLOCK + CAMERA_ACTIVE_OFFSET,
        struct.pack("<I", int(camera_active)),
    )
    process.write(
        COMMAND_BLOCK + INTERACTION_ROUTING_ACTIVE_OFFSET,
        struct.pack("<I", int(interaction_routing_active)),
    )


def write_camera_command(
    process,
    target,
    extra_distance,
    yaw_offset=0.0,
    pitch_offset=0.0,
    collision_exclusion=(0.0, 0.0),
    *,
    focus_velocity=(0.0, 0.0, 0.0),
    reset_epoch=0,
    publisher=None,
    producer_tick_us=None,
):
    hold = process.read(
        third_player_block() + EXTERNAL_CAMERA_HOLD_OFFSET,
        EXTERNAL_CAMERA_HOLD_SIZE,
    )
    magic, active, *held = struct.unpack("<II8f", hold)
    hold_active = bool(
        magic == EXTERNAL_CAMERA_HOLD_MAGIC
        and active == 1
        and all(math.isfinite(value) for value in held)
    )
    if hold_active:
        target = held[0:3]
        extra_distance = held[3]
        yaw_offset = held[4]
        pitch_offset = held[5]
        collision_exclusion = held[6:8]
        # A held camera is a fixed external pose.  Carrying spring velocity into
        # the native predictor would make that supposedly frozen pose drift.
        focus_velocity = (0.0, 0.0, 0.0)
    if publisher is None:
        publisher = camera_frame_sync.CameraGoalPublisher()
    return publisher.publish(
        process,
        CAMERA_FRAME_STATE,
        target,
        focus_velocity,
        extra_distance,
        yaw_offset,
        pitch_offset,
        collision_exclusion,
        source_reset_epoch=reset_epoch,
        source_mode=int(hold_active),
        producer_tick_us=producer_tick_us,
    )


def clear_internal_camera_wall_latch(
    process,
    restored_native_boom,
    native_camera_flags,
):
    process.write(
        CAMERA_PRIMARY_RIG + 0x38,
        struct.pack("<f", float(restored_native_boom)),
    )
    if CAMERA_WALL_BIT_RELEASED_BY_RESCUE:
        process.write(
            CAMERA_PRIMARY_RIG + 0x40,
            struct.pack(
                "<I",
                int(native_camera_flags) & ~CAMERA_BACK_ON_WALL_MASK,
            ),
        )


RUNTIME_NATIVE_FEATURE_NAMES = (
    "FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE",
    "MOVEMENT_HOLDS_LEADER",
    "ENABLE_IDLE_MOTION_CANCEL",
    "VISUAL_FACING_HOOK_ENABLED",
    "FOLLOWER_NPC_INTERACTIONS_ENABLED",
    "FOLLOWER_LIST_GUARD_ENABLED",
    "FOLLOWER_LOCOMOTION_ANIMATION_GUARD_ENABLED",
    "FOLLOWER_GATEWAY_COLLISION_FILTER_ENABLED",
    "FOLLOWER_GATEWAY_TRIGGER_ENABLED",
    "FOLLOWER_ACTION_APPROACH_GATE_ENABLED",
    "FOLLOWER_ACTION_CHASE_GUARD_ENABLED",
    "BERSERK_MANUAL_COMMAND_ENABLED",
    "PAD_BACKGROUND_INPUT_ENABLED",
)


def runtime_native_feature_bits(*, upstream_camera_v2):
    bits = int(bool(upstream_camera_v2))
    for index, name in enumerate(RUNTIME_NATIVE_FEATURE_NAMES, start=1):
        bits |= int(bool(globals()[name])) << index
    return bits


def runtime_install_marker_addresses():
    return (
        COMMAND_BLOCK - 0x200 + RUNTIME_INSTALL_MARKER_OFFSET,
        DUAL_INTERACTION_BASE + RUNTIME_INSTALL_MARKER_OFFSET,
        DAMAGE_NUMBER_THEME_BASE + RUNTIME_INSTALL_MARKER_OFFSET,
    )


def runtime_install_marker_address():
    """Compatibility alias for the primary member of the triple commit."""

    return runtime_install_marker_addresses()[0]


def _runtime_allocation_ranges():
    return (
        (COMMAND_BLOCK - 0x200, CAVE_ALLOCATION_SIZE),
        (DUAL_INTERACTION_BASE, DUAL_INTERACTION_ALLOCATION_SIZE),
        (DAMAGE_NUMBER_THEME_BASE, DAMAGE_NUMBER_THEME_ALLOCATION_SIZE),
    )


def _runtime_executable_write_addresses():
    executable_tokens = ("_CAVE", "_HELPER", "_WRAPPER")
    ranges = _runtime_allocation_ranges()
    addresses = set()
    for name, value in globals().items():
        if (
            not any(token in name for token in executable_tokens)
            or not isinstance(value, int)
            or not value
        ):
            continue
        if any(base <= value < base + size for base, size in ranges):
            addresses.add(value)
    return frozenset(addresses)


def _runtime_root_read_addresses():
    addresses = {
        value
        for name, value in globals().items()
        if isinstance(value, int)
        and value
        and (name.endswith("_SITE") or name.endswith("_CALL_SITE"))
    }
    for name, values in globals().items():
        if name.endswith("_SITES") and isinstance(values, tuple):
            addresses.update(value for value in values if isinstance(value, int))
    addresses.update((NATIVE_PARTY_LEADER_SETTER, FOCUS_INACTIVE_DISPATCH_CALL))
    if not FOLLOWER_NPC_INTERACTIONS_ENABLED:
        # These three guards are owned by the headless-menu runtime in the
        # default topology.  Its cave embeds a different active-state address;
        # the local restore intentionally leaves that signed owner untouched.
        addresses.difference_update(
            (
                FOLLOWER_NPC_ENTRY_GUARD_SITE,
                FOLLOWER_NPC_ACCEPT_GUARD_SITE,
                FOLLOWER_NPC_CLEANUP_GUARD_SITE,
            )
        )
    return frozenset(addresses)


def _process_lifetime_adoptable_roots():
    """Roots whose installer proves a complete immutable image before adoption."""

    roots = {
        MOVEMENT_HOOK_SITE,
        STEERING_HEADING_STORE_SITE,
        KEYBOARD_HOOK_SITE,
        FACING_HOOK_SITE,
        XINPUT_STATE_RESULT_SITE,
        XINPUT_DEVICE_FINALIZE_SITE,
        DINPUT_GAMEPAD_FINALIZE_SITE,
        STEAM_GAMEPAD_FINALIZE_SITE,
        DAMAGE_NUMBER_SOURCE_CALL_SITE,
        DAMAGE_NUMBER_INIT_CALL_SITE,
    }
    if VISUAL_FACING_HOOK_ENABLED:
        roots.add(VISUAL_FACING_CALL_SITE)
    if PAD_BACKGROUND_INPUT_ENABLED:
        roots.add(PAD_BACKGROUND_INPUT_SITE)
    return frozenset(roots)


def _direct_rel32_target(site, instruction):
    instruction = bytes(instruction)
    if len(instruction) < 5 or instruction[0] not in (0xE8, 0xE9):
        return None
    return int(site) + 5 + struct.unpack("<i", instruction[1:5])[0]


def _reject_uncommitted_legacy_detours(process, bases):
    """Reject orphaned legacy roots before the first transactional write.

    Committed installs bypass the installer graph entirely and are validated by
    their manifest.  With zero markers, an old root into one of our allocations
    may still reach a cave while a legacy ``restore_*`` or installer rewrites it.
    Only roots with strict full-image adoption are permitted in that state.
    """

    ranges = tuple(
        (int(base), int(size))
        for base, size in zip(
            bases,
            (
                CAVE_ALLOCATION_SIZE,
                DUAL_INTERACTION_ALLOCATION_SIZE,
                DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
            ),
        )
        if base
    )
    if not ranges:
        return
    adoptable = _process_lifetime_adoptable_roots()
    for site in sorted(_runtime_root_read_addresses() - adoptable):
        current = process.read(site, 5)
        target = _direct_rel32_target(site, current)
        if target is None:
            continue
        if any(base <= target < base + size for base, size in ranges):
            raise RuntimeError(
                f"uncommitted native runtime hook: legacy detour at {site:#x} "
                "still reaches "
                f"resident code at {target:#x}; restart the game"
            )


class _NativeInstallRecorder:
    """Overlay or delegating process that records final native code regions."""

    def __init__(self, process, *, simulate=False, zero_ranges=()):
        self.process = process
        self.simulate = bool(simulate)
        self.zero_ranges = tuple(
            (int(base), int(size)) for base, size in zero_ranges
        )
        self.overlay = {}
        self.root_sizes = {}
        self.executable_sizes = {}
        self.root_candidates = _runtime_root_read_addresses()
        self.executable_starts = _runtime_executable_write_addresses()

    def __getattr__(self, name):
        return getattr(self.process, name)

    def _inside_zero_range(self, address, size):
        return any(
            base <= address and address + size <= base + length
            for base, length in self.zero_ranges
        )

    def _put(self, address, payload):
        for offset, value in enumerate(bytes(payload)):
            self.overlay[int(address) + offset] = value

    def _read_overlay(self, address, size):
        address = int(address)
        size = int(size)
        if self._inside_zero_range(address, size):
            payload = bytearray(size)
        else:
            payload = bytearray(self.process.read(address, size))
        for offset in range(size):
            value = self.overlay.get(address + offset)
            if value is not None:
                payload[offset] = value
        return bytes(payload)

    def read(self, address, size):
        address = int(address)
        size = int(size)
        if address in self.root_candidates:
            self.root_sizes[address] = max(self.root_sizes.get(address, 0), size)
        return self._read_overlay(address, size)

    def record_executable_region(self, address, size):
        address = int(address)
        size = int(size)
        if address not in self.executable_starts:
            raise RuntimeError(
                f"adopted native region begins at unknown address {address:#x}"
            )
        self.executable_sizes[address] = max(
            self.executable_sizes.get(address, 0), size
        )

    def write(self, address, payload):
        address = int(address)
        payload = bytes(payload)
        if address in self.executable_starts:
            self.executable_sizes[address] = max(
                self.executable_sizes.get(address, 0), len(payload)
            )
        if not self.simulate:
            self.process.write(address, payload)
        self._put(address, payload)

    def patch_code(self, address, payload):
        address = int(address)
        payload = bytes(payload)
        self.root_sizes[address] = max(self.root_sizes.get(address, 0), len(payload))
        if not self.simulate:
            self.process.patch_code(address, payload)
        self._put(address, payload)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def f32(self, address):
        return struct.unpack("<f", self.read(address, 4))[0]

    def regions(self):
        regions = []
        for kind, sizes in (
            (RUNTIME_INSTALL_REGION_ROOT, self.root_sizes),
            (RUNTIME_INSTALL_REGION_EXECUTABLE, self.executable_sizes),
        ):
            for address, size in sorted(sizes.items()):
                regions.append((kind, address, self._read_overlay(address, size)))
        return tuple(regions)


def _native_region_digest(payload):
    return hashlib.blake2s(
        bytes(payload),
        digest_size=16,
        person=b"ITREGN02",
    ).digest()


def serialize_runtime_install_manifest(regions):
    ordered = tuple(sorted(regions, key=lambda item: (item[0], item[1], len(item[2]))))
    payload = bytearray(
        RUNTIME_INSTALL_MANIFEST_HEADER.pack(
            RUNTIME_INSTALL_MANIFEST_MAGIC,
            RUNTIME_INSTALL_MANIFEST_VERSION,
            len(ordered),
        )
    )
    seen = set()
    for kind, address, region in ordered:
        region = bytes(region)
        key = (int(kind), int(address), len(region))
        if key in seen or not region:
            raise RuntimeError("invalid duplicate or empty native manifest region")
        seen.add(key)
        payload += RUNTIME_INSTALL_MANIFEST_ENTRY.pack(
            int(address),
            len(region),
            int(kind),
            _native_region_digest(region),
        )
    if len(payload) > RUNTIME_INSTALL_MANIFEST_MAX_SIZE:
        raise RuntimeError("native install manifest exceeds its bounded allocation")
    return bytes(payload)


def _runtime_manifest_digest(payload):
    return hashlib.blake2s(
        bytes(payload),
        digest_size=16,
        person=b"ITMANF02",
    ).digest()


def _runtime_build_id(feature_bits, manifest_digest):
    digest = hashlib.blake2s(digest_size=16, person=b"ITBLD002")
    digest.update(RUNTIME_NATIVE_LAYOUT_ID)
    digest.update(struct.pack("<I", int(feature_bits)))
    digest.update(bytes(manifest_digest))
    return digest.digest()


def _runtime_install_marker(
    *,
    state,
    feature_bits,
    manifest_address,
    manifest_size,
    manifest_count,
    manifest_digest,
):
    return RUNTIME_INSTALL_MARKER_STRUCT.pack(
        RUNTIME_INSTALL_MARKER_MAGIC,
        RUNTIME_INSTALL_MARKER_VERSION,
        int(state),
        int(feature_bits),
        int(manifest_count),
        int(manifest_address),
        int(manifest_size),
        bytes(manifest_digest),
        _runtime_build_id(feature_bits, manifest_digest),
    )


def _parse_runtime_manifest(payload, *, expected_count):
    payload = bytes(payload)
    if len(payload) < RUNTIME_INSTALL_MANIFEST_HEADER.size:
        raise RuntimeError("native runtime manifest is truncated; restart the game")
    magic, version, count = RUNTIME_INSTALL_MANIFEST_HEADER.unpack_from(payload)
    expected_size = (
        RUNTIME_INSTALL_MANIFEST_HEADER.size
        + int(count) * RUNTIME_INSTALL_MANIFEST_ENTRY.size
    )
    if (
        magic != RUNTIME_INSTALL_MANIFEST_MAGIC
        or version != RUNTIME_INSTALL_MANIFEST_VERSION
        or count != int(expected_count)
        or expected_size != len(payload)
    ):
        raise RuntimeError("native runtime manifest header differs; restart the game")

    regions = []
    seen = set()
    previous_key = None
    previous_end = {}
    offset = RUNTIME_INSTALL_MANIFEST_HEADER.size
    for _index in range(count):
        address, size, kind, digest = RUNTIME_INSTALL_MANIFEST_ENTRY.unpack_from(
            payload, offset
        )
        offset += RUNTIME_INSTALL_MANIFEST_ENTRY.size
        key = (kind, address, size)
        if (
            kind not in (RUNTIME_INSTALL_REGION_ROOT, RUNTIME_INSTALL_REGION_EXECUTABLE)
            or not size
            or size > CAVE_ALLOCATION_SIZE
            or key in seen
            or (previous_key is not None and key <= previous_key)
            or address < previous_end.get(kind, 0)
        ):
            raise RuntimeError("native runtime manifest contains an invalid region")
        seen.add(key)
        previous_key = key
        previous_end[kind] = address + size
        regions.append((kind, address, size, digest))
    return tuple(regions)


def _discover_runtime_bases(process):
    return (
        process.ensure_cave_allocation(allow_allocate=False),
        process.ensure_dual_interaction_allocation(allow_allocate=False),
        process.ensure_damage_number_theme_allocation(allow_allocate=False),
    )


def _validate_committed_native_install(process, *, upstream_camera_v2):
    marker_payloads = tuple(
        process.read(address, RUNTIME_INSTALL_MARKER_STRUCT.size)
        for address in runtime_install_marker_addresses()
    )
    if len(set(marker_payloads)) != 1:
        raise RuntimeError(
            "native runtime triple commit is incomplete; restart the game"
        )

    (
        magic,
        version,
        state,
        feature_bits,
        manifest_count,
        manifest_address,
        manifest_size,
        manifest_digest,
        build_id,
    ) = RUNTIME_INSTALL_MARKER_STRUCT.unpack(marker_payloads[0])
    current_features = runtime_native_feature_bits(
        upstream_camera_v2=upstream_camera_v2
    )
    if (
        magic != RUNTIME_INSTALL_MARKER_MAGIC
        or version != RUNTIME_INSTALL_MARKER_VERSION
        or state != RUNTIME_INSTALL_COMMITTED
    ):
        raise RuntimeError("native runtime is uncommitted; restart the game")
    if feature_bits != current_features:
        raise RuntimeError(
            "native runtime feature layout differs; restart the game before upgrading"
        )
    expected_size = (
        RUNTIME_INSTALL_MANIFEST_HEADER.size
        + int(manifest_count) * RUNTIME_INSTALL_MANIFEST_ENTRY.size
    )
    if (
        not manifest_count
        or manifest_count > 512
        or manifest_size != expected_size
        or manifest_size > RUNTIME_INSTALL_MANIFEST_MAX_SIZE
        or manifest_address < REL32_ALLOCATION_LOW
        or manifest_address > RUNTIME_INSTALL_MAX_REMOTE_ADDRESS - manifest_size
    ):
        raise RuntimeError("native runtime manifest pointer is invalid; restart the game")

    try:
        manifest = process.read(manifest_address, manifest_size)
    except OSError as exc:
        raise RuntimeError(
            "native runtime manifest is unreadable; restart the game"
        ) from exc
    if _runtime_manifest_digest(manifest) != manifest_digest:
        raise RuntimeError("native runtime manifest digest differs; restart the game")
    if build_id != _runtime_build_id(feature_bits, manifest_digest):
        raise RuntimeError(
            "native runtime build differs; restart the game before upgrading"
        )

    regions = _parse_runtime_manifest(manifest, expected_count=manifest_count)
    roots = _runtime_root_read_addresses()
    executable_starts = _runtime_executable_write_addresses()
    root_count = sum(
        kind == RUNTIME_INSTALL_REGION_ROOT for kind, *_rest in regions
    )
    executable_count = sum(
        kind == RUNTIME_INSTALL_REGION_EXECUTABLE for kind, *_rest in regions
    )
    if (
        root_count < RUNTIME_INSTALL_MIN_ROOT_REGIONS
        or executable_count < RUNTIME_INSTALL_MIN_EXECUTABLE_REGIONS
    ):
        raise RuntimeError("native runtime manifest is incomplete; restart the game")
    for kind, address, size, expected_digest in regions:
        if kind == RUNTIME_INSTALL_REGION_ROOT and address not in roots:
            raise RuntimeError("native runtime manifest names an unknown hook root")
        if kind == RUNTIME_INSTALL_REGION_ROOT and size > 0x100:
            raise RuntimeError("native runtime manifest has an invalid hook span")
        if kind == RUNTIME_INSTALL_REGION_EXECUTABLE:
            inside_allocation = any(
                base <= address and address + size <= base + allocation_size
                for base, allocation_size in _runtime_allocation_ranges()
            )
            if address not in executable_starts or not inside_allocation:
                raise RuntimeError(
                    "native runtime manifest names an unknown executable region"
                )
        try:
            current = process.read(address, size)
        except OSError as exc:
            raise RuntimeError(
                f"native runtime region {address:#x} is unreadable; restart the game"
            ) from exc
        if _native_region_digest(current) != expected_digest:
            label = "hook" if kind == RUNTIME_INSTALL_REGION_ROOT else "cave"
            raise RuntimeError(
                f"native runtime {label} mismatch at {address:#x}; restart the game"
            )
    return regions


def recover_or_reuse_native_install(
    process,
    *,
    upstream_camera_v2,
    discovered_bases=None,
):
    """Read-only hot-attach.  Any partial or different install is refused."""

    bases = (
        _discover_runtime_bases(process)
        if discovered_bases is None
        else tuple(discovered_bases)
    )
    if not any(bases):
        return None

    marker_payloads = tuple(
        process.read(base + RUNTIME_INSTALL_MARKER_OFFSET, RUNTIME_INSTALL_MARKER_STRUCT.size)
        for base in bases
        if base
    )
    empty = b"\x00" * RUNTIME_INSTALL_MARKER_STRUCT.size
    if all(payload == empty for payload in marker_payloads):
        # The headless menu and story helpers legitimately reserve the shared
        # dual/damage allocations before this worker starts.  Zero local
        # markers plus the untouched first co-op entry root are a pristine
        # preallocation, not a partial local install.
        _reject_uncommitted_legacy_detours(process, bases)
        movement_original = b"\x40\x53\x56\x41\x56"
        if process.read(MOVEMENT_HOOK_SITE, len(movement_original)) != movement_original:
            raise RuntimeError(
                "uncommitted native runtime hook found; restart the game"
            )
        return None

    if not all(bases):
        raise RuntimeError(
            "partial native runtime allocation set found; restart the game"
        )
    return _validate_committed_native_install(
        process,
        upstream_camera_v2=upstream_camera_v2,
    )


def inspect_native_install(process, *, upstream_camera_v2):
    """Return a read-only ``(mode, bases, regions)`` launcher preflight.

    ``mode == "adopt"`` means any existing shared allocations have zero local
    markers and the first local entry root is stock, so a transactional first
    bind is permitted.  ``mode == "reuse"`` means the complete committed
    manifest was validated.  Every other state raises without writing.
    """

    bases = _discover_runtime_bases(process)
    regions = recover_or_reuse_native_install(
        process,
        upstream_camera_v2=upstream_camera_v2,
        discovered_bases=bases,
    )
    return ("reuse" if regions is not None else "adopt", bases, regions)


def _install_native_code(process, *, upstream_camera_v2):
    """Install the complete local native-code topology."""

    if FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE:
        install_follower_action_native_approach_helper(process)
    install_p1_berserk_movement_helper(process)
    install_movement_override(process)
    install_p1_sdl_xinput_bridge(process)
    install_p2_device_isolation(process)
    install_dinput_gamepad_filter(process)
    install_steam_gamepad_filter(process)
    install_dual_interaction_proximity_filter(process)
    install_dual_interaction_secondary_filter(process)
    install_p2_interaction_source(process)
    install_p2_interaction_proximity_source(process)

    if FOLLOWER_NPC_INTERACTIONS_ENABLED:
        install_follower_npc_private_object_guards(process)
        install_p2_interaction_indicator_source(process)
        install_follower_npc_exact_scan(process)
    else:
        restore_follower_npc_exact_scan(process)
        restore_p2_interaction_indicator_source(process)
        restore_follower_npc_private_object_guards(
            process,
            active_state_address=DUAL_INTERACTION_STATE,
        )
    install_dual_interaction_publisher(process)
    install_dual_interaction_scan(process)
    install_native_party_leader_guard(process)
    install_p2_interaction_enable_gate(process)
    install_p2_interaction_range_gates(process)
    install_p2_interaction_geometry_result_logger(process)
    install_p2_interaction_broadphase_filter(process)
    restore_p2_interaction_actor_filter(process)
    install_interaction_geometry_logger(process)
    install_interaction_update_logger(process)

    install_steering_heading_override(process)
    install_keyboard_filter(process)
    install_facing_override(process)
    if VISUAL_FACING_HOOK_ENABLED:
        install_visual_facing_update(process)
    else:
        restore_visual_facing_update(process)
    restore_phys_facing_guard(process)
    restore_follower_turn_guard(process)

    if FOLLOWER_LIST_GUARD_ENABLED:
        install_follower_list_guard(process)
    else:
        restore_follower_list_guard(process)
    if FOLLOWER_LOCOMOTION_ANIMATION_GUARD_ENABLED:
        install_follower_locomotion_animation_guard(process)
    else:
        restore_follower_locomotion_animation_guard(process)
    if FOLLOWER_GATEWAY_COLLISION_FILTER_ENABLED:
        install_follower_gateway_collision_filter(process)
    else:
        restore_follower_gateway_collision_filter(process)
    if FOLLOWER_GATEWAY_TRIGGER_ENABLED:
        install_follower_gateway_trigger(process)
    else:
        restore_follower_gateway_trigger(process)
    restore_follower_arbitrator_guard(process)
    if FOLLOWER_ACTION_APPROACH_GATE_ENABLED:
        install_follower_action_approach_gate(process)
    else:
        restore_follower_action_approach_gate(process)
    if FOLLOWER_ACTION_CHASE_GUARD_ENABLED:
        install_follower_action_chase_guard(process)
    else:
        restore_follower_action_chase_guard(process)
    restore_follower_action_filter(process)

    if upstream_camera_v2:
        restore_camera_midpoint(process)
        restore_camera_eye_smoothing(process)
        restore_upstream_camera_focus_v2(process)
        install_controller_shared_focus(process)
        install_shared_camera_internal_collision_filter(process)
    else:
        restore_upstream_camera_focus_v2(process)
        restore_controller_shared_focus(process)
        restore_shared_camera_internal_collision_filter(process)
        restore_camera_eye_smoothing(process)
        install_camera_midpoint(process)

    install_collision_aware_camera_zoom(process)
    install_p2_gauge_color(process)
    install_damage_number_source_themes(process)
    restore_p2_hud_text_color(process)
    install_background_unpause(process)
    install_menu_side_arrow_suppression(process)
    if BERSERK_MANUAL_COMMAND_ENABLED:
        install_berserk_manual_command_gate(process)
    else:
        restore_berserk_manual_command_gate(process)
    if PAD_BACKGROUND_INPUT_ENABLED:
        install_pad_background_input(process)
    else:
        restore_pad_background_input(process)
    restore_native_cursor(process)


def recover_native_leader_role_transfer_for_restart(
    process,
    *,
    producer_quiesced=False,
) -> bool:
    """Adopt an interrupted transfer before any startup reset can erase it."""

    try:
        _recover_native_leader_role_cancellation(process)
        if native_leader_role_cancellation_pending(process):
            if not settle_native_leader_role_cancellation(process):
                raise RuntimeError(
                    "terminal leader cancellation reservations did not settle"
                )
            return True
        claim_in_progress = native_leader_role_claim_in_progress(process)
        if claim_in_progress:
            fence_raw = process.read(NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
            if not isinstance(
                fence_raw, (bytes, bytearray, memoryview)
            ) or len(fence_raw) != 4:
                raise struct.error("native leader shutdown fence is unreadable")
            fence_closed = struct.unpack("<I", fence_raw)[0] == 1
            if not bool(producer_quiesced) or not fence_closed:
                # A live producer legitimately occupies CLAIMING while it
                # copies the request payload. Returning preserved lets the
                # frame caller freeze/retry without interpreting an old
                # terminal payload or killing movement as a torn write.
                return True
            _recover_abandoned_native_leader_role_claim(process)
        transfer = read_native_leader_role_transfer(process)
        recovery_required = native_leader_role_transfer_recovery_required(process)
        if transfer is None and recovery_required:
            transfer = read_native_leader_role_transfer(
                process,
                include_inactive=True,
                include_claiming=bool(producer_quiesced),
            )
        if transfer is None:
            if recovery_required:
                raise RuntimeError("role transfer publication is torn")
            return False

        publishers = {
            "p2": movement_mailbox.publisher_for(process, P2_MOVEMENT_MAILBOX),
            "p3": movement_mailbox.publisher_for(process, P3_MOVEMENT_MAILBOX),
        }
        if transfer.native_applied:
            if not settle_native_leader_role_transfer_publishers(process, transfer):
                raise RuntimeError("applied leader journal is not observable")
            publish_native_leader_role_transfer_active(process, True)
            if (
                process.u32(
                    P2_DEVICE_FILTER_STATE + NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
                )
                == NATIVE_LEADER_HANDOFF_ROLE_TRANSFER
            ):
                publish_native_leader_handoff_active(process, False)
            return True

        for publisher in publishers.values():
            if not publisher.adopt_current():
                raise RuntimeError("pending leader journal has no committed fallback")
        if transfer.prepared:
            journal = read_native_leader_role_journal(process, transfer)
            if journal is None:
                raise RuntimeError("prepared leader journal is torn")
            for role in ("p2", "p3"):
                sequence = journal[f"{role}_sequence"]
                if not sequence:
                    continue
                reservation = publishers[role].reserve(journal[f"{role}_command"])
                if reservation.sequence != sequence:
                    raise RuntimeError("prepared journal generation is stale")
        # An old shell may have cleared its legacy shared mode just before it
        # died.  Reactivate only after the fixed payload/mailbox generations
        # have been authenticated and adopted.
        publish_native_leader_role_transfer_active(process, True)
        if (
            process.u32(
                P2_DEVICE_FILTER_STATE + NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
            )
            == NATIVE_LEADER_HANDOFF_ROLE_TRANSFER
        ):
            publish_native_leader_handoff_active(process, False)
        return True
    except (OSError, RuntimeError, struct.error, ValueError):
        # Destructive startup is never a safe fallback for an authenticated
        # in-flight transaction.  Keep the old native state and let the caller
        # retry/restart instead of manufacturing an inactive ownership frame.
        raise RuntimeError("cannot safely recover native leader role transfer")


def _reset_runtime_dynamic_state(
    process,
    initial_manual_control_mask,
    *,
    role_producer_quiesced=False,
):
    """Disarm stale publications before a new helper begins heartbeating."""

    if recover_native_leader_role_transfer_for_restart(
        process,
        producer_quiesced=role_producer_quiesced,
    ):
        return
    clear_movement_helper_liveness(process)
    movement_mailbox.reset_mailbox(process, P2_MOVEMENT_MAILBOX)
    movement_mailbox.reset_mailbox(process, P3_MOVEMENT_MAILBOX)
    process.write(COMMAND_BLOCK + MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 0))
    process.write(COMMAND_BLOCK + 0x10, struct.pack("<Q", 0))
    clear_p1_berserk_command(process)
    clear_third_player_command(process)
    process.write(DUAL_INTERACTION_STATE, b"\x00" * 0x600)
    process.write(
        P2_FOLLOWER_ACTION_MAILBOX,
        b"\x00" * FOLLOWER_ACTION_MAILBOX_REGION_SIZE,
    )
    process.write(P1_SDL_XINPUT_STATE, b"\x00" * P1_SDL_XINPUT_STATE_SIZE)
    device_state = bytearray(P2_DEVICE_FILTER_STATE_SIZE)
    struct.pack_into(
        "<I",
        device_state,
        P1_NATIVE_CONTROLLER_SLOT_OFFSET,
        INVALID_NATIVE_CONTROLLER_SLOT,
    )
    process.write(P2_DEVICE_FILTER_STATE, device_state)
    # The lifetime fence is outside the authenticated journal record and must
    # remain closed until this helper has installed and published liveness.
    process.write(
        NATIVE_LEADER_ROLE_JOURNAL,
        b"\x00" * NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET,
    )
    fence_end = NATIVE_LEADER_ROLE_SHUTDOWN_FENCE_OFFSET + 4
    process.write(
        NATIVE_LEADER_ROLE_JOURNAL + fence_end,
        b"\x00" * (NATIVE_LEADER_ROLE_JOURNAL_SIZE - fence_end),
    )
    process.write(
        FOLLOWER_GATEWAY_TRIGGER_SCRATCH,
        b"\x00" * FOLLOWER_GATEWAY_TRIGGER_SCRATCH_SIZE,
    )
    process.write(DAMAGE_NUMBER_SOURCE_STATE, struct.pack("<Q", 0))
    reset_camera_eye_smoothing(process)

    process.write(COMMAND_BLOCK + COOP_ACTIVE_OFFSET, struct.pack("<I", 0))
    process.write(COMMAND_BLOCK + MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 0))
    process.write(COMMAND_BLOCK + CAMERA_ACTIVE_OFFSET, struct.pack("<I", 0))
    process.write(
        COMMAND_BLOCK + INTERACTION_ROUTING_ACTIVE_OFFSET,
        struct.pack("<I", 0),
    )
    process.write(
        COMMAND_BLOCK + KEYBOARD_FILTER_ACTIVE_OFFSET,
        struct.pack("<I", 0),
    )
    process.write(COMMAND_BLOCK + P2_LOOT_HANDLE_OFFSET, struct.pack("<I", 0))
    process.write(
        third_player_block() + P3_LOOT_HANDLE_OFFSET,
        struct.pack("<I", 0),
    )
    process.write(COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET, b"\x00")
    process.write(COMMAND_BLOCK + P2_SKELETON_KEEP_OFFSET, struct.pack("<Q", 0))
    process.write(
        third_player_block() + P3_SKELETON_KEEP_OFFSET,
        struct.pack("<Q", 0),
    )
    process.write(
        third_player_block() + EXTERNAL_CAMERA_HOLD_OFFSET,
        b"\x00" * EXTERNAL_CAMERA_HOLD_SIZE,
    )
    process.write(COMMAND_BLOCK + 0x04, struct.pack("<f", 0.0))
    process.write(COMMAND_BLOCK + 0x20, struct.pack("<3f", 0.0, 0.0, 0.0))
    process.write(COMMAND_BLOCK + 0x2C, struct.pack("<f", 0.0))
    process.write(COMMAND_BLOCK + 0x30, struct.pack("<2f", 0.0, 0.0))
    process.write(
        COMMAND_BLOCK + CAMERA_COLLISION_EXCLUSION_START_OFFSET,
        struct.pack("<2f", 0.0, 0.0),
    )
    process.write(
        COMMAND_BLOCK + P1_HUD_PARTY_SLOT_OFFSET,
        struct.pack("<2I", INVALID_HUD_PARTY_SLOT, INVALID_HUD_PARTY_SLOT),
    )
    process.write(
        third_player_block() + P3_HUD_PARTY_SLOT_OFFSET,
        struct.pack("<I", INVALID_HUD_PARTY_SLOT),
    )
    publish_manual_control_mask(process, initial_manual_control_mask)
    for offset in (0x78, 0x90, 0x98, 0xA0, 0xA8):
        process.write(COMMAND_BLOCK + offset, struct.pack("<Q", 0))
    process.write(
        COMMAND_BLOCK + P2_INTERACTION_STATE_POINTER_OFFSET,
        struct.pack(
            "<Q",
            DUAL_INTERACTION_STATE + DUAL_INTERACTION_P2_SNAPSHOT_OFFSET,
        ),
    )
    process.write(COMMAND_BLOCK + P2_INTERACTION_ENTRY_OFFSET, struct.pack("<Q", 0))
    process.write(
        third_player_block() + P3_INTERACTION_ENTRY_OFFSET,
        struct.pack("<Q", 0),
    )
    process.write(
        third_player_block() + P3_INTERACTION_SCAN_ENTRY_OFFSET,
        struct.pack("<Q", 0),
    )
    process.write(
        third_player_block() + P3_INTERACTION_STATE_POINTER_OFFSET,
        struct.pack(
            "<Q",
            DUAL_INTERACTION_STATE + DUAL_INTERACTION_P3_SNAPSHOT_OFFSET,
        ),
    )
    for offset in (
        INTERACTION_DEBUG_LAST_ENTRY_OFFSET,
        INTERACTION_DEBUG_TOTAL_OFFSET,
        INTERACTION_DEBUG_P2_MATCH_OFFSET,
        INTERACTION_GEOMETRY_LAST_ENTRY_OFFSET,
        INTERACTION_GEOMETRY_TOTAL_OFFSET,
        INTERACTION_GEOMETRY_P2_MATCH_OFFSET,
        INTERACTION_UPDATE_COUNT_OFFSET,
    ):
        process.write(COMMAND_BLOCK + offset, struct.pack("<Q", 0))


class _NoExecutableWrites:
    def __init__(self, process, regions):
        self.process = process
        self.executable_ranges = tuple(
            (address, address + size)
            for kind, address, size, _digest in regions
            if kind == RUNTIME_INSTALL_REGION_EXECUTABLE
        )

    def __getattr__(self, name):
        return getattr(self.process, name)

    def write(self, address, payload):
        start = int(address)
        end = start + len(payload)
        if any(start < limit and base < end for base, limit in self.executable_ranges):
            raise RuntimeError("dynamic reset attempted to overwrite native code")
        self.process.write(start, payload)

    def patch_code(self, address, payload):
        del address, payload
        raise RuntimeError("dynamic reset attempted to patch native code")


def _set_runtime_bases(bases):
    set_cave_base(bases[0])
    set_dual_interaction_base(bases[1])
    set_damage_number_theme_base(bases[2])


def _native_leader_role_producer_quiesced(pid) -> bool:
    """Prove that no menu producer owns the process-lifetime lease."""

    try:
        return not helper_lease.active(int(pid), "headless_menu")
    except (OSError, RuntimeError, TypeError, ValueError):
        # An uncertain lease can never authorize clearing CLAIMING.
        return False


def _preflight_native_install(process, *, upstream_camera_v2, bases, zero_ranges=()):
    _set_runtime_bases(bases)
    planner = _NativeInstallRecorder(
        process,
        simulate=True,
        zero_ranges=zero_ranges,
    )
    _install_native_code(planner, upstream_camera_v2=upstream_camera_v2)
    return planner.regions()


def _commit_runtime_install(process, *, regions, upstream_camera_v2):
    manifest = serialize_runtime_install_manifest(regions)
    manifest_address = process.allocate_runtime_manifest(len(manifest))
    process.write(manifest_address, manifest)
    manifest_digest = _runtime_manifest_digest(manifest)
    manifest_count = len(regions)
    feature_bits = runtime_native_feature_bits(
        upstream_camera_v2=upstream_camera_v2
    )
    prepared = _runtime_install_marker(
        state=RUNTIME_INSTALL_PREPARED,
        feature_bits=feature_bits,
        manifest_address=manifest_address,
        manifest_size=len(manifest),
        manifest_count=manifest_count,
        manifest_digest=manifest_digest,
    )
    committed = _runtime_install_marker(
        state=RUNTIME_INSTALL_COMMITTED,
        feature_bits=feature_bits,
        manifest_address=manifest_address,
        manifest_size=len(manifest),
        manifest_count=manifest_count,
        manifest_digest=manifest_digest,
    )
    marker_addresses = runtime_install_marker_addresses()
    for address in marker_addresses:
        process.write(address, prepared)
    for address in marker_addresses:
        process.write(address, committed)


def install_runtime(
    process,
    *,
    pid,
    active,
    upstream_camera_v2,
    initial_manual_control_mask=DEFAULT_MANUAL_CONTROL_MASK,
    preserve_dynamic_state_on_reuse=False,
):
    mode, discovered, live_regions = inspect_native_install(
        process,
        upstream_camera_v2=upstream_camera_v2,
    )
    if mode == "reuse":
        _set_runtime_bases(discovered)
        _write_native_leader_role_word_confirmed(
            process,
            NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
            1,
            label="movement reuse role admission close",
        )
        guarded = _NoExecutableWrites(process, live_regions)
        producer_quiesced = _native_leader_role_producer_quiesced(pid)
        if preserve_dynamic_state_on_reuse:
            recovered = recover_native_leader_role_transfer_for_restart(
                process,
                producer_quiesced=producer_quiesced,
            )
            if not recovered:
                for mailbox in (P2_MOVEMENT_MAILBOX, P3_MOVEMENT_MAILBOX):
                    if not movement_mailbox.publisher_for(
                        process,
                        mailbox,
                    ).adopt_current():
                        raise RuntimeError(
                            "resident movement mailbox is not coherent"
                        )
        else:
            _reset_runtime_dynamic_state(
                guarded,
                initial_manual_control_mask,
                role_producer_quiesced=producer_quiesced,
            )
        print(
            f"Attached to {PROCESS_NAME}, PID {pid}; native runtime reused "
            "without executable rewrites.",
            flush=True,
        )
        return "reused"

    preflight_bases = tuple(
        existing or placeholder
        for existing, placeholder in zip(discovered, RUNTIME_PREFLIGHT_BASES)
    )
    zero_ranges = tuple(
        (base, size)
        for existing, base, size in zip(
            discovered,
            preflight_bases,
            (
                CAVE_ALLOCATION_SIZE,
                DUAL_INTERACTION_ALLOCATION_SIZE,
                DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
            ),
        )
        if not existing
    )
    _preflight_native_install(
        process,
        upstream_camera_v2=upstream_camera_v2,
        bases=preflight_bases,
        zero_ranges=zero_ranges,
    )

    actual_bases = (
        process.ensure_cave_allocation(),
        process.ensure_dual_interaction_allocation(),
        process.ensure_damage_number_theme_allocation(),
    )
    _set_runtime_bases(actual_bases)
    _write_native_leader_role_word_confirmed(
        process,
        NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        1,
        label="movement initial role admission close",
    )
    legacy_base = actual_bases[0]
    if CAMERA_FOCUS_V2_CAVE == legacy_base + 0xD00:
        raise RuntimeError("camera V2 focus cave overlaps the cursor/panel feed")

    planned_regions = _preflight_native_install(
        process,
        upstream_camera_v2=upstream_camera_v2,
        bases=actual_bases,
    )
    _reset_runtime_dynamic_state(
        process,
        initial_manual_control_mask,
        role_producer_quiesced=_native_leader_role_producer_quiesced(pid),
    )
    recorder = _NativeInstallRecorder(process)
    _install_native_code(recorder, upstream_camera_v2=upstream_camera_v2)
    installed_regions = recorder.regions()
    if installed_regions != planned_regions:
        raise RuntimeError(
            "native install changed between preflight and commit; restart the game"
        )
    _commit_runtime_install(
        process,
        regions=installed_regions,
        upstream_camera_v2=upstream_camera_v2,
    )

    print(f"Attached to {PROCESS_NAME}, PID {pid}")
    print(
        "F8 toggles co-op movement. Starts",
        "enabled." if active else "disabled.",
    )
    print(
        "Camera mode:",
        "camera-relative" if CAMERA_VIEW_MATRIX else "world-relative",
    )
    print(
        "Shared camera pipeline:",
        "upstream/native-collision"
        if upstream_camera_v2
        else "legacy/late-translation",
    )
    return "installed"

def drain_follower_handoff_for_shutdown(
    process,
    handoff_state,
    session_snapshot,
    party_actor_slots,
    *,
    world_epoch,
    native_sample,
    now=None,
):
    """Make one final safe follow attempt, then terminate pending repairs."""

    shutdown_tokens = {
        token.incarnation_key: token
        for token in (
            tuple(handoff_state.owner_map().values())
            + tuple(repair.token for repair in handoff_state.repairs)
        )
    }
    shutdown_observations = {
        key: observe_follower_handoff(
            process,
            token,
            session_snapshot,
            party_actor_slots or {},
            int(native_sample) + 1,
        )
        for key, token in shutdown_tokens.items()
    }
    shutdown_now = time.monotonic() if now is None else float(now)
    handoff_state, shutdown_effects = follow_handoff.reconcile_owners(
        handoff_state,
        {"p2": None, "p3": None},
        acquire_eligible={"p2": False, "p3": False},
        observations=shutdown_observations,
        now=shutdown_now,
        world_epoch=int(world_epoch),
    )
    handoff_state, final_repair_effects = follow_handoff.advance_repairs(
        handoff_state,
        shutdown_observations,
        now=shutdown_now,
        world_epoch=int(world_epoch),
    )
    handoff_state, cancellation_effects = follow_handoff.cancel_repairs(
        handoff_state,
        reason="shutdown",
    )
    apply_follower_handoff_effects(
        process,
        shutdown_effects + final_repair_effects + cancellation_effects,
        shutdown_observations,
        current_world_epoch=world_epoch,
    )
    return handoff_state


def shutdown_runtime(
    process,
    *,
    ko_leader_ownership,
    area_handoff,
    camera_tuning,
    bindings=None,
    handoff_state=None,
    session_snapshot=None,
    party_actor_slots=None,
    world_epoch=0,
    native_sample=0,
):
    failures: list[str] = []

    def preserve_remote_state(reason: str) -> None:
        if bindings is not None:
            channel = getattr(bindings, "_controller_channel", None)
            close_channel = getattr(channel, "close", None)
            if callable(close_channel):
                try:
                    close_channel()
                except BaseException:
                    pass
        try:
            process.close()
        except BaseException:
            pass
        try:
            print(reason, flush=True)
        except BaseException:
            pass

    # Coordinated worker replacement deliberately leaves the native runtime,
    # mailboxes, HUD and ownership gates resident.  The supervisor has already
    # closed role admission and will start the consumer before the producer.
    # This check must precede even the fence write: an ambiguous filesystem
    # marker cannot become authority to clear fixed remote state.
    if runtime_shutdown.preserve_resident_runtime(force=True):
        preserve_remote_state("COOP_RESTART_REMOTE_STATE_PRESERVED")
        return

    # Close admission *before* sampling the role state.  The producer writes
    # CLAIMING before its post-fence recheck, so either its claim is visible to
    # the sample below or it observes this fence and aborts before publishing a
    # request.  A write reported as failed is ambiguous (it may have landed),
    # therefore it authorizes no destructive cleanup at all.
    try:
        publish_native_leader_role_shutdown_fence(process, True)
    except BaseException:
        preserve_remote_state("COOP_SHUTDOWN_FENCE_AMBIGUOUS_PRESERVED")
        return

    # A published role transaction belongs to fixed game memory and may be
    # completed after shutdown began.  Preserve every ownership/HUD byte; the
    # supervised replacement authenticates and adopts it before reopening the
    # fence.
    try:
        preserve_role_transfer = native_leader_role_transfer_recovery_required(
            process
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        preserve_role_transfer = True
    if preserve_role_transfer:
        preserve_remote_state("COOP_SHUTDOWN_ROLE_TRANSFER_PRESERVED")
        return

    def attempt(label, operation) -> None:
        try:
            operation()
        except BaseException as error:
            # Teardown is deliberately broader than the frame loop.  There is
            # no compatibility decision left to hide here, and one broken
            # mailbox must not strand unrelated input/controller publications.
            failures.append(f"{label}:{type(error).__name__}")

    def write(label: str, address: int, payload: bytes) -> None:
        attempt(
            label,
            lambda address=address, payload=bytes(payload): process.write(
                address,
                payload,
            ),
        )

    zero_u32 = struct.pack("<I", 0)
    zero_u64 = struct.pack("<Q", 0)

    # Release native desktop input and every master gate before optional actor
    # handoff I/O.  Loading/KO/Stone can invalidate an actor while the fixed
    # mailboxes remain healthy, so every write is an independent transaction.
    attempt("movement-liveness", lambda: clear_movement_helper_liveness(process))
    write(
        "keyboard-filter",
        COMMAND_BLOCK + KEYBOARD_FILTER_ACTIVE_OFFSET,
        zero_u32,
    )
    write("keyboard-budget", KEYBOARD_FILTER_BUDGET, zero_u32)
    for label, address in (
        ("movement-master", COMMAND_BLOCK + MOVEMENT_ACTIVE_OFFSET),
        ("logical-active", COMMAND_BLOCK + COOP_ACTIVE_OFFSET),
        (
            "interaction-routing",
            COMMAND_BLOCK + INTERACTION_ROUTING_ACTIVE_OFFSET,
        ),
        ("camera-active", COMMAND_BLOCK + CAMERA_ACTIVE_OFFSET),
        (
            "p1-sdl-xinput",
            P1_SDL_XINPUT_STATE + P1_SDL_XINPUT_ENABLED_OFFSET,
        ),
        (
            "flee-active",
            P2_DEVICE_FILTER_STATE + COOP_FLEE_ACTIVE_OFFSET,
        ),
    ):
        write(label, address, zero_u32)
    write(
        "camera-frame-state",
        CAMERA_FRAME_STATE,
        b"\x00" * camera_frame_sync.CAMERA_FRAME_STATE_SIZE,
    )

    if handoff_state is not None and session_snapshot is not None:
        attempt(
            "follower-handoff",
            lambda: drain_follower_handoff_for_shutdown(
                process,
                handoff_state,
                session_snapshot,
                party_actor_slots,
                world_epoch=world_epoch,
                native_sample=native_sample,
            ),
        )
    attempt("ko-leader", lambda: ko_leader_ownership.shutdown(process))
    attempt("area-handoff", lambda: area_handoff.shutdown(process))
    attempt(
        "leader-handoff-gate",
        lambda: publish_native_leader_handoff_active(process, False),
    )
    attempt(
        "native-controller-slot",
        lambda: publish_native_controller_slot(process, None),
    )
    attempt("device-isolation", lambda: publish_coop_device_isolation(process, ()))

    for label, address in (
        (
            "flee-request",
            P2_DEVICE_FILTER_STATE + COOP_FLEE_REQUEST_OFFSET,
        ),
        (
            "flee-p1-physical",
            P2_DEVICE_FILTER_STATE + COOP_FLEE_P1_PHYSICAL_OFFSET,
        ),
    ):
        write(label, address, zero_u32)

    # Ask each sequence-aware publisher to commit an inactive generation.  Raw
    # mirrors below are still cleared independently if a torn mailbox prevents
    # either higher-level disarm from completing.
    attempt(
        "p2-command-mailbox",
        lambda: disarm_second_player_command(process, world_epoch=world_epoch),
    )
    attempt(
        "p3-command-mailbox",
        lambda: disarm_third_player_command(process, world_epoch=world_epoch),
    )
    attempt(
        "p2-movement-mailbox-raw",
        lambda: movement_mailbox.reset_mailbox(process, P2_MOVEMENT_MAILBOX),
    )
    attempt(
        "p3-movement-mailbox-raw",
        lambda: movement_mailbox.reset_mailbox(process, P3_MOVEMENT_MAILBOX),
    )
    attempt(
        "p2-action-mailbox-raw",
        lambda: clear_follower_action_mailbox(process, "p2"),
    )
    attempt(
        "p3-action-mailbox-raw",
        lambda: clear_follower_action_mailbox(process, "p3"),
    )
    attempt("p1-berserk", lambda: clear_p1_berserk_command(process))
    attempt(
        "p3-command-metadata",
        lambda: clear_third_player_command(process, world_epoch=world_epoch),
    )

    block = third_player_block()
    for label, address, payload in (
        ("p2-actor", COMMAND_BLOCK + 0x10, zero_u64),
        ("p2-legacy-actor", COMMAND_BLOCK + 0x18, zero_u64),
        ("p2-phys", COMMAND_BLOCK + 0x78, zero_u64),
        ("p2-magnitude", COMMAND_BLOCK + 0x04, struct.pack("<f", 0.0)),
        ("p3-active", block + P3_MOVEMENT_ACTIVE_OFFSET, zero_u32),
        ("p3-actor", block + P3_ACTOR_OFFSET, zero_u64),
        ("p3-phys", block + P3_PHYS_OFFSET, zero_u64),
        ("p3-magnitude", block + P3_MAGNITUDE_OFFSET, struct.pack("<f", 0.0)),
        (
            "p1-berserk-active",
            P1_BERSERK_MOVEMENT_STATE + P1_BERSERK_ACTIVE_OFFSET,
            zero_u32,
        ),
        (
            "p1-berserk-actor",
            P1_BERSERK_MOVEMENT_STATE + P1_BERSERK_ACTOR_OFFSET,
            zero_u64,
        ),
        (
            "p1-berserk-phys",
            P1_BERSERK_MOVEMENT_STATE + P1_BERSERK_PHYS_OFFSET,
            zero_u64,
        ),
        (
            "p1-berserk-interaction",
            P1_BERSERK_MOVEMENT_STATE + P1_BERSERK_INTERACTION_ENTRY_OFFSET,
            zero_u64,
        ),
        (
            "p1-berserk-skeleton",
            P1_BERSERK_MOVEMENT_STATE + P1_BERSERK_SKELETON_KEEP_OFFSET,
            zero_u64,
        ),
        ("manual-control", block + MANUAL_CONTROL_STATE_OFFSET, b"\x00" * 8),
        ("p2-loot", COMMAND_BLOCK + P2_LOOT_HANDLE_OFFSET, zero_u32),
        ("p3-loot", block + P3_LOOT_HANDLE_OFFSET, zero_u32),
        ("p2-interaction-owner", COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET, b"\x00"),
        ("p2-skeleton", COMMAND_BLOCK + P2_SKELETON_KEEP_OFFSET, zero_u64),
        ("p3-skeleton", block + P3_SKELETON_KEEP_OFFSET, zero_u64),
        (
            "external-camera-hold",
            block + EXTERNAL_CAMERA_HOLD_OFFSET,
            b"\x00" * EXTERNAL_CAMERA_HOLD_SIZE,
        ),
        (
            "camera-collision-exclusion",
            COMMAND_BLOCK + CAMERA_COLLISION_EXCLUSION_START_OFFSET,
            struct.pack("<2f", 0.0, 0.0),
        ),
        (
            "p1-p2-hud-slots",
            COMMAND_BLOCK + P1_HUD_PARTY_SLOT_OFFSET,
            struct.pack(
                "<2I",
                INVALID_HUD_PARTY_SLOT,
                INVALID_HUD_PARTY_SLOT,
            ),
        ),
        (
            "p3-hud-slot",
            block + P3_HUD_PARTY_SLOT_OFFSET,
            struct.pack("<I", INVALID_HUD_PARTY_SLOT),
        ),
        (
            "p2-interaction-entry",
            COMMAND_BLOCK + P2_INTERACTION_ENTRY_OFFSET,
            zero_u64,
        ),
        ("p3-interaction-entry", block + P3_INTERACTION_ENTRY_OFFSET, zero_u64),
        (
            "p3-interaction-scan-entry",
            block + P3_INTERACTION_SCAN_ENTRY_OFFSET,
            zero_u64,
        ),
    ):
        write(label, address, payload)

    attempt(
        "camera-collision-formation",
        lambda: publish_camera_collision_formation(
            process,
            (),
            (),
            camera_tuning,
            enabled=False,
        ),
    )
    attempt(
        "camera-collision-focus",
        lambda: publish_camera_collision_focus_escape(
            process,
            (),
            (),
            camera_tuning,
            enabled=False,
        ),
    )

    if bindings is not None:
        channel = getattr(bindings, "_controller_channel", None)
        close_channel = getattr(channel, "close", None)
        if callable(close_channel):
            attempt("controller-channel", close_channel)
    attempt("process-handle", process.close)
    if failures:
        try:
            print(
                "COOP_SHUTDOWN_PARTIAL " + ",".join(failures),
                file=sys.stderr,
                flush=True,
            )
        except BaseException:
            pass
    try:
        print("Co-op movement disabled; native keyboard restored.")
    except BaseException:
        pass


def movement_runtime_process_alive(process) -> bool:
    """Return whether the already-open game-process handle is still running."""

    handle = getattr(process, "handle", None)
    if not handle:
        return False
    exit_code = wt.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ct.byref(exit_code)):
        # The helper owns this process handle until shutdown.  A failed query
        # does not prove exit and must not combine with the original transient
        # frame fault to stop movement.  The subsequent read-only topology
        # validation still exposes an actually dead or invalid process.
        return True
    return exit_code.value == STILL_ACTIVE


def validate_movement_frame_recovery_topology(process) -> None:
    """Prove that an I/O fault did not come from detached native storage."""

    expected_root = b"\xe9" + rel32(MOVEMENT_HOOK_SITE, 5, MOVEMENT_CAVE)
    current_root = process.read(MOVEMENT_HOOK_SITE, len(expected_root))
    if current_root != expected_root:
        raise RuntimeError(
            "movement root changed during frame recovery; restart the game"
        )
    current_magic = process.read(CAVE_ALLOCATION_BASE, len(CAVE_MAGIC))
    if current_magic != CAVE_MAGIC:
        raise RuntimeError(
            "movement allocation changed during frame recovery; restart the game"
        )


def quarantine_movement_frame_publications(process) -> tuple[str, ...]:
    """Best-effort fail-closed publication reset after a transient frame fault.

    Each operation is independent.  A stale actor allocation may make one write
    fail while the fixed native mailboxes remain writable; one failure must not
    prevent the other players, camera and interaction routes from being disarmed.
    Executable roots and caves are deliberately absent from this list.
    """

    block = third_player_block()
    zero_u32 = struct.pack("<I", 0)
    zero_u64 = struct.pack("<Q", 0)
    operations = (
        (
            "movement-master",
            lambda: process.write(COMMAND_BLOCK + MOVEMENT_ACTIVE_OFFSET, zero_u32),
        ),
        (
            "logical-active",
            lambda: process.write(COMMAND_BLOCK + COOP_ACTIVE_OFFSET, zero_u32),
        ),
        (
            "camera-active",
            lambda: process.write(COMMAND_BLOCK + CAMERA_ACTIVE_OFFSET, zero_u32),
        ),
        (
            "interaction-active",
            lambda: process.write(
                COMMAND_BLOCK + INTERACTION_ROUTING_ACTIVE_OFFSET, zero_u32
            ),
        ),
        (
            "p1-actor",
            lambda: process.write(COMMAND_BLOCK + 0x18, zero_u64),
        ),
        ("p1-berserk", lambda: clear_p1_berserk_command(process)),
        (
            "p2-mailbox",
            lambda: movement_mailbox.reset_mailbox(process, P2_MOVEMENT_MAILBOX),
        ),
        (
            "p3-mailbox",
            lambda: movement_mailbox.reset_mailbox(process, P3_MOVEMENT_MAILBOX),
        ),
        ("p2-action", lambda: clear_follower_action_mailbox(process, "p2")),
        ("p3-action", lambda: clear_follower_action_mailbox(process, "p3")),
        (
            "p2-actor",
            lambda: process.write(COMMAND_BLOCK + 0x10, zero_u64),
        ),
        (
            "p2-physics",
            lambda: process.write(COMMAND_BLOCK + 0x78, zero_u64),
        ),
        (
            "p3-active",
            lambda: process.write(block + P3_MOVEMENT_ACTIVE_OFFSET, zero_u32),
        ),
        ("p3-actor", lambda: process.write(block + P3_ACTOR_OFFSET, zero_u64)),
        ("p3-physics", lambda: process.write(block + P3_PHYS_OFFSET, zero_u64)),
        (
            "p2-skeleton",
            lambda: process.write(COMMAND_BLOCK + P2_SKELETON_KEEP_OFFSET, zero_u64),
        ),
        (
            "p3-skeleton",
            lambda: process.write(block + P3_SKELETON_KEEP_OFFSET, zero_u64),
        ),
        (
            "p2-interaction",
            lambda: process.write(COMMAND_BLOCK + P2_INTERACTION_ENTRY_OFFSET, zero_u64),
        ),
        (
            "p3-interaction",
            lambda: process.write(block + P3_INTERACTION_ENTRY_OFFSET, zero_u64),
        ),
        (
            "p3-interaction-scan",
            lambda: process.write(block + P3_INTERACTION_SCAN_ENTRY_OFFSET, zero_u64),
        ),
        (
            "flee-request",
            lambda: process.write(
                P2_DEVICE_FILTER_STATE + COOP_FLEE_REQUEST_OFFSET, zero_u32
            ),
        ),
    )
    failures: list[str] = []
    for label, operation in operations:
        try:
            operation()
        except (OSError, struct.error):
            failures.append(label)
    return tuple(failures)


class MovementFrameIoRecovery:
    """Rate-limited, indefinitely retrying recovery for process-memory races."""

    def __init__(self, *, clock=None, sleeper=None) -> None:
        self.clock = time.monotonic if clock is None else clock
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.attempts = 0
        self.started_at: float | None = None
        self.next_log_at = 0.0
        self.last_signature: tuple[
            type[BaseException],
            int | None,
            type[BaseException] | None,
            int | None,
        ] | None = None

    @staticmethod
    def _signature(
        error: BaseException,
        topology_error: BaseException | None = None,
    ) -> tuple[
        type[BaseException],
        int | None,
        type[BaseException] | None,
        int | None,
    ]:
        return (
            type(error),
            getattr(error, "errno", None),
            type(topology_error) if topology_error is not None else None,
            getattr(topology_error, "errno", None),
        )

    def recover(self, process, error: BaseException) -> bool:
        if runtime_shutdown.requested(force=True):
            return False
        if not movement_runtime_process_alive(process):
            return False

        # A byte mismatch is structural and validate_* reports it as
        # RuntimeError.  Failure to *read* those bytes proves only that this
        # recovery sample is unavailable (the original frame already failed
        # for the same reason); it must not turn a loading race into worker
        # exit.  Do not write even fixed mailboxes until topology is readable.
        topology_error: BaseException | None = None
        try:
            validate_movement_frame_recovery_topology(process)
        except (OSError, struct.error) as probe_error:
            topology_error = probe_error
            failed_disarms = ("all:topology-unverified",)
        else:
            try:
                preserve_role_transfer = (
                    native_leader_role_transfer_recovery_required(process)
                )
            except (OSError, RuntimeError, struct.error, ValueError):
                preserve_role_transfer = True
            if preserve_role_transfer:
                failed_disarms = ("all:leader-role-transfer-preserved",)
            else:
                failed_disarms = quarantine_movement_frame_publications(process)

        now = float(self.clock())
        if self.started_at is None:
            self.started_at = now
        self.attempts += 1
        signature = self._signature(error, topology_error)
        if signature != self.last_signature or now >= self.next_log_at:
            failed = ",".join(failed_disarms) if failed_disarms else "none"
            topology = (
                "verified"
                if topology_error is None
                else f"unavailable:{type(topology_error).__name__}:{topology_error}"
            )
            print(
                "COOP_FRAME_IO_RETRY "
                f"attempt={self.attempts} error={type(error).__name__}: {error} "
                f"topology={topology} deferred_disarms={failed}",
                file=sys.stderr,
                flush=True,
            )
            self.last_signature = signature
            self.next_log_at = now + FRAME_IO_RETRY_LOG_INTERVAL_SECONDS

        exponent = min(max(self.attempts - 1, 0), 8)
        delay = min(
            POLL_SECONDS * (2**exponent),
            FRAME_IO_RETRY_MAX_BACKOFF_SECONDS,
        )
        self.sleeper(delay)
        return True

    def recovered(self) -> None:
        if self.started_at is None:
            return
        elapsed = max(0.0, float(self.clock()) - self.started_at)
        print(
            f"COOP_FRAME_IO_RECOVERED attempts={self.attempts} seconds={elapsed:.3f}",
            flush=True,
        )
        self.attempts = 0
        self.started_at = None
        self.next_log_at = 0.0
        self.last_signature = None


def _close_binding_transport(bindings) -> None:
    """Release the poller's named-mapping handle without touching SDL."""

    channel = getattr(bindings, "_controller_channel", None)
    close_channel = getattr(channel, "close", None)
    if callable(close_channel):
        close_channel()


def _initialize_movement_services():
    """Construct handle-owning frame services as one rollback boundary."""

    bindings = None
    try:
        shared_camera = SharedCameraController()
        internal_collision_latch = InternalCollisionLatchGuard(shared_camera.tuning)
        bindings = control_bindings.BindingPoller()
        manual_control_mask = configured_follower_control_mask(bindings)
        configured_control_mask = manual_control_mask
        session = session_state.SessionTracker()
        area_handoff = NativeAreaHandoffController()
        gateway_memory = area_transition.GatewayMemory(
            storage_path=area_transition.DEFAULT_GATEWAY_CACHE,
        )
        follower_transition = area_transition.FollowerTransitionController(
            watcher=area_transition.AreaTransitionWatcher(gateway_memory),
            requester=request_native_area_transition,
        )
        gateway_stall = FollowerGatewayStallDetector()
        ko_leader_ownership = NativeKoLeaderOwnershipController()
        party_hud_scaler = party_hud_layout.PartyHudLayoutScaler(
            settle_seconds=1.25,
        )
        handoff_state = follow_handoff.HandoffState()
        world_epochs = follow_handoff.WorldEpochTracker()
        actor_generations = follow_handoff.ActorGenerationTracker()
        frame_io_recovery = MovementFrameIoRecovery()
    except BaseException:
        # BindingPoller owns a named mapping handle.  A later constructor
        # failure happens before main's ordinary runtime-finally boundary.
        if bindings is not None:
            try:
                _close_binding_transport(bindings)
            except BaseException:
                pass
        raise
    return (
        shared_camera,
        internal_collision_latch,
        bindings,
        manual_control_mask,
        configured_control_mask,
        session,
        area_handoff,
        follower_transition,
        gateway_stall,
        ko_leader_ownership,
        party_hud_scaler,
        handoff_state,
        world_epochs,
        actor_generations,
        frame_io_recovery,
    )


def main():
    pid = find_pid()
    lease = helper_lease.HelperLease(pid, "movement")
    try:
        process = RemoteProcess(pid)
    except BaseException:
        try:
            lease.close()
        except BaseException:
            pass
        raise
    active = "--active" in sys.argv[1:]
    upstream_camera_v2 = "--camera-focus-v2" in sys.argv[1:]
    # ``None`` means there is no trustworthy prior sample yet.  A helper may
    # start/restart while F8 is held, and that inherited hold must never toggle
    # the whole co-op runtime.  The raw key is tracked even while the game is
    # in the background so returning focus cannot manufacture an edge either.
    previous_f8: bool | None = None
    manual_control_mask = 0
    p2_walk_mode = False
    p3_walk_mode = False
    last_resolve = 0.0
    p1_actor = 0
    p1_phys = 0
    p1_skeleton_keep = 0
    configured_p1_actor = 0
    p2_actor = 0
    p3_actor = 0
    last_p1_berserk_heading = 0.0
    p1_berserk_visual_heading = 0.0
    last_p1_berserk_movement_owned = False
    last_third_player_heading = 0.0
    third_player_visual_heading = 0.0
    p2_phys = 0
    p3_phys = 0
    actor_by_handle = {}
    last_reported_actor = None
    last_lifecycle = None
    published_device_mask = None
    published_native_controller_slot = None
    p1_sdl_bridge_enabled = False
    p1_sdl_gamepad = None
    p1_sdl_packet = 0
    p1_sdl_active_buffer = 0
    published_flee_state = None
    published_lifecycle_flags = None
    published_keyboard_filter_active = None
    published_follower_movement_active = None
    published_role_metadata = None
    liveness_sequence = 0
    next_liveness_at = 0.0
    last_heading = 0.0
    visual_heading = 0.0
    camera_basis = ((0.0, 1.0), (-1.0, 0.0))
    camera_needs_party_centre_seed = True
    last_camera_requested = False
    camera_reacquire_pending = False
    camera_native_ready_samples = 0
    last_camera_feedback_signature = None
    previous_native_camera_yaw = None
    manual_camera_orbit_hold = 0.0
    camera_collision_direction = None
    collision_exclusion = (0.0, 0.0)
    camera_collision_published = False
    camera_goal_publisher = camera_frame_sync.CameraGoalPublisher()
    last_camera_present_tick_us = None
    last_party_hud_layout_report = None
    session_snapshot = None
    party_actor_slots = {}
    try:
        (
            shared_camera,
            internal_collision_latch,
            bindings,
            manual_control_mask,
            configured_control_mask,
            session,
            area_handoff,
            follower_transition,
            gateway_stall,
            ko_leader_ownership,
            party_hud_scaler,
            handoff_state,
            world_epochs,
            actor_generations,
            frame_io_recovery,
        ) = _initialize_movement_services()
    except BaseException:
        try:
            process.close()
        except BaseException:
            pass
        try:
            lease.close()
        except BaseException:
            pass
        raise
    world_epoch = 0
    native_sample = 0
    leader_role_transfer = None
    leader_role_cancel_tracker = NativeLeaderRolePrepareCancellationTracker()
    leader_role_target_cancel_tracker = NativeLeaderRolePrepareCancellationTracker()

    try:
        resolved_identity = None
        resolved_p1_owner_member_id = None
        party_actors = []
        last_session_signature = None
        previous_tick = time.monotonic()

        install_runtime(
            process,
            pid=pid,
            active=active,
            upstream_camera_v2=upstream_camera_v2,
            initial_manual_control_mask=manual_control_mask,
            preserve_dynamic_state_on_reuse=(
                "--preserve-resident-state" in sys.argv[1:]
            ),
        )
        startup_keyboard_filter = native_desktop_filter_required(
            active,
            bindings.desktop_owner(),
        )
        if startup_keyboard_filter:
            process.write(
                COMMAND_BLOCK + KEYBOARD_FILTER_ACTIVE_OFFSET,
                struct.pack("<I", 1),
            )
        published_keyboard_filter_active = startup_keyboard_filter
        liveness_sequence = publish_movement_helper_liveness(
            process,
            liveness_sequence,
            owner_pid=os.getpid(),
        )
        # Admission opens only after native installation and the first durable
        # movement heartbeat.  A pending authenticated request may now resume.
        _write_native_leader_role_word_confirmed(
            process,
            NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
            0,
            label="movement ready role admission open",
        )
        next_liveness_at = (
            time.monotonic() + MOVEMENT_HELPER_LIVENESS_HEARTBEAT_SECONDS
        )
        # Corridor hysteresis must survive between frames.
        camera_corridor_active = False

        while True:
            try:
                if runtime_shutdown.requested():
                    break
                now = time.monotonic()
                # Keep the native keyboard fail-open lease and the helper
                # heartbeat alive even while an atomic role transfer freezes
                # every ordinary ownership/HUD writer below.  These are
                # independent liveness publications and therefore belong
                # before every recovery/settle early-continue boundary.
                process.write(
                    KEYBOARD_FILTER_BUDGET,
                    struct.pack("<I", KEYBOARD_FILTER_BUDGET_REFILL),
                )
                if now >= next_liveness_at:
                    liveness_sequence = publish_movement_helper_liveness(
                        process,
                        liveness_sequence,
                        owner_pid=os.getpid(),
                        heartbeat_ms=max(1, int(now * 1000.0)),
                    )
                    next_liveness_at = (
                        now + MOVEMENT_HELPER_LIVENESS_HEARTBEAT_SECONDS
                    )
                leader_role_transfer = read_native_leader_role_transfer(process)
                recovery_required = native_leader_role_transfer_recovery_required(
                    process
                )
                if leader_role_transfer is None and recovery_required:
                    recover_native_leader_role_transfer_for_restart(process)
                    leader_role_transfer = read_native_leader_role_transfer(process)
                    if leader_role_transfer is None:
                        # A role transaction marker is an ownership lock.  A
                        # torn sample may become coherent on the next flip; it
                        # must never authorize an ordinary stale-state frame.
                        time.sleep(POLL_SECONDS)
                        continue
                p2_publisher = movement_mailbox.publisher_for(
                    process, P2_MOVEMENT_MAILBOX
                )
                p3_publisher = movement_mailbox.publisher_for(
                    process, P3_MOVEMENT_MAILBOX
                )
                transfer_mailbox_active = leader_role_transfer is not None
                if not transfer_mailbox_active and (
                    p2_publisher.reserved is not None
                    or p3_publisher.reserved is not None
                ):
                    # Compatibility recovery for a helper restarted from an
                    # older role-transfer build.
                    leader_role_transfer = read_native_leader_role_transfer(
                        process, include_inactive=True
                    )
                    transfer_mailbox_active = leader_role_transfer is not None
                if transfer_mailbox_active:
                    if leader_role_transfer.native_applied:
                        if not settle_native_leader_role_transfer_publishers(
                            process, leader_role_transfer
                        ):
                            # APPLIED is irreversible, but a transient read can
                            # delay adoption of the exact even mailbox words.
                            # No ordinary publisher/reconcile may run first.
                            time.sleep(POLL_SECONDS)
                            continue
                    if (
                        not leader_role_transfer.prepared
                        or leader_role_transfer.native_applied
                    ):
                        last_resolve = 0.0
                elif leader_role_transfer is not None:
                    compatibility_settled = settle_native_leader_role_transfer_publishers(
                        process, leader_role_transfer
                    )
                    if (
                        leader_role_transfer.native_applied
                        and not compatibility_settled
                    ):
                        time.sleep(POLL_SECONDS)
                        continue
                elif (
                    p2_publisher.reserved is not None
                    or p3_publisher.reserved is not None
                ):
                    settle_native_leader_role_transfer_publishers(process, None)
                if (
                    transfer_mailbox_active
                    and leader_role_transfer.prepared
                    and not leader_role_transfer.native_applied
                ):
                    # The game thread may cross the native commit immediately
                    # after our sample.  Freeze this Python frame completely so
                    # no stale resolver/HUD/disarm publication can follow it.
                    time.sleep(POLL_SECONDS)
                    continue
                if (
                    transfer_mailbox_active
                    and not leader_role_transfer.prepared
                    and not leader_role_transfer.native_applied
                ):
                    # Inspect the roster before area/input/resolver/HUD/KO can
                    # publish anything.  From the first coherent mismatch we
                    # preserve the last committed frame while debouncing; the
                    # third identical sample owns a safe pre-PREPARED cancel.
                    # This intentionally ignores identity-relative ``loading``
                    # so removal of the old P1 cannot deadlock the transaction.
                    structural_snapshot = session.snapshot(
                        process,
                        materialized_party_count=None,
                    )
                    cancel_ready = leader_role_cancel_tracker.observe(
                        leader_role_transfer,
                        structural_snapshot,
                    )
                    if leader_role_cancel_tracker.samples:
                        if cancel_ready:
                            cancel_native_leader_role_transfer_before_prepared(
                                process,
                                leader_role_transfer,
                            )
                            leader_role_cancel_tracker.reset()
                        time.sleep(POLL_SECONDS)
                        continue
                else:
                    leader_role_cancel_tracker.reset()
                delta_time = min(max(now - previous_tick, 0.0), 0.050)
                previous_tick = now

                area_handoff.tick(process, now)
                foreground = process_is_foreground(pid)
                # Controllers remain live while the game is in the background.  The
                # keyboard half of the same binding table remains focus-bound, just
                # like FF12's untouched native mouse/keyboard path.
                bindings.begin_frame(True, keyboard_enabled=foreground)
                current_configured_control_mask = configured_follower_control_mask(bindings)
                reconciled_manual_control_mask = reconcile_follower_control_sources(
                    manual_control_mask,
                    configured_control_mask,
                    current_configured_control_mask,
                )
                if reconciled_manual_control_mask != manual_control_mask:
                    removed_manual_control = (
                        manual_control_mask & ~reconciled_manual_control_mask
                    )
                    manual_control_mask = reconciled_manual_control_mask
                    publish_manual_control_mask(process, manual_control_mask)
                    if removed_manual_control & P2_MANUAL_CONTROL_BIT:
                        disarm_second_player_command(process, world_epoch=world_epoch)
                    if removed_manual_control & P3_MANUAL_CONTROL_BIT:
                        disarm_third_player_command(process, world_epoch=world_epoch)
                    shared_camera.reset()
                    camera_needs_party_centre_seed = True
                    print(
                        "FOLLOWER_INPUT_SOURCES "
                        f"configured={current_configured_control_mask:#x} "
                        f"manual={manual_control_mask:#x}",
                        flush=True,
                    )
                configured_control_mask = current_configured_control_mask
                keyboard_filter_active = native_desktop_filter_required(
                    active,
                    bindings.desktop_owner(),
                )
                if keyboard_filter_active != published_keyboard_filter_active:
                    process.write(
                        COMMAND_BLOCK + KEYBOARD_FILTER_ACTIVE_OFFSET,
                        struct.pack("<I", int(keyboard_filter_active)),
                    )
                    published_keyboard_filter_active = keyboard_filter_active
                current_p1_sdl_gamepad = bindings.sdl_xinput_gamepad("p1")
                if current_p1_sdl_gamepad is not None:
                    if (
                        not p1_sdl_bridge_enabled
                        or current_p1_sdl_gamepad != p1_sdl_gamepad
                    ):
                        p1_sdl_packet = (p1_sdl_packet + 1) & 0xFFFFFFFF
                        if p1_sdl_packet == 0:
                            p1_sdl_packet = 1
                        p1_sdl_active_buffer = publish_p1_sdl_xinput_state(
                            process,
                            current_p1_sdl_gamepad,
                            p1_sdl_packet,
                            p1_sdl_active_buffer,
                        )
                        p1_sdl_gamepad = current_p1_sdl_gamepad
                    p1_sdl_bridge_enabled = True
                elif p1_sdl_bridge_enabled:
                    disable_p1_sdl_xinput(process)
                    p1_sdl_bridge_enabled = False
                    p1_sdl_gamepad = None
                p2_control_toggle = bindings.edge("p2", "toggle_control")
                p3_control_toggle = bindings.edge("p3", "toggle_control")
                if active and p2_control_toggle:
                    manual_control_mask ^= P2_MANUAL_CONTROL_BIT
                    publish_manual_control_mask(process, manual_control_mask)
                    if not manual_control_mask & P2_MANUAL_CONTROL_BIT:
                        disarm_second_player_command(process, world_epoch=world_epoch)
                    shared_camera.reset()
                    camera_needs_party_centre_seed = True
                    print(
                        "P2 manual control",
                        "ON" if manual_control_mask & P2_MANUAL_CONTROL_BIT else "OFF",
                        flush=True,
                    )
                if active and p3_control_toggle:
                    manual_control_mask ^= P3_MANUAL_CONTROL_BIT
                    publish_manual_control_mask(process, manual_control_mask)
                    if not manual_control_mask & P3_MANUAL_CONTROL_BIT:
                        disarm_third_player_command(process, world_epoch=world_epoch)
                    shared_camera.reset()
                    camera_needs_party_centre_seed = True
                    print(
                        "P3 manual control",
                        "ON" if manual_control_mask & P3_MANUAL_CONTROL_BIT else "OFF",
                        flush=True,
                    )
                p2_manual_control = bool(manual_control_mask & P2_MANUAL_CONTROL_BIT)
                p3_manual_control = bool(manual_control_mask & P3_MANUAL_CONTROL_BIT)
                assigned_p1_device = bindings.effective_gamepad_assignment("p1")
                native_p1_slot = native_controller_slot(assigned_p1_device)
                if native_p1_slot != published_native_controller_slot:
                    publish_native_controller_slot(process, assigned_p1_device)
                    published_native_controller_slot = native_p1_slot
                p1_uses_sdl = isinstance(assigned_p1_device, dict)
                isolated_coop_pads = (
                    native_controller_isolation_indices(assigned_p1_device)
                    if active or p1_uses_sdl
                    else ()
                )
                device_mask = controller_isolation_mask(isolated_coop_pads)
                if device_mask != published_device_mask:
                    publish_coop_device_isolation(process, isolated_coop_pads)
                    published_device_mask = device_mask
                flee_requested = bool(
                    active
                    and (
                        (p2_manual_control and bindings.down("p2", "flee"))
                        or (p3_manual_control and bindings.down("p3", "flee"))
                    )
                )
                flee_state = (
                    int(flee_requested),
                    int(active and bindings.xinput_control_down("p1", "RT")),
                    int(active),
                )
                if flee_state != published_flee_state:
                    process.write(
                        P2_DEVICE_FILTER_STATE + COOP_FLEE_REQUEST_OFFSET,
                        struct.pack("<III", *flee_state),
                    )
                    published_flee_state = flee_state

                f8 = key_down(VK_F8)
                p2_run_toggle = bindings.edge("p2", "run_toggle")
                p3_run_toggle = bindings.edge("p3", "run_toggle")
                if startup_safe_foreground_edge(f8, previous_f8, foreground):
                    active = not active
                    toggled_keyboard_filter = native_desktop_filter_required(
                        active,
                        bindings.desktop_owner(),
                    )
                    if toggled_keyboard_filter != published_keyboard_filter_active:
                        process.write(
                            COMMAND_BLOCK + KEYBOARD_FILTER_ACTIVE_OFFSET,
                            struct.pack("<I", int(toggled_keyboard_filter)),
                        )
                        published_keyboard_filter_active = toggled_keyboard_filter
                    if active and p2_phys:
                        try:
                            visual_heading = process.f32(p2_phys + 0xA8)
                            last_heading = visual_heading
                        except OSError:
                            pass
                    elif not active:
                        shared_camera.reset()
                        clear_second_player_command(
                            process,
                            world_epoch=world_epoch,
                        )
                        clear_p1_berserk_command(process)
                        clear_third_player_command(
                            process,
                            world_epoch=world_epoch,
                        )
                        publish_follower_movement_master(process, False)
                        published_follower_movement_active = False
                        publish_lifecycle_flags(process, False, False, False)
                        published_lifecycle_flags = (False, False, False)
                    print("P2 WASD", "ON" if active else "OFF")
                if active and p2_manual_control and p2_run_toggle:
                    p2_walk_mode = not p2_walk_mode
                    print("P2 movement", "WALK" if p2_walk_mode else "RUN")
                if active and p3_manual_control and p3_run_toggle:
                    p3_walk_mode = not p3_walk_mode
                    print("P3 movement", "WALK" if p3_walk_mode else "RUN")
                previous_f8 = f8

                if now - last_resolve >= RESOLVE_SECONDS:
                    actors = resolve_active_actors(process)
                    party_actor_slots = resolve_active_party_actor_slots(process, actors)
                    party_actors = list(party_actor_slots.values())

                    actor_by_handle = {}
                    for _actor_id, actor, _phys, _pos in actors:
                        try:
                            actor_by_handle[process.u32(actor + 0x08)] = actor
                        except OSError:
                            continue
                    previous_p1_actor = p1_actor
                    previous_p1_phys = p1_phys
                    session_snapshot = session.snapshot(
                        process,
                        materialized_party_count=len(party_actors),
                    )
                    session_snapshot = snapshot_with_native_leader_role_transfer(
                        session_snapshot,
                        leader_role_transfer,
                    )
                    if (
                        leader_role_transfer is not None
                        and leader_role_transfer.native_applied
                        and session_snapshot.identity
                        != leader_role_transfer.identity
                    ):
                        # APPLIED is irreversible.  Loading or a torn layout may
                        # temporarily prevent authentication of its role
                        # permutation; freeze instead of publishing old roles.
                        time.sleep(POLL_SECONDS)
                        continue
                    role_actors = session_state.map_party_slot_actors(
                        session_snapshot, party_actor_slots
                    )
                    p1_owner_member_id = session_snapshot.p1_owner_party_member_id
                    p1_entry = role_actors.get(p1_owner_member_id)
                    configured_p1_entry = role_actors.get(
                        session_snapshot.identity.p1_party_member_id
                    )
                    p2_entry = role_actors.get(session_snapshot.identity.p2_party_member_id)
                    p3_entry = (
                        role_actors.get(session_snapshot.identity.p3_party_member_id)
                        if session_snapshot.identity.p3_party_member_id is not None
                        else None
                    )

                    current_identity = session_snapshot.identity
                    current_layout = session_snapshot.current_layout
                    ui_layout = session_snapshot.ui_layout
                    current_member_ids = {
                        member_id
                        for member_id in current_layout[:4]
                        if member_id != session_state.EMPTY_PARTY_ID
                    }
                    p1_removed = p1_owner_member_id not in current_member_ids
                    configured_p1_removed = (
                        current_identity.p1_party_member_id not in current_member_ids
                    )
                    p2_removed = (
                        current_identity.p2_party_member_id not in current_member_ids
                    )
                    p3_removed = bool(
                        current_identity.p3_party_member_id is None
                        or current_identity.p3_party_member_id not in current_member_ids
                    )
                    p1_layout_coherent = role_member_layout_coherent(
                        p1_owner_member_id,
                        current_layout,
                        ui_layout,
                    )
                    configured_p1_layout_coherent = role_member_layout_coherent(
                        current_identity.p1_party_member_id,
                        current_layout,
                        ui_layout,
                    )
                    p2_layout_coherent = role_member_layout_coherent(
                        current_identity.p2_party_member_id,
                        current_layout,
                        ui_layout,
                    )
                    p3_layout_coherent = role_member_layout_coherent(
                        current_identity.p3_party_member_id,
                        current_layout,
                        ui_layout,
                    )

                    p1_owner_changed = resolved_p1_owner_member_id != p1_owner_member_id
                    seamless_leader_commit = bool(
                        leader_role_transfer is not None
                        and leader_role_transfer.native_applied
                        and session_snapshot.identity == leader_role_transfer.identity
                    )
                    if p1_owner_changed and not seamless_leader_commit:
                        p1_actor = 0
                        p1_phys = 0
                        process.write(COMMAND_BLOCK + 0x18, struct.pack("<Q", 0))
                        process.write(
                            COMMAND_BLOCK + P1_HUD_PARTY_SLOT_OFFSET,
                            struct.pack("<I", INVALID_HUD_PARTY_SLOT),
                        )
                        shared_camera.reset()
                        camera_needs_party_centre_seed = True

                    if p1_entry is not None and p1_layout_coherent:
                        p1_actor, p1_phys = fresh_role_actor_mapping(p1_entry)
                        p1_skeleton_keep = resolve_skeleton_keep(process, p1_entry[0])
                    else:
                        if p1_removed:
                            p1_actor = 0
                            p1_phys = 0
                            p1_skeleton_keep = 0
                        elif not p1_layout_coherent:
                            p1_actor = 0
                            p1_phys = 0
                            p1_skeleton_keep = 0
                        elif not role_cache_survives_scan_gap(
                            member_changed=p1_owner_changed,
                            member_id=p1_owner_member_id,
                            current_layout=current_layout,
                            ui_layout=ui_layout,
                            actor_validity=party_actor_validity(process, p1_actor),
                        ):
                            p1_actor = 0
                            p1_phys = 0
                            p1_skeleton_keep = 0

                    if configured_p1_entry is not None and configured_p1_layout_coherent:
                        configured_p1_actor, _configured_p1_phys = fresh_role_actor_mapping(
                            configured_p1_entry
                        )
                    else:
                        if configured_p1_removed or not configured_p1_layout_coherent:
                            configured_p1_actor = 0
                        elif not role_cache_survives_scan_gap(
                            member_changed=role_member_changed(
                                resolved_identity, current_identity, "p1"
                            ),
                            member_id=current_identity.p1_party_member_id,
                            current_layout=current_layout,
                            ui_layout=ui_layout,
                            actor_validity=party_actor_validity(
                                process, configured_p1_actor
                            ),
                        ):
                            configured_p1_actor = 0

                    if p2_entry is not None and p2_layout_coherent:
                        p2_actor, p2_phys = fresh_role_actor_mapping(p2_entry)
                    else:
                        if p2_removed:
                            p2_actor = 0
                            p2_phys = 0
                        elif not p2_layout_coherent:
                            p2_actor = 0
                            p2_phys = 0
                        elif not role_cache_survives_scan_gap(
                            member_changed=role_member_changed(
                                resolved_identity, current_identity, "p2"
                            ),
                            member_id=current_identity.p2_party_member_id,
                            current_layout=current_layout,
                            ui_layout=ui_layout,
                            actor_validity=party_actor_validity(process, p2_actor),
                        ):
                            p2_actor = 0
                            p2_phys = 0

                    if p3_entry is not None and p3_layout_coherent:
                        p3_actor, p3_phys = fresh_role_actor_mapping(p3_entry)
                    else:
                        if p3_removed:
                            p3_actor = 0
                            p3_phys = 0
                        elif not p3_layout_coherent:
                            p3_actor = 0
                            p3_phys = 0
                        elif not role_cache_survives_scan_gap(
                            member_changed=role_member_changed(
                                resolved_identity, current_identity, "p3"
                            ),
                            member_id=current_identity.p3_party_member_id,
                            current_layout=current_layout,
                            ui_layout=ui_layout,
                            actor_validity=party_actor_validity(process, p3_actor),
                        ):
                            p3_actor = 0
                            p3_phys = 0
                    resolved_identity = session_snapshot.identity
                    resolved_p1_owner_member_id = p1_owner_member_id
                    process.write(COMMAND_BLOCK + 0x18, struct.pack("<Q", p1_actor))
                    p1_hud_slot = session_state.party_slot_for_member(
                        session_snapshot,
                        p1_owner_member_id,
                    )
                    process.write(
                        COMMAND_BLOCK + P1_HUD_PARTY_SLOT_OFFSET,
                        struct.pack(
                            "<I",
                            INVALID_HUD_PARTY_SLOT if p1_hud_slot is None else p1_hud_slot,
                        ),
                    )
                    if (
                        active
                        and p1_phys
                        and (not previous_p1_phys or p1_actor != previous_p1_actor)
                    ):
                        try:
                            p1_berserk_visual_heading = process.f32(p1_phys + 0xA8)
                            last_p1_berserk_heading = p1_berserk_visual_heading
                        except OSError:
                            pass
                    last_resolve = now
                    if p2_actor != last_reported_actor:
                        summary = ", ".join(
                            f"id={actor_id}:actor={actor:#x}"
                            for actor_id, actor, _phys, _pos in party_actors
                        )
                        print("Active party actors:", summary or "none")
                        print("Resolved P2:", f"{p2_actor:#x}" if p2_actor else "none")
                        last_reported_actor = p2_actor

                session_snapshot = session.snapshot(
                    process,
                    materialized_party_count=len(party_actors),
                )
                session_snapshot = snapshot_with_native_leader_role_transfer(
                    session_snapshot,
                    leader_role_transfer,
                )
                if (
                    leader_role_transfer is not None
                    and leader_role_transfer.native_applied
                    and session_snapshot.identity != leader_role_transfer.identity
                ):
                    time.sleep(POLL_SECONDS)
                    continue

                if not session_snapshot.loading and p1_actor and p1_phys:
                    try:
                        party_hud_layout_result = party_hud_scaler.tick(process, now)
                    except (OSError, RuntimeError, ValueError) as exc:
                        party_hud_layout_result = f"error:{exc}"
                    if party_hud_layout_result != last_party_hud_layout_report:
                        if party_hud_layout_result in ("applied", "reduced"):
                            print(
                                "PARTY_HUD_SCALE reduced=75% anchor=bottom-right",
                                flush=True,
                            )

                            last_party_hud_layout_report = "reduced"
                        elif party_hud_layout_result == "foreign":
                            print(
                                "PARTY_HUD_SCALE skipped: overlapping MRP layout mod",
                                flush=True,
                            )
                            last_party_hud_layout_report = party_hud_layout_result
                        elif party_hud_layout_result.startswith("error:"):
                            print(
                                "PARTY_HUD_SCALE skipped:",
                                party_hud_layout_result.removeprefix("error:"),
                                flush=True,
                            )
                            last_party_hud_layout_report = party_hud_layout_result
                        else:
                            last_party_hud_layout_report = party_hud_layout_result
                identity_current = bool(
                    resolved_identity is not None
                    and resolved_identity == session_snapshot.identity
                )
                p1_owner_current = bool(
                    resolved_p1_owner_member_id == session_snapshot.p1_owner_party_member_id
                )
                p2_identity_current = bool(
                    resolved_identity is not None
                    and not role_member_changed(
                        resolved_identity, session_snapshot.identity, "p2"
                    )
                )
                p3_identity_current = bool(
                    resolved_identity is not None
                    and not role_member_changed(
                        resolved_identity, session_snapshot.identity, "p3"
                    )
                )
                if not identity_current or not p1_owner_current:
                    last_resolve = 0.0
                    if not p1_owner_current and not (
                        leader_role_transfer is not None
                        and leader_role_transfer.native_applied
                        and session_snapshot.identity == leader_role_transfer.identity
                    ):
                        p1_actor = 0
                        p1_phys = 0
                        process.write(COMMAND_BLOCK + 0x18, struct.pack("<Q", 0))
                        p1_skeleton_keep = 0
                        process.write(
                            COMMAND_BLOCK + P1_HUD_PARTY_SLOT_OFFSET,
                            struct.pack("<I", INVALID_HUD_PARTY_SLOT),
                        )
                        shared_camera.reset()
                        camera_needs_party_centre_seed = True
                session_signature = (
                    session_snapshot.loading,
                    session_snapshot.cutscene,
                    session_snapshot.town,
                    session_snapshot.native_menu_open,
                    session_snapshot.leader_party_member_id,
                    session_snapshot.p1_owner_party_member_id,
                    session_snapshot.identity,
                )
                if session_signature != last_session_signature:
                    print(
                        "COOP_SESSION "
                        f"reason={session_snapshot.reason} "
                        f"leader={session_snapshot.leader_party_member_id} "
                        f"p1={session_snapshot.identity.p1_party_member_id} "
                        f"p1_owner={session_snapshot.p1_owner_party_member_id} "
                        f"p2={session_snapshot.identity.p2_party_member_id}",
                        flush=True,
                    )
                    last_session_signature = session_signature

                p1_alive = actor_is_alive(process, p1_actor)
                configured_p1_leader_available = actor_available_for_native_leader(
                    process, configured_p1_actor
                )
                p2_alive = actor_is_alive(process, p2_actor)
                p3_alive = actor_is_alive(process, p3_actor)
                p2_leader_available = actor_available_for_ko_leader(
                    process, p2_actor
                )
                p3_leader_available = actor_available_for_ko_leader(
                    process, p3_actor
                )
                configured_p1_layout_coherent = role_member_layout_coherent(
                    session_snapshot.identity.p1_party_member_id,
                    session_snapshot.current_layout,
                    session_snapshot.ui_layout,
                )
                p2_layout_coherent = role_member_layout_coherent(
                    session_snapshot.identity.p2_party_member_id,
                    session_snapshot.current_layout,
                    session_snapshot.ui_layout,
                )
                p3_layout_coherent = role_member_layout_coherent(
                    session_snapshot.identity.p3_party_member_id,
                    session_snapshot.current_layout,
                    session_snapshot.ui_layout,
                )
                native_leader_member_id = session_snapshot.leader_party_member_id
                native_leader_is_known_guest = bool(
                    native_leader_member_id in session_state.PARTY_NAMES
                    and native_leader_member_id not in session_state.MAIN_PARTY_MEMBER_IDS
                    and role_member_layout_coherent(
                        native_leader_member_id,
                        session_snapshot.current_layout,
                        session_snapshot.ui_layout,
                    )
                )
                summon_owner_role = session_state.active_summon_owner_role(
                    session_snapshot.identity,
                    session_snapshot.current_layout,
                    session_snapshot.ui_layout,
                    native_leader_member_id,
                )
                if summon_owner_role in ("p2", "p3"):
                    # An Esper temporarily removes the other main actors from
                    # Current/UI. The summoner keeps the same player role even
                    # though that actor is also the native party leader.
                    ko_promoted_role = summon_owner_role
                else:
                    ko_promoted_role = ko_leader_ownership.tick(
                        process,
                        session_snapshot.identity,
                        native_leader_member_id,
                        configured_p1_leader_available,
                        p1_present=configured_p1_layout_coherent,
                        p2_available=(
                            p2_leader_available
                            if p2_actor and p2_layout_coherent
                            else False
                        ),
                        p3_available=(
                            p3_leader_available
                            if p3_actor and p3_layout_coherent
                            else False
                        ),
                        now=now,
                        transient_area_handoff=area_handoff.active,
                        native_leader_is_known_guest=(native_leader_is_known_guest),
                    )
                logical_active, lifecycle_movement_active, shared_camera_active = (
                    coop_lifecycle_flags(
                        active,
                        p1_alive,
                        p2_alive if p2_manual_control else False,
                        p3_alive if p3_manual_control else False,
                        p2_present=bool(p2_actor and p2_manual_control),
                        p3_present=bool(p3_actor and p3_manual_control),
                    )
                )
                custom_wait_movement = custom_wait_shadow_preserves_movement(process)
                battle_mode_active = bool(
                    custom_wait_movement and native_battle_mode_is_active(process)
                )
                session_movement_allowed = follower_session_movement_allowed(
                    session_snapshot,
                    p2_present=bool(p2_actor and p2_manual_control),
                    p3_present=bool(p3_actor and p3_manual_control),
                )
                base_movement_active, base_movement_input_allowed = (
                    coop_movement_control_flags(
                        lifecycle_movement_active,
                        session_movement_allowed,
                        custom_wait_movement,
                        battle_mode_active,
                    )
                )
                p1_base_movement_active, p1_base_movement_input_allowed = (
                    coop_movement_control_flags(
                        logical_active,
                        p1_session_movement_allowed(session_snapshot),
                        custom_wait_movement,
                        battle_mode_active,
                    )
                )
                p2_movement_owned = follower_role_owns_movement(
                    base_movement_active and p2_manual_control,
                    p2_alive,
                    session_snapshot.leader_party_member_id,
                    session_snapshot.identity.p2_party_member_id,
                    actor_present=bool(p2_actor),
                    identity_current=p2_identity_current,
                    layout_coherent=p2_layout_coherent,
                    allow_native_leader=ko_promoted_role == "p2",
                )
                p3_movement_owned = follower_role_owns_movement(
                    base_movement_active and p3_manual_control,
                    p3_alive,
                    session_snapshot.leader_party_member_id,
                    session_snapshot.identity.p3_party_member_id,
                    actor_present=bool(p3_actor),
                    identity_current=p3_identity_current,
                    layout_coherent=p3_layout_coherent,
                    allow_native_leader=ko_promoted_role == "p3",
                )

                native_sample += 1
                world_signature = session_snapshot.coherent_world_signature
                world_valid = bool(
                    world_signature is not None
                    and not session_snapshot.loading
                    and not session_snapshot.cutscene
                )
                world_epoch = world_epochs.observe(
                    world_signature,
                    valid=world_valid,
                )
                if (
                    leader_role_transfer is not None
                    and not leader_role_transfer.native_applied
                ):
                    target_unavailable = (
                        native_leader_role_target_unavailable_signature(
                            process,
                            leader_role_transfer,
                            session_snapshot,
                            party_actor_slots,
                        )
                    )
                    cancel_ready = (
                        leader_role_target_cancel_tracker.observe_signature(
                            target_unavailable
                        )
                    )
                    if leader_role_target_cancel_tracker.samples:
                        if cancel_ready:
                            cancel_native_leader_role_transfer_before_prepared(
                                process,
                                leader_role_transfer,
                            )
                            leader_role_target_cancel_tracker.reset()
                        time.sleep(POLL_SECONDS)
                        continue
                    transfer_prepared = prepare_native_leader_role_transfer(
                        process,
                        leader_role_transfer,
                        session_snapshot,
                        party_actor_slots,
                        actor_generations,
                        world_epoch=world_epoch,
                        manual_control_mask=manual_control_mask,
                    )
                    refreshed_transfer = read_native_leader_role_transfer(process)
                    claimed_sequence = process.u32(
                        P2_DEVICE_FILTER_STATE
                        + NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
                    )
                    if native_leader_role_transfer_write_authority_crossed(
                        refreshed_transfer,
                        prepare_reported=transfer_prepared,
                        claimed_sequence=claimed_sequence,
                    ):
                        # PREPARED is the hand-off of write authority to the
                        # game thread. End the frame even if APPLIED raced the
                        # reread; adoption starts from a fresh snapshot only.
                        time.sleep(POLL_SECONDS)
                        continue
                    leader_role_transfer = refreshed_transfer
                else:
                    leader_role_target_cancel_tracker.reset()

                handoff_role_actors = session_state.map_party_slot_actors(
                    session_snapshot,
                    party_actor_slots,
                )
                p2_member_id = session_snapshot.identity.p2_party_member_id
                p2_handoff_entry = handoff_role_actors.get(p2_member_id)
                p2_handoff_actor, p2_handoff_phys = fresh_role_actor_mapping(
                    p2_handoff_entry
                )
                p2_handoff_handle = actor_u32_or_zero(
                    process,
                    p2_handoff_actor,
                    0x08,
                )
                p2_handoff_alive = actor_is_alive(process, p2_handoff_actor)
                p2_token = (
                    actor_generations.observe(
                        "p2",
                        p2_member_id,
                        p2_handoff_actor,
                        p2_handoff_phys,
                        native_handle=p2_handoff_handle,
                        world_epoch=world_epoch,
                        alive=p2_handoff_alive,
                        structurally_present=p2_layout_coherent,
                    )
                    if world_epoch
                    else None
                )
                p3_member_id = session_snapshot.identity.p3_party_member_id
                p3_handoff_entry = handoff_role_actors.get(p3_member_id)
                p3_handoff_actor, p3_handoff_phys = fresh_role_actor_mapping(
                    p3_handoff_entry
                )
                p3_handoff_handle = actor_u32_or_zero(
                    process,
                    p3_handoff_actor,
                    0x08,
                )
                p3_handoff_alive = actor_is_alive(process, p3_handoff_actor)
                p3_token = (
                    actor_generations.observe(
                        "p3",
                        p3_member_id,
                        p3_handoff_actor,
                        p3_handoff_phys,
                        native_handle=p3_handoff_handle,
                        world_epoch=world_epoch,
                        alive=p3_handoff_alive,
                        structurally_present=p3_layout_coherent,
                    )
                    if p3_member_id is not None and world_epoch
                    else None
                )

                seamless_role_permutation = bool(
                    leader_role_transfer is not None
                    and leader_role_transfer.native_applied
                    and session_snapshot.identity == leader_role_transfer.identity
                    and leader_role_transfer.world_epoch == world_epoch
                    and leader_role_layout_generation(
                        session_snapshot.current_layout,
                        session_snapshot.ui_layout,
                    )
                    == leader_role_transfer.layout_generation
                    and settle_native_leader_role_transfer_publishers(
                        process, leader_role_transfer
                    )
                )
                if seamless_role_permutation:
                    (
                        last_heading,
                        visual_heading,
                        last_third_player_heading,
                        third_player_visual_heading,
                    ) = adopt_committed_orientation(
                        process,
                        leader_role_transfer,
                        p2_token,
                        p3_token,
                        world_epoch,
                        last_heading,
                        visual_heading,
                        last_third_player_heading,
                        third_player_visual_heading,
                    )
                    adopted_owners = {}
                    if p2_manual_control and p2_token is not None:
                        adopted_owners["p2"] = p2_token
                    if p3_manual_control and p3_token is not None:
                        adopted_owners["p3"] = p3_token
                    # The native journal already changed both commands and HUD
                    # slots in one game-thread callback.  Adopt the exact same
                    # generation as one pure state transition; ordinary
                    # reconcile must not emit DISARM/CLEAR/ACQUIRE afterward.
                    handoff_state = follow_handoff.HandoffState(
                        tuple(sorted(adopted_owners.items())),
                        (),
                    )

                desired_p2 = bool(
                    active
                    and p2_manual_control
                    and world_valid
                    and p2_token is not None
                    and p2_handoff_alive is True
                    and (
                        session_snapshot.leader_party_member_id != p2_token.member_id
                        or ko_promoted_role == "p2"
                    )
                )
                desired_p3 = bool(
                    active
                    and p3_manual_control
                    and world_valid
                    and p3_token is not None
                    and p3_handoff_alive is True
                    and (
                        session_snapshot.leader_party_member_id != p3_token.member_id
                        or ko_promoted_role == "p3"
                    )
                )
                current_handoff_owners = handoff_state.owner_map()
                p2_mapping_unknown = bool(
                    world_valid and p2_layout_coherent and p2_handoff_entry is None
                )
                p3_mapping_unknown = bool(
                    world_valid
                    and p3_member_id is not None
                    and p3_layout_coherent
                    and p3_handoff_entry is None
                )
                p2_gap_owner = current_handoff_owners.get("p2")
                p3_gap_owner = current_handoff_owners.get("p3")
                retain_p2_mapping_gap = retain_owner_during_mapping_gap(
                    mapping_unknown=p2_mapping_unknown,
                    coop_active=active,
                    manual_control=p2_manual_control,
                    current_owner=p2_gap_owner,
                    role_member_id=p2_member_id,
                    native_leader_member_id=session_snapshot.leader_party_member_id,
                    world_epoch=world_epoch,
                    allow_native_leader=ko_promoted_role == "p2",
                )
                retain_p3_mapping_gap = retain_owner_during_mapping_gap(
                    mapping_unknown=p3_mapping_unknown,
                    coop_active=active,
                    manual_control=p3_manual_control,
                    current_owner=p3_gap_owner,
                    role_member_id=p3_member_id,
                    native_leader_member_id=session_snapshot.leader_party_member_id,
                    world_epoch=world_epoch,
                    allow_native_leader=ko_promoted_role == "p3",
                )
                retain_p2_liveness_gap = retain_owner_during_liveness_gap(
                    actor_alive=p2_handoff_alive,
                    coop_active=active,
                    manual_control=p2_manual_control,
                    current_owner=p2_gap_owner,
                    candidate_owner=p2_token,
                    role_member_id=p2_member_id,
                    native_leader_member_id=session_snapshot.leader_party_member_id,
                    world_epoch=world_epoch,
                    allow_native_leader=ko_promoted_role == "p2",
                )
                retain_p3_liveness_gap = retain_owner_during_liveness_gap(
                    actor_alive=p3_handoff_alive,
                    coop_active=active,
                    manual_control=p3_manual_control,
                    current_owner=p3_gap_owner,
                    candidate_owner=p3_token,
                    role_member_id=p3_member_id,
                    native_leader_member_id=session_snapshot.leader_party_member_id,
                    world_epoch=world_epoch,
                    allow_native_leader=ko_promoted_role == "p3",
                )
                desired_owners = {
                    "p2": (
                        p2_gap_owner
                        if retain_p2_mapping_gap or retain_p2_liveness_gap
                        else p2_token
                        if desired_p2
                        else None
                    ),
                    "p3": (
                        p3_gap_owner
                        if retain_p3_mapping_gap or retain_p3_liveness_gap
                        else p3_token
                        if desired_p3
                        else None
                    ),
                }

                observed_tokens = {
                    token.incarnation_key: token
                    for token in (
                        tuple(handoff_state.owner_map().values())
                        + tuple(repair.token for repair in handoff_state.repairs)
                    )
                }
                handoff_observations = {
                    key: observe_follower_handoff(
                        process,
                        token,
                        session_snapshot,
                        party_actor_slots,
                        native_sample,
                    )
                    for key, token in observed_tokens.items()
                }
                if seamless_role_permutation:
                    handoff_effects = ()
                else:
                    handoff_state, handoff_effects = follow_handoff.reconcile_owners(
                        handoff_state,
                        desired_owners,
                        acquire_eligible={
                            "p2": bool(desired_owners["p2"]),
                            "p3": bool(desired_owners["p3"]),
                        },
                        observations=handoff_observations,
                        now=now,
                        world_epoch=world_epoch,
                    )
                    handoff_state, retry_effects = follow_handoff.advance_repairs(
                        handoff_state,
                        handoff_observations,
                        now=now,
                        world_epoch=world_epoch,
                    )
                    handoff_effects += retry_effects
                transfer_acquisition_keys = {
                    effect.token.incarnation_key
                    for effect in handoff_effects
                    if effect.kind == follow_handoff.EffectKind.ACQUIRE_NEW
                    and effect.reason == "transfer"
                }
                acquired_headings, rejected_acquisitions = apply_follower_handoff_effects(
                    process,
                    handoff_effects,
                    handoff_observations,
                    current_world_epoch=world_epoch,
                )
                for rejected in rejected_acquisitions:
                    handoff_state = follow_handoff.reject_acquisition(
                        handoff_state,
                        rejected,
                        restore_repair=(
                            rejected.incarnation_key in transfer_acquisition_keys
                        ),
                        now=now,
                    )
                if "p2" in acquired_headings:
                    last_heading, visual_heading = acquired_headings["p2"]
                if "p3" in acquired_headings:
                    (
                        last_third_player_heading,
                        third_player_visual_heading,
                    ) = acquired_headings["p3"]
                if follower_handoff_requires_camera_reset(
                    handoff_effects,
                    acquired_headings,
                    rejected_acquisitions,
                ):
                    shared_camera.reset()
                    camera_needs_party_centre_seed = True

                committed_owners = handoff_state.owner_map()
                p2_committed_owner = committed_owners.get("p2")
                p3_committed_owner = committed_owners.get("p3")
                p2_command_owned = bool(
                    p2_movement_owned
                    and p2_committed_owner is not None
                    and p2_committed_owner.actor == p2_actor
                    and p2_committed_owner.phys == p2_phys
                )
                p3_command_owned = bool(
                    p3_movement_owned
                    and p3_committed_owner is not None
                    and p3_committed_owner.actor == p3_actor
                    and p3_committed_owner.phys == p3_phys
                )

                p2_effective_hud_slot = (
                    session_state.party_slot_for_member(
                        session_snapshot,
                        p2_committed_owner.member_id,
                    )
                    if p2_committed_owner is not None
                    else None
                )
                p3_effective_hud_slot = (
                    session_state.party_slot_for_member(
                        session_snapshot,
                        p3_committed_owner.member_id,
                    )
                    if p3_committed_owner is not None
                    else None
                )
                p2_handoff_skeleton_keep = (
                    resolve_skeleton_keep(process, p2_handoff_entry[0])
                    if p2_handoff_entry is not None
                    and p2_committed_owner is not None
                    and p2_committed_owner.actor == p2_handoff_actor
                    else 0
                )
                p3_handoff_skeleton_keep = (
                    resolve_skeleton_keep(process, p3_handoff_entry[0])
                    if p3_handoff_entry is not None
                    and p3_committed_owner is not None
                    and p3_committed_owner.actor == p3_handoff_actor
                    else 0
                )

                role_metadata = (
                    p2_committed_owner,
                    p3_committed_owner,
                    p2_handoff_skeleton_keep,
                    p3_handoff_skeleton_keep,
                    p2_effective_hud_slot,
                    p3_effective_hud_slot,
                )
                if role_metadata != published_role_metadata:
                    publish_follower_role_metadata(
                        process,
                        active=bool(p2_committed_owner or p3_committed_owner),
                        p2_manual_control=bool(p2_committed_owner),
                        p3_manual_control=bool(p3_committed_owner),
                        p2_actor=(p2_committed_owner.actor if p2_committed_owner else 0),
                        p3_actor=(p3_committed_owner.actor if p3_committed_owner else 0),
                        p2_skeleton_keep=(p2_handoff_skeleton_keep),
                        p3_skeleton_keep=(p3_handoff_skeleton_keep),
                        p2_hud_slot=p2_effective_hud_slot,
                        p3_hud_slot=p3_effective_hud_slot,
                    )
                    published_role_metadata = role_metadata
                if (
                    seamless_role_permutation
                    and leader_role_transfer is not None
                    and session.identity == leader_role_transfer.identity
                    and resolved_p1_owner_member_id
                    == leader_role_transfer.identity.p1_party_member_id
                    and p1_actor
                    and (
                        not p2_manual_control
                        or (
                            p2_committed_owner is not None
                            and p2_committed_owner.member_id
                            == leader_role_transfer.identity.p2_party_member_id
                        )
                    )
                    and (
                        not p3_manual_control
                        or (
                            p3_committed_owner is not None
                            and p3_committed_owner.member_id
                            == leader_role_transfer.identity.p3_party_member_id
                        )
                    )
                    and not leader_role_transfer.committed
                ):
                    acknowledge_native_leader_role_transfer(
                        process, leader_role_transfer
                    )
                interaction_routing_active = follower_interaction_routing_active(
                    logical_active,
                    session_movement_allowed,
                    p2_command_owned,
                    p3_command_owned,
                    town=session_snapshot.town,
                )
                p1_accepts_manual_movement = actor_accepts_manual_movement(
                    process, p1_actor
                )
                p1_has_berserk = actor_has_berserk(process, p1_actor)
                p1_berserk_movement_owned = berserk_leader_owns_movement(
                    p1_base_movement_active,
                    p1_alive,
                    bool(p1_actor and p1_phys),
                    session_snapshot.leader_party_member_id,
                    session_snapshot.p1_owner_party_member_id,
                    p1_has_berserk,
                    p1_accepts_manual_movement,
                )
                if p1_berserk_movement_owned and not last_p1_berserk_movement_owned:
                    try:
                        p1_berserk_visual_heading = process.f32(p1_phys + 0xA8)
                        last_p1_berserk_heading = p1_berserk_visual_heading
                    except OSError:
                        pass
                if not p1_berserk_movement_owned and last_p1_berserk_movement_owned:
                    clear_p1_berserk_command(process)
                last_p1_berserk_movement_owned = p1_berserk_movement_owned
                follower_movement_active = follower_movement_master_enabled(
                    base_movement_active,
                    p2_command_owned,
                    p3_command_owned,
                )
                movement_active = bool(
                    p1_berserk_movement_owned or follower_movement_active
                )
                movement_input_allowed = base_movement_input_allowed
                p1_berserk_input_allowed = bool(
                    p1_base_movement_input_allowed and p1_berserk_movement_owned
                )
                p2_input_allowed = bool(
                    movement_input_allowed
                    and p2_command_owned
                    and actor_accepts_manual_movement(process, p2_actor) is True
                )
                p3_input_allowed = bool(
                    movement_input_allowed
                    and p3_command_owned
                    and actor_accepts_manual_movement(process, p3_actor) is True
                )
                p1_position = None
                p2_position = None
                p3_position = None
                p2_gateway_magnitude = 0.0
                p3_gateway_magnitude = 0.0
                p2_gateway_heading = None
                p3_gateway_heading = None
                camera_present_before = None
                if movement_active or shared_camera_active:
                    if shared_camera_active:
                        try:
                            camera_present_before = (
                                camera_frame_sync.read_present_sample(
                                    process,
                                    CAMERA_FRAME_STATE
                                    - camera_frame_sync.CAMERA_FRAME_STATE_OFFSET,
                                    CAMERA_FRAME_STATE,
                                )
                            )
                        except (OSError, struct.error, ValueError):
                            camera_present_before = None
                    for actor, destination in (
                        (p1_actor if p1_alive is True else 0, "p1"),
                        (
                            p2_actor if p2_manual_control and p2_alive is True else 0,
                            "p2",
                        ),
                        (
                            p3_actor if p3_manual_control and p3_alive is True else 0,
                            "p3",
                        ),
                    ):
                        if not actor:
                            continue
                        try:
                            position = struct.unpack("<3f", process.read(actor + 0xD0, 12))
                        except (OSError, struct.error):
                            continue
                        if destination == "p1":
                            p1_position = position
                        elif destination == "p2":
                            p2_position = position
                        else:
                            p3_position = position
                if shared_camera_active and camera_present_before is not None:
                    try:
                        camera_present_after = camera_frame_sync.read_present_sample(
                            process,
                            CAMERA_FRAME_STATE
                            - camera_frame_sync.CAMERA_FRAME_STATE_OFFSET,
                            CAMERA_FRAME_STATE,
                        )
                    except (OSError, struct.error, ValueError):
                        camera_present_after = None
                    if camera_present_after == camera_present_before:
                        last_camera_present_tick_us = camera_present_after.tick_us
                camera_positions = [
                    position
                    for position in (p1_position, p2_position, p3_position)
                    if position is not None
                ]

                p2_input_allowed = bool(p2_input_allowed and p2_position is not None)
                p3_input_allowed = bool(p3_input_allowed and p3_position is not None)
                p1_berserk_input_allowed = bool(
                    p1_berserk_input_allowed and p1_position is not None
                )
                try:
                    interaction_owner = process.read(
                        COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET, 1
                    )[0]
                except OSError:
                    interaction_owner = INTERACTION_OWNER_NONE
                camera_requested = bool(
                    len(camera_positions) >= 2
                    and camera_session_gate(
                        shared_camera_active,
                        session_snapshot.camera_allowed,
                        session_snapshot.gameplay_ready,
                        session_snapshot.loading,
                        interaction_owner,
                    )
                )
                native_focus_distance = None
                if camera_requested and len(camera_positions) >= 2:
                    try:
                        native_focus = struct.unpack(
                            "<3f", process.read(CAMERA_MASTER_STATE + 0xA0, 12)
                        )
                        native_focus_distance = (
                            shared_camera.native_focus_distance_to_party(
                                native_focus, *camera_positions
                            )
                        )
                    except (OSError, struct.error, ValueError):
                        native_focus_distance = None
                native_camera_ready = bool(
                    len(camera_positions) >= 2
                    and camera_party_positions_ready(*camera_positions)
                    and native_focus_distance is not None
                    and native_focus_distance
                    <= shared_camera.tuning.native_seed_max_distance
                )
                if camera_requested and not last_camera_requested:
                    print(
                        "CAMERA_REACQUIRE_WAIT native-focus",
                        flush=True,
                    )
                was_reacquire_pending = camera_reacquire_pending
                (
                    shared_camera_active,
                    camera_reacquire_pending,
                    camera_native_ready_samples,
                ) = advance_camera_reacquire_gate(
                    camera_requested,
                    last_camera_requested,
                    camera_reacquire_pending,
                    camera_native_ready_samples,
                    native_camera_ready,
                )
                if (
                    shared_camera_active
                    and not camera_reacquire_pending
                    and (was_reacquire_pending or not last_camera_requested)
                ):
                    print(
                        "CAMERA_REACQUIRE_READY "
                        f"distance={native_focus_distance:.3f} "
                        "positions=valid "
                        f"samples={camera_native_ready_samples}",
                        flush=True,
                    )
                last_camera_requested = camera_requested
                lifecycle = (
                    logical_active,
                    movement_active,
                    shared_camera_active,
                    p1_alive,
                    p2_alive,
                    p3_alive,
                    movement_input_allowed,
                    p2_movement_owned,
                    p3_movement_owned,
                    ko_promoted_role,
                )
                if lifecycle != last_lifecycle:
                    if last_lifecycle is not None and (
                        shared_camera_active != last_lifecycle[2]
                    ):
                        shared_camera.reset()
                        internal_collision_latch.reset()
                        camera_needs_party_centre_seed = shared_camera_active
                        last_camera_feedback_signature = None
                        previous_native_camera_yaw = None
                        manual_camera_orbit_hold = 0.0
                        camera_collision_direction = None
                    print(
                        "COOP_LIFECYCLE "
                        f"p1={'alive' if p1_alive else 'ko/unknown'} "
                        f"p2={'alive' if p2_alive else 'ko/unknown'} "
                        f"p3={'alive' if p3_alive else 'ko/unknown'} "
                        f"owners=p2:{int(p2_movement_owned)},p3:{int(p3_movement_owned)} "
                        f"promoted={ko_promoted_role or 'none'} "
                        "movement="
                        f"{'mod/custom-menu' if movement_input_allowed and custom_wait_movement else 'mod' if movement_input_allowed else 'held/custom-wait' if movement_active else 'native/off'} "
                        f"camera={'shared' if shared_camera_active else 'native'}",
                        flush=True,
                    )
                    last_lifecycle = lifecycle

                for (
                    label,
                    role,
                    follower_actor,
                    follower_phys,
                    follower_handle,
                    follower_owned,
                ) in (
                    (
                        "P2",
                        "p2",
                        p2_actor,
                        p2_phys,
                        (
                            p2_token.native_handle
                            if p2_token is not None
                            and p2_token.actor == p2_actor
                            and p2_token.phys == p2_phys
                            else 0
                        ),
                        logical_active and p2_command_owned and p2_alive is True,
                    ),
                    (
                        "P3",
                        "p3",
                        p3_actor,
                        p3_phys,
                        (
                            p3_token.native_handle
                            if p3_token is not None
                            and p3_token.actor == p3_actor
                            and p3_token.phys == p3_phys
                            else 0
                        ),
                        logical_active and p3_command_owned and p3_alive is True,
                    ),
                ):
                    if sanitize_residual_follower_action(
                        process,
                        follower_actor,
                        follower_owned,
                        role=role,
                        handle=follower_handle,
                        expected_phys=follower_phys,
                    ):
                        print(
                            f"{label} residual follower action 0x4124 -> 0x4021",
                            flush=True,
                        )

                if movement_active or shared_camera_active:
                    if shared_camera_active and camera_needs_party_centre_seed:
                        shared_camera.reset()
                        print(
                            "CAMERA_REACQUIRE_SEED source=party-centre",
                            flush=True,
                        )
                        camera_needs_party_centre_seed = False
                    camera_frame = shared_camera.update_group(
                        camera_positions,
                        delta_time,
                        present_tick_us=last_camera_present_tick_us,
                    )
                    camera_delta_time = (
                        shared_camera.frame_delta_seconds
                        if last_camera_present_tick_us is not None
                        else delta_time
                    )
                    published_extra_distance = camera_frame.extra_distance
                    published_yaw_offset = 0.0
                    published_pitch_offset = 0.0

                    try:
                        camera_state = process.read(CAMERA_MASTER_STATE + 0x90, 0x1C)
                        camera_values = struct.unpack("<7f", camera_state)
                        native_orbit_raw = process.read(CAMERA_PRIMARY_RIG + 0x30, 12)
                        native_camera_flags_raw = process.read(CAMERA_PRIMARY_RIG + 0x40, 4)
                        native_camera_yaw, _native_camera_pitch, native_boom = (
                            struct.unpack("<3f", native_orbit_raw)
                        )
                        native_camera_flags = struct.unpack("<I", native_camera_flags_raw)[
                            0
                        ]
                        native_back_on_wall = bool(
                            native_camera_flags & CAMERA_BACK_ON_WALL_MASK
                        )
                        collision_counters = struct.unpack(
                            "<4I",
                            process.read(
                                CAMERA_COLLISION_FILTER_SCRATCH + 0x10,
                                16,
                            ),
                        )
                        eye = camera_values[0:3]
                        focus = camera_values[4:7]
                        direction = camera_direction_from_yaw(native_camera_yaw)
                        if direction is not None:
                            camera_collision_direction = direction
                        actual_boom = math.sqrt(
                            sum((eye[index] - focus[index]) ** 2 for index in range(3))
                        )
                        feedback_signature = (
                            camera_state + native_orbit_raw + native_camera_flags_raw
                        )
                        new_native_sample = (
                            feedback_signature != last_camera_feedback_signature
                        )
                        last_camera_feedback_signature = feedback_signature
                        manual_camera_orbit_hold = max(
                            0.0, manual_camera_orbit_hold - camera_delta_time
                        )
                        if new_native_sample:
                            if previous_native_camera_yaw is not None:
                                yaw_delta = math.atan2(
                                    math.sin(
                                        native_camera_yaw - previous_native_camera_yaw
                                    ),
                                    math.cos(
                                        native_camera_yaw - previous_native_camera_yaw
                                    ),
                                )
                                if abs(yaw_delta) >= 0.012:
                                    manual_camera_orbit_hold = 0.35
                            previous_native_camera_yaw = native_camera_yaw
                        latch_decision = internal_collision_latch.observe(
                            collision_counters,
                            native_boom,
                            native_back_on_wall,
                            camera_delta_time,
                        )
                        if (
                            latch_decision.suppress
                            and latch_decision.restored_native_boom is not None
                        ):
                            clear_internal_camera_wall_latch(
                                process,
                                latch_decision.restored_native_boom,
                                native_camera_flags,
                            )
                            native_boom = latch_decision.restored_native_boom
                            if CAMERA_WALL_BIT_RELEASED_BY_RESCUE:
                                native_back_on_wall = False

                            last_camera_feedback_signature = None
                            if latch_decision.activated:
                                print(
                                    "CAMERA_INTERNAL_LATCH_SUPPRESSED "
                                    f"boom={native_boom:.3f}",
                                    flush=True,
                                )
                        if math.isfinite(actual_boom) and math.isfinite(native_boom):
                            obstruction = None
                            if not latch_decision.suppress:
                                obstruction = shared_camera.solve_obstruction(
                                    actual_boom,
                                    native_boom,
                                    camera_delta_time,
                                    new_native_sample=new_native_sample,
                                    allow_auto_orbit=False,
                                    allow_collision_limiter=(
                                        manual_camera_orbit_hold <= 0.0
                                    ),
                                    native_back_on_wall=native_back_on_wall,
                                    wall_release_requested=(manual_camera_orbit_hold > 0.0),
                                )
                            if obstruction is not None:
                                published_extra_distance = obstruction.extra_distance
                                published_yaw_offset = obstruction.yaw_offset
                                published_pitch_offset = obstruction.pitch_offset
                    except (OSError, struct.error):
                        pass
                    if camera_collision_direction is not None:
                        exclusion = shared_camera.internal_collision_exclusion_group(
                            camera_positions,
                            camera_frame.focus,
                            camera_collision_direction,
                        )
                        collision_exclusion = (
                            exclusion.start_distance,
                            exclusion.end_distance,
                        )
                    if shared_camera_active:
                        collision_first, collision_second = widest_party_pair(
                            camera_positions
                        )
                        camera_corridor_active = publish_camera_collision_formation(
                            process,
                            collision_first,
                            collision_second,
                            shared_camera.tuning,
                            enabled=True,
                            native_boom=native_boom,
                            previously_active=camera_corridor_active,
                        )
                        publish_camera_collision_focus_escape(
                            process,
                            camera_positions,
                            camera_frame.focus,
                            shared_camera.tuning,
                            enabled=True,
                        )
                        if shared_camera.frame_advanced:
                            write_camera_command(
                                process,
                                camera_frame.focus,
                                published_extra_distance,
                                published_yaw_offset,
                                published_pitch_offset,
                                collision_exclusion,
                                focus_velocity=camera_frame.focus_velocity,
                                reset_epoch=camera_frame.reset_epoch,
                                publisher=camera_goal_publisher,
                                producer_tick_us=last_camera_present_tick_us,
                            )
                        camera_collision_published = True
                    elif camera_collision_published:
                        camera_corridor_active = publish_camera_collision_formation(
                            process,
                            (),
                            (),
                            shared_camera.tuning,
                            enabled=False,
                        )
                        publish_camera_collision_focus_escape(
                            process,
                            (),
                            (),
                            shared_camera.tuning,
                            enabled=False,
                        )
                        camera_collision_published = False
                    lifecycle_flags = (
                        logical_active,
                        shared_camera_active,
                        interaction_routing_active,
                    )
                    if lifecycle_flags != published_lifecycle_flags:
                        publish_lifecycle_flags(process, *lifecycle_flags)
                        published_lifecycle_flags = lifecycle_flags

                    if (
                        movement_input_allowed or p1_berserk_input_allowed
                    ) and movement_active:
                        camera_basis = horizontal_camera_basis(
                            process,
                            camera_basis,
                        )

                    p1_berserk_heading, p1_berserk_magnitude, p1_berserk_moving = (
                        player_steering(
                            bindings,
                            "p1",
                            camera_basis,
                            False,
                            p1_berserk_input_allowed,
                            native_leader_pad=True,
                        )
                    )
                    if p1_berserk_heading is None:
                        p1_berserk_heading = last_p1_berserk_heading
                    else:
                        last_p1_berserk_heading = p1_berserk_heading
                    if p1_berserk_moving and actor_fighting_stance(process, p1_actor):
                        p1_berserk_magnitude *= ARMED_RUN_STEP / RUN_STEP
                    p1_berserk_execution_animation = actor_execution_animation_active(
                        process,
                        p1_actor,
                        p1_alive,
                    )
                    if p1_berserk_execution_animation:
                        p1_berserk_magnitude = 0.0
                        p1_target_heading = actor_execution_target_heading(
                            process,
                            p1_actor,
                            p1_position,
                            actor_by_handle,
                        )
                        if p1_target_heading is not None:
                            p1_berserk_heading = p1_target_heading
                            last_p1_berserk_heading = p1_berserk_heading
                    p1_turn = math.atan2(
                        math.sin(p1_berserk_heading - p1_berserk_visual_heading),
                        math.cos(p1_berserk_heading - p1_berserk_visual_heading),
                    )
                    p1_turn_rate = (
                        ACTION_TURN_RATE_RADIANS
                        if p1_berserk_execution_animation
                        else TURN_RATE_RADIANS
                    )
                    p1_max_turn = p1_turn_rate * delta_time
                    p1_turn = max(-p1_max_turn, min(p1_max_turn, p1_turn))
                    p1_berserk_visual_heading = math.atan2(
                        math.sin(p1_berserk_visual_heading + p1_turn),
                        math.cos(p1_berserk_visual_heading + p1_turn),
                    )
                    if p1_berserk_movement_owned:
                        p1_interaction_entry = actor_u64_or_zero(
                            process,
                            p1_actor,
                            0x10,
                        )
                        write_p1_berserk_command(
                            process,
                            p1_actor,
                            p1_phys,
                            p1_berserk_heading,
                            p1_berserk_magnitude,
                            True,
                            p1_berserk_visual_heading,
                            p1_interaction_entry,
                            p1_skeleton_keep,
                        )

                    if p2_input_allowed:
                        forward_input = bindings.signed_axis("p2", "move_up", "move_down")
                        right_input = bindings.signed_axis("p2", "move_right", "move_left")
                    else:
                        forward_input = 0.0
                        right_input = 0.0

                    if forward_input or right_input:
                        input_strength = min(1.0, math.hypot(forward_input, right_input))
                        forward, right = camera_basis
                        world_x = forward_input * forward[0] + right_input * right[0]
                        world_z = forward_input * forward[1] + right_input * right[1]
                        heading = math.atan2(world_x, world_z)
                        last_heading = heading

                        share = native_stick_fraction(input_strength)
                        if p2_walk_mode:
                            magnitude = WALK_STEP * share
                        else:
                            fighting_stance = actor_fighting_stance(process, p2_actor)
                            magnitude = share * (
                                ARMED_RUN_STEP if fighting_stance else RUN_STEP
                            )
                    else:
                        heading = last_heading
                        magnitude = 0.0

                    if interaction_owner == INTERACTION_OWNER_P2:
                        magnitude = 0.0

                    execution_animation = actor_execution_animation_active(
                        process,
                        p2_actor,
                        p2_alive,
                    )
                    if execution_animation:
                        magnitude = 0.0
                        target_heading = actor_execution_target_heading(
                            process,
                            p2_actor,
                            p2_position,
                            actor_by_handle,
                        )
                        if target_heading is not None:
                            heading = target_heading
                            last_heading = heading
                    p2_gateway_magnitude = magnitude
                    p2_gateway_heading = heading

                    turn_delta = math.atan2(
                        math.sin(heading - visual_heading),
                        math.cos(heading - visual_heading),
                    )
                    turn_rate = (
                        ACTION_TURN_RATE_RADIANS
                        if execution_animation
                        else TURN_RATE_RADIANS
                    )
                    max_turn = turn_rate * delta_time
                    turn_delta = max(-max_turn, min(max_turn, turn_delta))
                    visual_heading += turn_delta
                    visual_heading = math.atan2(
                        math.sin(visual_heading), math.cos(visual_heading)
                    )

                    published_visual = action_facing_release_heading(
                        process, p2_actor, visual_heading
                    )
                    if p2_command_owned and p2_committed_owner is not None:
                        write_command(
                            process,
                            p2_actor,
                            heading,
                            published_visual,
                            magnitude,
                            True,
                            phys=p2_phys,
                            member_id=p2_committed_owner.member_id,
                            actor_generation=p2_committed_owner.actor_generation,
                            world_epoch=p2_committed_owner.world_epoch,
                        )
                    else:
                        disarm_second_player_command(
                            process,
                            world_epoch=world_epoch,
                        )

                    if p3_actor:
                        p3_heading, p3_magnitude, p3_moving = third_player_steering(
                            bindings,
                            camera_basis,
                            p3_walk_mode,
                            p3_input_allowed,
                        )
                        if p3_heading is None:
                            p3_heading = last_third_player_heading
                        else:
                            last_third_player_heading = p3_heading

                        p3_execution_animation = actor_execution_animation_active(
                            process,
                            p3_actor,
                            p3_alive,
                        )
                        if p3_execution_animation:
                            p3_magnitude = 0.0
                            p3_target_heading = actor_execution_target_heading(
                                process,
                                p3_actor,
                                p3_position,
                                actor_by_handle,
                            )
                            if p3_target_heading is not None:
                                p3_heading = p3_target_heading
                                last_third_player_heading = p3_heading

                        if interaction_owner == INTERACTION_OWNER_P3:
                            p3_magnitude = 0.0

                        if p3_moving and not p3_walk_mode:
                            p3_fighting_stance = actor_fighting_stance(
                                process,
                                p3_actor,
                            )
                            if p3_fighting_stance:
                                p3_magnitude *= ARMED_RUN_STEP / RUN_STEP
                        p3_gateway_magnitude = p3_magnitude
                        p3_gateway_heading = p3_heading

                        p3_turn = math.atan2(
                            math.sin(p3_heading - third_player_visual_heading),
                            math.cos(p3_heading - third_player_visual_heading),
                        )
                        p3_turn_rate = (
                            ACTION_TURN_RATE_RADIANS
                            if p3_execution_animation
                            else TURN_RATE_RADIANS
                        )
                        p3_max_turn = p3_turn_rate * delta_time
                        p3_turn = max(-p3_max_turn, min(p3_max_turn, p3_turn))
                        third_player_visual_heading = math.atan2(
                            math.sin(third_player_visual_heading + p3_turn),
                            math.cos(third_player_visual_heading + p3_turn),
                        )
                        if p3_command_owned and p3_committed_owner is not None:
                            write_third_player_command(
                                process,
                                p3_actor,
                                p3_heading,
                                p3_magnitude,
                                True,
                                action_facing_release_heading(
                                    process, p3_actor, third_player_visual_heading
                                ),
                                phys=p3_phys,
                                member_id=p3_committed_owner.member_id,
                                actor_generation=p3_committed_owner.actor_generation,
                                world_epoch=p3_committed_owner.world_epoch,
                            )
                        else:
                            disarm_third_player_command(
                                process,
                                world_epoch=world_epoch,
                            )
                    else:
                        disarm_third_player_command(process, world_epoch=world_epoch)
                    if follower_movement_active != published_follower_movement_active:
                        publish_follower_movement_master(
                            process,
                            follower_movement_active,
                        )
                        published_follower_movement_active = follower_movement_active
                else:
                    if camera_collision_published:
                        camera_corridor_active = publish_camera_collision_formation(
                            process,
                            (),
                            (),
                            shared_camera.tuning,
                            enabled=False,
                        )
                        publish_camera_collision_focus_escape(
                            process,
                            (),
                            (),
                            shared_camera.tuning,
                            enabled=False,
                        )
                        camera_collision_published = False
                    disarm_second_player_command(process, world_epoch=world_epoch)
                    disarm_third_player_command(process, world_epoch=world_epoch)
                    if published_follower_movement_active is not False:
                        publish_follower_movement_master(process, False)
                        published_follower_movement_active = False
                    lifecycle_flags = (
                        logical_active,
                        False,
                        interaction_routing_active,
                    )
                    if lifecycle_flags != published_lifecycle_flags:
                        publish_lifecycle_flags(process, *lifecycle_flags)
                        published_lifecycle_flags = lifecycle_flags

                if FOLLOWER_AREA_TRANSITIONS_ENABLED and session_snapshot is not None:
                    handoff_eligible = bool(
                        active
                        and movement_active
                        and movement_input_allowed
                        and not ko_leader_ownership.active
                        and not session_snapshot.loading
                        and not session_snapshot.cutscene
                        and not session_snapshot.native_menu_open
                    )
                    area_handoff.consider(
                        process,
                        2,
                        p2_position,
                        p2_gateway_magnitude,
                        handoff_eligible and p2_manual_control and p2_alive is True,
                        session_snapshot.identity.p1_party_member_id,
                        session_snapshot.identity.p2_party_member_id,
                        now,
                        p2_gateway_heading,
                    )
                    p3_member_id = session_snapshot.identity.p3_party_member_id
                    area_handoff.consider(
                        process,
                        3,
                        p3_position,
                        p3_gateway_magnitude,
                        handoff_eligible
                        and p3_manual_control
                        and p3_alive is True
                        and p3_member_id is not None,
                        session_snapshot.identity.p1_party_member_id,
                        p3_member_id,
                        now,
                        p3_gateway_heading,
                    )

                if FOLLOWER_AREA_TRANSITION_REQUEST_ENABLED:
                    follower_transition.watcher.sample(process, p1_position)

                    if session_snapshot is not None:
                        transition_eligible = bool(
                            active
                            and movement_active
                            and movement_input_allowed
                            and not ko_leader_ownership.active
                            and not area_handoff.active
                            and not session_snapshot.loading
                            and not session_snapshot.cutscene
                            and not session_snapshot.native_menu_open
                        )
                        p2_stalled = gateway_stall.observe(
                            2,
                            p2_position,
                            p2_gateway_magnitude,
                            transition_eligible and p2_manual_control and p2_alive is True,
                            now,
                            p2_gateway_heading,
                        )
                        follower_transition.consider(
                            process, 2, p2_position, p2_stalled, now
                        )
                        p3_stalled = gateway_stall.observe(
                            3,
                            p3_position,
                            p3_gateway_magnitude,
                            transition_eligible and p3_manual_control and p3_alive is True,
                            now,
                            p3_gateway_heading,
                        )
                        follower_transition.consider(
                            process, 3, p3_position, p3_stalled, now
                        )

                time.sleep(POLL_SECONDS)
            except (OSError, struct.error) as error:
                if not frame_io_recovery.recover(process, error):
                    break

                # Mark the sampled world invalid without inventing a new epoch.
                # The first later coherent sample advances it, which makes every
                # retained owner token and actor generation provably stale.
                world_epoch = world_epochs.observe(None, valid=False)
                last_resolve = 0.0
                p1_actor = 0
                p1_phys = 0
                p1_skeleton_keep = 0
                configured_p1_actor = 0
                p2_actor = 0
                p2_phys = 0
                p3_actor = 0
                p3_phys = 0
                actor_by_handle.clear()
                party_actors = []
                party_actor_slots = {}
                session_snapshot = None
                resolved_identity = None
                resolved_p1_owner_member_id = None
                last_reported_actor = None
                last_lifecycle = None
                last_session_signature = None
                published_flee_state = None
                published_lifecycle_flags = None
                published_follower_movement_active = None
                published_role_metadata = None
                shared_camera.reset()
                internal_collision_latch.reset()
                camera_needs_party_centre_seed = True
                last_camera_requested = False
                camera_reacquire_pending = True
                camera_native_ready_samples = 0
                last_camera_feedback_signature = None
                previous_native_camera_yaw = None
                manual_camera_orbit_hold = 0.0
                camera_collision_direction = None
                collision_exclusion = (0.0, 0.0)
                # A failed read does not prove that the last native corridor
                # command disappeared.  Preserve both local facts until the
                # first coherent frame can either replace the formation or
                # explicitly revoke it.  Clearing them here lost hysteresis
                # and, worse, could leave a stale native command published
                # while the Python side incorrectly believed it was absent.
                gateway_stall.states.clear()
                area_handoff.detector.states.clear()
                previous_tick = time.monotonic()
                continue
            else:
                frame_io_recovery.recovered()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            shutdown_runtime(
                process,
                ko_leader_ownership=ko_leader_ownership,
                area_handoff=area_handoff,
                camera_tuning=shared_camera.tuning,
                bindings=bindings,
                handoff_state=handoff_state,
                session_snapshot=session_snapshot,
                party_actor_slots=party_actor_slots,
                world_epoch=world_epoch,
                native_sample=native_sample,
            )
        finally:
            try:
                lease.close()
            except BaseException:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
