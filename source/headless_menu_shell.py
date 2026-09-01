from __future__ import annotations

import builtins
import ctypes as ct
import math
import os
import struct
import time
from collections import deque
from ctypes import wintypes as wt
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import controller_bindings as control_bindings
import damage_meter
import headless_menu_core as legacy
import headless_menu_renderer as renderer
import headless_world_target_overlay as world_overlay
import menu_themes
import menu_runtime_state
import native_action_line_theme as action_line_theme
import native_interaction_route as native_interaction
import native_prompt_glyphs
import native_treasure_log
import native_ui_assets as native_assets
import party_hud_layout
import party_intent_channel
import runtime_shutdown
import session_state
from battle_pack_runtime_catalog import (
    PERMANENT_STATUS_EFFECTS_OFFSET,
    PENELO_PARTY_MEMBER_ID,
    TEMPORARY_STATUS_EFFECTS_OFFSET,
    BattlePackRuntimeCatalog,
    SharedCatalogState,
)
from headless_frontend_controller import HeadlessFrontendController, InputCommand
from headless_menu_presenter import CursorTween
from headless_menu_model import (
    ActionDefinition,
    MenuMode,
    P2MenuModel,
    TargetCandidate,
    TargetGroup,
    make_demo_catalog,
)
from interaction_transaction import (
    follower_interaction_leader_allows,
    interaction_press_allowed,
)
from loot_ownership import LootOwnershipBridge
from native_action_catalog import NativeActionCatalog
from native_area_target_circle_controller import (
    P1_OWNER_TAG as P1_AREA_OWNER_TAG,
)
from native_area_target_circle_controller import (
    P2_OWNER_TAG as P2_AREA_OWNER_TAG,
)
from native_area_target_circle_controller import (
    P3_OWNER_TAG as P3_AREA_OWNER_TAG,
)
from native_area_target_circle_controller import (
    NativeAreaRenderGate,
    NativeAreaTargetCircleController,
)
from native_target_circle_controller import (
    NativeTargetCircleCapacity,
    NativeTargetCircleController,
)
from native_target_circle_router import (
    P1_TARGET_OWNER_TAG,
    P2_TARGET_OWNER_TAG,
    P3_TARGET_OWNER_TAG,
)
from native_wait_mode_bridge import NativeWaitModeBridge
from p1_native_menu_mirror import P1NativeMirrorPresenter
from p2_catalog_atomic import commit_pages_atomically
from PIL import Image, ImageDraw, ImageFilter


def _runtime_print(*args, **kwargs) -> None:
    """Best-effort diagnostics must never become a control-plane failure."""

    try:
        builtins.print(*args, **kwargs)
    except Exception:
        pass


# All output in this long-lived helper is diagnostic.  A closed launcher pipe,
# a full log volume or a failed flush cannot be allowed to abort input polling.
print = _runtime_print

# Compatibility exports for tools and tests that imported these names from the
# original shell module before the native hook was isolated.
ACTION_LINE_RENDER_ORIGINAL = action_line_theme.ACTION_LINE_RENDER_ORIGINAL
ACTION_LINE_RENDER_RETURN = action_line_theme.ACTION_LINE_RENDER_RETURN
ACTION_LINE_RENDER_SITE = action_line_theme.ACTION_LINE_RENDER_SITE
ACTION_LINE_THEME_CAVE_OFFSET = action_line_theme.ACTION_LINE_THEME_CAVE_OFFSET
ACTION_LINE_THEME_MAX_ENTRIES = action_line_theme.ACTION_LINE_THEME_MAX_ENTRIES
ACTION_LINE_THEME_STATE_MAGIC = action_line_theme.ACTION_LINE_THEME_STATE_MAGIC
ACTION_LINE_THEME_STATE_OFFSET = action_line_theme.ACTION_LINE_THEME_STATE_OFFSET
ACTION_LINE_THEME_STATE_SIZE = action_line_theme.ACTION_LINE_THEME_STATE_SIZE
ACTION_TARGETLINE_RENDER_ORIGINAL = action_line_theme.ACTION_TARGETLINE_RENDER_ORIGINAL
ACTION_TARGETLINE_RENDER_RETURN = action_line_theme.ACTION_TARGETLINE_RENDER_RETURN
ACTION_TARGETLINE_RENDER_SITE = action_line_theme.ACTION_TARGETLINE_RENDER_SITE
ACTION_TARGETLINE_THEME_CAVE_OFFSET = (
    action_line_theme.ACTION_TARGETLINE_THEME_CAVE_OFFSET
)
NativeActionLineThemeRouter = action_line_theme.NativeActionLineThemeRouter
build_action_line_theme_cave = action_line_theme.build_action_line_theme_cave
build_action_targetline_theme_cave = (
    action_line_theme.build_action_targetline_theme_cave
)
BattleMenuSoundSettingsTracker = menu_runtime_state.BattleMenuSoundSettingsTracker
BATTLE_MENU_SOUND_CONFIG_POLL_SECONDS = (
    menu_runtime_state.BATTLE_MENU_SOUND_CONFIG_POLL_SECONDS
)
InteractionTransitionGate = menu_runtime_state.InteractionTransitionGate
INTERACTION_POST_LOAD_GRACE_SECONDS = (
    menu_runtime_state.INTERACTION_POST_LOAD_GRACE_SECONDS
)
PendingCombatIndicator = menu_runtime_state.PendingCombatIndicator
PlayerMenuInput = menu_runtime_state.PlayerMenuInput
WorldInteractions = menu_runtime_state.WorldInteractions


def vertical_input_command(direction: int, fresh_edge: bool | None) -> InputCommand:
    """Wrap only a new directional press; held repeats stop at list boundaries."""

    if int(direction) < 0:
        return InputCommand.UP if fresh_edge is not False else InputCommand.UP_REPEAT
    return InputCommand.DOWN if fresh_edge is not False else InputCommand.DOWN_REPEAT

CAMERA_PROJECTION_MATRIX = 0x2CB84B0
INPROCESS_RING_READY_OFFSET = 0xD80
INPROCESS_RING_READY_MAGIC = 0x33474E52
INPROCESS_RING_P1_HANDLE_OFFSET = 0xD84
INPROCESS_RING_P2_HANDLE_OFFSET = 0xD88
INPROCESS_RING_P3_HANDLE_OFFSET = 0xDA4


P2_GAUGE_THEME_PALETTE_OFFSET = 0xB00
P1_GAUGE_THEME_PALETTE_OFFSET = 0xE00
P3_GAUGE_THEME_PALETTE_OFFSET = 0x8240
P2_HUD_TEXT_THEME_COLOR_OFFSET = 0xB20
P1_HUD_TEXT_THEME_COLOR_OFFSET = 0xE20
P3_HUD_TEXT_THEME_COLOR_OFFSET = 0x8260
P2_MENU_THEME_COLOR_OFFSET = 0xB30
P1_MENU_THEME_COLOR_OFFSET = 0xE30
P3_MENU_THEME_COLOR_OFFSET = 0x8270


P2_DAMAGE_NUMBER_COLOR_OFFSET = 0xB40
P1_DAMAGE_NUMBER_COLOR_OFFSET = 0xE40
P3_DAMAGE_NUMBER_COLOR_OFFSET = 0x8280


RENDER_PRESENTATION_SUPPRESSED_OFFSET = 0x8600


BATTLE_DEFAULT_LAYOUT = 0
BATTLE_MENU_LAYOUT = 3
P2_CONTROLLED_ACTOR_OFFSET = 0x210
P1_CONTROLLED_ACTOR_OFFSET = 0x218
P3_CONTROLLED_ACTOR_OFFSET = 0x8208
COMBAT_LOG_COOP_RISE_PX = 40


COMBAT_LOG_LINE_PX = 26
# Section 2 of the battle pack contains the combat-log copy. Four messages have
# authored line breaks: successful steal, failed steal and both distance
# rejections. Stealing gil and "could not steal" use one line, so the action
# follows the message selected by the log. Action rules are keyed by ID and have
# no name, so this list also uses the ID: 0xA9 is Steal.
TWO_LINE_ACTION_IDS = frozenset({0xA9})


TWO_LINE_MESSAGE_SECONDS = 9.0
P1_INFO_TOP_Y = 145 - COMBAT_LOG_COOP_RISE_PX
P2_INFO_TOP_Y = P1_INFO_TOP_Y
P1_TARGET_INFO_X = 62
P1_TARGET_INFO_Y = P1_INFO_TOP_Y
P2_TARGET_INFO_PRIMARY_X = P1_TARGET_INFO_X
P3_TARGET_INFO_Y = P1_INFO_TOP_Y
TARGET_INFO_Y = P2_INFO_TOP_Y


P1_OVERLAY_X = 77
P1_OVERLAY_Y = 681
P3_OVERLAY_X = 1011
P3_OVERLAY_Y = 681
HELP_PANEL_X_COMPENSATION = -3
HELP_PANEL_Y_COMPENSATION = -2
P1_HELP_X = 116 + HELP_PANEL_X_COMPENSATION
P1_HELP_Y = P1_INFO_TOP_Y + 4 + HELP_PANEL_Y_COMPENSATION
P2_HELP_COMPACT_Y = P1_HELP_Y + 54
P2_TARGET_INFO_HELP_CLEARANCE_Y = P2_HELP_COMPACT_Y - P1_HELP_Y


INFO_LANE_TOP_PADDING_PX = 3
# A target card gains one row when the target has an elemental weakness, so
# everything beneath it must clear the tallest shape. Reserving the maximum
# prevents help text from being covered as soon as a weak target is selected,
# which happened with the old value of 150. Query the panel instead of copying
# a fixed 188 that becomes stale whenever the card is rescaled. Query the row
# count too, because the status strip wraps and a fixed count of two can be
# exceeded by a heavily afflicted target.
TARGET_INFO_PANEL_HEIGHT = world_overlay.panel_height(
    has_weak_row=True, effect_rows=world_overlay.max_status_lines()
)
P2_HELP_TARGET_CLEAR_Y = P1_TARGET_INFO_Y + TARGET_INFO_PANEL_HEIGHT + 4


def live_target_panel_height(target) -> int:
    if target is None:
        return TARGET_INFO_PANEL_HEIGHT
    return world_overlay.panel_height(
        has_weak_row=bool(getattr(target, "weak_mask", 0)),
        effect_rows=len(
            world_overlay.status_effect_lines(getattr(target, "status_mask", 0))
        ),
    )


INFO_FADE_SECONDS = 0.18
INFO_SLIDE_TIME_CONSTANT_SECONDS = 0.065
UPPER_INFO_IDLE_LIFT_PX = 40


# 68 px covered slightly more than two log rows (COMBAT_LOG_LINE_PX = 26). The
# extra clearance was added only inside the TWO_LINE_ACTION_IDS window, which
# predicts actions submitted BY THE PLAYER. Enemy logs, statuses, steals and
# incoming damage never created space, leaving help under the text. Reserving
# three rows up front covers those cases without guessing message provenance.
COMBAT_LOG_CLEARANCE_PX = 94


# Clear the native two-line log's lower visual edge. Target Info and the log
# itself keep their existing positions.
COMBAT_LOG_ACTIVE_INFO_NUDGE_PX = 20


BOSS_GAUGE_INFO_DROP_PX = 54


def coop_runtime_enabled(process, *, fallback: bool = True) -> bool:
    try:
        return bool(
            process.u32(legacy.coop.COMMAND_BLOCK + legacy.coop.COOP_ACTIVE_OFFSET)
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        return bool(fallback)


NATIVE_PAUSE_PRIMARY_OBJECT = 0x0209AD28
NATIVE_PAUSE_SECONDARY_OBJECT = 0x0209AD30
NATIVE_PAUSE_POINTER_DROPOUT_SECONDS = 0.30
NATIVE_PAUSE_FOCUS_RESUME_SECONDS = 0.50


NATIVE_RENDERING_FLAGS = 0x022C7F90
NATIVE_UI_RENDER_ENABLED_MASK = 1 << 3


NATIVE_BOSS_GAUGE_MANAGER = 0x02B62D88
NATIVE_BOSS_GAUGE_OWNER_OFFSET = 0xC0
NATIVE_BOSS_GAUGE_OBJECT_OFFSETS = (0xC8, 0xD0)
NATIVE_BOSS_GAUGE_RENDER_FLAGS_OFFSET = 0x130
NATIVE_BOSS_GAUGE_VISIBLE_MASK = 0x10
# These are the two globals consulted by the game's own interaction update before
# accepting a candidate. Disassembly of 0x25C830..0x25D45D proved that it tests
# no action or animation field: suppression consists entirely of these globals,
# read through 0x25C8FE -> 0x226BD0 and 0x25C909 -> 0x233D20. Closing either also
# aborts interaction for the leader. The follower gate mirrors them instead of
# inferring that the leader "looks busy" from actor bits. Their exact semantics
# are intentionally not assumed; the mod applies the same test as the game.
NATIVE_INTERACTION_SUPPRESSION_FLAGS = 0x02089340
NATIVE_INTERACTION_SUPPRESSION_MASK = 0xF00
NATIVE_INTERACTION_EVENT_FLAGS = 0x01FD46D4
NATIVE_INTERACTION_EVENT_BUSY_MASK = 1 << 10
P1_MENU_OFFSET = 0xAB30
P1_ACTIVE_MENU_GLOBAL = 0x208EE48
P1_MENU_CALLBACK = 0x279E20
TARGET_ENTER_SECONDS = 0.24
TARGET_EXIT_SECONDS = 0.24
TARGET_BACKDROP_LAYOUT_SECONDS = 0.11
TARGET_BACKDROP_BOTTOM_PADDING = 4
TARGET_TRACK_SMOOTHING_SECONDS = 0.014
TARGET_RASTER_INTERVAL = 1.0 / 60.0

# The native list can omit a target for one or two samples without changing the
# selection. This tolerance prevents the marker from beginning to close and
# flickering when the same handle reappears.

TARGET_SAMPLE_DROPOUT_GRACE_SECONDS = 0.75
TARGET_PROJECTION_DROPOUT_GRACE_SECONDS = 0.50
TARGET_MARKER_DROPOUT_GRACE_SECONDS = 0.75
FRAME_INTERVAL = 1.0 / 60.0
FRAME_IO_RETRY_WINDOW_SECONDS = 2.0
WINDOW_MESSAGE_BUDGET_PER_FRAME = 256
TARGET_LIST_INTERVAL = 0.10
RUNTIME_CATALOG_POLL_SECONDS = 0.25
MENU_RASTER_WARM_DELAY_SECONDS = 0.05
MENU_RASTER_WARM_STEP_SECONDS = 0.01
NATIVE_INDICATOR_FIRST_DELAY = 0.075

ACTOR_CLASS_RECORD_OFFSET = 0xE68
CLASS_HAS_FLYING_INFO_OFFSET = 0x12
CLASS_HAS_FLYING_INFO_MASK = 0x80
CLASS_MOVEMENT_FLAGS_OFFSET = 0x18
CLASS_IS_FLYING_MASK = 0x10
NATIVE_INDICATOR_RETRY_DELAY = 0.35
NATIVE_INDICATOR_STATE_GRACE = 0.40
NATIVE_INDICATOR_PENDING_SECONDS = 30.0
NATIVE_INDICATOR_SOURCE_OFFSET = 0x2D0
NATIVE_INDICATOR_FLAGS_OFFSET = 0x2EC
NATIVE_INDICATOR_STATE_OFFSET = 0x2F0
P2_ACTION_STATE_OFFSET = 0x6B4
P2_ACTION_TARGET_OFFSET = 0x710
P2_ACTION_CURRENT_OFFSET = 0x714
P2_ACTION_IDLE_VALUES = frozenset((0xFFFF, 0x4021))


MIST_PRESENTATION_ACTION_IDS = frozenset((*range(0xD0, 0xF6), *range(0x106, 0x113)))
DISMISS_ACTION_ID = 0x113
ESPER_SUMMON_ACTION_IDS = frozenset(range(0x106, DISMISS_ACTION_ID))
SUMMON_OWNER_PENDING_SECONDS = 20.0
QUICKENING_PRESENTATION_ACTION_IDS = frozenset(range(0xD0, 0xE2))


NATIVE_MENU_SETTLE_SECONDS = 0.25


NATIVE_PARTY_MENU_TRANSFER_TIMEOUT_SECONDS = 1.25
NATIVE_FRONTEND_CLOSE_SETTLE_SECONDS = 0.20
NATIVE_FRONTEND_OPEN_SETTLE_SECONDS = 0.04
UNEXPECTED_NATIVE_COMMAND_CANCEL_RETRY_SECONDS = 0.20


P1_FRONTEND_DEFERRED_OPEN_SECONDS = 0.75


MENU_REOPEN_QUIET_SECONDS = 0.08


CHARACTER_SELECTOR_EXIT_DELAY = 0.30
MENU_EXIT_INPUT_DELAY = 0.30
NATIVE_LEADER_HANDOFF_TIMEOUT_SECONDS = 1.50
PARTY_IDENTITY_RECONCILE_SECONDS = 0.20
ERROR_ALREADY_EXISTS = 183


PERMANENT_STATUS_EFFECT_IMMUNITIES_OFFSET = 0x38
BATTLE_KEEP_LEVEL_OFFSET = 0x1C2
LIBRA_STATUS_MASK = 1 << 30


TEMPORARY_AUGMENTS_OFFSET = 0x68
PERMANENT_AUGMENTS_OFFSET = 0x78
ANTI_LIBRA_AUGMENT_BYTE_INDEX = 5
ANTI_LIBRA_AUGMENT_MASK = 0x02
# The temporary word carries common applied statuses, one bit per
# world_overlay.STATUS_EFFECTS entry; permanent statuses use the same order in
# the parallel word at +0x3C. Five statuses were observed entering and leaving
# here across party and enemies, all matching the game's order: Slow (15) on a
# Cactoid, Sleep (4) leaving a Cactite, Protect (18) on Penelo and an Urstrix,
# and Penelo changing from HP Critical (29) to KO (0) before being revived.
P2_CHANGE_CHARACTER_ACTION_ID = 0x10000
P2_CHANGE_CHARACTER_ACTION = ActionDefinition(
    P2_CHANGE_CHARACTER_ACTION_ID,
    "Change Character",
    "Choose which active party member is controlled by Player 2.",
    (TargetGroup.PARTY,),
    TargetGroup.PARTY,
    "Party",
)
P1_CHANGE_LEADER_ACTION_ID = 0x10001
P1_CHANGE_LEADER_ACTION = ActionDefinition(
    P1_CHANGE_LEADER_ACTION_ID,
    "Change Leader",
    "Choose the active party leader.",
    (TargetGroup.PARTY,),
    TargetGroup.PARTY,
    "Party",
)


def status_effect_words(process, battle_keep: int) -> tuple[int, int]:
    return (
        process.u32(battle_keep + TEMPORARY_STATUS_EFFECTS_OFFSET),
        process.u32(battle_keep + PERMANENT_STATUS_EFFECTS_OFFSET),
    )


def combined_status_effects(process, battle_keep: int) -> int:
    """Return the native union of temporary and permanent actor statuses."""

    temporary, permanent = status_effect_words(process, battle_keep)
    return temporary | permanent


# Native manual action submission rejects this effective set while co-op is
# running.  Berserk is intentionally absent: the resident patch explicitly
# enables manual co-op commands for it.  Immobilize restricts locomotion, not
# command/leader ownership, and likewise must not force a native handoff.
LEADER_CONTROL_UNAVAILABLE_STATUS_MASK = (
    (1 << 0)   # KO
    | (1 << 1)  # Stone
    | (1 << 3)  # Stop
    | (1 << 4)  # Sleep
    | (1 << 5)  # Confuse
    | (1 << 13)  # Disable
    | (1 << 31)  # X-Zone
)
LEADER_CONTROL_SNAPSHOT_ATTEMPTS = 3


def leader_control_availability(process, actor: int) -> bool | None:
    """Sample whether an actor can own co-op leader commands.

    ``None`` is deliberately distinct from unavailable.  A torn actor/battle
    rebuild cannot prove a handoff is needed and therefore must preserve any
    existing DeadLeader gate/latch until a coherent sample arrives.
    """

    if not actor:
        return None
    sample_errors = (OSError, RuntimeError, struct.error, TypeError, ValueError)
    for _attempt in range(LEADER_CONTROL_SNAPSHOT_ATTEMPTS):
        try:
            battle_keep_before = process.u64(actor + 0x698)
            if not isinstance(battle_keep_before, int):
                raise TypeError("battle-keep sample is not integral")
            if not battle_keep_before:
                return None
            hp_before = process.u32(battle_keep_before + 0x48)
            temporary_before = process.u32(
                battle_keep_before + TEMPORARY_STATUS_EFFECTS_OFFSET
            )
            permanent = process.u32(
                battle_keep_before + PERMANENT_STATUS_EFFECTS_OFFSET
            )
            temporary_after = process.u32(
                battle_keep_before + TEMPORARY_STATUS_EFFECTS_OFFSET
            )
            hp_after = process.u32(battle_keep_before + 0x48)
            battle_keep_after = process.u64(actor + 0x698)
            samples = (
                hp_before,
                temporary_before,
                permanent,
                temporary_after,
                hp_after,
                battle_keep_after,
            )
            if not all(isinstance(value, int) for value in samples):
                raise TypeError("leader availability sample is not integral")
        except sample_errors:
            continue
        if (
            battle_keep_before == battle_keep_after
            and hp_before == hp_after
            and temporary_before == temporary_after
        ):
            status = temporary_before | permanent
            return bool(
                hp_before
                and not (status & LEADER_CONTROL_UNAVAILABLE_STATUS_MASK)
            )
    return None


def ko_promoted_role_for_availability(
    identity: session_state.SessionIdentity,
    native_leader_member_id: int | None,
    p1_available: bool | None,
    *,
    p1_present: bool,
    previous_role: str | None,
) -> str | None:
    """Classify a native follower promotion without resolving UNKNOWN to KO."""

    if not p1_present:
        return None
    if p1_available is None:
        return previous_role if previous_role in ("p2", "p3") else None
    return session_state.ko_promoted_follower_role(
        identity,
        native_leader_member_id,
        p1_available,
        p1_present=True,
    )


def identity_reconcile_has_uncertain_follower_leader(
    identity: session_state.SessionIdentity,
    native_leader_member_id: int | None,
    p1_available: bool | None,
    *,
    p1_present: bool,
) -> bool:
    return bool(
        p1_available is None
        and p1_present
        and native_leader_member_id
        in (identity.p2_party_member_id, identity.p3_party_member_id)
    )


def member_id_for_actor(
    active_party: list[tuple[int, str, int, int]],
    actor: int,
    fallback: int,
) -> int:
    if actor:
        for member_id, _name, _handle, candidate_actor in active_party:
            if candidate_actor == actor:
                return int(member_id)
    return int(fallback)


def party_targets_in_hud_order(
    target_party: list[tuple[str, int, int]],
    active_party: list[tuple[int, str, int, int]],
    current_layout,
) -> tuple[tuple[str, int, int], ...]:
    """Order materialized allies from the top native HUD row to the bottom."""

    member_by_handle = {
        int(handle): int(member_id) for member_id, _name, handle, _actor in active_party
    }

    def visual_key(entry: tuple[str, int, int]) -> int:
        member_id = member_by_handle.get(int(entry[1]))
        if member_id is None:
            return 4
        row = party_hud_layout.party_hud_member_row(current_layout, member_id)
        return 4 if row is None else int(row)

    return tuple(sorted(target_party, key=visual_key))


def area_secondary_target_indices(
    candidates,
    selected_index: int,
    area_radius: float,
    positions_by_handle: dict[int, tuple[float, float, float]],
) -> tuple[int, ...]:
    """Resolve the non-primary candidates reached by one native area radius."""

    candidates = tuple(candidates)
    radius = float(area_radius)
    if len(candidates) <= 1 or not math.isfinite(radius) or radius <= 0.0:
        return ()
    selected_index = int(selected_index) % len(candidates)
    selected = candidates[selected_index]

    # Reserve members do not have materialized world actors.  Native target
    # presentation treats an area action there as a logical group instead of
    # pretending that zero handles share a physical coordinate.
    if selected.group is TargetGroup.RESERVE:
        return tuple(
            index for index in range(len(candidates)) if index != selected_index
        )

    center = positions_by_handle.get(int(selected.handle))
    if center is None or len(center) < 3:
        return ()
    if not all(math.isfinite(float(value)) for value in center[:3]):
        return ()

    reached: list[int] = []
    radius_squared = radius * radius
    for index, candidate in enumerate(candidates):
        if index == selected_index:
            continue
        position = positions_by_handle.get(int(candidate.handle))
        if position is None or len(position) < 3:
            continue
        try:
            delta_x = float(position[0]) - float(center[0])
            delta_z = float(position[2]) - float(center[2])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(delta_x) or not math.isfinite(delta_z):
            continue
        if delta_x * delta_x + delta_z * delta_z <= radius_squared + 1.0e-6:
            reached.append(index)
    return tuple(reached)


def native_leader_actor_for(
    active_party: list[tuple[int, str, int, int]],
    leader_party_member_id: int | None,
) -> int:
    if leader_party_member_id is None:
        return 0
    return next(
        (
            int(actor)
            for member_id, _name, _handle, actor in active_party
            if int(member_id) == int(leader_party_member_id)
        ),
        0,
    )


def follower_interaction_leader_flags(
    process,
    leader_actor: int,
) -> int | None:
    if not leader_actor:
        return None
    try:
        return process.u64(int(leader_actor))
    except (OSError, RuntimeError, struct.error, ValueError):
        return None


def member_id_for_actor_slot(
    process,
    actor: int,
    current_layout: tuple[int, ...],
) -> int | None:
    if not actor:
        return None
    try:
        manager = process.u64(legacy.coop.ACTOR_MANAGER_GLOBAL)
        count = process.u32(manager) if manager else 0
        if not count or count > 4096:
            return None
        for actor_id in range(count):
            bwei = process.u64(manager + 8 + actor_id * 8)
            if not bwei or process.u64(bwei + 0x30) != actor:
                continue
            party_slot = process.read(bwei + 0x12, 1)[0]
            if party_slot >= min(4, len(current_layout)):
                return None
            member_id = int(current_layout[party_slot])
            return None if member_id == session_state.EMPTY_PARTY_ID else member_id
    except (OSError, RuntimeError, struct.error, ValueError):
        return None
    return None


def targetable_party_actor_slots(
    process,
    active_slots: dict[int, tuple],
    current_layout: tuple[int, ...],
) -> dict[int, tuple]:
    result = dict(active_slots)
    try:
        manager = process.u64(legacy.coop.ACTOR_MANAGER_GLOBAL)
        count = process.u32(manager) if manager else 0
    except (OSError, RuntimeError, struct.error, ValueError):
        return result
    if not manager or not 0 < count <= 4096:
        return result
    for actor_id in range(count):
        try:
            bwei = process.u64(manager + 8 + actor_id * 8)
            actor = process.u64(bwei + 0x30) if bwei else 0
            if not actor:
                continue
            party_slot = process.read(bwei + 0x12, 1)[0]
            if (
                party_slot in result
                or party_slot >= min(4, len(current_layout))
                or current_layout[party_slot] == session_state.EMPTY_PARTY_ID
            ):
                continue
            handle = process.u32(actor + 0x08)
            battle_keep = process.u64(actor + 0x698)
            if not handle or (handle & 0xFFFF) != actor_id or not battle_keep:
                continue
            position = struct.unpack("<3f", process.read(actor + 0xD0, 12))
            if not all(
                math.isfinite(value) and abs(value) < 1.0e8 for value in position
            ):
                continue
            root = process.u64(actor + 0x10)
            phys = process.u64(root + 0xB8) if root else 0
            result[party_slot] = (
                actor_id,
                actor,
                phys,
                position,
            )
        except (OSError, RuntimeError, struct.error, ValueError):
            continue
    return dict(sorted(result.items()))


def reserve_party_candidates(
    complete_layout: tuple[int, ...],
    active_member_ids: set[int] | frozenset[int],
) -> tuple[TargetCandidate, ...]:
    """Build non-materialized targets from Default/Menu's reserve slots."""

    active = {int(member_id) for member_id in active_member_ids}
    seen: set[int] = set()
    result: list[TargetCandidate] = []
    for raw_member_id in complete_layout[4:]:
        member_id = int(raw_member_id)
        if (
            member_id == session_state.EMPTY_PARTY_ID
            or member_id in active
            or member_id in seen
            or not 0 <= member_id <= 0xFF
        ):
            continue
        seen.add(member_id)
        result.append(
            TargetCandidate(
                0,
                session_state.PARTY_NAMES.get(member_id, f"Party {member_id}"),
                TargetGroup.RESERVE,
                party_member_id=member_id,
            )
        )
    return tuple(result)


def complete_party_layout(
    process,
    battle_info: int,
    current_layout: tuple[int, ...],
) -> tuple[int, ...]:
    """Read the complete native roster without mistaking field slots for it.

    Current/UI intentionally contain only materialized field members.  Native
    Default/Menu layouts retain the unlocked reserve members in slots 4..8.
    Both complete layouts must agree on membership and contain every active
    field member; otherwise a party transition is in flight and we fail closed
    to Current rather than exposing a stale reserve target.
    """

    fallback = tuple(current_layout)
    if not battle_info or len(fallback) != 9:
        return fallback
    try:
        layouts = tuple(
            struct.unpack(
                "<9H",
                process.read(
                    int(battle_info)
                    + session_state.BATTLE_PARTY_LAYOUT_OFFSET
                    + layout_id * session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                    session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                ),
            )
            for layout_id in (BATTLE_DEFAULT_LAYOUT, BATTLE_MENU_LAYOUT)
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        return fallback

    def members(layout: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(
            int(member_id)
            for member_id in layout
            if member_id != session_state.EMPTY_PARTY_ID
        )

    default_members = members(layouts[0])
    menu_members = members(layouts[1])
    active_members = {
        int(member_id)
        for member_id in fallback[:4]
        if (
            member_id != session_state.EMPTY_PARTY_ID
            # An Esper is materialized temporarily in Current/UI but is not a
            # member of the persistent Default/Menu roster. Requiring it there
            # rejects a stable layout and erases all reserve healing targets.
            and member_id not in session_state.ESPER_PARTY_MEMBER_IDS
        )
    }
    if (
        not default_members
        or len(set(default_members)) != len(default_members)
        or len(set(menu_members)) != len(menu_members)
        or set(default_members) != set(menu_members)
        or not active_members.issubset(default_members)
    ):
        return fallback
    return layouts[1]


def sleeping_mimic_state(process, handle: int) -> int | None:
    handle &= 0xFFFFFFFF
    if not handle:
        return None
    try:
        manager = process.u64(legacy.coop.ACTOR_MANAGER_GLOBAL)
        count = process.u32(manager) if manager else 0
        actor_id = handle & 0xFFFF
        if not manager or actor_id >= count or count > 4096:
            return None
        bwei = process.u64(manager + 8 + actor_id * 8)
        actor = process.u64(bwei + 0x30) if bwei else 0
        if not actor or process.u32(actor + 0x08) != handle:
            return None
        if process.read(bwei + 0x0E, 1)[0] & 0x0F != MIMIC_UNIT_TYPE:
            return None
        extended = process.u64(actor + 0x6A0)
        if not extended:
            return None
        if not process.read(extended, 1)[0] & MIMIC_ASLEEP_BKEI_FLAG:
            return None
    except (OSError, RuntimeError, struct.error):
        return None
    return NATIVE_INTERACTION_MIMIC_STATE


def forced_interaction_state(descriptor: bytes, handle: int) -> int:
    if len(descriptor) < 0x18:
        return NATIVE_INTERACTION_MINIMUM_STATE
    state, live_handle = struct.unpack_from("<II", descriptor, 0x0C)
    if (
        live_handle == (handle & 0xFFFFFFFF)
        and state >= NATIVE_INTERACTION_MINIMUM_STATE
    ):
        return state
    return NATIVE_INTERACTION_MINIMUM_STATE


def interaction_matches_live_foe(
    interaction: tuple[int, int, int, int] | None,
    foes: list[tuple[str, int, int]],
) -> bool:
    if interaction is None:
        return False
    handle = int(interaction[1]) & 0xFFFFFFFF
    return bool(
        handle
        and any(
            (int(foe_handle) & 0xFFFFFFFF) == handle
            for _name, foe_handle, _actor in foes
        )
    )


def action_line_theme_entries(
    active_party: list[tuple[int, str, int, int]],
    selection: menu_themes.ThemeSelection,
    *,
    player_by_handle: dict[int, int] | None = None,
) -> tuple[tuple[int, int], ...]:
    owners = {
        int(handle) & 0xFFFFFFFF: int(player)
        for handle, player in (player_by_handle or {}).items()
        if handle
    }

    candidates = (
        active_party[:3]
        if selection.assignment_mode == menu_themes.ASSIGNMENT_MODE_CHARACTER
        else active_party
    )
    entries: list[tuple[int, int]] = []
    for member_id, _name, raw_handle, _actor in candidates:
        handle = int(raw_handle) & 0xFFFFFFFF
        if not handle:
            continue
        player = owners.get(handle)
        if selection.assignment_mode == menu_themes.ASSIGNMENT_MODE_PLAYER:
            if player is None:
                continue
            theme_name = selection.for_player(player, member_id)
        else:
            theme_name = selection.for_character(member_id)
        entries.append((handle, menu_themes.target_circle_palette(theme_name)))
        if len(entries) >= ACTION_LINE_THEME_MAX_ENTRIES:
            break
    return tuple(entries)


def character_selector_theme(
    current_theme: str,
    resolved_theme: str,
    *,
    selector_active: bool,
) -> str:
    """Keep colour ownership stable until a character swap is committed."""

    if selector_active and current_theme in menu_themes.PRESETS:
        return current_theme
    return resolved_theme


def p1_command_mask_enabled(
    replacement_enabled: bool,
    *,
    native_menu_open: bool,
    native_command_open: bool,
    close_guard_active: bool = False,
    native_menu_settled: bool = False,
) -> bool:
    return bool(
        replacement_enabled
        and (
            not native_menu_open
            or native_command_open
            or (close_guard_active and not native_menu_settled)
        )
    )


def p1_command_mask_requested(
    replacement_enabled: bool,
    *,
    user_control_enabled: bool,
    native_command_open: bool,
    close_guard_active: bool,
    native_pause_open: bool,
    native_modal_active: bool,
    town: bool = False,
    chocobo_mode_active: bool = False,
    game_thread_handoff: bool = False,
) -> bool:
    return bool(
        replacement_enabled
        and not town
        and not chocobo_mode_active
        and not native_pause_open
        and not native_modal_active
        and (
            user_control_enabled
            or native_command_open
            or close_guard_active
            or game_thread_handoff
        )
    )


def p1_frontend_close_handoff_active(
    *,
    suspended: bool,
    observed_open: bool,
    close_candidate_since: float,
    native_menu_open: bool,
    native_modal_active: bool,
    user_control_enabled: bool,
    loading: bool,
    cutscene: bool,
    town: bool,
) -> bool:
    return bool(
        suspended
        and observed_open
        and float(close_candidate_since) > 0.0
        and not native_menu_open
        and not native_modal_active
        and user_control_enabled
        and not loading
        and not cutscene
        and not town
    )


def custom_directional_input_owned(
    p1_mode: MenuMode,
    p2_mode: MenuMode,
    p3_mode: MenuMode,
    *,
    p1_transition_kind: str | None = None,
    p2_transition_kind: str | None = None,
    p3_transition_kind: str | None = None,
    p1_release_guard_active: bool = False,
    p2_release_guard_active: bool = False,
    p3_release_guard_active: bool = False,
) -> bool:
    modes = (p1_mode, p2_mode, p3_mode)
    transitions = (
        p1_transition_kind,
        p2_transition_kind,
        p3_transition_kind,
    )
    release_guards = (
        p1_release_guard_active,
        p2_release_guard_active,
        p3_release_guard_active,
    )
    return bool(
        any(mode is not MenuMode.CLOSED for mode in modes)
        or any(transition is not None for transition in transitions)
        or any(release_guards)
    )


def custom_directional_bridge_enabled(
    bridge_ownership_allowed: bool,
    custom_ui_suppressed: bool,
    directional_owner: bool,
) -> bool:
    return bool(
        bridge_ownership_allowed and not custom_ui_suppressed and directional_owner
    )


def p1_menu_input_owned(mode: MenuMode, transition_kind: str | None) -> bool:
    return bool(mode is not MenuMode.CLOSED or transition_kind is not None)


def p1_menu_owns_lb(
    mode: MenuMode,
    *,
    directional_owner: bool | None = None,
    leader_selector_active: bool = False,
) -> bool:
    """Reserve P1 LB except while the custom Leader selector needs Turbo."""

    lifecycle_owned = (
        mode is not MenuMode.CLOSED
        if directional_owner is None
        else bool(directional_owner)
    )
    return bool(lifecycle_owned and not leader_selector_active)


@dataclass
class DirectionalReleaseGuard:
    delay_seconds: float = MENU_EXIT_INPUT_DELAY
    requires_release: bool = False
    blocked_until: float = 0.0

    def update(
        self,
        *,
        lifecycle_owned: bool,
        directional_down: bool,
        now: float,
    ) -> bool:
        now = float(now)
        if lifecycle_owned:
            self.requires_release = True
            self.blocked_until = max(self.blocked_until, now + self.delay_seconds)
        elif self.requires_release and not directional_down:
            self.requires_release = False

            self.blocked_until = max(self.blocked_until, now + self.delay_seconds)
        return bool(self.requires_release or now < self.blocked_until)


@dataclass
class NativePauseVisibilityGate:
    """Keep custom input suspended across transient Pause pointer gaps."""

    pointer_dropout_seconds: float = NATIVE_PAUSE_POINTER_DROPOUT_SECONDS
    focus_resume_seconds: float = NATIVE_PAUSE_FOCUS_RESUME_SECONDS
    latched: bool = False
    last_confirmed_at: float = float("-inf")
    focus_guard_until: float = 0.0
    previous_foreground: bool | None = None

    def arm(self, *, now: float, foreground: bool) -> None:
        """Cover the interval between an accepted Pause edge and its object."""

        now = float(now)
        self.latched = True
        self.last_confirmed_at = now
        self.previous_foreground = bool(foreground)

    def update(self, raw_open: bool, *, foreground: bool, now: float) -> bool:
        now = float(now)
        foreground = bool(foreground)
        focus_resumed = bool(
            foreground and self.previous_foreground is False
        )
        self.previous_foreground = foreground

        if raw_open:
            self.latched = True
            self.last_confirmed_at = now
        if not self.latched:
            return False
        if not foreground:
            return True
        if focus_resumed:
            self.focus_guard_until = max(
                self.focus_guard_until,
                now + self.focus_resume_seconds,
            )
        if raw_open:
            return True

        guarded_until = max(
            self.last_confirmed_at + self.pointer_dropout_seconds,
            self.focus_guard_until,
        )
        if now < guarded_until:
            return True
        self.latched = False
        self.focus_guard_until = 0.0
        return False


def custom_directional_control_down(bindings) -> bool:
    players = ("p1", "p2", "p3")
    actions = (
        "menu_up",
        "menu_down",
        "menu_left",
        "menu_right",
        "target_previous",
        "target_next",
    )
    if any(bindings.down(player, action) for player in players for action in actions):
        return True
    return any(
        bindings.xinput_control_down("p1", control)
        for control in ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
    )


def p1_pause_suspension_required(
    native_menu_open: bool,
    native_command_open: bool,
    native_pause_open: bool = False,
) -> bool:
    _ = native_menu_open, native_command_open
    return bool(native_pause_open)


def native_pause_screen_open(process) -> bool:
    try:
        return bool(
            process.u64(NATIVE_PAUSE_PRIMARY_OBJECT)
            or process.u64(NATIVE_PAUSE_SECONDARY_OBJECT)
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        return False


def native_ui_render_suppressed(process) -> bool:
    try:
        flags = struct.unpack("<H", process.read(NATIVE_RENDERING_FLAGS, 2))[0]
    except (OSError, RuntimeError, struct.error, ValueError, TypeError):
        return False
    return not bool(flags & NATIVE_UI_RENDER_ENABLED_MASK)


def native_boss_gauge_visible(process) -> bool:
    try:
        manager = process.u64(NATIVE_BOSS_GAUGE_MANAGER)
        owner = process.u64(manager + NATIVE_BOSS_GAUGE_OWNER_OFFSET) if manager else 0
        if not owner:
            return False
        for offset in NATIVE_BOSS_GAUGE_OBJECT_OFFSETS:
            ui_object = process.u64(owner + offset)
            if (
                ui_object
                and process.u32(ui_object + NATIVE_BOSS_GAUGE_RENDER_FLAGS_OFFSET)
                & NATIVE_BOSS_GAUGE_VISIBLE_MASK
            ):
                return True
    except (OSError, RuntimeError, struct.error, ValueError):
        return False
    return False


def native_party_action_ids(process, party) -> frozenset[int]:
    action_ids: set[int] = set()
    for entry in party:
        try:
            actor = int(entry[2])
            if not actor:
                continue
            action_id = struct.unpack(
                "<H", process.read(actor + P2_ACTION_CURRENT_OFFSET, 2)
            )[0]
        except (IndexError, TypeError, ValueError, OSError, struct.error):
            continue
        action_ids.add(action_id)
    return frozenset(action_ids)


def custom_ui_suppression_required(
    snapshot: session_state.SessionSnapshot,
    *,
    native_ui_suppressed: bool = False,
    native_pause_open: bool = False,
    mist_presentation_active: bool = False,
    owned_wait_presentation: bool = False,
) -> bool:
    return bool(
        (native_ui_suppressed and not owned_wait_presentation)
        or native_pause_open
        or mist_presentation_active
        or snapshot.loading
        or snapshot.cutscene
        or not snapshot.user_control_enabled
        or snapshot.native_menu_open
        or snapshot.native_modal_active
        or snapshot.town
    )


def controlled_actor_alive(process, actor: int) -> bool | None:
    if not actor:
        return None
    try:
        battle_keep = process.u64(actor + 0x698)
        if not battle_keep:
            return None
        hp = process.u32(battle_keep + 0x48)
        status = combined_status_effects(process, battle_keep)
    except (OSError, struct.error, ValueError):
        return None
    return bool(hp and not (status & 0x01))


def custom_menu_input_allowed(
    session_allowed: bool,
    actor_alive: bool | None,
) -> bool:
    return bool(session_allowed and actor_alive is True)


def p3_session_menu_allowed(
    snapshot: session_state.SessionSnapshot,
    party_count: int,
) -> bool:
    current_layout = getattr(snapshot, "current_layout", ())
    ui_layout = getattr(snapshot, "ui_layout", ())
    summon_owner_role = (
        session_state.active_summon_owner_role(
            snapshot.identity,
            current_layout,
            ui_layout,
            getattr(snapshot, "leader_party_member_id", None),
        )
        if current_layout and ui_layout
        else None
    )
    follower_ready = bool(
        getattr(
            snapshot,
            "gameplay_ready",
            getattr(snapshot, "p2_menu_allowed", False),
        )
        and getattr(snapshot, "user_control_enabled", True)
        and not getattr(snapshot, "native_menu_open", False)
        and not getattr(snapshot, "native_modal_active", False)
        and not getattr(snapshot, "town", False)
        and not getattr(snapshot, "chocobo_mode_active", False)
    )
    if summon_owner_role == "p3":
        return bool(
            follower_ready
            and snapshot.identity.p3_party_member_id is not None
            and int(party_count) >= 1
        )
    return bool(
        follower_ready
        and summon_owner_role is None
        and snapshot.identity.p3_party_member_id is not None
        and int(party_count) >= 3
    )


def combat_log_field_layout_allowed(
    snapshot: session_state.SessionSnapshot,
) -> bool:
    return bool(snapshot.p1_gameplay_ready)


def combat_log_raise_requested(
    snapshot: session_state.SessionSnapshot,
    *,
    custom_menu_open: bool,
    custom_menu_transition_active: bool,
) -> bool:
    """Raise the stock log only while a custom command panel occupies its lane."""

    return bool(
        combat_log_field_layout_allowed(snapshot)
        and (custom_menu_open or custom_menu_transition_active)
    )


def model_target_content_rows(model: P2MenuModel) -> int:
    candidates = model.valid_candidates()
    return renderer.target_content_rows(
        model.target_group.value,
        len(candidates),
        show_party_names=bool(
            model.pending_action is not None and model.pending_action.revive_only
        ),
    )


def coop_character_role_candidate(
    member_id: int,
    p1_owner_member_id: int,
    p1_reserved_member_id: int | None = None,
) -> bool:
    member_id = int(member_id)
    p1_owner_member_id = int(p1_owner_member_id)
    p1_reserved_member_id = (
        p1_owner_member_id
        if p1_reserved_member_id is None
        else int(p1_reserved_member_id)
    )
    if member_id not in session_state.MAIN_PARTY_MEMBER_IDS:
        return False
    # A stable P1 role is a valid exchange target: selecting it from P2/P3
    # atomically swaps both owners through the native leader transaction.  A
    # temporary KO/summon override must settle before either P1 identity can
    # participate, otherwise two roles could briefly claim the same actor.
    return bool(
        p1_owner_member_id == p1_reserved_member_id
        or member_id not in (p1_owner_member_id, p1_reserved_member_id)
    )


def native_party_menu_request_allowed(
    snapshot: session_state.SessionSnapshot,
    player: str,
    party_count: int,
) -> bool:
    if (
        snapshot.loading
        or snapshot.cutscene
        or not snapshot.user_control_enabled
        or snapshot.native_menu_open
        or snapshot.native_modal_active
    ):
        return False
    party_count = int(party_count)
    if player == "p1":
        return bool(snapshot.p1_gameplay_ready and party_count >= 1)
    if player == "p2":
        return bool(
            snapshot.gameplay_ready
            and snapshot.identity.p2_party_member_id is not None
            and party_count >= 2
        )
    if player == "p3":
        return bool(
            snapshot.gameplay_ready
            and snapshot.identity.p3_party_member_id is not None
            and party_count >= 3
        )
    return False


def native_pause_request_allowed(snapshot: session_state.SessionSnapshot) -> bool:
    """Allow the stock Pause screen wherever normal field control is available."""

    return bool(
        snapshot.p1_gameplay_ready
        and snapshot.user_control_enabled
        and not snapshot.loading
        and not snapshot.cutscene
        and not snapshot.native_menu_open
        and not snapshot.native_modal_active
    )


def p1_command_firewall_state(
    ordinary_policy: bool,
    *,
    coop_active: bool,
    leader_selection_pending: bool,
) -> bool:
    """Prevent the forced dead-leader frontend from reclaiming P1 buttons."""

    return bool(
        ordinary_policy or (coop_active and leader_selection_pending)
    )


def native_party_recovery_required(
    active_actor_alive: tuple[bool | None, ...],
) -> bool:
    """Return whether FF12 must own input to recover an entirely dead party.

    ``None`` is fail-closed because it can mean an actor is being rebuilt.  The
    native recovery frontend is released only after every active actor has
    produced a coherent dead sample.
    """

    states = tuple(active_actor_alive)
    return bool(states) and all(state is False for state in states)


def native_party_recovery_passthrough_required(
    active_actor_alive: tuple[bool | None, ...],
    *,
    latched: bool,
    native_menu_observed: bool,
    native_menu_open: bool,
    native_frontend_suspended: bool,
    suspension_source: str,
) -> bool:
    """Keep native input ownership until the forced recovery menu truly closes."""

    if native_party_recovery_required(active_actor_alive):
        return True
    if not latched:
        return False
    if not native_menu_observed:
        return True
    return bool(
        native_menu_open
        or (
            native_frontend_suspended
            and suspension_source == "dead_party_recovery"
        )
    )


def native_party_menu_source_allowed(
    snapshot: session_state.SessionSnapshot,
    player: str,
    party_count: int,
    *,
    coop_active: bool,
    p2_manual_control: bool,
    p3_manual_control: bool,
) -> bool:
    if not coop_active:
        return native_party_menu_request_allowed(snapshot, "p1", party_count)
    if player == "p2" and not p2_manual_control:
        return False
    if player == "p3" and not p3_manual_control:
        return False
    return native_party_menu_request_allowed(snapshot, player, party_count)


def p1_native_party_request_blocked(
    mode: MenuMode,
    transition_kind: str | None,
    battle_menu_edge: bool,
) -> bool:
    return bool(battle_menu_edge or p1_menu_input_owned(mode, transition_kind))


@dataclass
class DeadLeaderMenuGate:
    pending: bool = False
    dead_member_id: int | None = None

    def update(
        self,
        *,
        owner_alive: bool | None,
        owner_member_id: int | None,
        native_leader_member_id: int | None,
        native_leader_alive: bool | None,
        user_control_enabled: bool,
    ) -> bool:
        owner = None if owner_member_id is None else int(owner_member_id)
        leader = (
            None if native_leader_member_id is None else int(native_leader_member_id)
        )

        if self.pending:
            original_recovered = bool(
                owner_alive is True
                and leader is not None
                and leader == owner
                and user_control_enabled
            )
            replacement_selected = bool(
                leader is not None
                and leader != self.dead_member_id
                and native_leader_alive is True
                and user_control_enabled
            )
            if original_recovered or replacement_selected:
                self.pending = False
                self.dead_member_id = None
            return self.pending

        if owner_alive is not False:
            return False

        if (
            leader is not None
            and owner is not None
            and leader != owner
            and native_leader_alive is True
            and user_control_enabled
        ):
            return False

        self.pending = True
        self.dead_member_id = owner
        return True


def native_party_follower_button(
    *,
    p2_up: bool = False,
    p2_down: bool = False,
    p2_left: bool = False,
    p2_right: bool = False,
    p2_confirm: bool = False,
    p2_cancel: bool = False,
    p2_party_menu: bool = False,
    p2_previous: bool = False,
    p2_next: bool = False,
    p3_up: bool = False,
    p3_down: bool = False,
    p3_left: bool = False,
    p3_right: bool = False,
    p3_confirm: bool = False,
    p3_cancel: bool = False,
    p3_party_menu: bool = False,
    p3_previous: bool = False,
    p3_next: bool = False,
) -> tuple[str, int] | None:
    controls = (
        ("p2", p2_cancel, NATIVE_DIALOG_CANCEL),
        ("p3", p3_cancel, NATIVE_DIALOG_CANCEL),
        ("p2", p2_party_menu, NATIVE_DIALOG_PARTY_MENU),
        ("p3", p3_party_menu, NATIVE_DIALOG_PARTY_MENU),
        ("p2", p2_confirm, NATIVE_DIALOG_CONFIRM),
        ("p3", p3_confirm, NATIVE_DIALOG_CONFIRM),
        ("p2", p2_previous, NATIVE_DIALOG_LB),
        ("p3", p3_previous, NATIVE_DIALOG_LB),
        ("p2", p2_next, NATIVE_DIALOG_RB),
        ("p3", p3_next, NATIVE_DIALOG_RB),
        ("p2", p2_up and not p2_down, NATIVE_DIALOG_UP),
        ("p2", p2_down and not p2_up, NATIVE_DIALOG_DOWN),
        ("p2", p2_left and not p2_right, NATIVE_DIALOG_LEFT),
        ("p2", p2_right and not p2_left, NATIVE_DIALOG_RIGHT),
        ("p3", p3_up and not p3_down, NATIVE_DIALOG_UP),
        ("p3", p3_down and not p3_up, NATIVE_DIALOG_DOWN),
        ("p3", p3_left and not p3_right, NATIVE_DIALOG_LEFT),
        ("p3", p3_right and not p3_left, NATIVE_DIALOG_RIGHT),
    )
    return next(
        ((player, button) for player, active, button in controls if active),
        None,
    )


def party_identity_reconcile_delay(
    snapshot: session_state.SessionSnapshot,
    replacement: session_state.SessionIdentity,
) -> float:
    del snapshot, replacement
    # Native leader and status/layout words are published independently.  Every
    # non-transactional identity change must remain coherent long enough for a
    # KO, Stone, X-Zone or party-removal transition to settle before persistence.
    return PARTY_IDENTITY_RECONCILE_SECONDS


def transient_native_leader_handoff_active(process) -> bool:
    address = (
        legacy.coop.P2_DEVICE_FILTER_STATE
        + legacy.coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
    )
    if not legacy.coop.P2_DEVICE_FILTER_STATE:
        return False
    try:
        return (
            process.u32(address)
            == legacy.coop.NATIVE_LEADER_HANDOFF_TRANSIENT
        )
    except (OSError, RuntimeError, struct.error, ValueError):
        return False


P2_INTERACTION_STATE_POINTER_OFFSET = 0xB0
P2_INTERACTION_ENTRY_POINTER_OFFSET = 0xB8
P2_INTERACTION_OWNER_OFFSET = 0x40
INTERACTION_OWNER_NONE = 0
INTERACTION_OWNER_P2 = 2
INTERACTION_OWNER_P3 = 3


EXECUTION_ANIMATION_FLAG = 1 << 46
P2_INTERACTION_RESOURCE_SLOTS_OFFSET = 0x40
# A publication remains valid for several passes so a short scan gap does not
# erase the prompt between frames. This interval controls how often the painter
# rereads the leader gate: short enough for the button row to disappear with the
# animation, but long enough to avoid one follower memory read per 120 Hz frame.
LEADER_INTERACTION_GATE_SAMPLE_SECONDS = 1 / 30
INTERACTION_PUBLISH_STALE_PASSES = 4
P2_INTERACTION_TEXT_SLOTS_OFFSET = 0x80
P2_INTERACTION_STATE_SLOTS_OFFSET = 0xC0
P2_INTERACTION_HANDLE_SLOTS_OFFSET = 0xE0
P2_INTERACTION_EXACT_DESCRIPTOR_OFFSET = 0x100
P2_INTERACTION_EXACT_CAN_USE_OFFSET = 0x118
P2_INTERACTION_SNAPSHOT_OFFSET = P2_INTERACTION_TEXT_SLOTS_OFFSET
P2_INTERACTION_SNAPSHOT_SIZE = (
    max(
        P2_INTERACTION_EXACT_CAN_USE_OFFSET + 1,
        legacy.coop.DUAL_INTERACTION_PUBLISH_TICK_OFFSET + 4,
        legacy.coop.DUAL_INTERACTION_PUBLISH_MARK_OFFSET + 4,
    )
    - P2_INTERACTION_SNAPSHOT_OFFSET
)
NATIVE_INTERACTION_SOURCE = 0x2099D78
NATIVE_INTERACTION_SLOT_BLOCK = 0x2B59C40
NATIVE_INTERACTION_OUTPUT = 0x2B59D00
NATIVE_INTERACTION_RANGE_FLAG_OFFSET = 0x4A1
NATIVE_INTERACTION_DESCRIPTOR = 0x209A2B0
NATIVE_INTERACTION_CAN_USE_GATE = 0x209A2AA
NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS = 0x3F1999AC
NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS = 0x01010100


NATIVE_INTERACTION_MINIMUM_STATE = 2
# A sleeping Mimic chest uses a dedicated wake state. Applying that byte to all
# interactions gives chests, crystals and NPCs an invalid state, so the correction
# applies only to the Mimic handle.
NATIVE_INTERACTION_MIMIC_STATE = 10

MIMIC_UNIT_TYPE = 4
MIMIC_ASLEEP_BKEI_FLAG = 0x20


NATIVE_NPC_NAME_KEY_OFFSET = 0x102
NATIVE_NPC_DIRECT_TEXT_OFFSET = 0xF8
NATIVE_NPC_WORLD_POSITION_OFFSET = 0x120
NATIVE_NPC_NAME_VARIANT_FLAG = 0x4000
NATIVE_NPC_NAME_STATE_BASE = 0x2164480
NATIVE_NPC_NAME_STATE_BITS_OFFSET = 0x13B4
NATIVE_NPC_NAME_STATE_LIMIT = 0x800
NATIVE_NPC_TEXT_TABLE_POINTER = 0x2B5E0D8
NATIVE_NPC_TEXT_TABLE_COUNT_OFFSET = 0x08
NATIVE_NPC_TEXT_TABLE_ENTRIES_OFFSET = 0x0C


# Must match coop_runtime.FOLLOWER_NPC_INTERACTIONS_ENABLED.  Descriptor-only
# follower NPC prompts are unsafe while the native exact-NPC route still shares
# P1's global descriptor.
FOLLOWER_NPC_INTERACTIONS_ENABLED = False


P1_WORLD_INTERACTION_DROPOUT_GRACE_SECONDS = 0.06


NATIVE_INTERACTION_CONSUMER_CALL = 0x3141F7
NATIVE_INTERACTION_CONSUMER_TARGET = 0x268D00
NATIVE_INTERACTION_AUXILIARY_RESOLVER = 0x263990
NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL = b"\xe8" + struct.pack(
    "<i",
    NATIVE_INTERACTION_CONSUMER_TARGET - (NATIVE_INTERACTION_CONSUMER_CALL + 5),
)
P2_INTERACTION_CONSUMER_CAVE_OFFSET = 0xE000
P2_INTERACTION_CONSUMER_MAILBOX_OFFSET = 0xE300
P2_INTERACTION_CONSUMER_CAVE_LIMIT = (
    P2_INTERACTION_CONSUMER_MAILBOX_OFFSET - P2_INTERACTION_CONSUMER_CAVE_OFFSET
)
P2_INTERACTION_CONSUMER_MAGIC = b"P2ICW002"
P2_INTERACTION_CONSUMER_ARMED_OFFSET = 0x08
P2_INTERACTION_CONSUMER_HIT_OFFSET = 0x0C
P2_INTERACTION_CONSUMER_DONE_OFFSET = 0x10
P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET = 0x14
P2_INTERACTION_CONSUMER_LEADER_MEMBER_OFFSET = 0x18
P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET = 0x1C
P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET = 0x20
P2_INTERACTION_CONSUMER_OBJECT_OFFSET = 0x38
P2_INTERACTION_CONSUMER_SOURCE_OFFSET = 0x40
P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET = 0x48
P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET = 0x50
P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET = 0x54
P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET = 0x58
P2_INTERACTION_CONSUMER_POLICY_STOCK_OWNER = 0
P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER = 1
P2_INTERACTION_CONSUMER_POLICY_TREASURE = 2
P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT = 3
P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN = 4
P2_INTERACTION_CONSUMER_MAILBOX_SIZE = 0x80

# Unlike the retired continuous exact-NPC publisher, this helper owns FF12's
# singleton descriptor only inside one native call.  The three measured object
# guards are active for that call alone, and the P1 descriptor/control globals
# are restored before the game thread returns to its stock frame.
FOLLOWER_NPC_ATOMIC_INTERACTIONS_ENABLED = True
FOLLOWER_NPC_ATOMIC_CAVE_OFFSET = legacy.coop.FOLLOWER_NPC_EXACT_SCAN_CAVE_OFFSET
FOLLOWER_NPC_ATOMIC_CAVE_LIMIT = legacy.coop.FOLLOWER_NPC_EXACT_SCAN_CAVE_LIMIT
NATIVE_INTERACTION_RESET_DISTANCE_BITS = 0x501502F9
NATIVE_INTERACTION_RESET_READY_FLAGS = 0x01010101
NATIVE_ENTITY_RESOLVER = 0x3588B0


P2_INTERACTION_HEAD_HEIGHT = 1.92


P2_INTERACTION_OUTER_MARKER_SCALE = 1.10
P2_INTERACTION_INNER_MARKER_SCALE = 1.22
P2_INTERACTION_ATLAS_PATH = (
    Path(__file__).resolve().parent
    / "runtime_ui_assets_png"
    / "battle_4_p__pic00__pal00.png"
)
P2_INTERACTION_OUTER_BOX = (862, 132, 894, 189)
P2_INTERACTION_INNER_BOX = (773, 130, 850, 207)
P2_INTERACTION_POP_SECONDS = 0.18

# The native marker is wider than the mod's former version, and the round button
# badge is taller than its text.
#
# The isolated scan rewrites each follower snapshot every frame and the copy has
# no seqlock, so the 120 Hz painter can observe an empty descriptor. Retain the
# last valid sprite for this interval; any shorter creates visible flicker.
INTERACTION_PROMPT_GRACE_SECONDS = 0.22


INTERACTION_PROMPT_INNER_HOLD_SECONDS = 0.16


INTERACTION_PROMPT_POINT_OUTLIER_PX = 32.0
INTERACTION_PROMPT_POINT_CONFIRM_PX = 36.0
P2_INTERACTION_LABEL_SCALE = 0.84
P2_INTERACTION_KEY_SCALE = 0.49
P2_INTERACTION_PAD_KEY_SCALE = 0.40
P2_INTERACTION_BADGE_RATIO = 1.42
P2_INTERACTION_PAD_BADGE_RATIO = 0.90


P2_INTERACTION_LABEL_TEXT = native_assets.TextStyle(
    core=(255, 255, 252, 255),
    edge=(9, 11, 13, 255),
    glow=(0, 0, 0, 190),
    spread_dilate=1.35,
    spread_sigma=1.00,
    spread_gain=2.70,
)
P2_INTERACTION_KEY_TEXT = native_assets.TextStyle(
    core=(255, 255, 250, 255),
    edge=(31, 37, 42, 245),
    glow=(0, 0, 0, 120),
    shadow=(0, 0, 0, 185),
    edge_radius=0.55,
    glow_radius=1.0,
    shadow_radius=1.0,
    shadow_offset=(1.0, 1.0),
)
P2_INTERACTION_PAD_TEXT = native_assets.TextStyle(
    core=(255, 255, 255, 255),
    edge=(18, 26, 20, 255),
    glow=(0, 0, 0, 0),
    shadow=(0, 0, 0, 100),
    edge_radius=0.45,
    glow_radius=0.0,
    shadow_radius=0.65,
    shadow_offset=(0.0, 1.0),
)


UI_MANAGER_GLOBAL = 0x209BE80
P1_HELP_OFFSET = 0x8FA0
HELP_CALLBACK = 0x2BE490
HELP_ROOT_CALLBACK = 0x2BF690
COMBAT_LOG_MENU_SHIFT_GLOBAL = 0x1E0CE98
COMBAT_LOG_MENU_SHIFT_NATIVE = 55
COMBAT_LOG_MENU_SHIFT_LEGACY_COOP = 0


COMBAT_LOG_MENU_SHIFT_BOSS_STATIC = 0
COMBAT_LOG_MENU_SHIFT_PREVIOUS_COOP = (-18) & 0xFFFFFFFF


COMBAT_LOG_MENU_SHIFT_COOP = (-COMBAT_LOG_COOP_RISE_PX) & 0xFFFFFFFF


COMBAT_LOG_MENU_SHIFT_PREVIOUS_RISE = (-COMBAT_LOG_COOP_RISE_PX) & 0xFFFFFFFF
COMBAT_LOG_MENU_SHIFT_MANAGED_VALUES = (
    COMBAT_LOG_MENU_SHIFT_NATIVE,
    COMBAT_LOG_MENU_SHIFT_LEGACY_COOP,
    COMBAT_LOG_MENU_SHIFT_PREVIOUS_COOP,
    COMBAT_LOG_MENU_SHIFT_COOP,
    COMBAT_LOG_MENU_SHIFT_PREVIOUS_RISE,
)
COMBAT_LOG_ROOTS = (
    (0xA018, 0x2C0B40, 0xDA),
    (0xA020, 0x2C0BE0, 0xEA),
)


HIDE_NATIVE_COMBAT_LOG = False
# The party panel and actor-bound indicators use adjacent paths but do not belong
# to the Combat Log. Modulate only the two roots below, preserving native fade.
NATIVE_COMBAT_LOG_DRAW_SITES = (
    0x2C0B71,
    0x2C0B86,
    0x2C0D6B,
    0x2C0D89,
)
NATIVE_COMBAT_LOG_DRAW_ORIGINAL = bytes.fromhex("0F AF C8")
NATIVE_COMBAT_LOG_DRAW_HIDDEN = bytes.fromhex("31 C9 90")


NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE = 0x2BF373
NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_ORIGINAL = bytes.fromhex("E8 D8 92 01 00")
NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_HIDDEN = b"\x90" * 5


NATIVE_COMBAT_LOG_TICK_CALL_SITE = 0x2BE6D3
NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL = bytes.fromhex("E8 E8 03 00 00")
NATIVE_COMBAT_LOG_TICK = 0x2BEAC0
NATIVE_COMBAT_LOG_TARGET_HIDE = 0x28E1D0
NATIVE_COMBAT_LOG_TARGET_SHOW = 0x28E240
NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE = 0x28E279
NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL = bytes.fromhex("E9 82 F9 02 00")
NATIVE_COMBAT_LOG_TARGET_SHOW_BODY = 0x2BDC00
NATIVE_COMBAT_LOG_WIDGET_OWNER_OFFSET = 0x1058
NATIVE_COMBAT_LOG_SECOND_ROOT_OWNER_OFFSET = 0x1080
NATIVE_TARGET_INFO_OBJECT_OWNER_OFFSET = 0x1030
NATIVE_TARGET_INFO_LIFECYCLE_FLAGS_OFFSET = 0x103E
NATIVE_TARGET_INFO_LIFECYCLE_PENDING_MASK = 0x01


NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET = 0x8400
NATIVE_COMBAT_LOG_LAYOUT_RBX_GATE_CAVE_OFFSET = 0x8500
NATIVE_COMBAT_LOG_LAYOUT_RDI_GATE_CAVE_OFFSET = 0x8520
NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET = 0x8580
NATIVE_COMBAT_LOG_GATE_STATE_OFFSET = 0x85F0
NATIVE_COMBAT_LOG_GATE_REGION_END = 0x8600
NATIVE_COMBAT_LOG_GATE_MAGIC = b"CLG3"

# ActiveLogDispHelpMoveY is consulted only when one of the game's own help
# widgets is present.  The custom command menus do not create those widgets,
# so the stock test must also accept a negative shift published by the co-op.
# Keeping the original native test in each cave preserves all stock behaviour.
NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES = (
    (
        0x2BDE53,
        bytes.fromhex("F6 83 FA 10 00 00 08"),
        NATIVE_COMBAT_LOG_LAYOUT_RBX_GATE_CAVE_OFFSET,
    ),
    (
        0x2BDF43,
        bytes.fromhex("F6 83 FA 10 00 00 08"),
        NATIVE_COMBAT_LOG_LAYOUT_RBX_GATE_CAVE_OFFSET,
    ),
    (
        0x2BE5D1,
        bytes.fromhex("F6 87 FA 10 00 00 08"),
        NATIVE_COMBAT_LOG_LAYOUT_RDI_GATE_CAVE_OFFSET,
    ),
    (
        0x2BE74E,
        bytes.fromhex("F6 87 FA 10 00 00 08"),
        NATIVE_COMBAT_LOG_LAYOUT_RDI_GATE_CAVE_OFFSET,
    ),
)
P1_HELP_BADGE_FEED_OFFSET = 0xCF40
P1_HELP_BADGE_MAGIC = 0x47444142


NATIVE_GET_BUTTON_SITE = 0x79D670
NATIVE_GET_BUTTON_RETURN = 0x79D676
NATIVE_GET_BUTTON_ORIGINAL = bytes.fromhex("8D 42 F5 83 F8 10")
NATIVE_DIALOG_CAVE_OFFSET = 0xEE00
NATIVE_DIALOG_STATE_OFFSET = 0xEF80
NATIVE_DIALOG_P1_SLOT_OFFSET = 0x0C
NATIVE_DIALOG_STATE_SIZE = 0x10
NATIVE_DIALOG_LAYOUT_MAGIC_OFFSET = 0x05
NATIVE_DIALOG_LAYOUT_MAGIC = b"NDIB3V3"


NATIVE_GET_AXIS_SITE = 0x79D730
NATIVE_GET_AXIS_RETURN = 0x79D735
NATIVE_GET_AXIS_ORIGINAL = bytes.fromhex("FF CA 83 FA 11")
NATIVE_AXIS_CAVE_OFFSET = 0xE400


# The desktop device has a separate vtable from the XInput-compatible devices.
# Its button query loads M1/M2/M3 from +0x1F4, and axis IDs 3, 6 and 9 all
# converge on the signed wheel field at +0x1F0.  Hook the final loads so the
# existing range/jump-table validation remains native-owned.
NATIVE_DESKTOP_BUTTON_SITE = 0x79D65E
NATIVE_DESKTOP_BUTTON_RETURN = 0x79D666
NATIVE_DESKTOP_BUTTON_ORIGINAL = bytes.fromhex("0F B6 84 0A F4 01 00 00")
NATIVE_DESKTOP_BUTTON_CAVE_OFFSET = 0xE6C0
NATIVE_DESKTOP_WHEEL_SITE = 0x79D6E6
NATIVE_DESKTOP_WHEEL_RETURN = 0x79D6EE
NATIVE_DESKTOP_WHEEL_ORIGINAL = bytes.fromhex("66 0F 6E 81 F0 01 00 00")
NATIVE_MOUSE_LOOK_SITE = 0x7A1A1E
NATIVE_MOUSE_LOOK_RETURN = 0x7A1A26
NATIVE_MOUSE_LOOK_ORIGINAL = bytes.fromhex("48 8B CB E8 7A FB FF FF")
NATIVE_MOUSE_LOOK_FINALIZER = 0x7A15A0
NATIVE_INPUT_COMMAND_HELPER_CAVE_OFFSET = 0xE500
NATIVE_MOUSE_LOOK_CAVE_OFFSET = 0xED10
NATIVE_DESKTOP_WHEEL_CAVE_OFFSET = 0xED60
NATIVE_INPUT_EVENT_PUBLISHER_CAVE_OFFSET = 0xEF90


# E740..E7FF is the unused tail of the P1-list reservation.  It contains two
# immutable policy slots followed by an ordered native-to-worker event ring.
# The writer fills the inactive slot and publishes a monotonic generation last.
# Its low bit selects the slot; the full value also timestamps every event so an
# input sampled under an earlier menu/owner route can never cross that boundary.
NATIVE_INPUT_STATE_OFFSET = 0xE740
NATIVE_INPUT_STATE_SIZE = 0xC0
NATIVE_INPUT_POLICY_SLOT_SIZE = 0x10
NATIVE_INPUT_POLICY_ACTIVE_OFFSET = 0x20
NATIVE_INPUT_EVENT_PRODUCER_OFFSET = 0x24
NATIVE_INPUT_EVENT_CONSUMER_OFFSET = 0x28
NATIVE_INPUT_EVENT_OVERFLOW_OFFSET = 0x2C
NATIVE_INPUT_COMMAND_SOURCES_OFFSET = 0x30
NATIVE_INPUT_COMMAND_SEED_OFFSET = 0x34
NATIVE_INPUT_WHEEL_SEED_OFFSET = 0x38
NATIVE_INPUT_EVENT_WRITER_OFFSET = 0x3C
NATIVE_INPUT_EVENT_RING_OFFSET = 0x40
NATIVE_INPUT_EVENT_RECORD_SIZE = 0x10
NATIVE_INPUT_EVENT_CAPACITY = 8
NATIVE_INPUT_POLICY_CAPTURE_DESKTOP = 1 << 0
NATIVE_INPUT_POLICY_SUPPRESS_WHEEL = 1 << 1
NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES = 1 << 2
NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK = 1 << 3
# The native normalized button path is shared by desktop mappings and gamepad
# devices.  Record every effective 0..3 native gamepad slot in the immutable
# policy so only a non-gamepad COMMAND query can feed the desktop event ring.
NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT = 8
NATIVE_INPUT_POLICY_GAMEPAD_SLOT_MASK = (
    0x0F << NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT
)
NATIVE_INPUT_EVENT_COMMAND = 1
NATIVE_INPUT_EVENT_WHEEL = 2
NATIVE_INPUT_EVENT_KIND_SHIFT = 28
NATIVE_INPUT_EVENT_VALUE_MASK = (1 << NATIVE_INPUT_EVENT_KIND_SHIFT) - 1
NATIVE_INPUT_EVENT_SIGN_BIT = 1 << (NATIVE_INPUT_EVENT_KIND_SHIFT - 1)
NATIVE_INPUT_EVENT_VALUE_MODULUS = 1 << NATIVE_INPUT_EVENT_KIND_SHIFT
NATIVE_WHEEL_DELTA_UNIT = 0xFF
NATIVE_WHEEL_COOLDOWN_SECONDS = 0.50


P1_LIST_CALLBACK_SITE = 0x2778C0
P1_LIST_CALLBACK_RETURN = 0x2778C5
P1_LIST_CALLBACK_ORIGINAL = bytes.fromhex("48 89 5C 24 18")
P1_SET_LIST_INDEX = 0x2D4650
P1_LIST_INPUT_CAVE_OFFSET = 0xE600
# E6C0 begins the resident desktop-button cave.  The P1 callback body is
# bounded to E600..E6BF; publishing a whole 0x100-byte image here would erase
# the dialog bridge after it had committed its root.
P1_LIST_INPUT_CAVE_SIZE = 0xC0
P1_LIST_INPUT_STATE_OFFSET = 0xE700
NATIVE_DIALOG_UP = 0x17
NATIVE_DIALOG_DOWN = 0x19
NATIVE_DIALOG_LEFT = 0x1A
NATIVE_DIALOG_RIGHT = 0x18
NATIVE_DIALOG_LB = 0x0F
NATIVE_DIALOG_RB = 0x10
NATIVE_DIALOG_COMMAND = 0x0B
NATIVE_DIALOG_CONFIRM = 0x0C
NATIVE_DIALOG_CANCEL = 0x0D
NATIVE_DIALOG_PAUSE = 0x14
NATIVE_GAME_SETTINGS_GLOBAL = 0x01F82D20
NATIVE_DECIDE_BUTTON_OFFSET = 0x64
NATIVE_DECIDE_BUTTON_CIRCLE = 0
NATIVE_DECIDE_BUTTON_CROSS = 1
NATIVE_DECIDE_BUTTON_REFRESH_SECONDS = 0.25


NATIVE_DIALOG_PARTY_MENU = 0x0E
NATIVE_DIALOG_MANAGER_FLAGS_OFFSET = 0x10F68
NATIVE_DIALOG_MANAGER_OBJECT_OFFSET = 0x10F80
NATIVE_DIALOG_OBJECT_FLAGS_OFFSET = 0x1D7A
NATIVE_DIALOG_PULSE_SECONDS = 0.065
QUICKENING_BLOCK_COMMAND = 0x01
QUICKENING_BLOCK_CONFIRM = 0x02


def native_semantic_mask(*buttons: int) -> int:
    mask = 0
    for button in buttons:
        index = int(button) - NATIVE_DIALOG_COMMAND
        if not 0 <= index <= 0x10:
            raise ValueError(f"native semantic is outside 0x0B..0x1B: {button:#x}")
        mask |= 1 << index
    return mask


NATIVE_INPUT_COMMAND_MASK = native_semantic_mask(NATIVE_DIALOG_COMMAND)
NATIVE_INPUT_CONFIRM_MASK = native_semantic_mask(NATIVE_DIALOG_CONFIRM)
NATIVE_INPUT_PARTY_MASK = native_semantic_mask(NATIVE_DIALOG_PARTY_MENU)
NATIVE_INPUT_LB_MASK = native_semantic_mask(NATIVE_DIALOG_LB)
NATIVE_INPUT_RB_MASK = native_semantic_mask(NATIVE_DIALOG_RB)
NATIVE_INPUT_DIRECTION_MASK = native_semantic_mask(
    NATIVE_DIALOG_UP,
    NATIVE_DIALOG_RIGHT,
    NATIVE_DIALOG_DOWN,
    NATIVE_DIALOG_LEFT,
)
NATIVE_INPUT_LEADER_MASK = native_semantic_mask(
    NATIVE_DIALOG_UP,
    NATIVE_DIALOG_RIGHT,
    NATIVE_DIALOG_DOWN,
    NATIVE_DIALOG_LEFT,
)
NATIVE_INPUT_CUSTOM_DIRECTION_MASK = native_semantic_mask(
    NATIVE_DIALOG_LB,
    NATIVE_DIALOG_RB,
    NATIVE_DIALOG_UP,
    NATIVE_DIALOG_RIGHT,
    NATIVE_DIALOG_DOWN,
    NATIVE_DIALOG_LEFT,
)
NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK = (
    NATIVE_INPUT_DIRECTION_MASK | NATIVE_INPUT_RB_MASK
)


@dataclass(frozen=True)
class NativeInputPolicy:
    p1_semantic_mask: int = 0
    other_semantic_mask: int = 0
    p1_native_slot: int = legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT
    flags: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            "<4I",
            int(self.p1_semantic_mask) & 0xFFFFFFFF,
            int(self.other_semantic_mask) & 0xFFFFFFFF,
            int(self.p1_native_slot) & 0xFFFFFFFF,
            int(self.flags) & 0xFFFFFFFF,
        )


@dataclass(frozen=True)
class NativeDesktopInputEvent:
    sequence: int
    kind: int
    value: int
    policy_generation: int = 0


def native_input_policy(
    *,
    p1_replacement: bool,
    non_p1_command_blocked: bool,
    custom_directional_owner: bool,
    p1_menu_owns_lb: bool,
    party_menu_blocked: bool,
    quickening_block_mask: int,
    leader_replacement: bool,
    capture_desktop_events: bool,
    suppress_mouse_look: bool = False,
    p1_native_slot: int,
    native_gamepad_slot_mask: int = 0,
) -> NativeInputPolicy:
    p1_mask = 0
    other_mask = 0
    if p1_replacement:
        p1_mask |= native_semantic_mask(
            NATIVE_DIALOG_COMMAND,
            NATIVE_DIALOG_CONFIRM,
            NATIVE_DIALOG_CANCEL,
        )
    if non_p1_command_blocked:
        other_mask |= NATIVE_INPUT_COMMAND_MASK
    # Capturing a desktop COMMAND is inseparable from suppressing that same
    # normalized native semantic.  Keeping this invariant in the immutable
    # policy builder prevents a partial caller (or a recovery publication) from
    # successfully enqueueing M1 while also leaking it into the stock command
    # tree for one frame.
    if capture_desktop_events:
        other_mask |= NATIVE_INPUT_COMMAND_MASK
    if custom_directional_owner:
        # P1 LB is a separate policy contribution so the custom Leader selector
        # can leave the game's Turbo semantic native without releasing RB or
        # any d-pad direction. Followers still own the complete navigation set.
        p1_mask |= NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK
        other_mask |= NATIVE_INPUT_CUSTOM_DIRECTION_MASK
    if leader_replacement:
        p1_mask |= NATIVE_INPUT_LEADER_MASK
        other_mask |= NATIVE_INPUT_LEADER_MASK
    if p1_menu_owns_lb:
        p1_mask |= NATIVE_INPUT_LB_MASK
    if party_menu_blocked:
        p1_mask |= NATIVE_INPUT_PARTY_MASK
        other_mask |= NATIVE_INPUT_PARTY_MASK
    if int(quickening_block_mask) & QUICKENING_BLOCK_COMMAND:
        p1_mask |= NATIVE_INPUT_COMMAND_MASK
        other_mask |= NATIVE_INPUT_COMMAND_MASK
    if int(quickening_block_mask) & QUICKENING_BLOCK_CONFIRM:
        p1_mask |= NATIVE_INPUT_CONFIRM_MASK
        other_mask |= NATIVE_INPUT_CONFIRM_MASK

    flags = 0
    if capture_desktop_events:
        flags |= NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
    if capture_desktop_events or leader_replacement or custom_directional_owner:
        flags |= NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
    if p1_replacement:
        flags |= NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES
    if suppress_mouse_look:
        flags |= NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK
    flags |= (
        int(native_gamepad_slot_mask) & 0x0F
    ) << NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT
    return NativeInputPolicy(
        p1_semantic_mask=p1_mask,
        other_semantic_mask=other_mask,
        p1_native_slot=p1_native_slot,
        flags=flags,
    )


def native_gamepad_slot_mask(*assignments) -> int:
    """Return every normalized native XInput slot, including unassigned pads.

    Native controller indices are the fixed 0..3 domain.  A connected pad is
    still queried when it is not assigned to a co-op role, so an assignment-only
    mask would misclassify that COMMAND as desktop input.  The arguments keep
    the runtime call site explicit about the three effective assignments.
    """

    return 0x0F


def native_leader_replacement_state(
    *,
    base_eligible: bool,
    menus_closed: bool,
    transitions_clear: bool,
    handoff_pending: bool,
) -> tuple[bool, bool]:
    """Return (selector_open_eligible, native_suppression_enabled)."""

    stable_frontend = bool(menus_closed and transitions_clear)
    selector_open_eligible = bool(
        base_eligible and stable_frontend and not handoff_pending
    )
    # Suppression owns the complete co-op lifecycle.  Only selector opening is
    # restricted to the stable closed state; dropping the native mask during an
    # opening/closing transition is precisely what lets a fast repeat leak the
    # stock leader menu.
    native_suppression_enabled = bool(base_eligible)
    return selector_open_eligible, native_suppression_enabled


def non_p1_command_firewall_state(
    *,
    coop_active: bool,
    bridge_ownership_allowed: bool,
    native_command_passthrough: bool,
    leader_selection_pending: bool,
) -> bool:
    """Keep follower COMMAND native-suppressed during KO leader recovery."""

    return bool(
        coop_active
        and (
            leader_selection_pending
            or (bridge_ownership_allowed and not native_command_passthrough)
        )
    )


def decode_native_input_event(
    sequence: int,
    payload: int,
    policy_generation: int = 0,
) -> NativeDesktopInputEvent:
    kind = (int(payload) >> NATIVE_INPUT_EVENT_KIND_SHIFT) & 0x0F
    value = int(payload) & NATIVE_INPUT_EVENT_VALUE_MASK
    if kind == NATIVE_INPUT_EVENT_WHEEL and value & NATIVE_INPUT_EVENT_SIGN_BIT:
        value -= NATIVE_INPUT_EVENT_VALUE_MODULUS
    return NativeDesktopInputEvent(
        int(sequence) & 0xFFFFFFFF,
        kind,
        value,
        int(policy_generation) & 0xFFFFFFFF,
    )


def native_wheel_directions(delta: int) -> tuple[int, ...]:
    """Return the direction of one exact native wheel gesture.

    The native field uses 0xFF per detent, but Windows/device polling can
    coalesce several detents into one sample.  Magnitude is therefore evidence
    of direction, never permission to perform several menu actions in one
    frame.  Fractional/corrupt samples remain rejected.
    """

    value = int(delta)
    if not value:
        return ()
    magnitude = abs(value)
    if magnitude % NATIVE_WHEEL_DELTA_UNIT:
        return ()
    return (-1,) if value > 0 else (1,)


@dataclass
class DesktopWheelGestureGate:
    """Consume transient wheel gestures without queuing or replaying them."""

    cooldown_seconds: float = NATIVE_WHEEL_COOLDOWN_SECONDS
    blocked_until: float = 0.0

    def reset(self) -> None:
        self.blocked_until = 0.0

    def consume(self, delta: int, *, now: float) -> int | None:
        directions = native_wheel_directions(delta)
        if not directions or float(now) < self.blocked_until:
            return None
        self.blocked_until = float(now) + self.cooldown_seconds
        return directions[0]


def native_command_source_transition(
    sources: int,
    source: int,
    down: bool,
    *,
    publish_succeeded: bool = True,
) -> tuple[int, bool]:
    """Model the two-source M1 aggregate used by the native command helper."""

    aggregate = int(sources) & 0xFFFFFFFF
    source_bit = int(source) & 0xFFFFFFFF
    if not source_bit or source_bit & (source_bit - 1):
        raise ValueError("native command source must be one bit")
    if not down:
        return aggregate & ~source_bit & 0xFFFFFFFF, False
    if aggregate & source_bit:
        return aggregate, False
    updated = aggregate | source_bit
    edge = aggregate == 0
    if edge and not publish_succeeded:
        # A concurrent alias may already have joined this aggregate and skipped
        # publication after observing it nonzero.  Rearm every held source when
        # the sole aggregate edge was refused; physical state will repopulate it.
        updated = 0
        edge = False
    return updated & 0xFFFFFFFF, edge


def native_command_source_is_desktop(
    source: int,
    native_slot: int,
    gamepad_slot_mask: int,
) -> bool:
    """Classify a trusted raw-mouse or normalized COMMAND observation."""

    source_bit = int(source)
    if source_bit == 1:
        return True
    if source_bit != 2:
        return False
    slot = int(native_slot) & 0xFFFFFFFF
    return not (
        0 <= slot <= 3 and int(gamepad_slot_mask) & (1 << slot)
    )


def native_command_capture_sample(
    sources: int,
    seed_sources: int,
    source: int,
    down: bool,
    *,
    publish_succeeded: bool = True,
) -> tuple[int, int, bool]:
    """Apply capture-resume seeding before the logical M1 OR transition."""

    aggregate = int(sources) & 0xFFFFFFFF
    seeds = int(seed_sources) & 0xFFFFFFFF
    source_bit = int(source) & 0xFFFFFFFF
    if seeds & source_bit:
        aggregate &= ~source_bit & 0xFFFFFFFF
        if down:
            return aggregate, seeds, False
        seeds &= ~source_bit & 0xFFFFFFFF
        return aggregate, seeds, False
    aggregate, edge = native_command_source_transition(
        aggregate,
        source_bit,
        down,
        publish_succeeded=publish_succeeded,
    )
    return aggregate, seeds, edge


def quickening_button_block_mask(
    *,
    active: bool,
    party_size: int,
    p2_manual_control: bool,
    p3_manual_control: bool,
) -> int:
    if not active:
        return 0
    mask = 0
    if party_size >= 2 and p2_manual_control:
        mask |= QUICKENING_BLOCK_COMMAND
    if party_size >= 3 and p3_manual_control:
        mask |= QUICKENING_BLOCK_CONFIRM
    return mask


def native_decide_button(
    process: legacy.coop.RemoteProcess,
    fallback: int = NATIVE_DECIDE_BUTTON_CROSS,
) -> int:
    try:
        settings = process.u64(NATIVE_GAME_SETTINGS_GLOBAL)
        if settings:
            value = process.u32(settings + NATIVE_DECIDE_BUTTON_OFFSET)
            if value in (NATIVE_DECIDE_BUTTON_CIRCLE, NATIVE_DECIDE_BUTTON_CROSS):
                return value
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return fallback


kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ct.c_void_p, ct.c_bool, ct.c_wchar_p]
kernel32.CreateMutexW.restype = ct.c_void_p
kernel32.CloseHandle.argtypes = [ct.c_void_p]
kernel32.CloseHandle.restype = ct.c_bool

NATIVE_SOUND_INVALID = 0x05
NATIVE_SOUND_GAMBITS_ON = 0x12
NATIVE_SOUND_GAMBITS_OFF = 0x13
NATIVE_SOUND_TARGET_GROUP = 0x0A

NATIVE_SOUND_BY_EVENT = {
    "cursor_moved": 1,
    "target_moved": 1,
    "target_group": NATIVE_SOUND_TARGET_GROUP,
    "page_opened": 2,
    "target_opened": 2,
    "submit": 2,
    "page_closed": 3,
    "target_closed": 3,
    "closed": 3,
    "opened": 4,
}
NATIVE_INTERACTION_PROXIMITY_SOUND = 0x2C


def custom_menu_leaves_free_player(
    p1_mode: MenuMode,
    p2_mode: MenuMode,
    p3_mode: MenuMode,
    *,
    p2_enabled: bool,
    p3_enabled: bool,
) -> bool:
    players = (
        (True, p1_mode),
        (bool(p2_enabled), p2_mode),
        (bool(p3_enabled), p3_mode),
    )
    return any(enabled and mode is MenuMode.CLOSED for enabled, mode in players)


class NativeCombatLogLayout:
    def __init__(self, process) -> None:
        self.process = process
        self.engaged = False
        self.compatible: bool | None = None

    def maintain(
        self,
        coop_field_layout: bool,
        *,
        boss_gauge_visible: bool = False,
    ) -> None:
        try:
            current = self.process.u32(COMBAT_LOG_MENU_SHIFT_GLOBAL)
        except OSError:
            return

        # Decide on every call, never once per session. The former latch froze on
        # its first sample and silently gave up when that sample was not one of
        # ours. In a live process, the global retained its stock value of 55 for
        # 300/300 samples, while a manually written -40 survived untouched for
        # four seconds, which is possible only when neither branch below runs. A
        # badly timed read before the game settled the value disabled raise for
        # the whole session. Rereading is equally safe for the case the latch
        # protected: a foreign mod value is still skipped, but only while present.
        compatible = current in COMBAT_LOG_MENU_SHIFT_MANAGED_VALUES
        if compatible != self.compatible:
            self.compatible = compatible
            if not compatible:
                print(
                    "COMBAT_LOG_LAYOUT cedido: "
                    f"0x{COMBAT_LOG_MENU_SHIFT_GLOBAL:X} = {current} "
                    "nao e um valor gerenciado",
                    flush=True,
                )
        if not compatible:
            return

        desired = (
            COMBAT_LOG_MENU_SHIFT_BOSS_STATIC
            if boss_gauge_visible
            else (
                COMBAT_LOG_MENU_SHIFT_COOP
                if coop_field_layout
                else COMBAT_LOG_MENU_SHIFT_NATIVE
            )
        )
        if coop_field_layout or boss_gauge_visible:
            if current in COMBAT_LOG_MENU_SHIFT_MANAGED_VALUES and current != desired:
                try:
                    self.process.write(
                        COMBAT_LOG_MENU_SHIFT_GLOBAL,
                        struct.pack("<I", desired),
                    )
                except OSError:
                    return
            elif current != desired:
                self.compatible = False
                return
            self.engaged = True
        else:
            if (
                current in COMBAT_LOG_MENU_SHIFT_MANAGED_VALUES
                and current != COMBAT_LOG_MENU_SHIFT_NATIVE
            ):
                try:
                    self.process.write(
                        COMBAT_LOG_MENU_SHIFT_GLOBAL,
                        struct.pack("<I", COMBAT_LOG_MENU_SHIFT_NATIVE),
                    )
                except OSError:
                    return
            self.engaged = False

    def restore(self) -> None:
        if self.compatible:
            try:
                if (
                    self.process.u32(COMBAT_LOG_MENU_SHIFT_GLOBAL)
                    in COMBAT_LOG_MENU_SHIFT_MANAGED_VALUES
                    and self.process.u32(COMBAT_LOG_MENU_SHIFT_GLOBAL)
                    != COMBAT_LOG_MENU_SHIFT_NATIVE
                ):
                    self.process.write(
                        COMBAT_LOG_MENU_SHIFT_GLOBAL,
                        struct.pack("<I", COMBAT_LOG_MENU_SHIFT_NATIVE),
                    )
            except OSError:
                pass
        self.engaged = False


class NativeCombatLogVisibility:
    def __init__(self, process) -> None:
        self.process = process
        self.visible = False

    def sample(self, now: float | None = None) -> bool:
        del now
        alphas = []
        try:
            manager = self.process.u64(UI_MANAGER_GLOBAL)
            if not manager:
                raise OSError("native UI manager unavailable")
            for offset, callback, alpha_offset in COMBAT_LOG_ROOTS:
                root = self.process.u64(manager + offset)
                if not root or self.process.u64(root) != callback:
                    continue
                alpha = struct.unpack(
                    "<H",
                    self.process.read(root + alpha_offset, 2),
                )[0]
                alphas.append(alpha)
            if not alphas:
                raise OSError("Combat Log roots unavailable")
        except (OSError, struct.error):
            return self.visible

        self.visible = any(alpha > 0 for alpha in alphas)
        return self.visible


class NativeCombatLogTargetInfoGate:
    DESIRED_OFFSET = 0
    APPLIED_OFFSET = 1
    MAGIC_OFFSET = 4

    def __init__(self, process, base: int) -> None:
        self.process = process
        self.base = base
        self.cave = base + NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
        self.show_cave = base + NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
        self.state = base + NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
        self.installed = False
        self.owns_static = False
        self.requested_hidden = False

    @staticmethod
    def _rel32(source: int, length: int, target: int) -> bytes:
        displacement = target - (source + length)
        if not -(1 << 31) <= displacement < (1 << 31):
            raise RuntimeError("Combat Log gate is outside rel32 range")
        return struct.pack("<i", displacement)

    def _tick_patch(self) -> bytes:
        return b"\xe8" + self._rel32(NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5, self.cave)

    def _show_patch(self) -> bytes:
        return b"\xe9" + self._rel32(
            NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE, 5, self.show_cave
        )

    def _layout_patches(self) -> tuple[tuple[int, bytes], ...]:
        return tuple(
            (
                site,
                b"\xe8"
                + self._rel32(site, 5, self.base + cave_offset)
                + b"\x90\x90",
            )
            for site, _original, cave_offset in NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES
        )

    @staticmethod
    def _build_layout_gate_cave(*, owner_is_rdi: bool) -> bytes:
        # Preserve RAX and reproduce the native owner+0x10FA bit test. When it
        # is clear, expose ZF=0 only for a signed-negative co-op shift. The
        # original JZ immediately following each patched instruction therefore
        # keeps its exact stock meaning and gains one narrowly scoped condition.
        owner_modrm = b"\x87" if owner_is_rdi else b"\x83"
        return (
            b"\x50"
            + b"\xf6"
            + owner_modrm
            + struct.pack("<I", 0x10FA)
            + b"\x08"
            + b"\x75\x14"
            + b"\x48\xb8"
            + struct.pack("<Q", COMBAT_LOG_MENU_SHIFT_GLOBAL)
            + b"\x83\x38\x00"
            + b"\x0f\x9c\xc0"
            + b"\x84\xc0"
            + b"\x58\xc3"
            + b"\x58\xc3"
        )

    def _build_tick_cave(self) -> bytes:
        code = bytearray()
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: bytes, target: str) -> None:
            code.extend(opcode)
            fixups.append((len(code), target))
            code.extend(b"\x00" * 4)

        def jump(target: str) -> None:
            branch(b"\xe9", target)

        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x8a\x10"
        code += b"\x3a\x50\x01"
        branch(b"\x0f\x84", "stock_tick")

        code += b"\x48\xb9" + struct.pack("<Q", UI_MANAGER_GLOBAL)
        code += b"\x48\x8b\x09"
        code += b"\x48\x85\xc9"
        branch(b"\x0f\x84", "stock_tick")
        code += (
            b"\xf6\x81"
            + struct.pack("<I", NATIVE_DIALOG_MANAGER_FLAGS_OFFSET)
            + b"\x40"
        )
        branch(b"\x0f\x85", "stock_tick")

        code += b"\x48\x8d\x81" + struct.pack("<I", P1_HELP_OFFSET)
        code += b"\x49\xb8" + struct.pack("<Q", HELP_CALLBACK)
        code += b"\x4c\x39\x00"
        branch(b"\x0f\x85", "stock_tick")

        code += b"\x48\x39\x98" + struct.pack(
            "<I", NATIVE_COMBAT_LOG_WIDGET_OWNER_OFFSET
        )
        branch(b"\x0f\x85", "stock_tick")
        code += (
            b"\x48\x83\xb8"
            + struct.pack("<I", NATIVE_COMBAT_LOG_SECOND_ROOT_OWNER_OFFSET)
            + b"\x00"
        )
        branch(b"\x0f\x84", "stock_tick")
        code += (
            b"\x48\x83\xb8"
            + struct.pack("<I", NATIVE_TARGET_INFO_OBJECT_OWNER_OFFSET)
            + b"\x00"
        )
        branch(b"\x0f\x85", "native_owns")
        code += (
            b"\xf6\x80"
            + struct.pack("<I", NATIVE_TARGET_INFO_LIFECYCLE_FLAGS_OFFSET)
            + bytes((NATIVE_TARGET_INFO_LIFECYCLE_PENDING_MASK,))
        )
        branch(b"\x0f\x85", "native_owns")

        code += b"\x84\xd2"
        branch(b"\x0f\x84", "show")

        code += b"\x48\x83\xec\x28"
        code += b"\x48\xb8" + struct.pack("<Q", NATIVE_COMBAT_LOG_TARGET_HIDE)
        code += b"\xff\xd0"
        code += b"\x48\x83\xc4\x28"
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\xc6\x40\x01\x01"
        jump("stock_tick")

        label("show")
        code += b"\x48\x83\xec\x28"
        code += b"\x48\xb8" + struct.pack("<Q", NATIVE_COMBAT_LOG_TARGET_SHOW)
        code += b"\xff\xd0"
        code += b"\x48\x83\xc4\x28"
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\xc6\x40\x01\x00"
        jump("stock_tick")

        label("native_owns")

        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x88\x50\x01"

        label("stock_tick")

        code += b"\x48\x8b\xcb"
        code += b"\x48\xb8" + struct.pack("<Q", NATIVE_COMBAT_LOG_TICK)
        code += b"\xff\xe0"

        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                self.cave + labels[target] - (self.cave + displacement_at + 4),
            )

        rbx_offset = (
            NATIVE_COMBAT_LOG_LAYOUT_RBX_GATE_CAVE_OFFSET
            - NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
        )
        rdi_offset = (
            NATIVE_COMBAT_LOG_LAYOUT_RDI_GATE_CAVE_OFFSET
            - NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
        )
        if len(code) > rbx_offset:
            raise RuntimeError("Combat Log tick cave overlaps its layout gates")
        code.extend(b"\x00" * (rbx_offset - len(code)))
        code.extend(self._build_layout_gate_cave(owner_is_rdi=False))
        if len(code) > rdi_offset:
            raise RuntimeError("Combat Log RBX layout gate exceeded its reservation")
        code.extend(b"\x00" * (rdi_offset - len(code)))
        code.extend(self._build_layout_gate_cave(owner_is_rdi=True))
        if len(code) > (
            NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET - NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
        ):
            raise RuntimeError("Combat Log tick cave exceeded its reservation")
        return bytes(code)

    def _build_show_cave(self) -> bytes:
        code = bytearray()
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x80\x38\x00"
        allow_at = len(code)
        code += b"\x74\x00"
        code += b"\xc3"
        allow = len(code)
        code += b"\xc6\x40\x01\x00"
        code += b"\x48\xb8" + struct.pack("<Q", NATIVE_COMBAT_LOG_TARGET_SHOW_BODY)
        code += b"\xff\xe0"
        displacement = allow - (allow_at + 2)
        if not -128 <= displacement <= 127:
            raise RuntimeError("Combat Log show-gate branch overflow")
        code[allow_at + 1] = displacement & 0xFF
        if len(code) > (
            NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
            - NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
        ):
            raise RuntimeError("Combat Log show cave exceeded its reservation")
        return bytes(code)

    def install(self) -> None:
        tick_patch = self._tick_patch()
        show_patch = self._show_patch()
        tick_cave = self._build_tick_cave()
        show_cave = self._build_show_cave()

        tick_region_size = (
            NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
            - NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
        )
        show_region_size = (
            NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
            - NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
        )
        state_region_size = (
            NATIVE_COMBAT_LOG_GATE_REGION_END - NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
        )
        tick_image = tick_cave.ljust(tick_region_size, b"\x00")
        show_image = show_cave.ljust(show_region_size, b"\x00")
        initial_state = bytearray(state_region_size)
        initial_state[self.MAGIC_OFFSET : self.MAGIC_OFFSET + 4] = (
            NATIVE_COMBAT_LOG_GATE_MAGIC
        )

        # Read every root and every reserved region before the first mutation.
        # In particular, never rewrite a live cave merely because one root still
        # looks stock: a previous installer can be between its two root commits.
        current_tick = self.process.read(
            NATIVE_COMBAT_LOG_TICK_CALL_SITE,
            len(NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL),
        )
        current_show = self.process.read(
            NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
            len(NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL),
        )
        layout_patches = self._layout_patches()
        current_layouts = tuple(
            self.process.read(site, len(original))
            for site, original, _cave_offset in NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES
        )
        current_tick_image = self.process.read(self.cave, tick_region_size)
        current_show_image = self.process.read(self.show_cave, show_region_size)
        current_state = self.process.read(self.state, state_region_size)

        fresh = (
            current_tick == NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL
            and current_show == NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL
            and all(
                current == original
                for current, (_site, original, _cave_offset) in zip(
                    current_layouts,
                    NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES,
                )
            )
        )
        adopted = (
            current_tick == tick_patch
            and current_show == show_patch
            and all(
                current == patch
                for current, (_site, patch) in zip(current_layouts, layout_patches)
            )
        )
        if not fresh and not adopted:
            raise RuntimeError(
                "native Combat Log gate occupied/incomplete: "
                f"tick={current_tick.hex(' ')} show={current_show.hex(' ')}"
            )

        if adopted:
            if current_tick_image != tick_image:
                raise RuntimeError("active native Combat Log tick cave mismatch")
            if current_show_image != show_image:
                raise RuntimeError("active native Combat Log show cave mismatch")
            if (
                current_state[self.MAGIC_OFFSET : self.MAGIC_OFFSET + 4]
                != NATIVE_COMBAT_LOG_GATE_MAGIC
                or current_state[self.DESIRED_OFFSET] not in (0, 1)
                or current_state[self.APPLIED_OFFSET] not in (0, 1)
                or current_state[2:4] != b"\x00\x00"
                or any(current_state[8:])
            ):
                raise RuntimeError("active native Combat Log gate state mismatch")

            # Exact adoption is deliberately read-only. Preserve the native
            # edge state and mirror its desired byte in the Python-side cache so
            # the next request remains edge driven.
            self.requested_hidden = bool(current_state[self.DESIRED_OFFSET])
            self.installed = True
            self.owns_static = False
            return

        blank_tick = b"\x00" * tick_region_size
        blank_show = b"\x00" * show_region_size
        blank_state = b"\x00" * state_region_size
        caves_blank = (
            current_tick_image == blank_tick
            and current_show_image == blank_show
            and current_state == blank_state
        )
        caves_clean = (
            current_tick_image == tick_image
            and current_show_image == show_image
            and current_state == bytes(initial_state)
        )
        if not caves_blank and not caves_clean:
            raise RuntimeError("inactive native Combat Log gate cave/state mismatch")

        # Prepare the complete, immutable topology while both roots are still
        # stock. A failed preparation is intentionally not rolled back: no code
        # can reach it yet, and a partial image is fail-closed on the next
        # preflight. An exact prepared image can safely resume at root commit.
        if not caves_clean:
            self.process.write(self.state, initial_state)
            self.process.patch_code(self.cave, tick_image)
            self.process.patch_code(self.show_cave, show_image)

        # Treat root publication as a one-way process-lifetime commit. Re-read
        # the complete topology immediately before it so any foreign mutation
        # detected before the commit leaves the roots untouched.
        if self.process.read(self.state, state_region_size) != bytes(initial_state):
            raise RuntimeError("native Combat Log gate state changed before commit")
        if self.process.read(self.cave, tick_region_size) != tick_image:
            raise RuntimeError("native Combat Log tick cave changed before commit")
        if self.process.read(self.show_cave, show_region_size) != show_image:
            raise RuntimeError("native Combat Log show cave changed before commit")
        if (
            self.process.read(
                NATIVE_COMBAT_LOG_TICK_CALL_SITE,
                len(NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL),
            )
            != NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL
            or self.process.read(
                NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
                len(NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL),
            )
            != NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL
            or any(
                self.process.read(site, len(original)) != original
                for site, original, _cave_offset in NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES
            )
        ):
            raise RuntimeError("native Combat Log roots changed before commit")

        # The show root is the commit point. From here onward never restore a
        # root or rewrite/free a reachable cave, even if the second write raises.
        self.process.patch_code(NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE, show_patch)
        self.process.patch_code(NATIVE_COMBAT_LOG_TICK_CALL_SITE, tick_patch)
        for site, patch in layout_patches:
            self.process.patch_code(site, patch)

        self.requested_hidden = False
        self.installed = True
        self.owns_static = True

    def request_hidden(self, hidden: bool) -> bool:
        hidden = bool(hidden)
        if not self.installed:
            return False
        if hidden == self.requested_hidden:
            return True
        self.process.write(
            self.state + self.DESIRED_OFFSET,
            bytes((int(hidden),)),
        )
        self.requested_hidden = hidden
        return True

    @property
    def applied_hidden(self) -> bool:
        if not self.installed:
            return False
        try:
            return bool(self.process.read(self.state + self.APPLIED_OFFSET, 1)[0])
        except (OSError, IndexError):
            return self.requested_hidden

    def restore(self, timeout: float = 0.35) -> bool:
        if not self.installed:
            return True
        try:
            self.request_hidden(False)
        except OSError:
            return False

        deadline = time.monotonic() + max(0.0, timeout)
        applied = True
        while True:
            try:
                applied = bool(
                    self.process.read(self.state + self.APPLIED_OFFSET, 1)[0]
                )
            except (OSError, IndexError):
                return False
            if not applied:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        if applied:
            return False

        # Root publication is process-lifetime. APPLIED becoming false only
        # confirms that the dynamic visibility request was reconciled; it is not
        # a proof that no game thread has an instruction pointer inside a cave.
        # Leave the exact static topology resident for read-only hot adoption.
        self.installed = False
        self.owns_static = False
        return True


class NativeStockFieldHudVisibility:
    def __init__(self, process) -> None:
        self.process = process
        self.compatible: dict[int, bool] = {}
        self.engaged: set[int] = set()
        self.owned: set[int] = set()

    @staticmethod
    def _patches() -> tuple[tuple[int, bytes, bytes], ...]:
        return (
            *(
                (
                    address,
                    NATIVE_COMBAT_LOG_DRAW_ORIGINAL,
                    NATIVE_COMBAT_LOG_DRAW_HIDDEN,
                )
                for address in NATIVE_COMBAT_LOG_DRAW_SITES
            ),
            (
                NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE,
                NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_ORIGINAL,
                NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_HIDDEN,
            ),
        )

    def maintain(
        self,
        *,
        hide_combat_log: bool,
    ) -> None:
        patches = self._patches()
        try:
            observed = tuple(
                self.process.read(address, len(original))
                for address, original, _hidden in patches
            )
        except OSError:
            return
        for (address, original, hidden), current in zip(patches, observed):
            compatible = current in (original, hidden)
            self.compatible[address] = compatible
            if not compatible:
                self.engaged.clear()
                return

        hidden_flags = tuple(
            current == hidden
            for current, (_address, _original, hidden) in zip(observed, patches)
        )
        if any(hidden_flags) and not all(hidden_flags):
            # A mixed topology may be executing in an older owner. Never repair
            # or detach only part of this five-site render contract live.
            self.engaged.clear()
            return
        addresses = {address for address, _original, _hidden in patches}
        if hide_combat_log:
            if all(hidden_flags):
                self.engaged = addresses
                return
            for address, _original, hidden in patches:
                self.process.patch_code(address, hidden)
            self.owned = addresses
            self.engaged = addresses
            return

        if not any(hidden_flags):
            self.engaged.clear()
            return
        if self.owned == addresses:
            for address, original, _hidden in patches:
                self.process.patch_code(address, original)
            self.owned.clear()
            self.engaged.clear()
            return
        # Exact resident suppression was adopted. Preserve code owned by the
        # previous process; a game restart is the safe upgrade boundary.
        self.engaged = addresses

    @property
    def combat_log_hidden(self) -> bool:
        return NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE in self.engaged and all(
            address in self.engaged for address in NATIVE_COMBAT_LOG_DRAW_SITES
        )

    def restore(self) -> None:
        patches = self._patches()
        addresses = {address for address, _original, _hidden in patches}
        if self.owned != addresses:
            self.engaged.clear()
            return
        try:
            if not all(
                self.process.read(address, len(hidden)) == hidden
                for address, _original, hidden in patches
            ):
                return
            for address, original, _hidden in patches:
                self.process.patch_code(address, original)
        except OSError:
            return
        finally:
            self.owned.clear()
            self.engaged.clear()


class InteractionPromptState:
    __slots__ = (
        "key",
        "image",
        "label_source",
        "label",
        "started",
        "missing_since",
        "last_point",
        "last_time",
        "last_visible",
        "bucket",
        "inner_seen_at",
        "pending_point",
        "pending_point_count",
    )

    def __init__(self) -> None:
        self.key = None
        self.image: Image.Image | None = None
        self.label_source = None
        self.label = ""
        self.started = 0.0
        self.missing_since = 0.0
        self.last_point = None
        self.last_time = 0.0
        self.last_visible = 0.0
        self.bucket = 0
        self.inner_seen_at = 0.0
        self.pending_point = None
        self.pending_point_count = 0

    def stabilize_point(self, point: tuple[float, float]) -> tuple[float, float]:
        if self.last_point is None:
            self.pending_point = None
            self.pending_point_count = 0
            return point

        distance = math.hypot(
            point[0] - self.last_point[0],
            point[1] - self.last_point[1],
        )
        if distance <= INTERACTION_PROMPT_POINT_OUTLIER_PX:
            self.pending_point = None
            self.pending_point_count = 0
            return point

        if (
            self.pending_point is not None
            and math.hypot(
                point[0] - self.pending_point[0],
                point[1] - self.pending_point[1],
            )
            <= INTERACTION_PROMPT_POINT_CONFIRM_PX
        ):
            self.pending_point_count += 1
        else:
            self.pending_point = point
            self.pending_point_count = 1

        if self.pending_point_count >= 2:
            self.pending_point = None
            self.pending_point_count = 0
            return point
        return self.last_point


@lru_cache(maxsize=2)
def native_interaction_prompt_sprite(inner_range: bool) -> Image.Image:
    with Image.open(P2_INTERACTION_ATLAS_PATH) as source:
        atlas = source.convert("RGBA")
    box = P2_INTERACTION_INNER_BOX if inner_range else P2_INTERACTION_OUTER_BOX
    marker = atlas.crop(box)
    marker_scale = (
        P2_INTERACTION_INNER_MARKER_SCALE
        if inner_range
        else P2_INTERACTION_OUTER_MARKER_SCALE
    )
    return marker.resize(
        (
            max(1, round(marker.width * marker_scale)),
            max(1, round(marker.height * marker_scale)),
        ),
        Image.Resampling.LANCZOS,
    )


def native_interaction_object_label(process, handle: int) -> str:
    try:
        interaction_object = native_interaction.resolve_handle(process, handle)
        if not interaction_object:
            return ""
        raw_name_key = struct.unpack(
            "<h",
            process.read(
                interaction_object + NATIVE_NPC_NAME_KEY_OFFSET,
                2,
            ),
        )[0]
        if raw_name_key < 0:
            direct_text = process.u64(
                interaction_object + NATIVE_NPC_DIRECT_TEXT_OFFSET
            )
            return legacy.decode_ff12_text(process, direct_text)

        name_index = raw_name_key & ~NATIVE_NPC_NAME_VARIANT_FLAG
        if name_index >= NATIVE_NPC_NAME_STATE_LIMIT:
            return ""
        state_byte = process.read(
            NATIVE_NPC_NAME_STATE_BASE
            + NATIVE_NPC_NAME_STATE_BITS_OFFSET
            + (name_index >> 3),
            1,
        )[0]
        variant = int(bool(state_byte & (1 << (name_index & 7))))

        table = process.u64(NATIVE_NPC_TEXT_TABLE_POINTER)
        if not table:
            return ""
        slot = name_index * 2 + variant
        if slot >= process.u32(table + NATIVE_NPC_TEXT_TABLE_COUNT_OFFSET):
            return ""
        text = process.u32(table + NATIVE_NPC_TEXT_TABLE_ENTRIES_OFFSET + slot * 4)
        return legacy.decode_ff12_text(process, text)
    except (OSError, RuntimeError, struct.error, IndexError):
        return ""


def native_interaction_object_position(process, handle: int):
    """Return the NPC's visual world position, if the descriptor is usable."""
    try:
        interaction_object = native_interaction.resolve_handle(process, handle)
        if not interaction_object:
            return None
        position = struct.unpack(
            "<3f",
            process.read(
                interaction_object + NATIVE_NPC_WORLD_POSITION_OFFSET,
                12,
            ),
        )
        if not all(math.isfinite(value) and abs(value) < 1.0e8 for value in position):
            return None
        return position
    except (OSError, RuntimeError, struct.error):
        return None


class NativeDialogInputBridge:
    def __init__(self, process, base: int) -> None:
        self.process = process
        self.base = base
        self.cave = base + NATIVE_DIALOG_CAVE_OFFSET
        self.axis_cave = base + NATIVE_AXIS_CAVE_OFFSET
        self.event_publisher_cave = base + NATIVE_INPUT_EVENT_PUBLISHER_CAVE_OFFSET
        self.command_helper_cave = base + NATIVE_INPUT_COMMAND_HELPER_CAVE_OFFSET
        self.desktop_button_cave = base + NATIVE_DESKTOP_BUTTON_CAVE_OFFSET
        self.mouse_look_cave = base + NATIVE_MOUSE_LOOK_CAVE_OFFSET
        self.desktop_wheel_cave = base + NATIVE_DESKTOP_WHEEL_CAVE_OFFSET
        self.state = base + NATIVE_DIALOG_STATE_OFFSET
        self.input_state = base + NATIVE_INPUT_STATE_OFFSET
        self.release_at = 0.0
        self.installed = False
        self.owns_static = False
        self.p1_replacement = False
        self.custom_directional_owner = False
        self.p1_menu_owns_lb = False
        self.party_menu_blocked = False
        self.quickening_block_mask = 0
        self.non_p1_command_blocked = False
        self.leader_replacement = False
        self.capture_desktop_events = False
        self.suppress_mouse_look = False
        self.p1_controller_slot = legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT
        self.native_gamepad_slot_mask = 0x0F
        self.policy_generation = 2
        self.policy_index = 0
        self.policy = NativeInputPolicy()
        # This compact slot predates the double-buffered policy and is not read
        # by any resident hook.  Retain it only as a repairable compatibility
        # mirror; the committed policy slot is the sole authority.
        self.compact_p1_controller_slot = (
            legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT
        )
        self.event_consumer_sequence = 0
        self.event_overflow_count = 0
        self.desktop_capture_epoch: tuple[str, int] | None = None

    def fail_closed_for_recovery(self) -> None:
        """Leave resident hooks safe while the supervisor replaces this worker."""

        if not self.installed:
            return
        errors: list[BaseException] = []

        # A replacement worker may be starting while remote memory is briefly
        # unstable.  Every independent safety stage is therefore best-effort:
        # one failed write must not skip the policy commit or leave later
        # latches/ring records armed.  Pulse-off stays first because the cave
        # observes a synthetic pulse before consulting the policy masks.
        try:
            self.release()
        except BaseException as error:
            errors.append(error)

        closed_contract = dict(
            p1_replacement=True,
            non_p1_command_blocked=True,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=True,
            quickening_block_mask=0,
            leader_replacement=True,
            capture_desktop_events=False,
            suppress_mouse_look=False,
            force=True,
        )
        try:
            self.publish_contract(**closed_contract)
        except BaseException as error:
            errors.append(error)
            # A failed inactive-slot or commit write is commonly transient.
            # Retry the same coherent publication once before continuing with
            # the independently safe mailbox teardown.
            try:
                self.publish_contract(**closed_contract)
            except BaseException as retry_error:
                errors.append(retry_error)
        for offset in (
            NATIVE_INPUT_COMMAND_SOURCES_OFFSET,
            NATIVE_INPUT_COMMAND_SEED_OFFSET,
            NATIVE_INPUT_WHEEL_SEED_OFFSET,
        ):
            try:
                self.process.write(self.input_state + offset, b"\x00" * 4)
            except BaseException as error:
                errors.append(error)
                try:
                    self.process.write(self.input_state + offset, b"\x00" * 4)
                except BaseException as retry_error:
                    errors.append(retry_error)
        try:
            producer = self._read_u32(
                self.input_state + NATIVE_INPUT_EVENT_PRODUCER_OFFSET
            )
            self.process.write(
                self.input_state + NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
                struct.pack("<I", producer),
            )
            self.event_consumer_sequence = producer
        except BaseException as error:
            errors.append(error)
            try:
                producer = self._read_u32(
                    self.input_state + NATIVE_INPUT_EVENT_PRODUCER_OFFSET
                )
                self.process.write(
                    self.input_state + NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
                    struct.pack("<I", producer),
                )
                self.event_consumer_sequence = producer
            except BaseException as retry_error:
                errors.append(retry_error)
        try:
            # Retry pulse disarm as the final independent operation in case the
            # leading byte write was the one transient failure.
            self.process.write(self.state, b"\x00" * 5)
            self.release_at = 0.0
        except BaseException as error:
            errors.append(error)
            try:
                self.process.write(self.state, b"\x00" * 5)
                self.release_at = 0.0
            except BaseException as retry_error:
                errors.append(retry_error)

        if errors:
            raise errors[0]

    @staticmethod
    def _rel32(source: int, length: int, target: int) -> bytes:
        return struct.pack("<i", target - (source + length))

    def _build_cave(self) -> bytes:
        code = bytearray()
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: bytes, target: str) -> None:
            code.extend(opcode)
            fixups.append((len(code), target))
            code.extend(b"\x00" * 4)

        # Select one immutable policy slot.  The generation low bit chooses it;
        # a second generation read rejects a slot reused across two publications
        # while this native query was pre-empted.
        code += b"\x49\xba" + struct.pack("<Q", self.input_state)
        code += b"\x45\x8b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        code += b"\x45\x89\xd8\x41\x83\xe0\x01\x41\xc1\xe0\x04"
        code += b"\x4f\x8d\x04\x02"
        code += b"\x45\x3b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        branch(b"\x0f\x85", "false")
        code += b"\x41\x8b\x40\x08"
        code += b"\x39\x81\x58\x04\x00\x00"
        branch(b"\x0f\x85", "other_slot")

        # Synthetic pulses are already authorized by the worker and therefore
        # precede every physical-input mask, including Party and Pause transfer.
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x3b\x10"
        branch(b"\x0f\x85", "matched_policy")
        code += b"\x80\x78\x04\x00"
        branch(b"\x0f\x84", "matched_policy")
        code += b"\xb8\x01\x00\x00\x00\xc3"

        label("matched_policy")
        code += b"\x45\x8b\x00"
        branch(b"\xe9", "check_mask")

        label("other_slot")
        code += b"\x83\xfa" + bytes((NATIVE_DIALOG_COMMAND,))
        branch(b"\x0f\x85", "other_policy")
        # Configuration A reaches the desktop-specific hook; configuration B
        # reaches this normalized slot.  Both feed the same logical-down helper.
        code += b"\x44\x0f\xb6\x89\x3c\x04\x00\x00"
        code += b"\x8b\x81\x58\x04\x00\x00"
        # A normalized COMMAND query is not proof of desktop origin: the same
        # function services the effective P1/P2/P3 gamepad slots.  Consult the
        # immutable slot mask before touching the two-source desktop latch.
        code += b"\x83\xf8\x03"
        branch(b"\x0f\x87", "normalized_desktop")
        code += b"\x83\xc0" + bytes((NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT,))
        code += b"\x41\x0f\xa3\x40\x0c"
        branch(b"\x0f\x82", "other_policy")
        code += b"\x83\xe8" + bytes((NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT,))
        label("normalized_desktop")
        code += b"\x41\xb8\x02\x00\x00\x00"
        call_site = self.cave + len(code)
        code += b"\xe8" + self._rel32(call_site, 5, self.command_helper_cave)
        code += b"\xc3"

        label("other_policy")
        code += b"\x45\x8b\x40\x04"

        label("check_mask")
        code += b"\x89\xd0\x83\xe8" + bytes((NATIVE_DIALOG_COMMAND,))
        code += b"\x83\xf8\x10"
        branch(b"\x0f\x87", "original")
        code += b"\x41\x0f\xa3\xc0"
        branch(b"\x0f\x82", "false")

        label("original")
        code += NATIVE_GET_BUTTON_ORIGINAL
        jump_site = self.cave + len(code)
        code += b"\xe9" + self._rel32(jump_site, 5, NATIVE_GET_BUTTON_RETURN)

        label("false")
        code += b"\x31\xc0\xc3"

        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                self.cave + labels[target] - (self.cave + displacement_at + 4),
            )
        return bytes(code)

    def _build_event_publisher_cave(self) -> bytes:
        """Append one generation-tagged event without waiting on another writer.

        EAX is the packed payload and R9D is the policy generation sampled by
        the calling hook.  The writer lock protects the two native producers
        from publishing the same sequence.  Callers preserve the volatile
        values they require for a failed non-blocking publication.
        """

        code = bytearray(b"\x41\x89\xc0")  # mov r8d,eax (payload)
        code += b"\x49\xba" + struct.pack("<Q", self.input_state)
        labels: dict[str, int] = {}
        short_fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: int, target: str) -> None:
            code.append(opcode)
            short_fixups.append((len(code), target))
            code.append(0)

        # A busy writer is dropped instead of spinning on a game input thread.
        code += b"\x31\xc0\x41\xbb\x01\x00\x00\x00"
        code += b"\xf0\x45\x0f\xb1\x5a" + bytes(
            (NATIVE_INPUT_EVENT_WRITER_OFFSET,)
        )
        branch(0x75, "busy")

        # Reject a caller pre-empted across a policy publication.  Its physical
        # field remains retryable by the caller and no stale record is committed.
        code += b"\x45\x39\x4a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        branch(0x75, "stale")

        code += b"\x45\x8b\x5a" + bytes((NATIVE_INPUT_EVENT_PRODUCER_OFFSET,))
        code += b"\x41\xff\xc3"  # next sequence
        code += b"\x44\x89\xd9"
        code += b"\x41\x2b\x4a" + bytes((NATIVE_INPUT_EVENT_CONSUMER_OFFSET,))
        code += b"\x83\xf9" + bytes((NATIVE_INPUT_EVENT_CAPACITY,))
        branch(0x77, "full")

        # Record index is based on the old producer, equivalently next-1.
        code += b"\x41\x8d\x4b\xff"
        code += b"\x83\xe1" + bytes((NATIVE_INPUT_EVENT_CAPACITY - 1,))
        code += b"\xc1\xe1\x04"
        code += b"\x49\x8d\x4c\x0a" + bytes((NATIVE_INPUT_EVENT_RING_OFFSET,))
        # Every reservation byte starts at zero and reserved is never written by
        # native code. Generation and payload precede record sequence; producer
        # is the final ring commit and an interlocked XCHG is its fence.
        code += b"\x44\x89\x49\x04\x44\x89\x41\x08"
        code += b"\x44\x89\x19"
        code += b"\x45\x87\x5a" + bytes((NATIVE_INPUT_EVENT_PRODUCER_OFFSET,))
        code += b"\x6a\x01\x58"
        branch(0xEB, "release")

        label("full")
        code += b"\x41\xff\x42" + bytes((NATIVE_INPUT_EVENT_OVERFLOW_OFFSET,))
        code += b"\x31\xc0"
        branch(0xEB, "release")
        label("stale")
        code += b"\x31\xc0"
        label("release")
        code += b"\x31\xc9\x41\x87\x4a" + bytes(
            (NATIVE_INPUT_EVENT_WRITER_OFFSET,)
        )
        code += b"\xc3"

        label("busy")
        code += b"\x31\xc0\xc3"

        for displacement_at, target in short_fixups:
            displacement = labels[target] - (displacement_at + 1)
            if not -128 <= displacement <= 127:
                raise RuntimeError("native event publisher short branch is out of range")
            struct.pack_into("<b", code, displacement_at, displacement)
        return bytes(code)

    def _build_command_helper_cave(self) -> bytes:
        """Merge one native M1 source and apply the desktop command policy.

        Inputs are EAX=device slot, R8D=source bit, R9D=raw-down.  The return in
        EAX is the value visible to the native caller after suppression.
        """

        code = bytearray(b"\x53\x56\x57")  # preserve rbx, rsi and rdi
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: bytes, target: str) -> None:
            code.extend(opcode)
            fixups.append((len(code), target))
            code.extend(b"\x00" * 4)

        code += b"\x44\x89\xce"  # raw down remains stable in ESI
        code += b"\x0f\xb6\xf8"
        code += b"\x81\xcf" + struct.pack(
            "<I", NATIVE_INPUT_EVENT_COMMAND << NATIVE_INPUT_EVENT_KIND_SHIFT
        )
        code += b"\x49\xba" + struct.pack("<Q", self.input_state)
        code += b"\x41\x8b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        code += b"\x89\xd8\x83\xe0\x01\xc1\xe0\x04"
        code += b"\x41\x8b\x54\x02\x04\x45\x8b\x5c\x02\x0c"
        # A slot may be reused after two worker publications while this native
        # call is pre-empted.  A mismatched second read therefore fails closed.
        code += b"\x41\x3b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        branch(b"\x0f\x85", "conservative_false")
        code += b"\x41\xf7\xc3" + struct.pack(
            "<I", NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
        )
        branch(b"\x0f\x84", "disabled")

        # Enabling capture seeds each independently observed alias.  A source
        # that is already held must first be observed released; an unused alias
        # remains only in this seed word and never contaminates logical_down.
        code += b"\x45\x85\x42" + bytes((NATIVE_INPUT_COMMAND_SEED_OFFSET,))
        branch(b"\x0f\x84", "seeded")
        code += b"\x85\xf6"
        branch(b"\x0f\x85", "seed_down")
        code += b"\x44\x89\xc0\xf7\xd0"
        code += b"\xf0\x41\x21\x42" + bytes((NATIVE_INPUT_COMMAND_SEED_OFFSET,))
        branch(b"\xe9", "released")

        label("seed_down")
        code += b"\x89\xf0"
        branch(b"\xe9", "suppression")

        label("seeded")
        code += b"\x85\xf6"
        branch(b"\x0f\x84", "released")
        code += b"\x45\x8b\x5a" + bytes((NATIVE_INPUT_COMMAND_SOURCES_OFFSET,))
        code += b"\x45\x85\xc3"
        # TEST r11d,r8d: a source already represented in the aggregate cannot
        # create another edge.  A different alias continues and is OR-latched.
        branch(b"\x0f\x85", "down_done")
        # One bounded CAS both acquires this physical source and returns the
        # exact previous aggregate.  A competing alias never spins on a game
        # input thread: it fails closed and retries from the still-held device
        # state on a later native query.
        code += b"\x44\x89\xd8\x44\x89\xd9\x44\x09\xc1"
        code += b"\xf0\x41\x0f\xb1\x4a" + bytes(
            (NATIVE_INPUT_COMMAND_SOURCES_OFFSET,)
        )
        branch(b"\x0f\x85", "conservative_false")
        code += b"\x85\xc0"
        branch(b"\x0f\x85", "down_done")
        code += b"\x89\xf8\x41\x89\xd9"  # payload; exact generation
        # Preserve the policy mask and source bit across the volatile publisher.
        code += b"\x52\x41\x50"
        call_site = self.command_helper_cave + len(code)
        code += b"\xe8" + self._rel32(call_site, 5, self.event_publisher_cave)
        code += b"\x41\x58\x5a"
        # A refused publication has delivered no aggregate edge.  Atomically
        # clear the whole latch, not only this source: another alias may have
        # joined after our successful zero->source CAS and intentionally skipped
        # publication because it observed our aggregate as already down.  Every
        # still-held physical source is naturally sampled again on a later query.
        code += b"\x85\xc0"
        branch(b"\x0f\x85", "down_done")
        code += b"\x31\xc0\x41\x87\x42" + bytes(
            (NATIVE_INPUT_COMMAND_SOURCES_OFFSET,)
        )
        label("down_done")
        code += b"\x89\xf0"
        branch(b"\xe9", "suppression")

        label("released")
        code += b"\x44\x89\xc0\xf7\xd0"
        code += b"\xf0\x41\x21\x42" + bytes((NATIVE_INPUT_COMMAND_SOURCES_OFFSET,))
        code += b"\x31\xc0"
        branch(b"\xe9", "suppression")

        label("disabled")
        code += b"\x44\x89\xc0\xf7\xd0"
        code += b"\xf0\x41\x21\x42" + bytes((NATIVE_INPUT_COMMAND_SOURCES_OFFSET,))
        code += b"\x89\xf0"

        label("suppression")
        code += b"\xf6\xc2" + bytes((NATIVE_INPUT_COMMAND_MASK,))
        branch(b"\x0f\x84", "done")
        label("conservative_false")
        code += b"\x31\xc0"
        label("done")
        code += b"\x5f\x5e\x5b\xc3"

        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                labels[target] - (displacement_at + 4),
            )
        return bytes(code)

    def _build_desktop_button_cave(self) -> bytes:
        code = bytearray(NATIVE_DESKTOP_BUTTON_ORIGINAL)
        code += b"\x85\xd2"
        native_return = len(code)
        code += b"\x0f\x85\x00\x00\x00\x00"
        code += b"\x41\x89\xc1\x41\xb8\x01\x00\x00\x00"
        code += b"\x8b\x81\x58\x04\x00\x00"
        call_site = self.desktop_button_cave + len(code)
        code += b"\xe8" + self._rel32(call_site, 5, self.command_helper_cave)
        return_offset = len(code)
        jump_site = self.desktop_button_cave + len(code)
        code += b"\xe9" + self._rel32(jump_site, 5, NATIVE_DESKTOP_BUTTON_RETURN)
        struct.pack_into("<i", code, native_return + 2, return_offset - (native_return + 6))
        return bytes(code)

    def _build_desktop_wheel_cave(self) -> bytes:
        code = bytearray()
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []
        short_fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: bytes, target: str) -> None:
            code.extend(opcode)
            fixups.append((len(code), target))
            code.extend(b"\x00" * 4)

        def short_branch(opcode: int, target: str) -> None:
            code.append(opcode)
            short_fixups.append((len(code), target))
            code.append(0)

        code += b"\x49\xba" + struct.pack("<Q", self.input_state)
        code += b"\x45\x8b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        code += b"\x44\x89\xd8\x83\xe0\x01\xc1\xe0\x04"
        code += b"\x45\x8b\x44\x02\x0c"
        # Do not consume the wheel field unless both generation reads delimit a
        # coherent immutable policy snapshot.
        code += b"\x45\x3b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        branch(b"\x0f\x85", "suppressed")
        code += b"\x41\xf7\xc0" + struct.pack(
            "<I", NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
        )
        short_branch(0x74, "native")
        # Consuming the field makes the three native aliases (IDs 3/6/9) one
        # ordered delta without relying on a debounce interval.
        code += b"\x31\xc0\x87\x81\xf0\x01\x00\x00\x89\xc2"
        code += b"\x41\xf7\xc0" + struct.pack(
            "<I", NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
        )
        short_branch(0x74, "suppressed")
        code += b"\x31\xc0\x41\x87\x42" + bytes((NATIVE_INPUT_WHEEL_SEED_OFFSET,))
        code += b"\x85\xc0"
        short_branch(0x75, "suppressed")
        code += b"\x85\xd2"
        short_branch(0x74, "suppressed")
        code += b"\x89\xd0\x25" + struct.pack(
            "<I", NATIVE_INPUT_EVENT_VALUE_MASK
        )
        code += b"\x0d" + struct.pack(
            "<I", NATIVE_INPUT_EVENT_WHEEL << NATIVE_INPUT_EVENT_KIND_SHIFT
        )
        code += b"\x45\x89\xd9"  # exact policy generation
        # Preserve volatile inputs across the leaf publisher.  A refused wheel
        # sample is intentionally lost: transient gestures must never be
        # restored, coalesced or replayed after a policy/cooldown boundary.
        code += b"\x52\x51"
        call_site = self.desktop_wheel_cave + len(code)
        code += b"\xe8" + self._rel32(call_site, 5, self.event_publisher_cave)
        code += b"\x59\x5a"

        label("suppressed")
        code += b"\x0f\x57\xc0\xc3"

        label("native")
        code += NATIVE_DESKTOP_WHEEL_ORIGINAL
        jump_site = self.desktop_wheel_cave + len(code)
        code += b"\xe9" + self._rel32(jump_site, 5, NATIVE_DESKTOP_WHEEL_RETURN)

        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                self.desktop_wheel_cave
                + labels[target]
                - (self.desktop_wheel_cave + displacement_at + 4),
            )
        for displacement_at, target in short_fixups:
            displacement = labels[target] - (displacement_at + 1)
            if not -128 <= displacement <= 127:
                raise RuntimeError("native desktop wheel short branch is out of range")
            struct.pack_into("<b", code, displacement_at, displacement)
        return bytes(code)

    def _build_mouse_look_cave(self) -> bytes:
        """Zero only raw desktop X/Y when the desktop belongs to P2 or P3."""

        code = bytearray()
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: bytes, target: str) -> None:
            code.extend(opcode)
            fixups.append((len(code), target))
            code.extend(b"\x00" * 4)

        code += b"\x49\xba" + struct.pack("<Q", self.input_state)
        code += b"\x45\x8b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        code += b"\x44\x89\xd8\x83\xe0\x01\xc1\xe0\x04"
        code += b"\x45\x8b\x44\x02\x0c"
        code += b"\x45\x3b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        # An unstable snapshot is conservatively suppressed for this one sample.
        branch(b"\x0f\x85", "suppress")
        code += b"\x41\xf7\xc0" + struct.pack(
            "<I", NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK
        )
        branch(b"\x0f\x84", "original")
        label("suppress")
        code += b"\x48\xc7\x83\xe8\x01\x00\x00\x00\x00\x00\x00"

        label("original")
        code += b"\x48\x8b\xcb"
        call_site = self.mouse_look_cave + len(code)
        code += b"\xe8" + self._rel32(
            call_site,
            5,
            NATIVE_MOUSE_LOOK_FINALIZER,
        )
        jump_site = self.mouse_look_cave + len(code)
        code += b"\xe9" + self._rel32(jump_site, 5, NATIVE_MOUSE_LOOK_RETURN)

        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                self.mouse_look_cave
                + labels[target]
                - (self.mouse_look_cave + displacement_at + 4),
            )
        return bytes(code)

    def _build_axis_cave(self) -> bytes:
        code = bytearray()
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def branch(opcode: bytes, target: str) -> None:
            code.extend(opcode)
            fixups.append((len(code), target))
            code.extend(b"\x00" * 4)

        # Read both the P1 slot and the axis-ownership bit from one immutable
        # policy generation.  The former compact state+5 byte was outside the
        # atomic policy and stopped being published when button ownership moved
        # to the double buffer.
        code += b"\x49\xba" + struct.pack("<Q", self.input_state)
        code += b"\x45\x8b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        code += b"\x45\x89\xd8\x41\x83\xe0\x01\x41\xc1\xe0\x04"
        code += b"\x4f\x8d\x04\x02"
        code += b"\x45\x3b\x5a" + bytes((NATIVE_INPUT_POLICY_ACTIVE_OFFSET,))
        branch(b"\x0f\x85", "zero")
        code += b"\x45\x8b\x58\x08"
        code += b"\x44\x39\x99\x58\x04\x00\x00"
        branch(b"\x0f\x85", "original")
        code += b"\x41\xf7\x40\x0c" + struct.pack(
            "<I", NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES
        )
        branch(b"\x0f\x84", "original")

        code += b"\x48\xa1" + struct.pack("<Q", NATIVE_PAUSE_PRIMARY_OBJECT)
        code += b"\x48\x85\xc0"
        branch(b"\x0f\x85", "original")
        code += b"\x48\xa1" + struct.pack("<Q", NATIVE_PAUSE_SECONDARY_OBJECT)
        code += b"\x48\x85\xc0"
        branch(b"\x0f\x85", "original")

        code += b"\x48\xa1" + struct.pack("<Q", UI_MANAGER_GLOBAL)
        code += b"\x48\x85\xc0"
        branch(b"\x0f\x84", "original")
        code += b"\x48\x05" + struct.pack("<I", P1_MENU_OFFSET)
        code += b"\x48\x81\x38" + struct.pack("<I", P1_MENU_CALLBACK)
        branch(b"\x0f\x85", "original")
        code += b"\x49\xb8" + struct.pack("<Q", P1_ACTIVE_MENU_GLOBAL)
        code += b"\x4d\x8b\x00\x49\x39\xc0"
        branch(b"\x0f\x85", "original")
        label("zero")
        code += b"\x66\x0f\xef\xc0\xc3"

        label("original")
        code += NATIVE_GET_AXIS_ORIGINAL
        jump = self.axis_cave + len(code)
        code += b"\xe9" + self._rel32(jump, 5, NATIVE_GET_AXIS_RETURN)
        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                self.axis_cave
                + labels[target]
                - (self.axis_cave + displacement_at + 4),
            )
        return bytes(code)

    def _static_patches(self):
        return (
            (
                "native dialog GET_BUTTON",
                NATIVE_GET_BUTTON_SITE,
                NATIVE_GET_BUTTON_ORIGINAL,
                b"\xe9"
                + self._rel32(NATIVE_GET_BUTTON_SITE, 5, self.cave)
                + b"\x90",
            ),
            (
                "native dialog GET_AXIS",
                NATIVE_GET_AXIS_SITE,
                NATIVE_GET_AXIS_ORIGINAL,
                b"\xe9" + self._rel32(NATIVE_GET_AXIS_SITE, 5, self.axis_cave),
            ),
            (
                "native desktop GET_BUTTON",
                NATIVE_DESKTOP_BUTTON_SITE,
                NATIVE_DESKTOP_BUTTON_ORIGINAL,
                b"\xe9"
                + self._rel32(
                    NATIVE_DESKTOP_BUTTON_SITE,
                    5,
                    self.desktop_button_cave,
                )
                + b"\x90" * 3,
            ),
            (
                "native desktop GET_WHEEL",
                NATIVE_DESKTOP_WHEEL_SITE,
                NATIVE_DESKTOP_WHEEL_ORIGINAL,
                b"\xe9"
                + self._rel32(
                    NATIVE_DESKTOP_WHEEL_SITE,
                    5,
                    self.desktop_wheel_cave,
                )
                + b"\x90" * 3,
            ),
            (
                "native desktop mouse-look publish",
                NATIVE_MOUSE_LOOK_SITE,
                NATIVE_MOUSE_LOOK_ORIGINAL,
                b"\xe9"
                + self._rel32(
                    NATIVE_MOUSE_LOOK_SITE,
                    5,
                    self.mouse_look_cave,
                )
                + b"\x90" * 3,
            ),
        )

    def _static_cave_images(self):
        caves = (
            (
                "native axis",
                self.axis_cave,
                self._build_axis_cave(),
                self.command_helper_cave,
            ),
            (
                "native command helper",
                self.command_helper_cave,
                self._build_command_helper_cave(),
                self.base + P1_LIST_INPUT_CAVE_OFFSET,
            ),
            (
                "native desktop button",
                self.desktop_button_cave,
                self._build_desktop_button_cave(),
                self.base + P1_LIST_INPUT_STATE_OFFSET,
            ),
            (
                "native mouse look",
                self.mouse_look_cave,
                self._build_mouse_look_cave(),
                self.desktop_wheel_cave,
            ),
            (
                "native desktop wheel",
                self.desktop_wheel_cave,
                self._build_desktop_wheel_cave(),
                self.cave,
            ),
            (
                "native dialog",
                self.cave,
                self._build_cave(),
                self.state,
            ),
            (
                "native event publisher",
                self.event_publisher_cave,
                self._build_event_publisher_cave(),
                self.base + 0xF000,
            ),
        )
        images = []
        for name, address, cave, limit in caves:
            if address + len(cave) > limit:
                raise RuntimeError(f"{name} cave exceeds its reserved region")
            images.append(
                (
                    name,
                    address,
                    cave + b"\x00" * (limit - address - len(cave)),
                )
            )
        if self.input_state + NATIVE_INPUT_STATE_SIZE > self.base + 0xE800:
            raise RuntimeError("native input state exceeds its reserved region")
        return tuple(images)

    @staticmethod
    def _default_state_images() -> tuple[bytes, bytes, NativeInputPolicy]:
        initial_state = bytearray(NATIVE_DIALOG_STATE_SIZE)
        initial_state[
            NATIVE_DIALOG_LAYOUT_MAGIC_OFFSET : NATIVE_DIALOG_LAYOUT_MAGIC_OFFSET
            + len(NATIVE_DIALOG_LAYOUT_MAGIC)
        ] = NATIVE_DIALOG_LAYOUT_MAGIC
        struct.pack_into(
            "<I",
            initial_state,
            NATIVE_DIALOG_P1_SLOT_OFFSET,
            legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT,
        )
        initial_input_state = bytearray(NATIVE_INPUT_STATE_SIZE)
        # Roots become executable one at a time.  Their very first generation
        # must therefore be fail-closed, not pass-through; otherwise the game
        # thread can accept COMMAND/leader/Party between the first root patch
        # and the later Python bootstrap publication.
        default_policy = native_input_policy(
            p1_replacement=True,
            non_p1_command_blocked=True,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=True,
            quickening_block_mask=0,
            leader_replacement=True,
            capture_desktop_events=False,
            suppress_mouse_look=False,
            p1_native_slot=legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT,
            native_gamepad_slot_mask=0x0F,
        )
        initial_input_state[:NATIVE_INPUT_POLICY_SLOT_SIZE] = default_policy.pack()
        initial_input_state[
            NATIVE_INPUT_POLICY_SLOT_SIZE : 2 * NATIVE_INPUT_POLICY_SLOT_SIZE
        ] = default_policy.pack()
        struct.pack_into(
            "<I",
            initial_input_state,
            NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
            2,
        )
        return bytes(initial_state), bytes(initial_input_state), default_policy

    @staticmethod
    def _adopted_layout(
        state_image: bytes,
        input_image: bytes,
    ) -> tuple[int, NativeInputPolicy, int, int]:
        magic_start = NATIVE_DIALOG_LAYOUT_MAGIC_OFFSET
        magic_end = magic_start + len(NATIVE_DIALOG_LAYOUT_MAGIC)
        if state_image[magic_start:magic_end] != NATIVE_DIALOG_LAYOUT_MAGIC:
            raise RuntimeError("active native dialog input layout magic mismatch")
        if state_image[4] not in (0, 1):
            raise RuntimeError("active native dialog pulse mailbox is incoherent")
        compact_slot = struct.unpack_from(
            "<I",
            state_image,
            NATIVE_DIALOG_P1_SLOT_OFFSET,
        )[0]
        valid_slots = {0, 1, 2, 3, legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT}
        if compact_slot not in valid_slots:
            raise RuntimeError("active native dialog controller slot is incoherent")

        policy_generation = struct.unpack_from(
            "<I",
            input_image,
            NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
        )[0]
        if policy_generation == 0:
            raise RuntimeError("active native dialog policy generation is incoherent")
        policies = tuple(
            NativeInputPolicy(*struct.unpack_from("<4I", input_image, index * 0x10))
            for index in (0, 1)
        )
        allowed_semantic_mask = (1 << 17) - 1
        allowed_flags = (
            NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
            | NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
            | NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES
            | NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK
            | NATIVE_INPUT_POLICY_GAMEPAD_SLOT_MASK
        )
        for policy in policies:
            if (
                policy.p1_semantic_mask & ~allowed_semantic_mask
                or policy.other_semantic_mask & ~allowed_semantic_mask
                or policy.p1_native_slot not in valid_slots
                or policy.flags & ~allowed_flags
            ):
                raise RuntimeError("active native dialog policy layout is incoherent")
        active_policy = policies[policy_generation & 1]
        producer = struct.unpack_from(
            "<I",
            input_image,
            NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        )[0]
        consumer = struct.unpack_from(
            "<I",
            input_image,
            NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
        )[0]
        if (producer - consumer) & 0xFFFFFFFF > NATIVE_INPUT_EVENT_CAPACITY:
            raise RuntimeError("active native dialog event ring is incoherent")
        overflow = struct.unpack_from(
            "<I",
            input_image,
            NATIVE_INPUT_EVENT_OVERFLOW_OFFSET,
        )[0]
        writer = struct.unpack_from(
            "<I",
            input_image,
            NATIVE_INPUT_EVENT_WRITER_OFFSET,
        )[0]
        if writer not in (0, 1):
            raise RuntimeError("active native dialog event writer is incoherent")
        return policy_generation, active_policy, consumer, overflow

    def _reset_local_contract(self, policy: NativeInputPolicy, generation: int) -> None:
        p1_mask = int(policy.p1_semantic_mask)
        other_mask = int(policy.other_semantic_mask)
        p1_replacement_mask = native_semantic_mask(
            NATIVE_DIALOG_COMMAND,
            NATIVE_DIALOG_CONFIRM,
            NATIVE_DIALOG_CANCEL,
        )

        # A hot adopter must treat the committed immutable policy as its sole
        # authority.  Resetting these mirrors to False made the first partial
        # setter silently rebuild an all-native policy, even though no caller
        # had asked to release the resident crash/bootstrap firewall.
        self.p1_replacement = bool(
            p1_mask & p1_replacement_mask == p1_replacement_mask
        )
        self.custom_directional_owner = bool(
            p1_mask & NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK
            == NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK
            and other_mask & NATIVE_INPUT_CUSTOM_DIRECTION_MASK
            == NATIVE_INPUT_CUSTOM_DIRECTION_MASK
        )
        self.p1_menu_owns_lb = bool(p1_mask & NATIVE_INPUT_LB_MASK)
        self.party_menu_blocked = bool(
            p1_mask & NATIVE_INPUT_PARTY_MASK
            and other_mask & NATIVE_INPUT_PARTY_MASK
        )

        # Some policy contributions deliberately overlap.  Hydrate their
        # canonical local representation conservatively: redundant owners
        # reproduce the exact same packed policy and ensure that a later
        # partial release cannot accidentally under-block an adopted route.
        quickening_block_mask = 0
        if (
            p1_mask & NATIVE_INPUT_COMMAND_MASK
            and other_mask & NATIVE_INPUT_COMMAND_MASK
        ):
            quickening_block_mask |= QUICKENING_BLOCK_COMMAND
        if (
            p1_mask & NATIVE_INPUT_CONFIRM_MASK
            and other_mask & NATIVE_INPUT_CONFIRM_MASK
        ):
            quickening_block_mask |= QUICKENING_BLOCK_CONFIRM
        self.quickening_block_mask = quickening_block_mask
        self.non_p1_command_blocked = bool(other_mask & NATIVE_INPUT_COMMAND_MASK)
        self.leader_replacement = bool(
            p1_mask & NATIVE_INPUT_LEADER_MASK == NATIVE_INPUT_LEADER_MASK
            and other_mask & NATIVE_INPUT_LEADER_MASK
            == NATIVE_INPUT_LEADER_MASK
        )
        self.capture_desktop_events = bool(
            policy.flags & NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
        )
        self.suppress_mouse_look = bool(
            policy.flags & NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK
        )
        self.p1_controller_slot = policy.p1_native_slot
        self.native_gamepad_slot_mask = (
            policy.flags & NATIVE_INPUT_POLICY_GAMEPAD_SLOT_MASK
        ) >> NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT
        self.policy_generation = int(generation) & 0xFFFFFFFF
        self.policy_index = self.policy_generation & 1
        self.policy = policy
        # A resident policy cannot identify the Python-side owner that authored
        # it.  A replacement worker establishes a fresh epoch on first publish.
        self.desktop_capture_epoch = None

    def install(self) -> None:
        patches = self._static_patches()
        caves = self._static_cave_images()
        initial_state, initial_input_state, default_policy = (
            self._default_state_images()
        )

        # Complete read-only preflight.  No branch below may mutate remote
        # memory until all four roots and every byte of every reservation have
        # been classified as one coherent topology.
        root_images = {
            site: self.process.read(site, len(original))
            for _name, site, original, _patch in patches
        }
        cave_images = {
            address: self.process.read(address, len(image))
            for _name, address, image in caves
        }
        state_image = self.process.read(self.state, NATIVE_DIALOG_STATE_SIZE)
        input_image = self.process.read(self.input_state, NATIVE_INPUT_STATE_SIZE)

        all_stock = all(
            root_images[site] == original
            for _name, site, original, _patch in patches
        )
        all_patched = all(
            root_images[site] == patch
            for _name, site, _original, patch in patches
        )
        if not all_stock and not all_patched:
            foreign = next(
                (
                    (name, root_images[site])
                    for name, site, original, patch in patches
                    if root_images[site] not in (original, patch)
                ),
                None,
            )
            if foreign is not None:
                name, current = foreign
                raise RuntimeError(f"{name} occupied: " + current.hex(" "))
            raise RuntimeError(
                "partial native dialog input bridge installation; restart the game"
            )

        if all_patched:
            for name, address, image in caves:
                if cave_images[address] != image:
                    raise RuntimeError(f"active {name} cave mismatch")
            policy_generation, policy, consumer, overflow = self._adopted_layout(
                state_image,
                input_image,
            )
            self._reset_local_contract(policy, policy_generation)
            self.compact_p1_controller_slot = struct.unpack_from(
                "<I",
                state_image,
                NATIVE_DIALOG_P1_SLOT_OFFSET,
            )[0]
            self.event_consumer_sequence = consumer
            self.event_overflow_count = overflow
            # A worker may die after publishing Party/Pause/Confirm but before
            # its timer releases the pulse.  A new worker has no trustworthy
            # deadline for that foreign pulse, so disarm it before adoption;
            # otherwise the game sees a permanently held synthetic button.
            if state_image[4]:
                try:
                    self.process.write(self.state + 4, b"\x00")
                except BaseException as error:
                    raise RuntimeError(
                        "stale native dialog pulse could not be disarmed"
                    ) from error
                self.release_at = 0.0
            self.installed = True
            self.owns_static = False
            return

        reservations_zero = bool(
            all(
                cave_images[address] == b"\x00" * len(image)
                for _name, address, image in caves
            )
            and state_image == b"\x00" * NATIVE_DIALOG_STATE_SIZE
            and input_image == b"\x00" * NATIVE_INPUT_STATE_SIZE
        )
        if not reservations_zero:
            raise RuntimeError(
                "non-empty native dialog input reservation with stock roots; "
                "restart the game"
            )

        try:
            self.process.write(self.state, initial_state)
            self.process.write(self.input_state, initial_input_state)
            for _name, address, image in caves:
                self.process.write(address, image)
        except BaseException as error:
            raise RuntimeError(
                "native dialog input cave publication failed; restart the game"
            ) from error

        self.owns_static = True
        for name, site, _original, patch in patches:
            try:
                self.process.patch_code(site, patch)
            except BaseException as error:
                # Never detach a root that may already be executing or rewrite
                # one of its caves.  The exact resident prefix is adoptable by
                # a later process only after a full game restart completes the
                # topology.
                self.installed = False
                raise RuntimeError(
                    f"{name} publication failed; restart the game"
                ) from error

        self._reset_local_contract(default_policy, 2)
        self.compact_p1_controller_slot = (
            legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT
        )
        self.event_consumer_sequence = 0
        self.event_overflow_count = 0
        self.installed = True

    def publish_contract(
        self,
        *,
        p1_replacement: bool | None = None,
        non_p1_command_blocked: bool | None = None,
        custom_directional_owner: bool | None = None,
        p1_menu_owns_lb: bool | None = None,
        party_menu_blocked: bool | None = None,
        quickening_block_mask: int | None = None,
        leader_replacement: bool | None = None,
        capture_desktop_events: bool | None = None,
        suppress_mouse_look: bool | None = None,
        p1_controller_slot: int | None = None,
        native_gamepad_slot_mask: int | None = None,
        desktop_capture_epoch: tuple[str, int] | None = None,
        force: bool = False,
    ) -> NativeInputPolicy:
        """Publish one coherent native-input policy via the inactive slot."""

        if p1_replacement is not None:
            self.p1_replacement = bool(p1_replacement)
        if non_p1_command_blocked is not None:
            self.non_p1_command_blocked = bool(non_p1_command_blocked)
        if custom_directional_owner is not None:
            self.custom_directional_owner = bool(custom_directional_owner)
        if p1_menu_owns_lb is not None:
            self.p1_menu_owns_lb = bool(p1_menu_owns_lb)
        if party_menu_blocked is not None:
            self.party_menu_blocked = bool(party_menu_blocked)
        if quickening_block_mask is not None:
            self.quickening_block_mask = int(quickening_block_mask) & (
                QUICKENING_BLOCK_COMMAND | QUICKENING_BLOCK_CONFIRM
            )
        if leader_replacement is not None:
            self.leader_replacement = bool(leader_replacement)
        if capture_desktop_events is not None:
            self.capture_desktop_events = bool(capture_desktop_events)
        if suppress_mouse_look is not None:
            self.suppress_mouse_look = bool(suppress_mouse_look)
        if p1_controller_slot is not None:
            self.p1_controller_slot = int(p1_controller_slot) & 0xFFFFFFFF
        if native_gamepad_slot_mask is not None:
            self.native_gamepad_slot_mask = int(native_gamepad_slot_mask) & 0x0F

        policy = native_input_policy(
            p1_replacement=self.p1_replacement,
            non_p1_command_blocked=self.non_p1_command_blocked,
            custom_directional_owner=self.custom_directional_owner,
            p1_menu_owns_lb=self.p1_menu_owns_lb,
            party_menu_blocked=self.party_menu_blocked,
            quickening_block_mask=self.quickening_block_mask,
            leader_replacement=self.leader_replacement,
            capture_desktop_events=self.capture_desktop_events,
            suppress_mouse_look=self.suppress_mouse_look,
            p1_native_slot=self.p1_controller_slot,
            native_gamepad_slot_mask=self.native_gamepad_slot_mask,
        )
        # Repair the obsolete compact mirror before the aligned policy commit.
        # A crash between these writes is harmless: adoption trusts the atomic
        # policy and a later publication repairs the mirror.  The reverse order
        # would make a failed mirror write strand an already-active generation.
        if self.compact_p1_controller_slot != policy.p1_native_slot:
            self.process.write(
                self.state + NATIVE_DIALOG_P1_SLOT_OFFSET,
                struct.pack("<I", policy.p1_native_slot),
            )
            self.compact_p1_controller_slot = policy.p1_native_slot
        capture_changed = bool(
            (policy.flags ^ self.policy.flags) & NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
        )
        capture_enabled = bool(policy.flags & NATIVE_INPUT_POLICY_CAPTURE_DESKTOP)
        capture_epoch_changed = bool(
            capture_enabled
            and desktop_capture_epoch is not None
            and desktop_capture_epoch != self.desktop_capture_epoch
        )
        if not force and policy == self.policy and not capture_epoch_changed:
            return policy

        # Seeds absorb inherited physical state only on a real capture handoff.
        # Internal menu transitions may force a fresh policy generation, but
        # rearming here would eat the next genuine click or wheel detent.
        if capture_enabled and (capture_changed or capture_epoch_changed):
            self.process.write(
                self.input_state + NATIVE_INPUT_COMMAND_SOURCES_OFFSET,
                b"\x00\x00\x00\x00",
            )
            self.process.write(
                self.input_state + NATIVE_INPUT_COMMAND_SEED_OFFSET,
                struct.pack("<I", 0x03),
            )
            self.process.write(
                self.input_state + NATIVE_INPUT_WHEEL_SEED_OFFSET,
                struct.pack("<I", 1),
            )
        next_generation = (int(self.policy_generation) + 1) & 0xFFFFFFFF
        if next_generation == 0:
            next_generation = 2
        inactive_index = next_generation & 1
        self.process.write(
            self.input_state + inactive_index * NATIVE_INPUT_POLICY_SLOT_SIZE,
            policy.pack(),
        )
        # This aligned DWORD is the sole commit point observed by every hook.
        # Its full value invalidates old events/routes; only its low bit chooses
        # the immutable slot.
        self.process.write(
            self.input_state + NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
            struct.pack("<I", next_generation),
        )
        self.policy_generation = next_generation
        self.policy_index = inactive_index
        self.policy = policy
        if not capture_enabled:
            self.desktop_capture_epoch = None
        elif desktop_capture_epoch is not None:
            # Partial policy setters intentionally omit the ownership epoch.
            # Preserve the established capture identity across those internal
            # commits so the following full frame cannot mistake them for a
            # desktop handoff and rearm the click/wheel seeds.
            self.desktop_capture_epoch = desktop_capture_epoch
        if capture_changed and not capture_enabled:
            for offset in (
                NATIVE_INPUT_COMMAND_SOURCES_OFFSET,
                NATIVE_INPUT_COMMAND_SEED_OFFSET,
                NATIVE_INPUT_WHEEL_SEED_OFFSET,
            ):
                self.process.write(self.input_state + offset, b"\x00\x00\x00\x00")
        return policy

    def set_p1_replacement(self, enabled: bool) -> None:
        self.publish_contract(p1_replacement=enabled)

    def set_p1_controller(self, assignment, *, publish: bool = True) -> None:
        slot = legacy.coop.native_controller_slot(assignment)
        if publish:
            # Do not short-circuit solely on the desired local value: a prior
            # WriteProcessMemory failure may have left the committed policy (or
            # its compatibility mirror) behind and this call is its retry.
            self.publish_contract(p1_controller_slot=slot)
        else:
            self.p1_controller_slot = slot

    def set_native_gamepad_slot_mask(
        self,
        slot_mask: int,
        *,
        publish: bool = True,
    ) -> None:
        slot_mask = int(slot_mask) & 0x0F
        if publish:
            self.publish_contract(native_gamepad_slot_mask=slot_mask)
        else:
            self.native_gamepad_slot_mask = slot_mask

    def set_custom_directional_owner(self, enabled: bool) -> None:
        self.publish_contract(custom_directional_owner=enabled)

    def set_p1_menu_owns_lb(self, enabled: bool) -> None:
        self.publish_contract(p1_menu_owns_lb=enabled)

    def set_party_menu_blocked(self, enabled: bool) -> None:
        self.publish_contract(party_menu_blocked=enabled)

    def set_quickening_block_mask(self, mask: int) -> None:
        self.publish_contract(quickening_block_mask=mask)

    def set_non_p1_command_blocked(self, enabled: bool) -> None:
        self.publish_contract(non_p1_command_blocked=enabled)

    def set_leader_replacement(self, enabled: bool) -> None:
        self.publish_contract(leader_replacement=enabled)

    def set_desktop_events_enabled(self, enabled: bool) -> None:
        self.publish_contract(capture_desktop_events=enabled)

    def set_mouse_look_suppressed(self, enabled: bool) -> None:
        self.publish_contract(suppress_mouse_look=enabled)

    def _read_u32(self, address: int) -> int:
        reader = getattr(self.process, "u32", None)
        if callable(reader):
            return int(reader(address)) & 0xFFFFFFFF
        return struct.unpack("<I", self.process.read(address, 4))[0]

    def committed_policy_generation(self) -> int:
        return self._read_u32(
            self.input_state + NATIVE_INPUT_POLICY_ACTIVE_OFFSET
        )

    def drain_desktop_events(self) -> tuple[NativeDesktopInputEvent, ...]:
        producer = self._read_u32(
            self.input_state + NATIVE_INPUT_EVENT_PRODUCER_OFFSET
        )
        consumer = self._read_u32(
            self.input_state + NATIVE_INPUT_EVENT_CONSUMER_OFFSET
        )
        available = (producer - consumer) & 0xFFFFFFFF
        if available > NATIVE_INPUT_EVENT_CAPACITY:
            raise RuntimeError(
                "native desktop input ring is incoherent: "
                f"producer={producer:#x}, consumer={consumer:#x}"
            )

        events: list[NativeDesktopInputEvent] = []
        while consumer != producer:
            sequence = (consumer + 1) & 0xFFFFFFFF
            index = (sequence - 1) & (NATIVE_INPUT_EVENT_CAPACITY - 1)
            record = self.process.read(
                self.input_state
                + NATIVE_INPUT_EVENT_RING_OFFSET
                + index * NATIVE_INPUT_EVENT_RECORD_SIZE,
                NATIVE_INPUT_EVENT_RECORD_SIZE,
            )
            record_sequence, generation, payload, reserved = struct.unpack(
                "<4I",
                record,
            )
            # Producer is committed last, but retain the record when a remote
            # read races visibility rather than consuming a partial event.
            if record_sequence != sequence:
                break
            if reserved:
                raise RuntimeError("native desktop input record is incoherent")
            events.append(
                decode_native_input_event(
                    sequence,
                    payload,
                    generation,
                )
            )
            consumer = sequence

        if consumer != self.event_consumer_sequence:
            self.process.write(
                self.input_state + NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
                struct.pack("<I", consumer),
            )
            self.event_consumer_sequence = consumer
        self.event_overflow_count = self._read_u32(
            self.input_state + NATIVE_INPUT_EVENT_OVERFLOW_OFFSET
        )
        return tuple(events)

    def discard_desktop_events(self) -> None:
        self.drain_desktop_events()

    def modal_active(self) -> bool:
        try:
            manager = self.process.u64(UI_MANAGER_GLOBAL)
            if not manager:
                return False
            manager_flags = self.process.u32(
                manager + NATIVE_DIALOG_MANAGER_FLAGS_OFFSET
            )
            ui_object = self.process.u64(manager + NATIVE_DIALOG_MANAGER_OBJECT_OFFSET)
            if not ui_object:
                return False
            object_flags = int.from_bytes(
                self.process.read(ui_object + NATIVE_DIALOG_OBJECT_FLAGS_OFFSET, 2),
                "little",
            )
            return bool((manager_flags & 0x40) and (object_flags & 0x08))
        except (OSError, RuntimeError):
            return False

    def pulse(self, button: int, duration: float | None = None) -> None:
        self.process.write(self.state, struct.pack("<I", button) + b"\x01")
        self.release_at = time.perf_counter() + (
            NATIVE_DIALOG_PULSE_SECONDS if duration is None else duration
        )

    def maintain(self) -> None:
        if self.release_at and time.perf_counter() >= self.release_at:
            self.release()

    def release(self) -> None:
        self.process.write(self.state + 4, b"\x00")
        self.release_at = 0.0

    def restore(self) -> None:
        if not self.installed:
            return
        errors: list[BaseException] = []

        # Static roots and caves are process-lifetime.  Teardown publishes an
        # all-native contract through the same double buffer, then disarms only
        # mutable mailboxes.  A game-thread call already inside any cave remains
        # valid throughout this sequence.
        try:
            self.release()
        except BaseException as error:
            errors.append(error)
        all_native_contract = dict(
            p1_replacement=False,
            non_p1_command_blocked=False,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=False,
            quickening_block_mask=0,
            leader_replacement=False,
            capture_desktop_events=False,
            suppress_mouse_look=False,
            force=True,
        )
        try:
            self.publish_contract(**all_native_contract)
        except BaseException as error:
            errors.append(error)
            try:
                self.publish_contract(**all_native_contract)
            except BaseException as retry_error:
                errors.append(retry_error)

        for offset in (
            NATIVE_INPUT_COMMAND_SOURCES_OFFSET,
            NATIVE_INPUT_COMMAND_SEED_OFFSET,
            NATIVE_INPUT_WHEEL_SEED_OFFSET,
        ):
            try:
                self.process.write(self.input_state + offset, b"\x00" * 4)
            except BaseException as error:
                errors.append(error)
                try:
                    self.process.write(self.input_state + offset, b"\x00" * 4)
                except BaseException as retry_error:
                    errors.append(retry_error)
        try:
            producer = self._read_u32(
                self.input_state + NATIVE_INPUT_EVENT_PRODUCER_OFFSET
            )
            self.process.write(
                self.input_state + NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
                struct.pack("<I", producer),
            )
            self.event_consumer_sequence = producer
        except BaseException as error:
            errors.append(error)
            try:
                producer = self._read_u32(
                    self.input_state + NATIVE_INPUT_EVENT_PRODUCER_OFFSET
                )
                self.process.write(
                    self.input_state + NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
                    struct.pack("<I", producer),
                )
                self.event_consumer_sequence = producer
            except BaseException as retry_error:
                errors.append(retry_error)
        try:
            self.process.write(self.state, b"\x00" * 5)
            self.release_at = 0.0
        except BaseException as error:
            errors.append(error)
            try:
                self.process.write(self.state, b"\x00" * 5)
                self.release_at = 0.0
            except BaseException as retry_error:
                errors.append(retry_error)

        if errors:
            raise errors[0]
        self.installed = False
        self.owns_static = False


class NativeP1ListInputBridge:
    def __init__(self, process, base: int) -> None:
        self.process = process
        self.cave = base + P1_LIST_INPUT_CAVE_OFFSET
        self.state = base + P1_LIST_INPUT_STATE_OFFSET
        self.installed = False
        self.owns_static = False

    @staticmethod
    def _rel32(source: int, length: int, target: int) -> bytes:
        return struct.pack("<i", target - (source + length))

    def _build_cave(self) -> bytes:
        code = bytearray()
        labels: dict[str, int] = {}
        fixups: list[tuple[int, str]] = []

        def label(name: str) -> None:
            labels[name] = len(code)

        def jne(name: str) -> None:
            code.extend(b"\x0f\x85")
            fixups.append((len(code), name))
            code.extend(b"\x00" * 4)

        def je(name: str) -> None:
            code.extend(b"\x0f\x84")
            fixups.append((len(code), name))
            code.extend(b"\x00" * 4)

        def jump(name: str) -> None:
            code.extend(b"\xe9")
            fixups.append((len(code), name))
            code.extend(b"\x00" * 4)

        code += b"\x50\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x83\x38\x01"
        jne("pass")
        code += b"\x83\x3a\x02"
        jne("pass")
        code += b"\xc7\x00\x02\x00\x00\x00\x58"

        code += b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
        code += b"\x48\x83\xec\x20"
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x83\x78\x04\x02"
        je("event")
        code += b"\x48\x8b\x48\x08"
        code += b"\x8b\x50\x10"
        code += b"\x48\xb8" + struct.pack("<Q", P1_SET_LIST_INDEX)
        code += b"\xff\xd0"
        jump("complete")

        label("event")
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\x48\x8b\x48\x08"
        code += b"\x48\x8d\x50\x20"
        code += b"\x48\xb8" + struct.pack("<Q", P1_LIST_CALLBACK_SITE)
        code += b"\xff\xd0"

        label("complete")
        code += b"\x48\xb8" + struct.pack("<Q", self.state)
        code += b"\xc7\x00\x03\x00\x00\x00"
        code += b"\x48\x83\xc4\x20"
        code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x58"
        code += P1_LIST_CALLBACK_ORIGINAL
        jump = self.cave + len(code)
        code += b"\xe9" + self._rel32(jump, 5, P1_LIST_CALLBACK_RETURN)

        label("pass")
        code += b"\x58" + P1_LIST_CALLBACK_ORIGINAL
        jump = self.cave + len(code)
        code += b"\xe9" + self._rel32(jump, 5, P1_LIST_CALLBACK_RETURN)
        for displacement_at, target in fixups:
            struct.pack_into(
                "<i",
                code,
                displacement_at,
                self.cave + labels[target] - (self.cave + displacement_at + 4),
            )
        return bytes(code)

    def install(self) -> None:
        patch = b"\xe9" + self._rel32(P1_LIST_CALLBACK_SITE, 5, self.cave)
        current = self.process.read(P1_LIST_CALLBACK_SITE, 5)
        if current not in (P1_LIST_CALLBACK_ORIGINAL, patch):
            raise RuntimeError("native P1 list callback occupied: " + current.hex(" "))
        cave = self._build_cave()
        if len(cave) > P1_LIST_INPUT_CAVE_SIZE:
            raise RuntimeError("native P1 list callback cave exceeds its reserved slot")
        cave_image = cave + b"\x00" * (P1_LIST_INPUT_CAVE_SIZE - len(cave))
        current_cave = self.process.read(self.cave, P1_LIST_INPUT_CAVE_SIZE)
        if current == patch:
            if current_cave != cave_image:
                raise RuntimeError("active native P1 list callback cave mismatch")
            self.installed = True
            return
        if current_cave != b"\x00" * P1_LIST_INPUT_CAVE_SIZE:
            raise RuntimeError(
                "non-empty native P1 list callback cave with a stock root; "
                "restart the game"
            )
        self.process.write(self.state, b"\x00" * 0x40)
        self.process.write(self.cave, cave_image)
        self.process.patch_code(P1_LIST_CALLBACK_SITE, patch)
        self.installed = True
        self.owns_static = True

    def request(self, controller: int, index: int) -> bool:
        if not self.installed or not controller:
            return False
        status = struct.unpack("<I", self.process.read(self.state, 4))[0]
        if status in (1, 2):
            return False

        self.process.write(
            self.state + 8,
            struct.pack("<QI", controller, index & 0xFFFFFFFF),
        )
        self.process.write(self.state + 4, struct.pack("<I", 1))
        self.process.write(self.state, struct.pack("<I", 1))
        return True

    def restore(self) -> None:
        if not self.installed:
            return
        # State zero makes the resident callback execute only its native
        # pass-through path.  Keep immutable code process-lifetime so another
        # helper cannot overwrite a cave that may still be on the game stack.
        try:
            self.process.write(self.state, b"\x00" * 4)
        finally:
            self.installed = False
            self.owns_static = False


def build_atomic_follower_npc_interaction_cave(
    cave_address: int,
    mailbox: int,
) -> bytes:
    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(label: str) -> None:
        labels[label] = len(code)

    def branch(opcode: bytes, label: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), label))
        code.extend(b"\x00" * 4)

    def call_absolute(target: int) -> None:
        code.extend(b"\x48\xb8" + struct.pack("<Q", int(target)) + b"\xff\xd0")

    # Win64: the caller supplies the return address.  One saved nonvolatile
    # register plus 0x70 bytes keeps RSP aligned and leaves the first 0x20 bytes
    # as shadow space for every native call below.
    code += b"\x53\x48\x83\xec\x70"
    code += b"\x48\xbb" + struct.pack("<Q", int(mailbox))
    code += b"\xc7\x44\x24\x60\x00\x00\x00\x00"

    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_SOURCE)
    code += b"\x48\x8b\x00\x48\x89\x44\x24\x28"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    code += b"\x8a\x00\x88\x44\x24\x30"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x48\x8b\x10\x48\x89\x54\x24\x38"
    code += b"\x48\x8b\x50\x08\x48\x89\x54\x24\x40"
    code += b"\x48\x8b\x50\x10\x48\x89\x54\x24\x48"

    # Python publishes only generation handles. Resolve the source immediately
    # before use so a delayed mailbox request cannot retain an entry across a
    # party swap, KO rematerialization, area transition or despawn.
    code += b"\x8b\x4b" + bytes((P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,))
    code += b"\x85\xc9"
    branch(b"\x0f\x84", "restore")
    call_absolute(NATIVE_ENTITY_RESOLVER)
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "restore")

    # The exact five-registry scanner validates the entry's common hierarchy,
    # but later dereferences entry+0xB8 without a null check.  During actor
    # rematerialization a generation-valid entry can briefly have no physics
    # object there.  Reject that transient state on the game thread before the
    # scanner sees it; standard descriptor consumption does not use this field.
    for policy in (
        P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
        P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
    ):
        code += b"\x83\x7b" + bytes((P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET, policy))
        branch(b"\x0f\x84", "validate_exact_source")
    branch(b"\xe9", "publish_source")

    mark("validate_exact_source")
    code += b"\x48\x83\xb8\xb8\x00\x00\x00\x00"
    branch(b"\x0f\x84", "restore")

    mark("publish_source")
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_SOURCE)
    code += b"\x49\x89\x03"

    # Exact NPC requests run the native five-registry scan.  Standard treasure,
    # crystal and door requests already carry a complete private descriptor and
    # can publish it directly inside this same atomic native call.
    for policy in (
        P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
        P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
    ):
        code += b"\x83\x7b" + bytes((P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET, policy))
        branch(b"\x0f\x84", "exact_scan")

    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x48\x8b\x43\x20\x49\x89\x03"
    code += b"\x48\x8b\x43\x28\x49\x89\x43\x08"
    code += b"\x48\x8b\x43\x30\x49\x89\x43\x10"
    code += b"\x49\xba" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    code += b"\x41\xc6\x02\x01"
    branch(b"\xe9", "resolve_target")

    mark("exact_scan")
    code += b"\xc7\x43" + bytes((P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET,))
    code += b"\x01\x00\x00\x00"
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    code += b"\x41\xc6\x03\x00"
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x41\xc7\x03" + struct.pack("<I", NATIVE_INTERACTION_RESET_DISTANCE_BITS)
    code += b"\x41\xc7\x43\x04\xff\xff\xff\xff"
    code += b"\x41\xc7\x43\x08\xff\xff\xff\xff"
    code += b"\x41\xc7\x43\x0c\x02\x00\x00\x00"
    code += b"\x41\xc7\x43\x10\x00\x00\x00\x00"
    code += b"\x41\xc7\x43\x14" + struct.pack("<I", NATIVE_INTERACTION_RESET_READY_FLAGS)
    for registry in range(5):
        code += b"\xb9" + struct.pack("<I", registry)
        call_absolute(legacy.coop.INTERACTION_NATIVE_INDICATOR_SCAN)
    code += b"\xc7\x43" + bytes((P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET,))
    code += b"\x00\x00\x00\x00"
    call_absolute(legacy.coop.INTERACTION_BUILD_EXACT_HANDLE)
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    code += b"\x41\x80\x3b\x01"
    branch(b"\x0f\x85", "restore")

    mark("resolve_target")
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x41\x8b\x4b\x10\x85\xc9"
    branch(b"\x0f\x84", "restore")
    code += b"\x89\x4c\x24\x50"
    call_absolute(NATIVE_ENTITY_RESOLVER)
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "restore")

    # The prompt sampler publishes only its private descriptor and never calls
    # the consumer. All other policies consume while the resolved target is live.
    code += b"\x83\x7b" + bytes(
        (
            P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
            P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
        )
    )
    branch(b"\x0f\x85", "consume")
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x49\x8b\x13\x48\x89\x53" + bytes(
        (P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,)
    )
    code += b"\x49\x8b\x53\x08\x48\x89\x53" + bytes(
        (P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET + 8,)
    )
    code += b"\x49\x8b\x53\x10\x48\x89\x53" + bytes(
        (P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET + 16,)
    )
    branch(b"\xe9", "consumed")

    mark("consume")
    # 0x268D00 assumes two optional target subobjects are present. A target can
    # remain generation-valid for a short teardown window after either one has
    # disappeared, so mirror those prerequisites before entering the native
    # consumer. The helper and consumer execute on the same game thread.
    code += b"\x80\x78\x18\x01"
    branch(b"\x0f\x84", "check_target_auxiliary")
    code += b"\x48\x83\x78\x40\x00"
    branch(b"\x0f\x84", "restore")

    # For a non-type-1 target, 0x268D10 conditionally enters 0x3DBBE0 when
    # descriptor state 2 pairs with target flag 0x4, or any other descriptor
    # state pairs with target flag 0x400.  That path unconditionally dereferences
    # target+0x48 in 0x3DBCF0 and can then dereference the action-slot storage at
    # target+0xA0 in 0x3DB7A0.  Mirror the native branch exactly so transiently
    # torn-down targets fail closed without rejecting targets for which the
    # native consumer would not inspect either subobject.
    code += b"\x49\xbb" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x41\x83\x7b\x0c\x02"
    branch(b"\x0f\x84", "validate_state_two_action_queue")
    code += b"\xf7\x40\x1c\x00\x04\x00\x00"
    branch(b"\x0f\x84", "check_target_auxiliary")
    branch(b"\xe9", "validate_action_queue")

    mark("validate_state_two_action_queue")
    code += b"\xf7\x40\x1c\x04\x00\x00\x00"
    branch(b"\x0f\x84", "check_target_auxiliary")

    mark("validate_action_queue")
    code += b"\x48\x83\x78\x48\x00"
    branch(b"\x0f\x84", "restore")
    code += b"\x48\x83\xb8\xa0\x00\x00\x00\x00"
    branch(b"\x0f\x84", "restore")

    mark("check_target_auxiliary")
    code += b"\xf7\x40\x1c\x00\x20\x00\x00"
    branch(b"\x0f\x84", "target_ready")
    code += b"\x48\x89\xc1"
    call_absolute(NATIVE_INTERACTION_AUXILIARY_RESOLVER)
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "restore")
    # The auxiliary resolver returns the subobject, so reacquire the target
    # entry before touching its temporary native-consumer flag.
    code += b"\x8b\x4c\x24\x50"
    call_absolute(NATIVE_ENTITY_RESOLVER)
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "restore")

    mark("target_ready")
    code += b"\x8a\x50\x0c\x88\x54\x24\x58"
    code += b"\x80\xca\x02\x88\x54\x24\x59\x88\x50\x0c"
    call_absolute(NATIVE_INTERACTION_CONSUMER_TARGET)
    # The consumer may synchronously remove the target. Re-resolve its handle
    # before restoring the temporary flag; never dereference the pre-call entry.
    code += b"\x8b\x4c\x24\x50"
    call_absolute(NATIVE_ENTITY_RESOLVER)
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "consumed")
    code += b"\x8a\x50\x0c\x3a\x54\x24\x59"
    branch(b"\x0f\x85", "consumed")
    code += b"\x8a\x54\x24\x58\x88\x50\x0c"

    mark("consumed")
    code += b"\xc7\x44\x24\x60\x01\x00\x00\x00"

    mark("restore")
    code += b"\xc7\x43" + bytes((P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET,))
    code += b"\x00\x00\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_SOURCE)
    code += b"\x48\x8b\x54\x24\x28\x48\x89\x10"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_CAN_USE_GATE)
    code += b"\x8a\x54\x24\x30\x88\x10"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_DESCRIPTOR)
    code += b"\x48\x8b\x54\x24\x38\x48\x89\x10"
    code += b"\x48\x8b\x54\x24\x40\x48\x89\x50\x08"
    code += b"\x48\x8b\x54\x24\x48\x48\x89\x50\x10"
    code += b"\x8b\x44\x24\x60\x48\x83\xc4\x70\x5b\xc3"

    for position, label in fixups:
        if label not in labels:
            raise RuntimeError(f"missing atomic NPC label: {label}")
        displacement = (int(cave_address) + labels[label]) - (
            int(cave_address) + position + 4
        )
        struct.pack_into("<i", code, position, displacement)
    if len(code) > FOLLOWER_NPC_ATOMIC_CAVE_LIMIT:
        raise RuntimeError(f"atomic follower NPC cave overflow: {len(code):#x}")
    return bytes(code)


def build_p2_interaction_consumer_wrapper(
    mailbox: int,
    atomic_helper: int | None = None,
) -> bytes:
    if atomic_helper is None:
        atomic_helper = (
            int(mailbox)
            - P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
            + FOLLOWER_NPC_ATOMIC_CAVE_OFFSET
        )
    code = bytearray()
    code += b"\x53"
    code += b"\x48\x83\xec\x30"
    code += b"\xc6\x44\x24\x20\x00"
    code += b"\x48\xbb" + struct.pack("<Q", mailbox)
    # Claim 1 -> 2 atomically before reading any request field. Python publishes
    # every field first and ARMED=1 last, and never clears a claimed request
    # until DONE. This closes the compare/read/cancel race with the game thread.
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\x41\xba\x02\x00\x00\x00"
    code += b"\xf0\x44\x0f\xb1\x53\x08"
    normal_jump = len(code)
    code += b"\x0f\x85\x00\x00\x00\x00"

    code += b"\x83\x7b" + bytes(
        (
            P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
            P2_INTERACTION_CONSUMER_POLICY_TREASURE,
        )
    )
    treasure_safe_jump = len(code)
    code += b"\x0f\x84\x00\x00\x00\x00"

    reject_jumps: list[int] = []

    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_RENDERING_FLAGS)
    code += b"\xf6\x00" + bytes((NATIVE_UI_RENDER_ENABLED_MASK,))
    reject_jumps.append(len(code))
    code += b"\x0f\x84\x00\x00\x00\x00"

    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_SUPPRESSION_FLAGS)
    code += b"\xf7\x00" + struct.pack("<I", NATIVE_INTERACTION_SUPPRESSION_MASK)
    reject_jumps.append(len(code))
    code += b"\x0f\x85\x00\x00\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_EVENT_FLAGS)
    code += b"\xf7\x00" + struct.pack("<I", NATIVE_INTERACTION_EVENT_BUSY_MASK)
    reject_jumps.append(len(code))
    code += b"\x0f\x85\x00\x00\x00\x00"

    # Resolve the gate actor from its generation handle on the game thread.
    # Never dereference the asynchronous Python actor pointer: the actor may be
    # rematerialized or removed before this consumer call is reached.
    code += b"\x8b\x4b" + bytes((P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET,))
    code += b"\x85\xc9"
    reject_jumps.append(len(code))
    code += b"\x0f\x84\x00\x00\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_ENTITY_RESOLVER)
    code += b"\xff\xd0\x48\x85\xc0"
    reject_jumps.append(len(code))
    code += b"\x0f\x84\x00\x00\x00\x00"
    code += b"\x48\x8b\x40\x30\x48\x85\xc0"
    reject_jumps.append(len(code))
    code += b"\x0f\x84\x00\x00\x00\x00"

    code += b"\xf7\x00\x00\x00\x00\x01"
    reject_jumps.append(len(code))
    code += b"\x0f\x85\x00\x00\x00\x00"

    code += b"\xf7\x40\x04\x00\x40\x00\x00"
    reject_jumps.append(len(code))
    code += b"\x0f\x85\x00\x00\x00\x00"

    code += b"\x83\x7b" + bytes(
        (
            P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
            P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
        )
    )
    follower_safe_jump = len(code)
    code += b"\x0f\x84\x00\x00\x00\x00"
    code += b"\x83\x7b" + bytes(
        (
            P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
            P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
        )
    )
    exact_safe_jump = len(code)
    code += b"\x0f\x84\x00\x00\x00\x00"
    code += b"\x83\x7b" + bytes(
        (
            P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
            P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
        )
    )
    exact_scan_safe_jump = len(code)
    code += b"\x0f\x84\x00\x00\x00\x00"
    code += b"\x83\xb8\xb4\x06\x00\x00\x01"
    reject_jumps.append(len(code))
    code += b"\x0f\x87\x00\x00\x00\x00"
    code += b"\x81\xb8\x14\x07\x00\x00\x21\x40\x00\x00"
    idle_jump = len(code)
    code += b"\x0f\x84\x00\x00\x00\x00"
    code += b"\x81\xb8\x14\x07\x00\x00\xff\xff\x00\x00"
    reject_jumps.append(len(code))
    code += b"\x0f\x85\x00\x00\x00\x00"

    safe = len(code)
    struct.pack_into(
        "<i",
        code,
        treasure_safe_jump + 2,
        safe - (treasure_safe_jump + 6),
    )
    struct.pack_into("<i", code, idle_jump + 2, safe - (idle_jump + 6))
    struct.pack_into(
        "<i",
        code,
        follower_safe_jump + 2,
        safe - (follower_safe_jump + 6),
    )
    struct.pack_into(
        "<i",
        code,
        exact_safe_jump + 2,
        safe - (exact_safe_jump + 6),
    )
    struct.pack_into(
        "<i",
        code,
        exact_scan_safe_jump + 2,
        safe - (exact_scan_safe_jump + 6),
    )
    # Every custom policy is executed by the same atomic helper. It validates
    # all generation handles before publishing anything, restores the stock P1
    # globals before returning, and reports zero when any object vanished.
    code += b"\x48\xb8" + struct.pack("<Q", int(atomic_helper))
    code += b"\xff\xd0"
    code += b"\x89\x43" + bytes((P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET,))
    code += b"\x01\x43" + bytes((P2_INTERACTION_CONSUMER_HIT_OFFSET,))
    code += b"\xff\x43" + bytes((P2_INTERACTION_CONSUMER_DONE_OFFSET,))
    custom_to_epilogue = len(code)
    code += b"\xe9\x00\x00\x00\x00"

    reject = len(code)
    for jump in reject_jumps:
        struct.pack_into("<i", code, jump + 2, reject - (jump + 6))
    code += b"\xc7\x43\x08\x03\x00\x00\x00"
    code += b"\xff\x43\x10"

    normal = len(code)
    struct.pack_into("<i", code, normal_jump + 2, normal - (normal_jump + 6))
    code += b"\x48\xb8" + struct.pack("<Q", NATIVE_INTERACTION_CONSUMER_TARGET)
    code += b"\xff\xd0"
    epilogue = len(code)
    struct.pack_into(
        "<i",
        code,
        custom_to_epilogue + 1,
        epilogue - (custom_to_epilogue + 5),
    )
    code += b"\x48\x83\xc4\x30\x5b\xc3"
    if len(code) > P2_INTERACTION_CONSUMER_CAVE_LIMIT:
        raise RuntimeError(f"P2 interaction consumer cave overflow: {len(code):#x}")
    return bytes(code)


class NativeWorldInteractionRouter:
    PRIME_SECONDS = 0.16
    CONSUMER_TIMEOUT_SECONDS = 0.50
    RELEASE_SECONDS = 0.08
    PROMPT_SCAN_INTERVAL_SECONDS = 0.035
    PROMPT_SCAN_TIMEOUT_SECONDS = 0.20
    PROMPT_SCAN_STALE_SECONDS = 0.30
    DIALOG_OPEN_TIMEOUT_SECONDS = 1.00
    DIALOG_RELEASE_SECONDS = 0.08

    def __init__(self, process, dialog_bridge: NativeDialogInputBridge) -> None:
        self.process = process
        self.dialog_bridge = dialog_bridge
        self.base = process.ensure_dual_interaction_allocation()
        self.consumer_cave = self.base + P2_INTERACTION_CONSUMER_CAVE_OFFSET
        self.consumer_mailbox = self.base + P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        self.atomic_npc_cave = self.base + FOLLOWER_NPC_ATOMIC_CAVE_OFFSET
        self.consumer_patch = b"\xe8" + struct.pack(
            "<i",
            self.consumer_cave - (NATIVE_INTERACTION_CONSUMER_CALL + 5),
        )
        self.consumer_installed = False
        self.consumer_static_owned = False
        self.atomic_npc_guards_installed = False
        self.atomic_npc_guards_owned = False
        self.active = False
        self.deadline = 0.0
        self.phase = ""
        self.slot = -1
        self.handle = 0
        self.p2_entry = 0
        self.resource = 0
        self.text = 0
        self.private_state = 0
        self.private_state_pointer = 0
        self.interaction_owner = INTERACTION_OWNER_NONE
        self.old_native_block = b""
        self.old_source = 0
        self.old_source_range = b""
        self.forced_source_range = b""
        self.interaction_object = 0
        self.old_object_flag = b""
        self.forced_object_flag = b""
        self.old_can_use_gate = b""
        self.old_descriptor = b""
        self.forced_descriptor = b""
        self.prompt_scan_player = 0
        self.prompt_scan_deadline = 0.0
        self.prompt_scan_abandoned = False
        self.prompt_scan_next_at = 0.0
        self.prompt_scan_next_player = 2
        self.prompt_scan_results: dict[
            int, tuple[int, int, int, int] | None
        ] = {2: None, 3: None}
        self.prompt_scan_result_at = {2: 0.0, 3: 0.0}
        self.dialog_hold_owner = INTERACTION_OWNER_NONE
        self.dialog_hold_open_until = 0.0
        self.dialog_hold_saw_modal = False
        self.dialog_hold_release_at = 0.0
        self.consumer_quarantined = False
        self.consumer_quarantine_owner = INTERACTION_OWNER_NONE
        self.consumer_quarantine_dialog = False
        self._install_consumer_wrapper()

    def _owner_address(self) -> int:
        return legacy.coop.COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET

    def _clear_dialog_hold(self) -> None:
        owner = self.dialog_hold_owner
        if owner:
            try:
                if self.process.read(self._owner_address(), 1) == bytes((owner,)):
                    self.process.write(
                        self._owner_address(), bytes((INTERACTION_OWNER_NONE,))
                    )
            except (OSError, RuntimeError):
                pass
        self.dialog_hold_owner = INTERACTION_OWNER_NONE
        self.dialog_hold_open_until = 0.0
        self.dialog_hold_saw_modal = False
        self.dialog_hold_release_at = 0.0

    def _begin_dialog_hold(self, owner: int, now: float) -> None:
        if self.dialog_hold_owner and self.dialog_hold_owner != owner:
            self._clear_dialog_hold()
            self.process.write(self._owner_address(), bytes((int(owner),)))
        self.dialog_hold_owner = int(owner)
        self.dialog_hold_open_until = now + self.DIALOG_OPEN_TIMEOUT_SECONDS
        self.dialog_hold_saw_modal = False
        self.dialog_hold_release_at = 0.0

    def _maintain_dialog_hold(self, modal_active: bool, now: float) -> None:
        if not self.dialog_hold_owner:
            return
        try:
            if self.process.read(self._owner_address(), 1) != bytes(
                (self.dialog_hold_owner,)
            ):
                self._clear_dialog_hold()
                return
        except (OSError, RuntimeError):
            self._clear_dialog_hold()
            return

        if modal_active:
            self.dialog_hold_saw_modal = True
            self.dialog_hold_release_at = 0.0
            return
        if not self.dialog_hold_saw_modal:
            if now >= self.dialog_hold_open_until:
                self._clear_dialog_hold()
            return
        if not self.dialog_hold_release_at:
            self.dialog_hold_release_at = now + self.DIALOG_RELEASE_SECONDS
        elif now >= self.dialog_hold_release_at:
            self._clear_dialog_hold()

    def _clear_quarantine_owner(self) -> None:
        owner = self.consumer_quarantine_owner
        dialog = self.consumer_quarantine_dialog
        self.consumer_quarantine_owner = INTERACTION_OWNER_NONE
        self.consumer_quarantine_dialog = False
        if not owner:
            return
        if dialog:
            try:
                consumed = bool(
                    self.process.u32(
                        self.consumer_mailbox
                        + P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET
                    )
                )
            except (OSError, RuntimeError):
                consumed = False
            if consumed:
                self._begin_dialog_hold(owner, time.monotonic())
                return
        try:
            if self.process.read(self._owner_address(), 1) == bytes((owner,)):
                self.process.write(
                    self._owner_address(), bytes((INTERACTION_OWNER_NONE,))
                )
        except (OSError, RuntimeError):
            pass

    def _release_consumer_if_complete(
        self,
        *,
        quarantine_owner: int = INTERACTION_OWNER_NONE,
        quarantine_dialog: bool = False,
    ) -> bool:
        try:
            armed = self.process.u32(
                self.consumer_mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET
            )
            done = self.process.u32(
                self.consumer_mailbox + P2_INTERACTION_CONSUMER_DONE_OFFSET
            )
        except (OSError, RuntimeError):
            armed = 1
            done = 0

        if armed and not done:
            self.consumer_quarantined = True
            if quarantine_owner:
                self.consumer_quarantine_owner = int(quarantine_owner)
                self.consumer_quarantine_dialog = bool(quarantine_dialog)
            return False

        was_quarantined = self.consumer_quarantined
        if armed:
            try:
                self.process.write(
                    self.consumer_mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                    b"\x00" * 4,
                )
            except (OSError, RuntimeError):
                self.consumer_quarantined = True
                return False
        self.consumer_quarantined = False
        if was_quarantined:
            self._clear_quarantine_owner()
        return True

    def _cancel_prompt_scan(self, *, wait: bool) -> bool:
        if not self.prompt_scan_player:
            return self._release_consumer_if_complete()
        if wait:
            deadline = time.perf_counter() + 0.075
            while time.perf_counter() < deadline:
                try:
                    if self.process.u32(
                        self.consumer_mailbox
                        + P2_INTERACTION_CONSUMER_DONE_OFFSET
                    ):
                        break
                except (OSError, RuntimeError):
                    break
                time.sleep(0.001)
            else:
                self.prompt_scan_abandoned = True
                if not self._release_consumer_if_complete():
                    return False
        if not self._release_consumer_if_complete():
            self.prompt_scan_abandoned = True
            return False
        self.prompt_scan_player = 0
        self.prompt_scan_deadline = 0.0
        self.prompt_scan_abandoned = False
        return True

    def _prepare_action(self) -> bool:
        if self.prompt_scan_player:
            return self._cancel_prompt_scan(wait=True)
        return self._release_consumer_if_complete()

    def clear_prompt_scans(self) -> None:
        self._cancel_prompt_scan(wait=False)
        self.prompt_scan_results = {2: None, 3: None}
        self.prompt_scan_result_at = {2: 0.0, 3: 0.0}
        self.prompt_scan_next_at = 0.0

    def prompt_interaction(
        self,
        player: int,
        *,
        now: float | None = None,
    ) -> tuple[int, int, int, int] | None:
        sampled_at = self.prompt_scan_result_at.get(int(player), 0.0)
        if (time.monotonic() if now is None else now) - sampled_at > (
            self.PROMPT_SCAN_STALE_SECONDS
        ):
            return None
        return self.prompt_scan_results.get(int(player))

    def maintain_prompt_scans(
        self,
        *,
        enabled: bool,
        candidates: dict[int, tuple[int, int]],
        now: float,
    ) -> None:
        if self.consumer_quarantined and not self._release_consumer_if_complete():
            return
        if not enabled or self.active or self.dialog_hold_owner:
            self.clear_prompt_scans()
            return

        mailbox = self.consumer_mailbox
        player = self.prompt_scan_player
        if player:
            try:
                done = self.process.u32(
                    mailbox + P2_INTERACTION_CONSUMER_DONE_OFFSET
                )
            except (OSError, RuntimeError):
                done = 0
            if done:
                interaction = None
                try:
                    rejected = self.process.u32(
                        mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET
                    ) == 3
                    found = self.process.u32(
                        mailbox + P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET
                    )
                    if not self.prompt_scan_abandoned and not rejected and found:
                        descriptor = self.process.read(
                            mailbox + P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET, 24
                        )
                        _distance, _registry, _index, state, handle, _flags = (
                            struct.unpack("<6I", descriptor)
                        )
                        if state >= 2 and handle:
                            interaction = (state, handle, 0, -1)
                except (OSError, RuntimeError, struct.error):
                    interaction = None
                self.prompt_scan_results[player] = interaction
                self.prompt_scan_result_at[player] = now
                self.process.write(
                    mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET, b"\x00" * 4
                )
                self.prompt_scan_player = 0
                self.prompt_scan_deadline = 0.0
                self.prompt_scan_abandoned = False
                self.prompt_scan_next_at = now + self.PROMPT_SCAN_INTERVAL_SECONDS
            elif now >= self.prompt_scan_deadline:
                self.prompt_scan_results[player] = None
                self.prompt_scan_result_at[player] = now
                if not self._cancel_prompt_scan(wait=False):
                    self.prompt_scan_next_at = (
                        now + self.PROMPT_SCAN_INTERVAL_SECONDS
                    )
                    return
                self.prompt_scan_next_at = now + self.PROMPT_SCAN_INTERVAL_SECONDS
            else:
                return

        if not self._release_consumer_if_complete():
            return

        if now < self.prompt_scan_next_at:
            return
        ordered_players = (
            (self.prompt_scan_next_player, 5 - self.prompt_scan_next_player)
        )
        selected = next(
            (
                candidate_player
                for candidate_player in ordered_players
                if all(candidates.get(candidate_player, (0, 0)))
            ),
            0,
        )
        if not selected:
            self.clear_prompt_scans()
            self.prompt_scan_next_at = now + self.PROMPT_SCAN_INTERVAL_SECONDS
            return
        owner_actor, source_handle = candidates[selected]
        self.prompt_scan_next_player = 5 - selected
        try:
            gate_handle = self.process.u32(int(owner_actor) + 0x08)
            if not gate_handle:
                raise RuntimeError("prompt-scan owner has no live handle")
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET, b"\x00" * 8
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET, b"\x00" * 32
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
                struct.pack(
                    "<III",
                    P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
                    0,
                    int(gate_handle),
                ),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,
                struct.pack("<I", int(source_handle)),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET,
                b"\x00" * 4,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            )
        except (OSError, RuntimeError, struct.error):
            self.prompt_scan_results[selected] = None
            self.prompt_scan_result_at[selected] = now
            self.prompt_scan_next_at = now + self.PROMPT_SCAN_INTERVAL_SECONDS
            return
        self.prompt_scan_player = selected
        self.prompt_scan_deadline = now + self.PROMPT_SCAN_TIMEOUT_SECONDS

    def _install_consumer_wrapper(self) -> None:
        wrapper = build_p2_interaction_consumer_wrapper(
            self.consumer_mailbox,
            self.atomic_npc_cave,
        )
        atomic_npc = build_atomic_follower_npc_interaction_cave(
            self.atomic_npc_cave,
            self.consumer_mailbox,
        )
        active_state = (
            self.consumer_mailbox
            + P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        )
        wrapper_image = wrapper.ljust(P2_INTERACTION_CONSUMER_CAVE_LIMIT, b"\x00")
        atomic_image = atomic_npc.ljust(FOLLOWER_NPC_ATOMIC_CAVE_LIMIT, b"\x00")
        mailbox_image = P2_INTERACTION_CONSUMER_MAGIC.ljust(
            P2_INTERACTION_CONSUMER_MAILBOX_SIZE,
            b"\x00",
        )
        guard_specs = (
            legacy.coop._follower_npc_private_object_guard_specs(active_state)
            if FOLLOWER_NPC_ATOMIC_INTERACTIONS_ENABLED
            else ()
        )
        root_specs = (
            (
                NATIVE_INTERACTION_CONSUMER_CALL,
                NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL,
                self.consumer_patch,
                "native interaction consumer",
            ),
            *(
                (site, original, patch, f"follower NPC guard {site:#x}")
                for site, _cave, original, _image, patch in guard_specs
            ),
        )
        cave_specs = (
            (
                self.consumer_cave,
                wrapper_image,
                "native interaction wrapper cave",
            ),
            *(
                (
                    (self.atomic_npc_cave, atomic_image, "atomic NPC helper cave"),
                    *(
                        (
                            cave_address,
                            cave_image,
                            f"follower NPC guard cave at {cave_address:#x}",
                        )
                        for _site, cave_address, _original, cave_image, _patch in guard_specs
                    ),
                )
                if FOLLOWER_NPC_ATOMIC_INTERACTIONS_ENABLED
                else ()
            ),
        )

        # One joint, read-only preflight covers every root and complete reserved
        # cave/mailbox slot before the first mutation.
        root_snapshots = tuple(
            self.process.read(address, len(original))
            for address, original, _patch, _label in root_specs
        )
        cave_snapshots = tuple(
            self.process.read(address, len(image))
            for address, image, _label in cave_specs
        )
        mailbox_snapshot = self.process.read(
            self.consumer_mailbox,
            P2_INTERACTION_CONSUMER_MAILBOX_SIZE,
        )
        roots_stock = all(
            current == original
            for current, (_address, original, _patch, _label) in zip(
                root_snapshots, root_specs, strict=True
            )
        )
        roots_active = all(
            current == patch
            for current, (_address, _original, patch, _label) in zip(
                root_snapshots, root_specs, strict=True
            )
        )

        if roots_active:
            for current, (_address, image, label) in zip(
                cave_snapshots, cave_specs, strict=True
            ):
                if current != image:
                    raise RuntimeError(f"active {label} mismatch")
            if not mailbox_snapshot.startswith(P2_INTERACTION_CONSUMER_MAGIC):
                raise RuntimeError("active native interaction mailbox mismatch")
            armed = struct.unpack_from(
                "<I",
                mailbox_snapshot,
                P2_INTERACTION_CONSUMER_ARMED_OFFSET,
            )[0]
            if armed not in (0, 1, 2, 3):
                raise RuntimeError(
                    f"active native interaction mailbox state mismatch: {armed}"
                )
            self.consumer_quarantined = bool(armed)
            self.consumer_installed = True
            self.consumer_static_owned = False
            self.atomic_npc_guards_installed = bool(guard_specs)
            self.atomic_npc_guards_owned = False
            return
        if not roots_stock:
            raise RuntimeError(
                "partial/foreign native interaction topology found; restart the game"
            )
        for current, (_address, _image, label) in zip(
            cave_snapshots, cave_specs, strict=True
        ):
            if any(current):
                raise RuntimeError(f"stock {label} slot is nonzero")
        if any(mailbox_snapshot):
            raise RuntimeError("stock native interaction mailbox slot is nonzero")

        # Prepare every complete image before publishing any executable root.
        self.process.write(self.consumer_mailbox, mailbox_image)
        for address, image, _label in cave_specs:
            self.process.write(address, image)

        # Revalidate the whole topology before the first one-way root commit.
        committed_mailbox = self.process.read(
            self.consumer_mailbox,
            P2_INTERACTION_CONSUMER_MAILBOX_SIZE,
        )
        committed_caves = tuple(
            self.process.read(address, len(image))
            for address, image, _label in cave_specs
        )
        committed_roots = tuple(
            self.process.read(address, len(original))
            for address, original, _patch, _label in root_specs
        )
        if committed_mailbox != mailbox_image:
            raise RuntimeError("native interaction mailbox changed before commit")
        for current, (_address, image, label) in zip(
            committed_caves, cave_specs, strict=True
        ):
            if current != image:
                raise RuntimeError(f"{label} changed before commit")
        for current, (_address, original, _patch, label) in zip(
            committed_roots, root_specs, strict=True
        ):
            if current != original:
                raise RuntimeError(f"{label} root changed before commit")

        # Guards must become reachable before the consumer can raise their
        # active-state byte. From the first guard root onward nothing rolls back.
        for site, _cave, _original, _image, patch in guard_specs:
            self.process.patch_code(site, patch)
        self.process.patch_code(
            NATIVE_INTERACTION_CONSUMER_CALL,
            self.consumer_patch,
        )
        self.atomic_npc_guards_installed = bool(guard_specs)
        self.atomic_npc_guards_owned = bool(guard_specs)
        self.consumer_static_owned = True
        self.consumer_installed = True

    def _private_slot(self) -> tuple[int, int, int, int] | None:
        private = self.private_state_pointer
        if not private:
            private = self.process.u64(
                legacy.coop.COMMAND_BLOCK + P2_INTERACTION_STATE_POINTER_OFFSET
            )
        if not private:
            return None
        if self.slot < 0:
            descriptor = self.process.read(
                private + P2_INTERACTION_EXACT_DESCRIPTOR_OFFSET, 24
            )
            _distance, _registry, _index, state, handle, _flags = struct.unpack(
                "<6I", descriptor
            )

            if state < 2 or not handle or handle != self.handle:
                return None
            return 0, 0, state, handle
        resource = self.process.u64(
            private + P2_INTERACTION_RESOURCE_SLOTS_OFFSET + self.slot * 8
        )
        text = self.process.u64(
            private + P2_INTERACTION_TEXT_SLOTS_OFFSET + self.slot * 8
        )
        state = self.process.u32(
            private + P2_INTERACTION_STATE_SLOTS_OFFSET + self.slot * 4
        )
        handle = self.process.u32(
            private + P2_INTERACTION_HANDLE_SLOTS_OFFSET + self.slot * 4
        )
        if state < 2 or not handle or handle != self.handle:
            return None
        return resource, text, state, handle

    def trigger(
        self,
        handle: int,
        slot: int,
        *,
        owner_actor: int = 0,
        leader_actor: int = 0,
        source_handle: int = 0,
        private_state_pointer: int = 0,
        interaction_owner: int = INTERACTION_OWNER_P2,
    ) -> bool:
        if self.active or not handle or not -1 <= slot < 8:
            return False
        if not owner_actor:
            return False
        if not self._prepare_action():
            return False
        try:
            self.interaction_object = native_interaction.resolve_handle(
                self.process, handle
            )
            treasure = native_interaction.is_treasure_object(
                self.process, self.interaction_object
            )
        except (OSError, RuntimeError, struct.error):
            return False
        if not self.interaction_object:
            return False
        try:
            source_handle = int(source_handle) or self.process.u32(owner_actor + 0x08)
            gate_actor = owner_actor if treasure else leader_actor
            gate_handle = self.process.u32(gate_actor + 0x08) if gate_actor else 0
        except (OSError, RuntimeError, struct.error, ValueError):
            return False
        if not source_handle or (not treasure and not gate_handle):
            return False
        if not treasure:
            leader_flags = follower_interaction_leader_flags(self.process, leader_actor)
            if leader_flags is None:
                return False
            if not follower_interaction_leader_allows(leader_flags):
                return False
        self.slot = slot
        self.handle = handle
        self.private_state_pointer = int(private_state_pointer)
        self.interaction_owner = int(interaction_owner)
        self.old_descriptor = self.process.read(NATIVE_INTERACTION_DESCRIPTOR, 0x18)
        old_ready_flags = struct.unpack_from("<I", self.old_descriptor, 0x14)[0]
        values = self._private_slot()
        if values is None:
            self._clear()
            return False
        self.resource, self.text, self.private_state, _private_handle = values

        self.forced_descriptor = struct.pack(
            "<6I",
            NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS,
            (handle >> 16) & 0x0F,
            handle & 0xFFFF,
            (sleeping_mimic_state(self.process, handle) or self.private_state),
            handle,
            old_ready_flags | NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS | 1,
        )
        self.active = True
        self.process.write(
            legacy.coop.COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET,
            bytes((self.interaction_owner,)),
        )
        print(
            f"COOP_INTERACTION_PRIME handle={handle:#x} "
            f"slot={slot} source_handle={int(source_handle):#x} "
            f"state={struct.unpack_from('<I', self.forced_descriptor, 0x0C)[0]} "
            f"private={self.private_state} "
            f"global={forced_interaction_state(self.old_descriptor, handle)} "
            f"target_handle={self.handle:#x}",
            flush=True,
        )
        completed = False
        try:
            mailbox = self.consumer_mailbox
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
                self.forced_descriptor,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_OBJECT_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_SOURCE_OFFSET,
                b"\x00" * 8,
            )

            if not treasure:
                leader_flags = follower_interaction_leader_flags(
                    self.process, leader_actor
                )
                if leader_flags is None:
                    print(
                        "COOP_INTERACTION_REJECTED_LEADER_UNAVAILABLE",
                        flush=True,
                    )
                    return False
                if not follower_interaction_leader_allows(leader_flags):
                    print(
                        "COOP_INTERACTION_REJECTED_LEADER_ANIMATION",
                        flush=True,
                    )
                    return False
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
                struct.pack(
                    "<III",
                    (
                        P2_INTERACTION_CONSUMER_POLICY_TREASURE
                        if treasure
                        else P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER
                    ),
                    0,
                    int(gate_handle),
                ),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,
                struct.pack("<I", int(source_handle)),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET,
                b"\x00" * 8,
            )

            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            )
            print("COOP_INTERACTION_CONSUMER_ARMED", flush=True)
            deadline = time.perf_counter() + self.CONSUMER_TIMEOUT_SECONDS
            while time.perf_counter() < deadline:
                if self.process.u32(mailbox + P2_INTERACTION_CONSUMER_DONE_OFFSET):
                    break
                time.sleep(0.001)
            else:
                print("COOP_INTERACTION_CONSUMER_TIMEOUT", flush=True)
                return False
            if self.process.u32(mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET) == 3:
                print(
                    "COOP_INTERACTION_CONSUMER_REJECTED_GATE",
                    flush=True,
                )
                return False
            if not self.process.u32(mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET):
                print("COOP_INTERACTION_CONSUMER_SOURCE_GONE", flush=True)
                return False
            print(
                "COOP_INTERACTION_CONSUMED "
                f"hits={self.process.u32(mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET)}",
                flush=True,
            )
            time.sleep(self.RELEASE_SECONDS)
            completed = True
            return True
        finally:
            self._release_consumer_if_complete(
                quarantine_owner=self.interaction_owner,
            )
            self.restore()
            if completed:
                print("COOP_INTERACTION_RELEASE", flush=True)

    def trigger_atomic_follower_npc(
        self,
        *,
        owner_actor: int,
        source_handle: int,
        interaction_owner: int,
    ) -> bool:
        if (
            not FOLLOWER_NPC_ATOMIC_INTERACTIONS_ENABLED
            or self.active
            or not owner_actor
            or not source_handle
        ):
            return False
        if not self._prepare_action():
            return True
        try:
            gate_handle = self.process.u32(int(owner_actor) + 0x08)
        except (OSError, RuntimeError, struct.error, ValueError):
            return False
        if not gate_handle:
            return False

        self.active = True
        self.interaction_owner = int(interaction_owner)
        mailbox = self.consumer_mailbox
        owner_address = legacy.coop.COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET
        consumed = False
        try:
            self.process.write(
                owner_address,
                bytes((self.interaction_owner,)),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET,
                b"\x00" * 4,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
                struct.pack(
                    "<III",
                    P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
                    0,
                    int(gate_handle),
                ),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,
                struct.pack("<I", int(source_handle)),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            )

            deadline = time.perf_counter() + self.CONSUMER_TIMEOUT_SECONDS
            while time.perf_counter() < deadline:
                if self.process.u32(
                    mailbox + P2_INTERACTION_CONSUMER_DONE_OFFSET
                ):
                    break
                time.sleep(0.001)
            else:
                print("FOLLOWER_NPC_ATOMIC_TIMEOUT", flush=True)
                return True

            if self.process.u32(
                mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET
            ) == 3:
                print("FOLLOWER_NPC_ATOMIC_REJECTED_GATE", flush=True)
                return True

            consumed = bool(
                self.process.u32(
                    mailbox + P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET
                )
            )
            if consumed:
                self._begin_dialog_hold(self.interaction_owner, time.monotonic())
                print(
                    "FOLLOWER_NPC_ATOMIC_CONSUMED "
                    f"owner={self.interaction_owner} handle={int(source_handle):#x}",
                    flush=True,
                )
            return consumed
        except (OSError, RuntimeError, struct.error):
            print("FOLLOWER_NPC_ATOMIC_FAILED", flush=True)
            return True
        finally:
            try:
                self._release_consumer_if_complete(
                    quarantine_owner=self.interaction_owner,
                    quarantine_dialog=True,
                )
            finally:
                self.restore(preserve_owner=consumed)

    def trigger_native_p1(
        self,
        handle: int,
        *,
        owner_actor: int = 0,
    ) -> bool:
        if self.active or not handle or not owner_actor:
            return False
        if not self._prepare_action():
            return False
        try:
            interaction_object = native_interaction.resolve_handle(self.process, handle)
            if not interaction_object:
                return False
            source_handle = self.process.u32(owner_actor + 0x08)
            if not source_handle:
                return False
            treasure = native_interaction.is_treasure_object(
                self.process, interaction_object
            )
            if not treasure and not interaction_press_allowed(
                self.process.u64(owner_actor),
                self.process.u32(owner_actor + P2_ACTION_STATE_OFFSET),
                self.process.u32(owner_actor + P2_ACTION_CURRENT_OFFSET),
            ):
                return False
            old_descriptor = self.process.read(NATIVE_INTERACTION_DESCRIPTOR, 0x18)
        except (OSError, RuntimeError, struct.error):
            return False

        old_ready_flags = struct.unpack_from("<I", old_descriptor, 0x14)[0]
        forced_state = forced_interaction_state(old_descriptor, handle)
        forced_descriptor = struct.pack(
            "<6I",
            NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS,
            (handle >> 16) & 0x0F,
            handle & 0xFFFF,
            forced_state,
            handle,
            old_ready_flags | NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS | 1,
        )
        self.active = True
        self.handle = handle
        self.interaction_object = interaction_object
        self.old_descriptor = old_descriptor
        self.forced_descriptor = forced_descriptor
        mailbox = self.consumer_mailbox
        completed = False
        try:
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
                forced_descriptor,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_OBJECT_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_SOURCE_OFFSET,
                b"\x00" * 8,
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
                struct.pack(
                    "<III",
                    (
                        P2_INTERACTION_CONSUMER_POLICY_TREASURE
                        if treasure
                        else P2_INTERACTION_CONSUMER_POLICY_STOCK_OWNER
                    ),
                    0,
                    int(source_handle),
                ),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,
                struct.pack("<I", int(source_handle)),
            )
            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET,
                b"\x00" * 8,
            )

            self.process.write(
                mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            )
            deadline = time.perf_counter() + self.CONSUMER_TIMEOUT_SECONDS
            while time.perf_counter() < deadline:
                if self.process.u32(mailbox + P2_INTERACTION_CONSUMER_DONE_OFFSET):
                    break
                time.sleep(0.001)
            else:
                print("P1_NATIVE_INTERACTION_CONSUMER_TIMEOUT", flush=True)
                return False
            if self.process.u32(mailbox + P2_INTERACTION_CONSUMER_ARMED_OFFSET) == 3:
                print("P1_NATIVE_INTERACTION_REJECTED_BUSY", flush=True)
                return False
            if not self.process.u32(mailbox + P2_INTERACTION_CONSUMER_HIT_OFFSET):
                print("P1_NATIVE_INTERACTION_SOURCE_GONE", flush=True)
                return False
            time.sleep(self.RELEASE_SECONDS)
            completed = True
            print(
                f"P1_NATIVE_INTERACTION_CONSUMED handle={handle:#x} "
                f"state={forced_state} source_handle={source_handle:#x}",
                flush=True,
            )
            return True
        finally:
            self._release_consumer_if_complete(
                quarantine_owner=self.interaction_owner,
            )
            self.restore()
            if completed:
                print("P1_NATIVE_INTERACTION_RELEASE", flush=True)

    def maintain(
        self,
        *,
        modal_active: bool = False,
        now: float | None = None,
    ) -> None:
        self._release_consumer_if_complete()
        self._maintain_dialog_hold(
            bool(modal_active), time.monotonic() if now is None else now
        )

    def _clear(self) -> None:
        self.active = False
        self.deadline = 0.0
        self.phase = ""
        self.slot = -1
        self.handle = 0
        self.p2_entry = 0
        self.resource = 0
        self.text = 0
        self.private_state = 0
        self.private_state_pointer = 0
        self.interaction_owner = INTERACTION_OWNER_NONE
        self.old_native_block = b""
        self.old_source = 0
        self.old_source_range = b""
        self.forced_source_range = b""
        self.interaction_object = 0
        self.old_object_flag = b""
        self.forced_object_flag = b""
        self.old_can_use_gate = b""
        self.old_descriptor = b""
        self.forced_descriptor = b""

    def restore(self, *, preserve_owner: bool = False) -> None:
        try:
            owner_address = legacy.coop.COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET
            if (
                not preserve_owner
                and not self.consumer_quarantined
                and self.process.read(owner_address, 1)
                == bytes((self.interaction_owner,))
            ):
                self.process.write(owner_address, bytes((INTERACTION_OWNER_NONE,)))
        finally:
            self._clear()

    def shutdown(self) -> None:
        self.clear_prompt_scans()
        if not self._release_consumer_if_complete():
            print("NATIVE_INTERACTION_SHUTDOWN_DEFERRED_IN_FLIGHT", flush=True)
            return
        self._clear_dialog_hold()
        self.restore()
        active_state = (
            self.consumer_mailbox
            + P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        )
        try:
            # The four executable roots and every cave are process-lifetime.
            # Once no ARMED=1/2 request is in flight, only the dynamic exact-scan
            # guard byte may be disarmed. A later helper adopts the static image.
            self.process.write(active_state, b"\x00" * 4)
        finally:
            self.consumer_installed = False
            self.consumer_static_owned = False
            self.atomic_npc_guards_installed = False
            self.atomic_npc_guards_owned = False


def advance_info_opacity(
    opacity: float,
    visible: bool,
    delta: float,
    *,
    immediate: bool = False,
) -> float:
    step = max(0.0, delta) / INFO_FADE_SECONDS
    if visible:
        return min(1.0, opacity + step)
    if immediate:
        return 0.0
    return max(0.0, opacity - step)


def smooth_layout_axis(
    current: float,
    desired: float,
    delta: float,
) -> float:
    if delta <= 0.0:
        return current
    blend = 1.0 - math.exp(-delta / INFO_SLIDE_TIME_CONSTANT_SECONDS)
    updated = current + (desired - current) * blend
    return desired if abs(desired - updated) < 0.25 else updated


def clearance_layout_axis(
    current: float,
    desired: float,
    delta: float,
) -> float:
    if desired > current:
        return desired
    return smooth_layout_axis(current, desired, delta)


def coherent_manual_party_members(
    process,
    snapshot: session_state.SessionSnapshot,
) -> tuple[int, ...] | None:
    if (
        not snapshot.battle_info
        or snapshot.native_menu_open
        or snapshot.native_modal_active
        or snapshot.loading
        or snapshot.cutscene
        or not snapshot.user_control_enabled
        or snapshot.current_location is None
        or snapshot.current_location == 0xFFFFFFFF
        or snapshot.current_location != snapshot.ui_location
    ):
        return None
    try:
        layouts = [
            struct.unpack(
                "<9H",
                process.read(
                    snapshot.battle_info
                    + session_state.BATTLE_PARTY_LAYOUT_OFFSET
                    + layout_id * session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                    session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                ),
            )
            for layout_id in (BATTLE_DEFAULT_LAYOUT, BATTLE_MENU_LAYOUT)
        ]
    except (OSError, RuntimeError, struct.error, ValueError):
        return None
    layouts.extend((snapshot.current_layout, snapshot.ui_layout))
    active = []
    for layout in layouts:
        members = tuple(
            member_id
            for member_id in layout[:3]
            if member_id != session_state.EMPTY_PARTY_ID
        )
        if (
            not 1 <= len(members) <= 3
            or len(set(members)) != len(members)
            or any(
                member_id not in session_state.MAIN_PARTY_MEMBER_IDS
                for member_id in members
            )
        ):
            return None
        active.append(members)
    expected = set(active[0])
    if any(set(members) != expected for members in active[1:]):
        return None
    return active[0]


class CoopMenuRuntime:
    def __init__(self) -> None:
        self._initialization_rollback_complete = False
        try:
            self._initialize()
        except BaseException:
            self._rollback_failed_initialization()
            raise

    def _initialize(self) -> None:
        self.pid = legacy.coop.find_pid()
        self.session_mutex = None
        # CreateMutexW reports pre-existence through last-error even when it
        # succeeds. Clear residual thread state and acquire the lease before
        # constructing publishers or opening the game process, so a duplicate
        # helper has no observable side effects.
        ct.set_last_error(0)
        self.session_mutex = kernel32.CreateMutexW(
            None, True, f"Local\\FF12CoopMenu_{self.pid}"
        )
        mutex_error = ct.get_last_error()
        if not self.session_mutex:
            raise OSError(mutex_error, "CreateMutexW")
        if mutex_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.session_mutex)
            self.session_mutex = None
            raise RuntimeError("co-op menu is already running for this game")
        self.party_intent_publisher = party_intent_channel.PartyIntentPublisher(
            self.pid
        )
        self.party_edit_capture = party_intent_channel.ExplicitPartyEditCapture()
        self.pending_party_intent_publish: (
            tuple[
                tuple[tuple[int, ...], int, str],
                session_state.SessionIdentity | None,
                tuple[int, int] | None,
            ]
            | None
        ) = None
        self.process = legacy.coop.RemoteProcess(self.pid)
        self.native_stock_field_hud = NativeStockFieldHudVisibility(self.process)
        self.combat_log_layout = NativeCombatLogLayout(self.process)
        self.combat_log_visibility = NativeCombatLogVisibility(self.process)
        self.combat_log_on_screen = False
        self.combat_log_gate_hidden = False
        self.boss_gauge_visible = False
        self.ko_promoted_follower_role: str | None = None
        self.leader_interaction_gate_sampled_at = 0.0
        self.leader_interaction_gate_allows = True
        self.two_line_message_until = 0.0
        self.upper_info_layout_offset_y = 0.0
        self.target_info_extra_offset_y = 0.0
        self.upper_info_layout_initialized = False
        self.upper_info_layout_last_tick = time.monotonic()
        self.base = self.process.ensure_cave_allocation()

        # This filter runs before visual components, preventing the command button
        # from opening the native menu while the remaining custom menu initializes.
        self.native_dialog_bridge = NativeDialogInputBridge(self.process, self.base)
        self.native_dialog_bridge.install()
        self.coop_runtime_active = True
        self.bootstrap_runtime_active = (
            os.environ.get("FF12_COOP_BOOTSTRAP_ACTIVE") == "1"
        )
        self.native_dialog_bridge.publish_contract(
            p1_replacement=True,
            non_p1_command_blocked=True,
            # Fail closed during bootstrap.  The first coherent session
            # snapshot will relax this only for an explicit native frontend
            # exception; no startup/replacement frame may leak leader input.
            leader_replacement=True,
        )

        recovered_tick = legacy.recover_orphaned_ui_tick_dispatcher(
            self.process, self.base
        )
        self.orphaned_ui_tick_dispatcher = None
        if recovered_tick is not None and recovered_tick.endswith(":in-flight"):
            self.orphaned_ui_tick_dispatcher = recovered_tick.removesuffix(
                ":in-flight"
            )
            print(
                "ORPHANED_UI_TICK_DEFERRED "
                f"dispatcher={self.orphaned_ui_tick_dispatcher}",
                flush=True,
            )
        elif recovered_tick is not None:
            print(
                f"ORPHANED_UI_TICK_RESTORED dispatcher={recovered_tick}",
                flush=True,
            )
        self.native_combat_log_target_gate = NativeCombatLogTargetInfoGate(
            self.process, self.base
        )
        try:
            self.native_combat_log_target_gate.install()
        except (OSError, RuntimeError) as error:
            print(
                "NATIVE_COMBAT_LOG_TARGET_GATE_UNAVAILABLE "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
            self.native_combat_log_target_gate = None

        self.native_target_circle_capacity = NativeTargetCircleCapacity(self.process)
        self.native_target_circle_capacity.maintain()

        self.native_p1_target_circle = NativeTargetCircleController(
            self.process,
            owner_tag=P1_TARGET_OWNER_TAG,
            palette=0,
            allowed_palettes=tuple(range(7)),
        )
        self.native_target_circle = NativeTargetCircleController(
            self.process,
            owner_tag=P2_TARGET_OWNER_TAG,
            palette=4,
            allowed_palettes=tuple(range(7)),
        )
        self.native_p3_target_circle = NativeTargetCircleController(
            self.process,
            owner_tag=P3_TARGET_OWNER_TAG,
            palette=2,
            allowed_palettes=tuple(range(7)),
        )
        self.native_p1_area_circle = NativeAreaTargetCircleController(
            self.process,
            owner_tag=P1_AREA_OWNER_TAG,
            palette=0,
        )
        self.native_p2_area_circle = NativeAreaTargetCircleController(
            self.process,
            owner_tag=P2_AREA_OWNER_TAG,
            palette=4,
        )
        self.native_p3_area_circle = NativeAreaTargetCircleController(
            self.process,
            owner_tag=P3_AREA_OWNER_TAG,
            palette=2,
        )
        self.native_area_render_gate = NativeAreaRenderGate(self.process)
        self.native_p1_list_bridge = NativeP1ListInputBridge(self.process, self.base)
        self.native_p1_list_bridge.install()

        self.native_interaction_router = NativeWorldInteractionRouter(
            self.process, self.native_dialog_bridge
        )
        self.native_wait_mode_bridge = NativeWaitModeBridge(self.process, self.base)
        self.native_treasure_log = native_treasure_log.NativeTreasureLog(
            self.process,
            self.base,
        )
        try:
            self.native_treasure_log.install()
            print("COOP_TREASURE_COMBAT_LOG ON", flush=True)
        except (OSError, RuntimeError, ValueError) as error:
            print(
                f"COOP_TREASURE_COMBAT_LOG SKIPPED {type(error).__name__}: {error}",
                flush=True,
            )
            self.native_treasure_log = None
        self.loot_ownership = LootOwnershipBridge(
            self.process,
            self.base + 0x200,
        )
        self.loot_ownership_retry_at = 0.0
        self.loot_ownership_last_error = None
        self.maintain_loot_ownership(time.monotonic())
        self.native_submitter = legacy.NativeActionSubmitter(self.process, self.base)
        self.native_combat_indicator = legacy.NativeCombatIndicatorRouter(
            self.process, self.base
        )
        self.native_action_line_theme_router = NativeActionLineThemeRouter(
            self.process, self.base
        )
        try:
            self.native_action_line_theme_router.install()
            print("NATIVE_ACTION_LINE_THEMES ON", flush=True)
        except (OSError, RuntimeError, ValueError, struct.error) as error:
            print(
                f"NATIVE_ACTION_LINE_THEMES SKIPPED {type(error).__name__}: {error}",
                flush=True,
            )
            self.native_action_line_theme_router = None

        self.pending_combat_indicators: dict[int, PendingCombatIndicator] = {}
        self.observed_native_actions: dict[int, tuple[int, int, int, int]] = {}
        self.observed_native_action_states: dict[int, int] = {}
        self.pending_summon_owner_party_member_id: int | None = None
        self.pending_summon_owner_until = 0.0
        self.summon_owner_party_member_id: int | None = None
        self.summon_was_active = False

        self.role_generation = 0
        self.menu_reopen_allowed_at = 0.0
        self.p2_confirm_reopen_guard = control_bindings.ConfirmReopenGuard(
            MENU_REOPEN_QUIET_SECONDS
        )
        self.p2_character_direction_guard = control_bindings.ConfirmReopenGuard(
            CHARACTER_SELECTOR_EXIT_DELAY
        )
        self.native_sound_player = legacy.NativeUISoundPlayer(self.process, self.base)
        self.pending_native_sounds: deque[int] = deque()
        self.battle_menu_sound_tracker = BattleMenuSoundSettingsTracker()
        self.battle_menu_sounds = self.battle_menu_sound_tracker.settings
        self.help_badge_sequence = 0
        self.help_badge_last_rect: tuple[float, float, float, float] | None = None

        self.world_marker_panel = legacy.LayeredPanel(self.pid, "world_target")
        self.interaction_prompt_panel = legacy.LayeredPanel(
            self.pid, "interaction_prompt_p2"
        )
        self.panel = legacy.LayeredPanel(self.pid, "menu_p2")
        self.help_panel = legacy.LayeredPanel(self.pid, "help_p2")
        self.target_info_backdrop_panel = legacy.LayeredPanel(
            self.pid, "target_info_backdrop"
        )
        self.target_info_panel = legacy.LayeredPanel(self.pid, "target_info_p2")
        self.p1_target_info_panel = legacy.LayeredPanel(self.pid, "target_info_p1")
        self.p1_panel = legacy.LayeredPanel(self.pid, "menu_p1")
        self.p1_help_panel = legacy.LayeredPanel(self.pid, "help_p1")
        self.p3_panel = legacy.LayeredPanel(self.pid, "menu_p3")
        self.p3_help_panel = legacy.LayeredPanel(self.pid, "help_p3")
        self.p3_target_info_panel = legacy.LayeredPanel(self.pid, "target_info_p3")
        self.p3_interaction_prompt_panel = legacy.LayeredPanel(
            self.pid, "interaction_prompt_p3"
        )
        self.p1_party_target_cursor_panel = legacy.LayeredPanel(
            self.pid, "party_target_cursor_p1"
        )
        self.p2_party_target_cursor_panel = legacy.LayeredPanel(
            self.pid, "party_target_cursor_p2"
        )
        self.p3_party_target_cursor_panel = legacy.LayeredPanel(
            self.pid, "party_target_cursor_p3"
        )
        self.live_damage_meter = damage_meter.LiveDamageMeter(
            self.process,
            lambda: legacy.LayeredPanel(self.pid, "damage_meter"),
        )
        self.party_target_cursor_tweens = {
            player: CursorTween() for player in (1, 2, 3)
        }
        self.party_target_cursor_active = {player: False for player in (1, 2, 3)}
        self.party_area_secondary_indices = {player: () for player in (1, 2, 3)}
        self.party_target_cursor_last_tick = time.monotonic()
        self.overlay_panels = (
            self.world_marker_panel,
            self.interaction_prompt_panel,
            self.panel,
            self.help_panel,
            self.target_info_backdrop_panel,
            self.target_info_panel,
            self.p1_target_info_panel,
            self.p1_panel,
            self.p1_help_panel,
            self.p3_panel,
            self.p3_help_panel,
            self.p3_target_info_panel,
            self.p3_interaction_prompt_panel,
            self.p1_party_target_cursor_panel,
            self.p2_party_target_cursor_panel,
            self.p3_party_target_cursor_panel,
        )
        self.keys = legacy.Keys()
        self.bindings = control_bindings.BindingPoller()
        self.session = session_state.SessionTracker()
        self.menu_theme_tracker = menu_themes.ThemeTracker()
        self.menu_theme_signature: tuple[object, ...] | None = None
        self.p1_theme_name = "sapphire"
        self.p2_theme_name = "gold"
        self.p3_theme_name = "gold"
        self.session_snapshot = self.session.snapshot(
            self.process, materialized_party_count=0
        )
        self.materialized_party_count = 0
        self.pending_party_identity: session_state.SessionIdentity | None = None
        self.pending_party_identity_since = 0.0
        self.party_identity_save_retry_at = 0.0
        self.last_session_gate_signature = None
        self.custom_ui_suppressed = False
        self.native_quickening_active = False
        self.presentation_suppressed_published: bool | None = None
        self.p2_manual_control_enabled = True
        self.p3_manual_control_enabled = True

        self.process.write(self.base + 0xD00, b"\x00" * 4)

        self.party: list[tuple[str, int, int]] = []
        self.active_party: list[tuple[int, str, int, int]] = []
        self.actor_topology_signature: tuple[object, ...] | None = None
        self.complete_party_layout = tuple(self.session_snapshot.current_layout)
        self.target_party: list[tuple[str, int, int]] = []
        self.foes: list[tuple[str, int, int]] = []
        self.p1_foes: list[tuple[str, int, int]] = []
        self.p3_foes: list[tuple[str, int, int]] = []
        self.neutrals: list[tuple[str, int, int]] = []
        self.p1_neutrals: list[tuple[str, int, int]] = []
        self.p3_neutrals: list[tuple[str, int, int]] = []
        self.last_actor_refresh = 0.0
        self.last_target_refresh = 0.0
        self.p1_last_target_refresh = 0.0
        self.p3_last_target_refresh = 0.0
        self.native_p1_last_handle = 0
        self.native_p1_last_handle_time = 0.0
        self.native_p2_last_handle = 0
        self.native_p2_last_handle_time = 0.0
        self.native_p3_last_handle = 0
        self.native_p3_last_handle_time = 0.0
        self.p1_world_interaction_last: tuple[int, int, int, int] | None = None
        self.p1_world_interaction_last_time = 0.0
        self.p1_target_info_handle = 0
        self.p1_target_info_started = 0.0
        self.p1_target_info_exit_started = 0.0
        self.p1_target_info_last_target = None
        self.p1_target_info_last_seen = 0.0
        self.p1_target_info_image = None
        self.p1_target_info_key = None
        self.p1_target_hp_animator = world_overlay.TargetHpAnimator()
        self.p1_target_info_layout_x = float(P1_TARGET_INFO_X)
        self.p1_target_info_layout_y_offset = 0.0
        self.p3_target_info_handle = 0
        self.p3_target_info_started = 0.0
        self.p3_target_info_exit_started = 0.0
        self.p3_target_info_last_target = None
        self.p3_target_info_last_seen = 0.0
        self.p3_target_info_image = None
        self.p3_target_info_key = None
        self.p3_target_hp_animator = world_overlay.TargetHpAnimator()
        self.p3_target_info_layout_x = float(P1_TARGET_INFO_X)
        self.p3_target_info_layout_y_offset = 0.0
        self.p3_target_info_presented = False
        self.p3_target_info_last_tick = time.monotonic()
        self.last_runtime_catalog_refresh = 0.0
        self.runtime_catalog_signature: tuple[bytes, bool] | None = None
        self.runtime_catalog_error: str | None = None
        self.action_catalog = NativeActionCatalog()
        self.runtime_catalog = BattlePackRuntimeCatalog(
            self.action_catalog,
            party_member_id=(
                self.session.identity.p2_party_member_id
                if self.session.identity.p2_party_member_id is not None
                else PENELO_PARTY_MEMBER_ID
            ),
        )
        gameplay_catalog = self.runtime_catalog.static
        initial_p1_owner = self.session_snapshot.p1_owner_party_member_id
        self.p1_runtime_catalog = BattlePackRuntimeCatalog(
            self.action_catalog,
            party_member_id=initial_p1_owner,
            gameplay_catalog=gameplay_catalog,
        )
        self.p3_runtime_catalog = BattlePackRuntimeCatalog(
            self.action_catalog,
            party_member_id=(
                self.session.identity.p3_party_member_id
                if self.session.identity.p3_party_member_id is not None
                else PENELO_PARTY_MEMBER_ID
            ),
            gameplay_catalog=gameplay_catalog,
        )

        self.runtime_catalog_identity = self.session.identity
        self.runtime_catalog_p1_owner = initial_p1_owner
        self.model = P2MenuModel(0x100008, make_demo_catalog())
        self.p1_model = P2MenuModel(0x100000, make_demo_catalog())
        self.p3_model = P2MenuModel(0x100010, make_demo_catalog())
        self.p1_mirror = P1NativeMirrorPresenter(self.action_catalog)
        self.p1_runtime_catalog_signature: tuple[bytes, bool] | None = None
        self.p1_runtime_catalog_error: str | None = None
        self.p1_last_runtime_catalog_refresh = 0.0
        self.p3_runtime_catalog_signature: tuple[bytes, bool] | None = None
        self.p3_runtime_catalog_error: str | None = None
        self.p3_last_runtime_catalog_refresh = 0.0
        self.last_runtime_catalog_shared_refresh = 0.0
        self.p1_visual_replacement = True
        self.p1_pause_suspended = False
        self.native_pause_visibility_gate = NativePauseVisibilityGate()

        self.p1_active_lifecycle: tuple[int, int] | None = None
        self.p1_transition_kind: str | None = None
        self.p1_transition_started = 0.0
        self.p1_last_image: Image.Image | None = None
        self.p1_last_help: Image.Image | None = None
        self.p1_help_fade_image: Image.Image | None = None
        self.p1_help_fade_opacity = 0.0
        self.p1_help_layout_y = float(P1_HELP_Y)
        self.p1_last_vertical_offset = 0
        self.p1_last_paint = time.monotonic()
        self.controller = HeadlessFrontendController(
            self.model,
            target_provider=self.target_provider,
            submitter=self.submit_p2_request,
        )
        self.p1_controller = HeadlessFrontendController(
            self.p1_model,
            target_provider=self.target_provider,
            submitter=self.submit_p1_request,
        )
        self.p3_controller = HeadlessFrontendController(
            self.p3_model,
            target_provider=self.target_provider,
            submitter=self.submit_p3_request,
        )
        self.controller.presenter.set_party_member_id(
            self.session_snapshot.identity.p2_party_member_id
        )
        self.p1_controller.presenter.set_party_member_id(initial_p1_owner)
        self.p3_controller.presenter.set_party_member_id(
            self.session_snapshot.identity.p3_party_member_id
        )
        self.menu_raster_warm_pending: set[int] = set()
        self.menu_raster_warm_after = 0.0
        self.menu_raster_warm_last = 0.0

        self._initial_remote_theme_state: list[tuple[int, bytes]] = []
        for offset, size in (
            (P1_GAUGE_THEME_PALETTE_OFFSET, 32),
            (P2_GAUGE_THEME_PALETTE_OFFSET, 32),
            (P3_GAUGE_THEME_PALETTE_OFFSET, 32),
            (P1_HUD_TEXT_THEME_COLOR_OFFSET, 12),
            (P2_HUD_TEXT_THEME_COLOR_OFFSET, 12),
            (P3_HUD_TEXT_THEME_COLOR_OFFSET, 12),
            (P1_MENU_THEME_COLOR_OFFSET, 12),
            (P2_MENU_THEME_COLOR_OFFSET, 12),
            (P3_MENU_THEME_COLOR_OFFSET, 12),
            (P1_DAMAGE_NUMBER_COLOR_OFFSET, 16),
            (P2_DAMAGE_NUMBER_COLOR_OFFSET, 16),
            (P3_DAMAGE_NUMBER_COLOR_OFFSET, 16),
            (RENDER_PRESENTATION_SUPPRESSED_OFFSET, 4),
        ):
            try:
                self._initial_remote_theme_state.append(
                    (self.base + offset, self.process.read(self.base + offset, size))
                )
            except (OSError, RuntimeError, ValueError):
                pass
        self.sync_presenter_themes(force=True)
        self.p1_closing_image: Image.Image | None = None
        self.p1_closing_help: Image.Image | None = None
        self.p1_closing_y_offset = 0
        self.p1_menu_reopen_allowed_at = 0.0

        self.p1_native_menu_open_since: float | None = None
        self.p1_confirm_reopen_guard = control_bindings.ConfirmReopenGuard(
            MENU_REOPEN_QUIET_SECONDS
        )
        self.native_decide_button = NATIVE_DECIDE_BUTTON_CROSS
        self.native_decide_button_refresh_at = 0.0
        self.p1_frontend_deferred_open_until = 0.0
        self.custom_directional_release_guard = DirectionalReleaseGuard()

        self.p1_leader_handoff_sequence = 0
        self.p1_leader_handoff_member_id: int | None = None
        self.p1_leader_handoff_deadline = 0.0
        self.p1_leader_handoff_identity: session_state.SessionIdentity | None = None
        self.live_role_identity_override: session_state.SessionIdentity | None = None
        self.p1_leader_handoff_identity_saved = False
        self.p1_leader_handoff_save_retry_at = 0.0
        self.native_desktop_capture_owner: str | None = None
        self.native_desktop_capture_epoch: tuple[str, int] | None = None
        self.native_desktop_wheel_gate = DesktopWheelGestureGate()
        self.native_desktop_pending_events: deque[tuple[str, int, int]] = deque()
        self.native_desktop_route_signature: tuple[object, ...] | None = None
        self.native_desktop_overflow_reported = 0
        self.dead_leader_menu_gate = DeadLeaderMenuGate()

        self.native_frontend_custom_suspended = False
        self.native_frontend_suspended_since = 0.0
        self.native_frontend_observed_open = False
        self.native_frontend_open_candidate_since: float | None = None
        self.native_frontend_close_candidate_since = 0.0
        self.native_frontend_suspension_source = ""
        self.native_frontend_suspended_battle_info = 0
        self.native_frontend_suspended_role_generation = 0
        self.unexpected_native_menu_cancel_latched = False
        self.unexpected_native_menu_cancel_retry_at = 0.0
        self.native_party_menu_active = False
        self.native_dead_party_recovery_passthrough = False
        self.native_dead_party_recovery_observed_open = False
        self.native_party_menu_request_pending = False
        self.native_party_menu_request_pulsed = False
        self.native_party_menu_request_source = ""
        self.native_party_menu_request_deadline = 0.0

        self.native_party_command_drain_pending = False
        self.refresh_runtime_catalogs(self.session_snapshot, force=True)

        self.transition_kind: str | None = None
        self.transition_started = 0.0
        self.closing_image: Image.Image | None = None
        self.closing_help: Image.Image | None = None
        self.closing_y_offset = 0
        self.last_paint = time.monotonic()
        self.p2_help_layout_y = float(P1_HELP_Y)
        self.p2_help_fade_image: Image.Image | None = None
        self.p2_help_fade_opacity = 0.0
        self.p2_help_presented = False
        self.world_target_owned = False
        self.world_target_saved = 0
        self.world_target_published = 0
        self.target_visual_handle = 0
        self.target_visual_started = 0.0
        self.target_visual_last_good = None
        self.target_visual_last_good_time = 0.0
        self.target_visual_raw_points = None
        self.target_visual_raw_time = 0.0
        self.target_visual_display_points = None
        self.target_visual_switch_from_points = None
        self.target_visual_entry_from_empty = False
        self.target_visual_last_tick = self.last_paint
        self.target_visual_raster = None
        self.target_visual_raster_center = None
        self.target_visual_raster_time = 0.0
        self.target_visual_last_info: Image.Image | None = None
        self.target_visual_info_key = None
        self.target_visual_info_draw_key = None
        self.p2_target_hp_animator = world_overlay.TargetHpAnimator()
        self.p2_target_info_layout_x = float(P2_TARGET_INFO_PRIMARY_X)
        self.p2_target_info_layout_y_offset = 0.0
        self.p2_target_info_presented = False
        self.p2_target_info_last_tick = self.last_paint
        self.target_visual_exit_started = 0.0
        self.target_visual_exit_marker = None
        self.target_visual_exit_info: Image.Image | None = None
        self.target_visual_last_target: world_overlay.LiveTargetInfo | None = None
        self.target_visual_last_target_time = 0.0

        self.interaction_prompts = {
            2: InteractionPromptState(),
            3: InteractionPromptState(),
        }
        self.interaction_transition_gate = InteractionTransitionGate()
        self.frame_world_interactions: WorldInteractions | None = None
        self.frame_interaction_projection = None
        self.p3_transition_kind: str | None = None
        self.p3_transition_started = 0.0
        self.p3_closing_image: Image.Image | None = None
        self.p3_closing_help: Image.Image | None = None
        self.p3_closing_y_offset = 0
        self.p3_menu_reopen_allowed_at = 0.0
        self.p3_confirm_reopen_guard = control_bindings.ConfirmReopenGuard(
            MENU_REOPEN_QUIET_SECONDS
        )
        self.p3_character_direction_guard = control_bindings.ConfirmReopenGuard(
            CHARACTER_SELECTOR_EXIT_DELAY
        )
        self.p3_last_paint = self.last_paint
        self.p3_help_fade_image: Image.Image | None = None
        self.p3_help_fade_opacity = 0.0
        self.p3_help_layout_y = float(P1_HELP_Y)
        self.p3_help_presented = False
        self.info_lane_layout_initialized = False
        self.info_lane_layout_last_tick = self.last_paint
        self.info_help_active_last = (False, False, False)
        self.info_target_active_last = (False, False, False)
        self.info_help_target_suppressed = (False, False, False)
        self.target_info_backdrop_width = 0.0
        self.target_info_backdrop_height = 0.0
        self.target_info_backdrop_last_tick = self.last_paint
        self.target_info_backdrop_draw_key = None

        self.process.write(
            self.base + INPROCESS_RING_P1_HANDLE_OFFSET,
            b"\x00" * 8,
        )
        self.process.write(
            self.base + INPROCESS_RING_P3_HANDLE_OFFSET,
            b"\x00" * 4,
        )
        self.frame_io_retry_started: float | None = None
        self.frame_io_retry_signature: tuple[int | None, str] | None = None
        self.frame_io_retry_last_log: float | None = None
        self.frame_subsystem_failures: dict[str, tuple[str, str]] = {}
        self.running = True

    def _rollback_failed_initialization(self) -> None:
        if getattr(self, "_initialization_rollback_complete", False):
            return
        self._initialization_rollback_complete = True
        failures: list[str] = []

        def attempt(label: str, callback) -> None:
            try:
                callback()
            except BaseException as error:
                description = self._frame_error_description(error)
                failures.append(
                    f"{label}: {type(error).__name__}: {description}"
                )

        def release(name: str, method: str) -> None:
            resource = getattr(self, name, None)
            if resource is None:
                return
            attempt(
                f"{name}.{method}",
                lambda: getattr(resource, method)(),
            )

            setattr(self, name, None)

        release("native_sound_player", "restore")
        release("native_action_line_theme_router", "restore")
        release("native_combat_indicator", "restore")
        release("native_submitter", "restore")
        release("loot_ownership", "restore")
        release("native_treasure_log", "restore")
        release("native_wait_mode_bridge", "restore")
        release("native_interaction_router", "shutdown")
        release("native_p1_list_bridge", "restore")
        native_dialog_bridge = getattr(self, "native_dialog_bridge", None)
        dialog_release_method = "restore"
        if bool(
            native_dialog_bridge is not None
            and getattr(native_dialog_bridge, "installed", False)
        ):
            try:
                shutdown_requested = runtime_shutdown.requested(force=True)
            except BaseException:
                # An unreadable shutdown channel is not authority to reopen
                # native replacement menus while the supervisor may retry.
                shutdown_requested = False
            recovery_expected = bool(
                getattr(self, "coop_runtime_active", False)
                or not getattr(native_dialog_bridge, "owns_static", True)
            )
            if recovery_expected and not shutdown_requested:
                # Fresh and adopted bridges alike remain process-resident while
                # the active co-op supervisor retries this initializer.
                dialog_release_method = "fail_closed_for_recovery"
        release("native_dialog_bridge", dialog_release_method)
        release("native_area_render_gate", "restore")
        release("native_p3_area_circle", "restore")
        release("native_p2_area_circle", "restore")
        release("native_p1_area_circle", "restore")
        release("native_p3_target_circle", "restore")
        release("native_target_circle", "restore")
        release("native_p1_target_circle", "restore")
        release("native_target_circle_capacity", "restore")
        release("native_combat_log_target_gate", "restore")
        release("combat_log_layout", "restore")
        release("native_stock_field_hud", "restore")
        release("live_damage_meter", "close")

        process = getattr(self, "process", None)
        base = getattr(self, "base", None)
        if process is not None:
            for address, payload in reversed(
                getattr(self, "_initial_remote_theme_state", ())
            ):
                attempt(
                    f"theme mailbox {address:#x}",
                    lambda address=address, payload=payload: process.write(
                        address, payload
                    ),
                )
            self._initial_remote_theme_state = []
        if process is not None and base is not None:
            attempt(
                "P1/P2 presentation mailboxes",
                lambda: process.write(
                    base + INPROCESS_RING_P1_HANDLE_OFFSET,
                    b"\x00" * 8,
                ),
            )
            attempt(
                "P3 presentation mailbox",
                lambda: process.write(
                    base + INPROCESS_RING_P3_HANDLE_OFFSET,
                    b"\x00" * 4,
                ),
            )

        for name in reversed(
            (
                "world_marker_panel",
                "interaction_prompt_panel",
                "panel",
                "help_panel",
                "target_info_panel",
                "p1_target_info_panel",
                "p1_panel",
                "p1_help_panel",
                "p3_panel",
                "p3_help_panel",
                "p3_target_info_panel",
                "p3_interaction_prompt_panel",
                "p1_party_target_cursor_panel",
                "p2_party_target_cursor_panel",
                "p3_party_target_cursor_panel",
            )
        ):
            release(name, "close")

        release("process", "close")
        session_mutex = getattr(self, "session_mutex", None)
        if session_mutex:
            attempt(
                "session_mutex.CloseHandle",
                lambda: kernel32.CloseHandle(session_mutex),
            )
            self.session_mutex = None

        for failure in failures:
            try:
                print(
                    f"HEADLESS_MENU_INIT_ROLLBACK_WARNING {failure}",
                    flush=True,
                )
            except BaseException:
                pass

    def private_player_interaction(
        self,
        player: int,
    ) -> tuple[int, int, int, int] | None:
        try:
            pointer_address = (
                legacy.coop.COMMAND_BLOCK + P2_INTERACTION_STATE_POINTER_OFFSET
                if player == 2
                else (
                    legacy.coop.third_player_block()
                    + legacy.coop.P3_INTERACTION_STATE_POINTER_OFFSET
                )
            )
            private = self.process.u64(pointer_address)
            if not private:
                return None
            snapshot = self.process.read(
                private + P2_INTERACTION_SNAPSHOT_OFFSET,
                P2_INTERACTION_SNAPSHOT_SIZE,
            )

            exact_descriptor = snapshot[
                P2_INTERACTION_EXACT_DESCRIPTOR_OFFSET
                - P2_INTERACTION_SNAPSHOT_OFFSET : P2_INTERACTION_EXACT_DESCRIPTOR_OFFSET
                - P2_INTERACTION_SNAPSHOT_OFFSET
                + 24
            ]
            exact_can_use = snapshot[
                P2_INTERACTION_EXACT_CAN_USE_OFFSET - P2_INTERACTION_SNAPSHOT_OFFSET
            ]
            _distance, _registry, _index, exact_state, exact_handle, _flags = (
                struct.unpack("<6I", exact_descriptor)
            )

            if exact_can_use and exact_state >= 2 and exact_handle:
                for slot in range(7, -1, -1):
                    state = struct.unpack_from(
                        "<I",
                        snapshot,
                        P2_INTERACTION_STATE_SLOTS_OFFSET
                        - P2_INTERACTION_SNAPSHOT_OFFSET
                        + slot * 4,
                    )[0]
                    handle = struct.unpack_from(
                        "<I",
                        snapshot,
                        P2_INTERACTION_HANDLE_SLOTS_OFFSET
                        - P2_INTERACTION_SNAPSHOT_OFFSET
                        + slot * 4,
                    )[0]
                    if state < 2 or handle != exact_handle:
                        continue
                    text = struct.unpack_from(
                        "<Q",
                        snapshot,
                        P2_INTERACTION_TEXT_SLOTS_OFFSET
                        - P2_INTERACTION_SNAPSHOT_OFFSET
                        + slot * 8,
                    )[0]
                    return exact_state, exact_handle, text, slot

                if not FOLLOWER_NPC_INTERACTIONS_ENABLED:
                    return None
                try:
                    if native_interaction.resolve_handle(self.process, exact_handle):
                        return exact_state, exact_handle, 0, -1
                except (OSError, RuntimeError, struct.error):
                    pass
            # The publisher retains the last populated pass instead of clearing
            # the snapshot on each pass, so an abandoned publication must be
            # recognized here rather than by searching for zeros. MARK equals TICK
            # on the publishing pass; their difference counts passes since the
            # last find, while moving away only stops MARK from advancing. This
            # applies to aggregate slots; the exact descriptor above has its own
            # can-use gate and is not republished this way.
            published = struct.unpack_from(
                "<I",
                snapshot,
                legacy.coop.DUAL_INTERACTION_PUBLISH_MARK_OFFSET
                - P2_INTERACTION_SNAPSHOT_OFFSET,
            )[0]
            if not published:
                return None
            tick = struct.unpack_from(
                "<I",
                snapshot,
                legacy.coop.DUAL_INTERACTION_PUBLISH_TICK_OFFSET
                - P2_INTERACTION_SNAPSHOT_OFFSET,
            )[0]
            if (tick - published) & 0xFFFFFFFF > INTERACTION_PUBLISH_STALE_PASSES:
                return None

            for slot in range(7, -1, -1):
                state = struct.unpack_from(
                    "<I",
                    snapshot,
                    P2_INTERACTION_STATE_SLOTS_OFFSET
                    - P2_INTERACTION_SNAPSHOT_OFFSET
                    + slot * 4,
                )[0]
                if not state:
                    continue
                handle = struct.unpack_from(
                    "<I",
                    snapshot,
                    P2_INTERACTION_HANDLE_SLOTS_OFFSET
                    - P2_INTERACTION_SNAPSHOT_OFFSET
                    + slot * 4,
                )[0]
                text = struct.unpack_from(
                    "<Q",
                    snapshot,
                    P2_INTERACTION_TEXT_SLOTS_OFFSET
                    - P2_INTERACTION_SNAPSHOT_OFFSET
                    + slot * 8,
                )[0]
                return state, handle, text, slot
        except (OSError, RuntimeError, struct.error, IndexError):
            return None
        return None

    def private_p2_interaction(self) -> tuple[int, int, int, int] | None:
        return self.private_player_interaction(2)

    def private_p3_interaction(self) -> tuple[int, int, int, int] | None:
        return self.private_player_interaction(3)

    def follower_prompt_scan_candidates(self) -> dict[int, tuple[int, int]]:
        candidates: dict[int, tuple[int, int]] = {}
        try:
            if len(self.party) >= 2 and self.party[1][2]:
                candidates[2] = (
                    int(self.party[1][2]),
                    self.process.u32(
                        legacy.coop.COMMAND_BLOCK
                        + legacy.coop.P2_LOOT_HANDLE_OFFSET
                    ),
                )
            if len(self.party) >= 3 and self.party[2][2]:
                block = legacy.coop.third_player_block()
                candidates[3] = (
                    int(self.party[2][2]),
                    self.process.u32(
                        block + legacy.coop.P3_LOOT_HANDLE_OFFSET
                    ),
                )
        except (OSError, RuntimeError, struct.error, IndexError):
            return {}
        return candidates

    def native_p1_interaction(self) -> tuple[int, int, int, int] | None:
        now = time.monotonic()
        try:
            owner = self.process.read(
                legacy.coop.COMMAND_BLOCK + P2_INTERACTION_OWNER_OFFSET, 1
            )[0]
            descriptor = self.process.read(NATIVE_INTERACTION_DESCRIPTOR, 24)
            can_use = self.process.read(NATIVE_INTERACTION_CAN_USE_GATE, 1)[0]
            _distance, _registry, _index, state, handle, _flags = struct.unpack(
                "<6I", descriptor
            )
            _resource, output_text, output_state, output_handle = struct.unpack(
                "<QQII",
                self.process.read(NATIVE_INTERACTION_OUTPUT, 24),
            )
        except (OSError, RuntimeError, struct.error, IndexError):
            return None

        if owner == INTERACTION_OWNER_NONE and can_use and state >= 2 and handle:
            try:
                object_pointer = native_interaction.resolve_handle(self.process, handle)
            except (OSError, RuntimeError, struct.error):
                object_pointer = 0
            if object_pointer:
                text = output_text if output_handle == handle else 0
                interaction = (state, handle, text, -1)
                self.p1_world_interaction_last = interaction
                self.p1_world_interaction_last_time = now
                return interaction

        cached = self.p1_world_interaction_last
        if (
            owner == INTERACTION_OWNER_NONE
            and cached is not None
            and now - self.p1_world_interaction_last_time
            <= P1_WORLD_INTERACTION_DROPOUT_GRACE_SECONDS
        ):
            try:
                if native_interaction.resolve_handle(self.process, cached[1]):
                    return cached
            except (OSError, RuntimeError, struct.error):
                pass

        self.p1_world_interaction_last = None
        self.p1_world_interaction_last_time = 0.0
        if output_state and output_handle:
            return output_state, output_handle, output_text, -1
        return None

    @staticmethod
    def render_interaction_prompt(
        state: int,
        label: str,
        key_label: str = "SPACE",
        *,
        npc_name_only: bool = False,
        key_device: str = "keyboard",
    ) -> Image.Image:
        inner_range = state >= 2
        marker = None
        if not npc_name_only:
            marker = native_interaction_prompt_sprite(inner_range).copy()
            if not inner_range:
                return marker

        round_badge = len(key_label) <= 1
        key_text = renderer.FONT.render(
            key_label,
            scale=P2_INTERACTION_PAD_KEY_SCALE
            if round_badge
            else P2_INTERACTION_KEY_SCALE,
            style=(
                P2_INTERACTION_PAD_TEXT
                if key_device == "xinput" and round_badge
                else P2_INTERACTION_KEY_TEXT
            ),
        )
        action_text = renderer.FONT.render(
            label or "Interact",
            scale=P2_INTERACTION_LABEL_SCALE,
            style=P2_INTERACTION_LABEL_TEXT,
        )
        badge_height = max(
            28 if key_device == "xinput" and round_badge else 34,
            round(
                action_text.height
                * (
                    P2_INTERACTION_PAD_BADGE_RATIO
                    if key_device == "xinput" and round_badge
                    else P2_INTERACTION_BADGE_RATIO
                )
            ),
        )

        keycap = native_prompt_glyphs.prompt_badge(key_device, key_label, badge_height)
        if keycap is None:
            if round_badge:
                key_width = badge_height
            else:
                key_width = max(badge_height + 22, key_text.width + 18)
            keycap = Image.new("RGBA", (key_width + 6, badge_height + 6), (0, 0, 0, 0))
            fill_color = (67, 126, 41, 248)
            outline_color = (205, 255, 162, 255)
            shadow = Image.new("RGBA", keycap.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle(
                (4, 5, key_width + 3, badge_height + 3),
                radius=badge_height // 2,
                fill=(0, 0, 0, 175),
            )
            keycap.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(1.5)))
            ImageDraw.Draw(keycap).rounded_rectangle(
                (2, 2, key_width + 1, badge_height),
                radius=badge_height // 2,
                fill=fill_color,
                outline=outline_color,
                width=2,
            )
            keycap.alpha_composite(
                key_text,
                (
                    2 + (key_width - key_text.width) // 2,
                    2 + (badge_height - key_text.height) // 2,
                ),
            )
        gap = 7
        row_width = keycap.width + gap + action_text.width
        if npc_name_only:
            row_height = max(keycap.height, action_text.height)
            result = Image.new("RGBA", (row_width, row_height), (0, 0, 0, 0))
            result.alpha_composite(keycap, (0, 0))
            result.alpha_composite(
                action_text,
                (
                    keycap.width + gap,
                    (row_height - action_text.height) // 2,
                ),
            )
            return result

        if marker is None:
            raise RuntimeError("interaction marker was not rendered")
        canvas_width = max(marker.width, row_width)
        canvas_height = marker.height + 3 + max(keycap.height, action_text.height)
        result = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        result.alpha_composite(marker, ((canvas_width - marker.width) // 2, 0))
        row_y = marker.height + 3
        row_x = (canvas_width - row_width) // 2
        result.alpha_composite(keycap, (row_x, row_y))
        result.alpha_composite(
            action_text,
            (
                row_x + keycap.width + gap,
                row_y + (keycap.height - action_text.height) // 2,
            ),
        )
        return result

    def p2_world_interaction_allowed(self) -> bool:
        return bool(
            len(self.party) >= 2
            and self.party[1][2]
            and self.follower_world_interaction_allowed()
        )

    def p3_world_interaction_allowed(self) -> bool:
        return bool(
            len(self.party) >= 3
            and self.party[2][2]
            and self.follower_world_interaction_allowed()
        )

    def interaction_is_treasure(self, interaction) -> bool:
        if interaction is None:
            return False
        try:
            return native_interaction.is_treasure_handle(
                self.process,
                interaction[1],
            )
        except (OSError, RuntimeError, struct.error):
            return False

    def leader_allows_interaction_cached(self, now: float) -> bool:
        sampled_at = getattr(self, "leader_interaction_gate_sampled_at", 0.0)
        if now - sampled_at >= LEADER_INTERACTION_GATE_SAMPLE_SECONDS:
            self.leader_interaction_gate_sampled_at = now
            try:
                self.leader_interaction_gate_allows = (
                    self.follower_world_interaction_allowed()
                )
            except (AttributeError, OSError, RuntimeError, struct.error):
                self.leader_interaction_gate_allows = True
        return getattr(self, "leader_interaction_gate_allows", True)

    def follower_world_interaction_allowed(self) -> bool:
        snapshot = self.session_snapshot
        leader_actor = native_leader_actor_for(
            self.active_party,
            snapshot.leader_party_member_id,
        )
        leader_flags = follower_interaction_leader_flags(self.process, leader_actor)
        return bool(
            leader_flags is not None
            and follower_interaction_leader_allows(leader_flags)
        )

    def paint_p2_interaction_prompt(self, foreground: bool) -> None:
        self.paint_coop_interaction_prompt(2, foreground)

    def paint_p3_interaction_prompt(self, foreground: bool) -> None:
        self.paint_coop_interaction_prompt(3, foreground)

    def paint_coop_interaction_prompt(self, player: int, foreground: bool) -> None:
        if player == 2:
            model = self.model
            panel = self.interaction_prompt_panel
            transition = self.transition_kind
            read_interaction = self.private_p2_interaction
            foes = self.foes
        else:
            model = self.p3_model
            panel = self.p3_interaction_prompt_panel
            transition = self.p3_transition_kind
            read_interaction = self.private_p3_interaction
            foes = self.p3_foes
        cache = self.interaction_prompts[player]

        if not foreground:
            cache.key = None
            cache.image = None
            cache.started = 0.0
            cache.missing_since = 0.0
            cache.last_point = None
            cache.last_visible = 0.0
            cache.inner_seen_at = 0.0
            cache.pending_point = None
            cache.pending_point_count = 0
            panel.hide()
            return

        prompt_allowed = (
            model.mode is MenuMode.CLOSED
            and transition is None
            and not (
                self.session_snapshot.native_modal_active
                if hasattr(self, "session_snapshot")
                else self.native_dialog_bridge.modal_active()
            )
        )
        frame_interactions = getattr(self, "frame_world_interactions", None)
        if prompt_allowed and frame_interactions is not None:
            interaction = (
                frame_interactions.p2 if player == 2 else frame_interactions.p3
            )
        else:
            interaction = read_interaction() if prompt_allowed else None
        if interaction_matches_live_foe(interaction, foes):
            cache.key = None
            cache.image = None
            cache.started = 0.0
            cache.missing_since = 0.0
            cache.last_point = None
            panel.hide()
            return
        if interaction is None or len(self.party) < player:
            now = time.monotonic()
            if not cache.missing_since:
                cache.missing_since = now
            # Snapshot copying is not atomic, so a paint can observe an empty read
            # while the object still exists. Retain the last sprite for a short
            # grace period instead of hiding it immediately, which caused flicker.
            #
            # Now that publication persists between passes, an empty inner-range
            # read means the object genuinely disappeared, usually because it was
            # collected. Waiting the full grace period there kept the label and
            # button visible after a chest opened. Outer range retains the grace.
            elif cache.bucket == 2:
                cache.key = None
                cache.image = None
                cache.started = 0.0
                cache.last_point = None
                cache.bucket = 0
                panel.hide()
                return
            elif now - cache.missing_since >= INTERACTION_PROMPT_GRACE_SECONDS:
                cache.key = None
                cache.image = None
                cache.started = 0.0
                cache.last_point = None
                panel.hide()
                return
            if cache.image is None or cache.last_point is None:
                panel.hide()
                return
            self._draw_interaction_prompt(panel, cache, now)
            return

        cache.missing_since = 0.0
        state, handle, text, slot = interaction
        descriptor_only_npc = slot < 0 and not text
        label_source = (handle, text, descriptor_only_npc)
        # The native NPC text table can lag the interaction descriptor by a
        # frame while an area finishes materializing.  Do not make the generic
        # fallback permanent for that handle; retry until the authored name is
        # available, then keep the resolved label cached as usual.
        retry_npc_name = descriptor_only_npc and cache.label == "Interact"
        if cache.label_source == label_source and not retry_npc_name:
            label = cache.label
        elif descriptor_only_npc:
            label = native_interaction_object_label(self.process, handle) or "Interact"
        else:
            label = legacy.decode_ff12_text(self.process, text) or "Save Crystal"
        cache.label_source = label_source
        cache.label = label
        actor = self.party[player - 1][2]
        position = (
            native_interaction_object_position(self.process, handle)
            if descriptor_only_npc
            else None
        )
        now = time.monotonic()
        try:
            if position is None:
                position = struct.unpack(
                    "<3f",
                    self.process.read(actor + 0xD0, 12),
                )
            matrices = getattr(self, "frame_interaction_projection", None)
            if matrices is None:
                view = struct.unpack(
                    "<16f", self.process.read(legacy.coop.CAMERA_VIEW_MATRIX, 64)
                )
                projection = struct.unpack(
                    "<16f", self.process.read(CAMERA_PROJECTION_MATRIX, 64)
                )
                if world_overlay.is_probable_minimap_view(view):
                    raise ValueError("minimap matrix")
                matrices = (view, projection)
                self.frame_interaction_projection = matrices
            view, projection = matrices
            projected = world_overlay.project_actor_point(
                (position[0], position[1] + P2_INTERACTION_HEAD_HEIGHT, position[2]),
                view,
                projection,
            )
            if projected is None:
                raise ValueError("behind camera")
        except (OSError, ValueError, struct.error):
            if cache.last_point is None or now - cache.last_time > 0.20:
                panel.hide()
            return

        point = cache.stabilize_point((projected[0], projected[1]))
        if cache.last_point is not None:
            point = (
                cache.last_point[0] + (point[0] - cache.last_point[0]) * 0.82,
                cache.last_point[1] + (point[1] - cache.last_point[1]) * 0.82,
            )
        cache.last_point = point
        cache.last_time = now

        key_device, key_label = self.bindings.action_prompt_source(
            f"p{player}",
            "confirm",
            swap_face_buttons=(
                getattr(
                    self,
                    "native_decide_button",
                    NATIVE_DECIDE_BUTTON_CROSS,
                )
                == NATIVE_DECIDE_BUTTON_CIRCLE
            ),
        )

        bucket = 1 if state == 1 else 2
        if bucket == 2:
            cache.inner_seen_at = now
        elif (
            cache.inner_seen_at
            and now - cache.inner_seen_at < INTERACTION_PROMPT_INNER_HOLD_SECONDS
        ):
            bucket = 2
        # If the leader is busy, retain only the proximity marker. The button row
        # returns once interaction can be consumed.
        #
        # Apply this after retention: retention smooths distance oscillation but
        # must never restore buttons while the leader is busy.

        if bucket == 2 and not self.leader_allows_interaction_cached(now):
            bucket = 1
        cache.bucket = bucket
        state = bucket

        key = (
            bucket,
            handle,
            label,
            key_label,
            key_device,
            descriptor_only_npc,
        )
        previous_key = cache.key
        if key != cache.key or cache.image is None:
            new_object = previous_key is None or previous_key[1] != handle
            if new_object:
                self.queue_native_sound(NATIVE_INTERACTION_PROXIMITY_SOUND)
            cache.image = self.render_interaction_prompt(
                state,
                label,
                key_label,
                npc_name_only=descriptor_only_npc,
                key_device=key_device,
            )
            cache.key = key

            if new_object:
                cache.started = now
        self._draw_interaction_prompt(panel, cache, now)

    def _draw_interaction_prompt(
        self, panel, cache: "InteractionPromptState", now: float
    ) -> None:
        image = cache.image
        if image is None or cache.last_point is None:
            panel.hide()
            return
        point = cache.last_point

        display = image
        if cache.key is not None and cache.key[0] == 1:
            progress = min(
                1.0,
                max(0.0, now - cache.started) / P2_INTERACTION_POP_SECONDS,
            )
            if progress < 0.58:
                phase = progress / 0.58
                eased = 1.0 - (1.0 - phase) ** 3
                scale = 0.28 + (1.22 - 0.28) * eased
            else:
                phase = (progress - 0.58) / 0.42
                eased = phase * phase * (3.0 - 2.0 * phase)
                scale = 1.22 + (1.0 - 1.22) * eased
            if abs(scale - 1.0) > 0.002:
                display = image.resize(
                    (
                        max(1, round(image.width * scale)),
                        max(1, round(image.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
        cache.last_visible = now
        panel.draw(
            display,
            point[0] - display.width * 0.5,
            point[1] - display.height - 12,
        )

    @staticmethod
    def _actor_topology_signature(snapshot) -> tuple[object, ...]:
        return (
            getattr(snapshot, "identity", None),
            int(getattr(snapshot, "battle_info", 0) or 0),
            tuple(getattr(snapshot, "current_layout", ())),
            tuple(getattr(snapshot, "ui_layout", ())),
        )

    def _actor_topology_sample_regressed(
        self,
        snapshot,
        sampled_active_party: list[tuple[int, str, int, int]],
    ) -> bool:
        """Reject a same-generation sample that only lost known actors."""

        signature = self._actor_topology_signature(snapshot)
        if signature != getattr(self, "actor_topology_signature", None):
            return False
        previous_members = {
            int(member_id)
            for member_id, _name, handle, actor in getattr(
                self,
                "active_party",
                (),
            )
            if handle and actor
        }
        if not previous_members:
            return False
        sampled_members = {
            int(member_id)
            for member_id, _name, handle, actor in sampled_active_party
            if handle and actor
        }
        return not previous_members.issubset(sampled_members)

    def refresh_actors(
        self,
        *,
        force: bool = False,
        snapshot: session_state.SessionSnapshot | None = None,
    ) -> None:
        now = time.monotonic()
        interval = (
            TARGET_LIST_INTERVAL
            if (
                self.model.mode is MenuMode.TARGET
                or self.p1_model.mode is MenuMode.TARGET
                or self.p3_model.mode is MenuMode.TARGET
            )
            else 0.25
        )
        if not force and now - self.last_actor_refresh < interval:
            return
        try:
            active_actors = legacy.coop.resolve_active_actors(self.process)
            party_actor_slots = legacy.coop.resolve_active_party_actor_slots(
                self.process, active_actors
            )
            initial_snapshot = snapshot
            if initial_snapshot is None:
                initial_snapshot = self.session.snapshot(
                    self.process,
                    materialized_party_count=len(party_actor_slots),
                )
            roster_layout = complete_party_layout(
                self.process,
                initial_snapshot.battle_info,
                initial_snapshot.current_layout,
            )
            party_actor_slots = targetable_party_actor_slots(
                self.process,
                party_actor_slots,
                initial_snapshot.current_layout,
            )
            materialized_party_count = len(party_actor_slots)
            snapshot = initial_snapshot
            role_actors = session_state.map_party_slot_actors(
                snapshot, party_actor_slots
            )
            active_party = []
            for party_slot, entry in party_actor_slots.items():
                member_id = snapshot.current_layout[party_slot]
                if member_id == session_state.EMPTY_PARTY_ID:
                    continue
                actor = entry[1]
                handle = self.process.u32(actor + 0x08)
                if handle:
                    active_party.append(
                        (
                            member_id,
                            session_state.PARTY_NAMES.get(
                                member_id, f"Party {member_id}"
                            ),
                            handle,
                            actor,
                        )
                    )
            p1_owner_member_id = snapshot.p1_owner_party_member_id
            p1_entry = role_actors.get(p1_owner_member_id)
            solo_leader_mode = snapshot.p1_owner_override is not None
            p2_entry = (
                None
                if solo_leader_mode
                else role_actors.get(snapshot.identity.p2_party_member_id)
            )
            p3_entry = (
                role_actors.get(snapshot.identity.p3_party_member_id)
                if (
                    not solo_leader_mode
                    and snapshot.identity.p3_party_member_id is not None
                )
                else None
            )
            def role_entry(entry, name):
                if entry is None:
                    return (name, 0, 0)
                actor = entry[1]
                handle = self.process.u32(actor + 0x08)
                return (name, handle, actor) if handle else (name, 0, 0)

            # Player indices are physical seats, not a compact list of active
            # characters. An empty P2 seat must never move P3 into index one.
            party = [
                role_entry(
                    p1_entry,
                    session_state.PARTY_NAMES.get(
                        p1_owner_member_id,
                        f"Party {p1_owner_member_id}",
                    ),
                ),
                role_entry(p2_entry, snapshot.identity.name("p2")),
            ]
            if snapshot.identity.player_count >= 3:
                party.append(role_entry(p3_entry, snapshot.identity.name("p3")))
            if self._actor_topology_sample_regressed(snapshot, active_party):
                # resolve_active_actors() deliberately skips an entry whose
                # individual pointer sample tears.  Without a generation fence
                # that looks exactly like a real party removal, and P3 is then
                # also lost because role ordering requires P2.  Same-generation
                # disappearance is never authoritative; a real edit, load or
                # handoff changes battle/layout/identity and passes this gate.
                return
            p1_actor = p1_entry[1] if p1_entry is not None else 0
            p2_actor = p2_entry[1] if p2_entry is not None else 0
            p3_actor = p3_entry[1] if p3_entry is not None else 0
            target_party = [
                (name, handle, actor)
                for _member_id, name, handle, actor in active_party
            ]
            target_candidates = legacy.live_target_candidates(
                self.process,
                target_party,
                active_actors=active_actors,
            )
            foes = legacy.live_foes(
                self.process,
                target_party,
                issuer_actor=p2_actor,
                candidates=target_candidates,
            )
            p1_foes = legacy.live_foes(
                self.process,
                target_party,
                issuer_actor=p1_actor,
                candidates=target_candidates,
            )
            p3_foes = legacy.live_foes(
                self.process,
                target_party,
                issuer_actor=p3_actor,
                candidates=target_candidates,
            )
            neutrals = legacy.live_neutrals(
                self.process,
                target_party,
                issuer_actor=p2_actor,
                candidates=target_candidates,
            )
            p1_neutrals = legacy.live_neutrals(
                self.process,
                target_party,
                issuer_actor=p1_actor,
                candidates=target_candidates,
            )
            p3_neutrals = legacy.live_neutrals(
                self.process,
                target_party,
                issuer_actor=p3_actor,
                candidates=target_candidates,
            )
        except (OSError, struct.error, ValueError):
            # Actor arrays are rebuilt independently of the session snapshot.
            # A torn generation can therefore fail after a complete P1/P2/P3
            # topology was already committed.  Clearing that topology here
            # turned one bad sample into a synthetic three-player disconnect
            # and could close every menu.  Retain the last coherent transaction
            # and leave ``last_actor_refresh`` unchanged so the next frame
            # retries immediately.  RuntimeError is deliberately not caught:
            # native-layout incompatibilities must remain structural failures.
            return
        self.materialized_party_count = materialized_party_count
        self.session_snapshot = snapshot
        self.party = party
        self.active_party = active_party
        self.complete_party_layout = roster_layout
        self.target_party = target_party
        self.foes = foes
        self.p1_foes = p1_foes
        self.p3_foes = p3_foes
        self.neutrals = neutrals
        self.p1_neutrals = p1_neutrals
        self.p3_neutrals = p3_neutrals
        self.last_actor_refresh = now
        self.actor_topology_signature = self._actor_topology_signature(snapshot)
        if self.party:
            self.p1_model.issuer = self.party[0][1]
        self.p1_model.issuer_party_member_id = int(p1_owner_member_id)
        if len(self.party) >= 2:
            self.model.issuer = self.party[1][1]
        self.model.issuer_party_member_id = (
            int(snapshot.identity.p2_party_member_id)
            if snapshot.identity.p2_party_member_id is not None
            else None
        )
        if len(self.party) >= 3:
            self.p3_model.issuer = self.party[2][1]
        self.p3_model.issuer_party_member_id = (
            int(snapshot.identity.p3_party_member_id)
            if snapshot.identity.p3_party_member_id is not None
            else None
        )

    def refresh_session_state(self) -> session_state.SessionSnapshot:
        coop_active = coop_runtime_enabled(
            self.process,
            fallback=getattr(self, "coop_runtime_active", True),
        )
        if getattr(self, "bootstrap_runtime_active", False):
            if coop_active:
                self.bootstrap_runtime_active = False
            else:
                coop_active = True
        snapshot = self.session.snapshot(
            self.process,
            materialized_party_count=self.materialized_party_count,
        )
        state_now = time.monotonic()
        summon_active, summon_owner_member_id = self.resolve_summon_ownership(
            snapshot.current_layout,
            snapshot.leader_party_member_id,
            now=state_now,
        )
        summon_owner_role = None
        if summon_owner_member_id == snapshot.identity.p1_party_member_id:
            summon_owner_role = "p1"
        elif (
            snapshot.identity.p2_party_member_id is not None
            and summon_owner_member_id == snapshot.identity.p2_party_member_id
        ):
            summon_owner_role = "p2"
        elif (
            snapshot.identity.p3_party_member_id is not None
            and summon_owner_member_id == snapshot.identity.p3_party_member_id
        ):
            summon_owner_role = "p3"
        self.summon_owner_role = summon_owner_role
        if not int(getattr(self, "p1_leader_handoff_sequence", 0)):
            try:
                legacy.coop.acknowledge_native_leader_role_cancellation(
                    self.process
                )
            except (OSError, RuntimeError, struct.error, ValueError):
                pass
        try:
            role_transfer = legacy.coop.read_native_leader_role_transfer(self.process)
            if (
                role_transfer is None
                and legacy.coop.native_leader_role_transfer_recovery_required(
                    self.process
                )
            ):
                role_transfer = legacy.coop.read_native_leader_role_transfer(
                    self.process,
                    include_inactive=True,
                )
        except (OSError, RuntimeError, struct.error, ValueError):
            role_transfer = None
        snapshot = legacy.coop.snapshot_with_native_leader_role_transfer(
            snapshot,
            role_transfer,
        )
        live_override = getattr(self, "live_role_identity_override", None)
        if live_override is not None and snapshot.identity == live_override:
            self.live_role_identity_override = None
        owns_native_presentation = (
            self.native_wait_mode_bridge.owns_native_presentation()
        )
        snapshot = session_state.mask_owned_command_shadow(
            snapshot,
            owned=owns_native_presentation,
        )
        if (
            role_transfer is not None
            and not int(getattr(self, "p1_leader_handoff_sequence", 0))
        ):
            # Recover a shell crash at any transaction word.  Pending requests
            # retain their bounded cancellation window; applied requests keep
            # the in-memory role overlay while persistence retries.
            self.p1_leader_handoff_sequence = role_transfer.sequence
            self.p1_leader_handoff_member_id = role_transfer.target_member_id
            self.p1_leader_handoff_identity = role_transfer.identity
            self.p1_leader_handoff_identity_saved = bool(
                self.session.identity == role_transfer.identity
            )
            self.p1_leader_handoff_save_retry_at = 0.0
            self.p1_leader_handoff_deadline = (
                time.monotonic() + NATIVE_LEADER_HANDOFF_TIMEOUT_SECONDS
            )
            print(
                "P1_LEADER_SELECT_RECOVERED "
                f"sequence={role_transfer.sequence} "
                f"applied={int(role_transfer.native_applied)}",
                flush=True,
            )
        transient_handoff = bool(
            transient_native_leader_handoff_active(self.process)
            or role_transfer is not None
        )
        configured_p1_actor = next(
            (
                actor
                for member_id, _name, _handle, actor in getattr(
                    self, "active_party", ()
                )
                if member_id == snapshot.identity.p1_party_member_id
            ),
            0,
        )
        configured_p1_available = leader_control_availability(
            self.process,
            configured_p1_actor,
        )
        p1_present = bool(
            snapshot.identity.p1_party_member_id in snapshot.current_layout[:4]
            and snapshot.identity.p1_party_member_id in snapshot.ui_layout[:4]
        )
        ko_promoted_role = (
            summon_owner_role
            if summon_owner_role in ("p2", "p3")
            else ko_promoted_role_for_availability(
                snapshot.identity,
                snapshot.leader_party_member_id,
                configured_p1_available,
                p1_present=p1_present,
                previous_role=getattr(self, "ko_promoted_follower_role", None),
            )
        )

        self.ko_promoted_follower_role = ko_promoted_role
        uncertain_follower_leader = identity_reconcile_has_uncertain_follower_leader(
            snapshot.identity,
            snapshot.leader_party_member_id,
            configured_p1_available,
            p1_present=p1_present,
        )
        replacement = (
            None
            if (
                not coop_active
                or summon_active
                or transient_handoff
                or ko_promoted_role is not None
                or uncertain_follower_leader
            )
            else session_state.replacement_identity_for_live_party(
                snapshot,
            )
        )
        now = time.monotonic()
        if replacement is None:
            self.pending_party_identity = None
            self.pending_party_identity_since = 0.0
        else:
            if replacement != self.pending_party_identity:
                self.pending_party_identity = replacement
                self.pending_party_identity_since = now
            settle_seconds = party_identity_reconcile_delay(snapshot, replacement)
            if (
                now >= self.party_identity_save_retry_at
                and now - self.pending_party_identity_since >= settle_seconds
            ):
                old_identity = snapshot.identity

                self.process.write(
                    legacy.coop.COMMAND_BLOCK + 0x04,
                    struct.pack("<f", 0.0),
                )
                try:
                    session_state.save_identity(replacement, self.session.path)
                except OSError as error:
                    self.party_identity_save_retry_at = now + 0.50
                    print(
                        f"COOP_PARTY_RECONCILE_RETRY {type(error).__name__}: {error}",
                        flush=True,
                    )
                    self.session_snapshot = snapshot
                    return snapshot
                self.party_identity_save_retry_at = 0.0
                self.session.reload(force=True)
                self.pending_party_identity = None
                self.pending_party_identity_since = 0.0
                self.last_actor_refresh = 0.0
                self.refresh_actors(force=True)
                snapshot = self.session.snapshot(
                    self.process,
                    materialized_party_count=self.materialized_party_count,
                )
                snapshot = session_state.mask_owned_command_shadow(
                    snapshot,
                    owned=owns_native_presentation,
                )
                print(
                    "COOP_PARTY_RECONCILE "
                    f"old_p1={old_identity.p1_party_member_id} "
                    f"old_p2={old_identity.p2_party_member_id} "
                    f"new_p1={replacement.p1_party_member_id} "
                    f"new_p2={replacement.p2_party_member_id}",
                    flush=True,
                )
        if not coop_active:
            snapshot = session_state.with_solo_leader_ownership(snapshot)
        self.coop_runtime_active = coop_active
        self.session_snapshot = snapshot
        p1_owner_member_id = getattr(
            snapshot,
            "p1_owner_party_member_id",
            snapshot.identity.p1_party_member_id,
        )
        if (
            snapshot.identity != self.runtime_catalog_identity
            or p1_owner_member_id
            != getattr(
                self,
                "runtime_catalog_p1_owner",
                self.runtime_catalog_identity.p1_party_member_id,
            )
        ):
            self.role_generation += 1
            self.hard_close_p1_menu()
            self.hard_close_p2_menu()
            self.hard_close_p3_menu()
            self.install_runtime_catalog_identity(
                snapshot.identity,
                p1_owner_member_id=p1_owner_member_id,
            )
            self.last_actor_refresh = 0.0
        return snapshot

    def install_runtime_catalog_identity(
        self,
        identity: session_state.SessionIdentity,
        *,
        p1_owner_member_id: int | None = None,
    ) -> None:
        if p1_owner_member_id is None:
            p1_owner_member_id = identity.p1_party_member_id

        current_catalog = getattr(self, "runtime_catalog", None)
        gameplay_catalog = (
            current_catalog.static if current_catalog is not None else None
        )
        runtime_catalog = BattlePackRuntimeCatalog(
            self.action_catalog,
            party_member_id=(
                identity.p2_party_member_id
                if identity.p2_party_member_id is not None
                else PENELO_PARTY_MEMBER_ID
            ),
            gameplay_catalog=gameplay_catalog,
        )
        gameplay_catalog = runtime_catalog.static
        p1_runtime_catalog = BattlePackRuntimeCatalog(
            self.action_catalog,
            party_member_id=p1_owner_member_id,
            gameplay_catalog=gameplay_catalog,
        )
        p3_runtime_catalog = BattlePackRuntimeCatalog(
            self.action_catalog,
            party_member_id=(
                identity.p3_party_member_id
                if identity.p3_party_member_id is not None
                else PENELO_PARTY_MEMBER_ID
            ),
            gameplay_catalog=gameplay_catalog,
        )
        (
            self.runtime_catalog,
            self.p1_runtime_catalog,
            self.p3_runtime_catalog,
        ) = (runtime_catalog, p1_runtime_catalog, p3_runtime_catalog)
        self.runtime_catalog_signature = None
        self.p1_runtime_catalog_signature = None
        self.p3_runtime_catalog_signature = None
        self.runtime_catalog_error = None
        self.p1_runtime_catalog_error = None
        self.p3_runtime_catalog_error = None
        self.last_runtime_catalog_refresh = 0.0
        self.p1_last_runtime_catalog_refresh = 0.0
        self.p3_last_runtime_catalog_refresh = 0.0
        self.last_runtime_catalog_shared_refresh = 0.0
        self.runtime_catalog_identity = identity
        self.runtime_catalog_p1_owner = int(p1_owner_member_id)

    def _controlled_member_id(self, player: int) -> int | None:
        identity = (
            getattr(self, "live_role_identity_override", None)
            or self.session_snapshot.identity
        )
        player = int(player)
        if player == 1:
            fallback = (
                identity.p1_party_member_id
                if identity != self.session_snapshot.identity
                else self.session_snapshot.p1_owner_party_member_id
            )
            offset = P1_CONTROLLED_ACTOR_OFFSET
        elif player == 2:
            fallback = identity.p2_party_member_id
            offset = P2_CONTROLLED_ACTOR_OFFSET
        else:
            fallback = identity.p3_party_member_id
            offset = P3_CONTROLLED_ACTOR_OFFSET
        if fallback is None:
            return None
        try:
            actor = self.process.u64(self.base + offset)
        except (OSError, RuntimeError, struct.error, ValueError):
            actor = 0
        cached_member_id = member_id_for_actor(self.active_party, actor, fallback=-1)
        if cached_member_id >= 0:
            return fallback if cached_member_id != fallback else cached_member_id
        live_member_id = member_id_for_actor_slot(
            self.process,
            actor,
            self.session_snapshot.current_layout,
        )
        return fallback if live_member_id != fallback else live_member_id

    def sync_presenter_character_ids(self) -> None:
        p1_controlled_member_id = self._controlled_member_id(1)
        p2_controlled_member_id = self._controlled_member_id(2)
        p3_controlled_member_id = self._controlled_member_id(3)
        p2_member_id = p2_controlled_member_id
        if self.p2_character_selector_active():
            candidates = self.model.valid_candidates()
            if candidates:
                handle = candidates[self.model.target_index % len(candidates)].handle
                selected = next(
                    (
                        member_id
                        for member_id, _name, candidate_handle, _actor in self.active_party
                        if candidate_handle == handle
                    ),
                    None,
                )
                if selected is not None:
                    p2_member_id = selected
        self.controller.presenter.set_party_member_id(p2_member_id)
        self.p1_controller.presenter.set_party_member_id(p1_controlled_member_id)
        self.p3_controller.presenter.set_party_member_id(p3_controlled_member_id)
        self.sync_presenter_themes(
            p1_member_id=p1_controlled_member_id,
            p2_member_id=p2_member_id,
            p3_member_id=p3_controlled_member_id,
            p1_hud_member_id=p1_controlled_member_id,
            p2_hud_member_id=p2_controlled_member_id,
            p3_hud_member_id=p3_controlled_member_id,
        )

    def sync_presenter_themes(
        self,
        *,
        p1_member_id: int | None = None,
        p2_member_id: int | None = None,
        p3_member_id: int | None = None,
        p1_hud_member_id: int | None = None,
        p2_hud_member_id: int | None = None,
        p3_hud_member_id: int | None = None,
        force: bool = False,
    ) -> None:
        selection = self.menu_theme_tracker.reload(force=force)
        if self.native_action_line_theme_router is not None:
            player_by_handle = {
                int(entry[1]) & 0xFFFFFFFF: player
                for player, entry in enumerate(self.party[:3], start=1)
                if entry[1]
            }
            entries = action_line_theme_entries(
                self.active_party,
                selection,
                player_by_handle=player_by_handle,
            )
            if self.native_action_line_theme_router.publish(entries):
                published = ",".join(
                    f"{handle:#x}:{palette}" for handle, palette in entries
                )
                print(
                    f"ACTION_LINE_THEMES entries={published or 'native'}",
                    flush=True,
                )
        if p1_member_id is None:
            p1_member_id = self._controlled_member_id(1)
        if p2_member_id is None:
            p2_member_id = self._controlled_member_id(2)
        if p3_member_id is None:
            p3_member_id = self._controlled_member_id(3)
        if p1_hud_member_id is None:
            p1_hud_member_id = self._controlled_member_id(1)
        if p2_hud_member_id is None:
            p2_hud_member_id = self._controlled_member_id(2)
        if p3_hud_member_id is None:
            p3_hud_member_id = self._controlled_member_id(3)
        p1_theme = selection.for_player(1, p1_member_id)
        p2_theme = character_selector_theme(
            self.p2_theme_name,
            selection.for_player(2, p2_member_id),
            selector_active=self.p2_character_selector_active(),
        )
        p3_theme = character_selector_theme(
            self.p3_theme_name,
            selection.for_player(3, p3_member_id),
            selector_active=self.p3_character_selector_active(),
        )

        p1_hud_theme = selection.for_player(1, p1_hud_member_id)
        p2_hud_theme = selection.for_player(2, p2_hud_member_id)
        p3_hud_theme = selection.for_player(3, p3_hud_member_id)
        signature = (
            p1_member_id,
            p2_member_id,
            p3_member_id,
            p1_theme,
            p2_theme,
            p3_theme,
            p1_hud_theme,
            p2_hud_theme,
            p3_hud_theme,
        )
        if signature == self.menu_theme_signature:
            return
        self.menu_theme_signature = signature
        self.p1_theme_name = p1_theme
        self.p2_theme_name = p2_theme
        self.p3_theme_name = p3_theme
        self.p1_controller.presenter.set_theme_name(p1_theme)
        self.controller.presenter.set_theme_name(p2_theme)
        self.p3_controller.presenter.set_theme_name(p3_theme)
        self.schedule_menu_raster_warmup(1, 2, 3)
        self.process.write(
            self.base + P1_GAUGE_THEME_PALETTE_OFFSET,
            struct.pack("<8i", *menu_themes.gauge_palette(p1_hud_theme)),
        )
        self.process.write(
            self.base + P2_GAUGE_THEME_PALETTE_OFFSET,
            struct.pack("<8i", *menu_themes.gauge_palette(p2_hud_theme)),
        )
        self.process.write(
            self.base + P3_GAUGE_THEME_PALETTE_OFFSET,
            struct.pack("<8i", *menu_themes.gauge_palette(p3_hud_theme)),
        )
        self.process.write(
            self.base + P1_HUD_TEXT_THEME_COLOR_OFFSET,
            struct.pack("<3i", *menu_themes.hud_text_color(p1_hud_theme)),
        )
        self.process.write(
            self.base + P2_HUD_TEXT_THEME_COLOR_OFFSET,
            struct.pack("<3i", *menu_themes.hud_text_color(p2_hud_theme)),
        )
        self.process.write(
            self.base + P3_HUD_TEXT_THEME_COLOR_OFFSET,
            struct.pack("<3i", *menu_themes.hud_text_color(p3_hud_theme)),
        )

        self.process.write(
            self.base + P1_MENU_THEME_COLOR_OFFSET,
            struct.pack("<3i", *menu_themes.hud_text_color(p1_theme)),
        )
        self.process.write(
            self.base + P2_MENU_THEME_COLOR_OFFSET,
            struct.pack("<3i", *menu_themes.hud_text_color(p2_theme)),
        )
        self.process.write(
            self.base + P3_MENU_THEME_COLOR_OFFSET,
            struct.pack("<3i", *menu_themes.hud_text_color(p3_theme)),
        )

        for offset, hud_theme in (
            (P1_DAMAGE_NUMBER_COLOR_OFFSET, p1_hud_theme),
            (P2_DAMAGE_NUMBER_COLOR_OFFSET, p2_hud_theme),
            (P3_DAMAGE_NUMBER_COLOR_OFFSET, p3_hud_theme),
        ):
            self.process.write(
                self.base + offset,
                struct.pack("<4i", *menu_themes.damage_number_style(hud_theme)),
            )
        print(
            "MENU_THEMES "
            f"p1_member={p1_member_id} p1={p1_theme} "
            f"p2_member={p2_member_id} p2={p2_theme} "
            f"p3_member={p3_member_id} p3={p3_theme} "
            f"hud_p1_member={p1_hud_member_id} hud_p1={p1_hud_theme} "
            f"hud_p2_member={p2_hud_member_id} hud_p2={p2_hud_theme} "
            f"hud_p3_member={p3_hud_member_id} hud_p3={p3_hud_theme}",
            flush=True,
        )

    def schedule_menu_raster_warmup(self, *players: int) -> None:
        self.menu_raster_warm_pending.update(
            player for player in players if player in (1, 2, 3)
        )
        self.menu_raster_warm_after = time.monotonic() + MENU_RASTER_WARM_DELAY_SECONDS

    def maintain_menu_raster_warmup(self, snapshot, *, now: float) -> None:
        if (
            not self.menu_raster_warm_pending
            or now < self.menu_raster_warm_after
            or now - self.menu_raster_warm_last < MENU_RASTER_WARM_STEP_SECONDS
            or not snapshot.gameplay_ready
            or snapshot.loading
            or snapshot.cutscene
            or snapshot.native_menu_open
            or snapshot.native_modal_active
            or self.custom_ui_suppressed
            or self.native_frontend_custom_suspended
            or self.dead_leader_menu_gate.pending
            or any(
                model.mode is not MenuMode.CLOSED
                for model in (self.p1_model, self.model, self.p3_model)
            )
            or any(
                transition is not None
                for transition in (
                    self.p1_transition_kind,
                    self.transition_kind,
                    self.p3_transition_kind,
                )
            )
        ):
            return

        player = min(self.menu_raster_warm_pending)
        self.menu_raster_warm_pending.remove(player)
        self.menu_raster_warm_last = now
        if player == 3 and (
            self.session_snapshot.identity.p3_party_member_id is None
            or len(self.party) < 3
        ):
            return
        if player == 2 and (
            self.session_snapshot.identity.p2_party_member_id is None
            or len(self.party) < 2
        ):
            return
        presenter = {
            1: self.p1_controller.presenter,
            2: self.controller.presenter,
            3: self.p3_controller.presenter,
        }[player]
        started = time.perf_counter()
        try:
            warmed = presenter.prewarm_page(
                "items",
                player=player,
                compose_layers=not self.three_player_layout(),
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(
                f"MENU_RASTER_WARM_RETAINED player={player} error={error!r}",
                flush=True,
            )
            return
        if warmed:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            print(
                f"MENU_RASTER_WARM player={player} page=items ms={elapsed_ms:.2f}",
                flush=True,
            )

    def refresh_runtime_catalogs(self, snapshot, *, force: bool = False) -> None:
        p2_ready = bool(
            (force or snapshot.gameplay_ready)
            and snapshot.identity.p2_party_member_id is not None
        )
        p1_ready = bool(force or snapshot.p1_gameplay_ready)
        p3_ready = bool(force or p3_session_menu_allowed(snapshot, len(self.party)))
        if not (p1_ready or p2_ready or p3_ready):
            return

        now = time.monotonic()
        if (
            not force
            and now - self.last_runtime_catalog_shared_refresh
            < RUNTIME_CATALOG_POLL_SECONDS
        ):
            return
        self.last_runtime_catalog_shared_refresh = now
        shared_state = self.runtime_catalog.read_shared_state(self.process)
        if shared_state is None:
            return
        current_layout = tuple(getattr(snapshot, "current_layout", ()))
        leader_party_member_id = getattr(snapshot, "leader_party_member_id", None)
        summon_active, dismiss_owner = self.resolve_summon_ownership(
            current_layout,
            leader_party_member_id,
            now=now,
        )

        if p2_ready:
            self.refresh_runtime_catalog(
                force=force,
                shared_state=shared_state,
                now=now,
                summon_active=(
                    summon_active
                    and self.runtime_catalog.party_member_id == dismiss_owner
                ),
            )
        if p1_ready:
            self.refresh_p1_runtime_catalog(
                force=force,
                shared_state=shared_state,
                now=now,
                summon_active=(
                    summon_active
                    and self.p1_runtime_catalog.party_member_id == dismiss_owner
                ),
            )
        if p3_ready:
            self.refresh_p3_runtime_catalog(
                force=force,
                shared_state=shared_state,
                now=now,
                summon_active=(
                    summon_active
                    and self.p3_runtime_catalog.party_member_id == dismiss_owner
                ),
            )

    def refresh_runtime_catalog(
        self,
        *,
        force: bool = False,
        shared_state: SharedCatalogState | None = None,
        now: float | None = None,
        summon_active: bool = False,
    ) -> None:
        if self.p2_character_selector_active():
            return
        now = time.monotonic() if now is None else now
        if (
            not force
            and now - self.last_runtime_catalog_refresh < RUNTIME_CATALOG_POLL_SECONDS
        ):
            return
        self.last_runtime_catalog_refresh = now
        p2_actor = self.party[1][2] if len(self.party) >= 2 else 0
        state = (
            self.runtime_catalog.read_state(self.process, live_actor=p2_actor)
            if shared_state is None
            else self.runtime_catalog.read_player_state(
                self.process,
                shared_state,
                live_actor=p2_actor,
            )
        )
        if state is None:
            return
        self.model.gambits_enabled = state.gambits_enabled
        signature = (state.signature, summon_active)
        if not force and signature == self.runtime_catalog_signature:
            return
        try:
            pages = self.runtime_catalog.build_pages(
                state,
                summon_active=summon_active,
            )
            result = commit_pages_atomically(self.model, pages)
        except Exception as error:
            message = repr(error)
            if force or message != self.runtime_catalog_error:
                print(
                    f"COOP_CATALOG_RETAINED error={message}",
                    flush=True,
                )
            self.runtime_catalog_error = message
            return

        self.runtime_catalog_signature = signature
        self.runtime_catalog_error = None
        if result.changed:
            self.schedule_menu_raster_warmup(2)
        if result.changed or force:
            root_names = ",".join(entry.name for entry in pages["root"].entries)
            print(
                "COOP_BATTLE_PACK_CATALOG "
                f"pages={len(pages)} changed={int(result.changed)} "
                f"stack={result.retained_stack_depth} "
                f"target_preserved={int(result.target_preserved)} "
                f"root={root_names}",
                flush=True,
            )

    def refresh_p1_runtime_catalog(
        self,
        *,
        force: bool = False,
        shared_state: SharedCatalogState | None = None,
        now: float | None = None,
        summon_active: bool = False,
    ) -> None:
        now = time.monotonic() if now is None else now
        if (
            not force
            and now - self.p1_last_runtime_catalog_refresh
            < RUNTIME_CATALOG_POLL_SECONDS
        ):
            return
        self.p1_last_runtime_catalog_refresh = now
        p1_actor = self.party[0][2] if self.party else 0
        state = (
            self.p1_runtime_catalog.read_state(self.process, live_actor=p1_actor)
            if shared_state is None
            else self.p1_runtime_catalog.read_player_state(
                self.process,
                shared_state,
                live_actor=p1_actor,
            )
        )
        if state is None:
            return
        self.p1_model.gambits_enabled = state.gambits_enabled
        signature = (state.signature, summon_active)
        if not force and signature == self.p1_runtime_catalog_signature:
            return
        try:
            pages = self.p1_runtime_catalog.build_pages(
                state,
                summon_active=summon_active,
            )
            result = commit_pages_atomically(self.p1_model, pages)
        except Exception as error:
            message = repr(error)
            if force or message != self.p1_runtime_catalog_error:
                print(
                    f"P1_CATALOG_RETAINED error={message}",
                    flush=True,
                )
            self.p1_runtime_catalog_error = message
            return
        self.p1_runtime_catalog_signature = signature
        self.p1_runtime_catalog_error = None
        if result.changed:
            self.schedule_menu_raster_warmup(1)

    def refresh_p3_runtime_catalog(
        self,
        *,
        force: bool = False,
        shared_state: SharedCatalogState | None = None,
        now: float | None = None,
        summon_active: bool = False,
    ) -> None:
        if self.p3_character_selector_active():
            return
        now = time.monotonic() if now is None else now
        if (
            not force
            and now - self.p3_last_runtime_catalog_refresh
            < RUNTIME_CATALOG_POLL_SECONDS
        ):
            return
        self.p3_last_runtime_catalog_refresh = now
        p3_actor = self.party[2][2] if len(self.party) >= 3 else 0
        state = (
            self.p3_runtime_catalog.read_state(self.process, live_actor=p3_actor)
            if shared_state is None
            else self.p3_runtime_catalog.read_player_state(
                self.process,
                shared_state,
                live_actor=p3_actor,
            )
        )
        if state is None:
            return
        self.p3_model.gambits_enabled = state.gambits_enabled
        signature = (state.signature, summon_active)
        if not force and signature == self.p3_runtime_catalog_signature:
            return
        try:
            pages = self.p3_runtime_catalog.build_pages(
                state,
                summon_active=summon_active,
            )
            result = commit_pages_atomically(self.p3_model, pages)
        except Exception as error:
            message = repr(error)
            if force or message != self.p3_runtime_catalog_error:
                print(
                    f"P3_CATALOG_RETAINED error={message}",
                    flush=True,
                )
            self.p3_runtime_catalog_error = message
            return
        self.p3_runtime_catalog_signature = signature
        self.p3_runtime_catalog_error = None
        if result.changed:
            self.schedule_menu_raster_warmup(3)

    def _gambit_role_identity(self, role: str) -> dict[str, int]:
        """Sample comparison-only identity for the native toggle mailbox."""

        self.refresh_actors(force=True)
        snapshot = self.session_snapshot
        if snapshot.loading:
            raise RuntimeError(f"{role.upper()} Gambits unavailable while loading")
        member_id = {
            "p1": snapshot.p1_owner_party_member_id,
            "p2": snapshot.identity.p2_party_member_id,
            "p3": snapshot.identity.p3_party_member_id,
        }.get(role)
        if member_id not in session_state.MAIN_PARTY_MEMBER_IDS:
            raise RuntimeError(f"{role.upper()} party member is not currently resolved")

        matches = tuple(
            (int(handle), int(actor))
            for candidate_id, _name, handle, actor in self.active_party
            if int(candidate_id) == int(member_id) and handle and actor
        )
        if len(matches) != 1:
            raise RuntimeError(f"{role.upper()} actor generation is not uniquely resolved")
        handle, actor = matches[0]
        if self.process.u32(actor + 0x08) != handle:
            raise RuntimeError(f"{role.upper()} actor generation changed during sampling")

        physics = legacy.coop.actor_physics_pointer(self.process, actor)
        battle_keep = self.process.u64(actor + 0x698)
        runtime_root = self.process.u64(session_state.BATTLE_INFO_GLOBAL)
        expected_battle_keep = (
            runtime_root
            + legacy.PARTY_MEMBER_RUNTIME_BASE_OFFSET
            + int(member_id) * legacy.PARTY_MEMBER_RUNTIME_STRIDE
            if runtime_root
            else 0
        )
        if not physics:
            raise RuntimeError(f"{role.upper()} physics identity is unavailable")
        if (
            not battle_keep
            or not runtime_root
            or runtime_root != int(snapshot.battle_info)
            or battle_keep != expected_battle_keep
        ):
            raise RuntimeError(f"{role.upper()} battle identity changed during sampling")
        return {
            "member_id": int(member_id),
            "handle": handle,
            "actor": actor,
            "physics": physics,
            "battle_keep": battle_keep,
            "runtime_root": runtime_root,
        }

    def _toggle_role_gambits(self, role: str) -> bool:
        identity = self._gambit_role_identity(role)
        return self.native_submitter.toggle_gambits(**identity)

    def toggle_p2_gambits(self) -> bool:
        return self._toggle_role_gambits("p2")

    def toggle_p1_gambits(self) -> bool:
        return self._toggle_role_gambits("p1")

    def toggle_p3_gambits(self) -> bool:
        return self._toggle_role_gambits("p3")

    def target_provider(self, model: P2MenuModel):
        # A fresh target page must not reuse an actor handle sampled just before
        # a role/party materialization change.  That stale handle made the self
        # lookup miss and fall back to HUD row zero until the menu was reopened.
        self.refresh_actors(force=not model.preserve_candidate_on_refresh)
        candidates: list[TargetCandidate] = []
        character_select = bool(
            model in (self.model, self.p3_model)
            and model.pending_action is not None
            and model.pending_action.action_id == P2_CHANGE_CHARACTER_ACTION_ID
        )
        leader_select = bool(
            model is self.p1_model
            and model.pending_action is not None
            and model.pending_action.action_id == P1_CHANGE_LEADER_ACTION_ID
        )
        if character_select or leader_select:
            p1_owner_member_id = self.session_snapshot.p1_owner_party_member_id
            p1_reserved_member_id = getattr(
                self.session_snapshot,
                "p1_reserved_party_member_id",
                self.session_snapshot.identity.p1_party_member_id,
            )
            for member_id, name, handle, actor in self.active_party:
                selectable = bool(
                    member_id in session_state.MAIN_PARTY_MEMBER_IDS
                    if leader_select
                    else coop_character_role_candidate(
                        member_id,
                        p1_owner_member_id,
                        p1_reserved_member_id,
                    )
                )
                # Both leader and follower role transfers hand manual ownership
                # to this actor.  Reuse the coherent effective command gate so
                # a status race cannot offer Stone/Stop/etc. while Berserk (the
                # explicitly patched exception) remains selectable.
                selectable = bool(
                    selectable
                    and leader_control_availability(self.process, actor) is True
                )
                candidates.append(
                    TargetCandidate(
                        handle,
                        name,
                        TargetGroup.PARTY,
                        selectable=selectable,
                        party_member_id=member_id,
                    )
                )
            return tuple(candidates)

        ordered_party = party_targets_in_hud_order(
            self.target_party,
            self.active_party,
            self.session_snapshot.current_layout,
        )
        pending_rules = None
        if model is self.p1_model:
            model_catalog = self.p1_runtime_catalog
            model_foes = self.p1_foes
            model_neutrals = self.p1_neutrals
        elif model is self.p3_model:
            model_catalog = self.p3_runtime_catalog
            model_foes = self.p3_foes
            model_neutrals = self.p3_neutrals
        else:
            model_catalog = self.runtime_catalog
            model_foes = self.foes
            model_neutrals = self.neutrals
        if model.pending_action is not None:
            try:
                pending_rules = model_catalog.static.rules(
                    model.pending_action.action_id
                )
            except (KeyError, IndexError):
                pending_rules = None
        revive_only = bool(
            model.pending_action is not None and model.pending_action.revive_only
        )
        member_id_by_handle = {
            int(handle): int(member_id)
            for member_id, _name, handle, _actor in self.active_party
        }
        for name, handle, actor in ordered_party:
            selectable = True
            status = 0
            try:
                battle_keep = self.process.u64(actor + 0x698)
                if battle_keep:
                    status = self.process.u32(
                        battle_keep + PERMANENT_STATUS_EFFECTS_OFFSET
                    ) | self.process.u32(battle_keep + TEMPORARY_STATUS_EFFECTS_OFFSET)
            except OSError:
                status = 0
            if pending_rules is not None:
                try:
                    battle_keep = self.process.u64(actor + 0x698)
                    current_hp = (
                        self.process.u32(battle_keep + 0x48) if battle_keep else 1
                    )
                    is_ko = current_hp == 0 or bool(status & 0x01)
                    is_stone = bool(status & 0x02)
                    if revive_only:
                        selectable = is_ko
                    elif is_ko and not pending_rules.possible_ko_party:
                        selectable = False
                    if is_stone and not pending_rules.possible_stone_party:
                        selectable = False
                except OSError:
                    selectable = True
            candidates.append(
                TargetCandidate(
                    handle,
                    name,
                    TargetGroup.PARTY,
                    selectable=selectable,
                    status_mask=status,
                    party_member_id=member_id_by_handle.get(int(handle)),
                )
            )
        candidates.extend(
            reserve_party_candidates(
                self.complete_party_layout,
                {
                    int(member_id)
                    for member_id, _name, _handle, _actor in self.active_party
                },
            )
        )
        for name, handle, _actor in model_foes:
            candidates.append(TargetCandidate(handle, name, TargetGroup.FOES))
        for name, handle, _actor in model_neutrals:
            candidates.append(TargetCandidate(handle, name, TargetGroup.NEUTRALS))
        return tuple(candidates)

    def live_actor_for_handle(
        self,
        handle: int,
        *,
        require_living_foe: bool = True,
    ) -> int:
        handle &= 0xFFFFFFFF
        if not handle:
            return 0
        try:
            manager = self.process.u64(legacy.coop.ACTOR_MANAGER_GLOBAL)
            count = self.process.u32(manager) if manager else 0
            actor_id = handle & 0xFFFF
            if not manager or actor_id >= count or count > 4096:
                return 0
            bwei = self.process.u64(manager + 8 + actor_id * 8)
            actor = self.process.u64(bwei + 0x30) if bwei else 0
            if not actor or self.process.u32(actor + 0x08) != handle:
                return 0
            actor_state = self.process.u64(actor)
            if not (actor_state & legacy.ACTOR_ACTIVE_FLAG):
                return 0
            if actor_state & legacy.ACTOR_HIDDEN_FLAG:
                return 0
            is_party = any(
                party_actor == actor
                for _name, _handle, party_actor in self.target_party
            )
            if require_living_foe and not is_party:
                battle_keep = self.process.u64(actor + 0x698)
                if not battle_keep:
                    return 0
                current_hp = self.process.u32(battle_keep + 0x48)
                status = combined_status_effects(self.process, battle_keep)
                if current_hp == 0 or status & 0x01:
                    return 0
        except OSError:
            return 0
        return actor

    def cancel_stale_combat_indicator(
        self,
        target: int,
        *,
        source: int | None = None,
    ) -> None:
        target &= 0xFFFFFFFF
        requested_source = None if source is None else source & 0xFFFFFFFF
        for owner, pending in tuple(self.pending_combat_indicators.items()):
            if pending.target != target or (
                requested_source is not None and owner != requested_source
            ):
                continue
            print(
                "COOP_NATIVE_INDICATOR_CANCEL_STALE "
                f"issuer={owner:#x} target={target:#x}",
                flush=True,
            )
            self.pending_combat_indicators.pop(owner, None)

    def select_coop_character(self, player: int, target_handle: int) -> int:
        if player not in (2, 3):
            return 0
        identity = self.session_snapshot.identity
        owner_member_id = (
            identity.p2_party_member_id
            if player == 2
            else identity.p3_party_member_id
        )
        owner = next(
            (
                entry
                for entry in self.active_party
                if entry[0] == owner_member_id
            ),
            None,
        )
        if (
            owner is None
            or leader_control_availability(self.process, owner[3]) is not True
        ):
            print(
                f"P{player}_CHARACTER_SELECT_REJECTED owner-unavailable",
                flush=True,
            )
            return 0
        p1_owner_member_id = self.session_snapshot.p1_owner_party_member_id
        p1_reserved_member_id = getattr(
            self.session_snapshot,
            "p1_reserved_party_member_id",
            identity.p1_party_member_id,
        )
        selected = next(
            (
                entry
                for entry in self.active_party
                if entry[2] == (target_handle & 0xFFFFFFFF)
            ),
            None,
        )
        if selected is None:
            print(
                f"P{player}_CHARACTER_SELECT_REJECTED stale-target",
                flush=True,
            )
            return 0
        member_id, name, handle, actor = selected
        if not coop_character_role_candidate(
            member_id,
            p1_owner_member_id,
            p1_reserved_member_id,
        ):
            reason = (
                "p1-role-unstable"
                if member_id in (p1_owner_member_id, p1_reserved_member_id)
                else "guest-or-story-member"
            )
            print(
                f"P{player}_CHARACTER_SELECT_REJECTED {reason}",
                flush=True,
            )
            return 0
        available = leader_control_availability(self.process, actor)
        try:
            busy = bool(self.process.u64(actor) & EXECUTION_ANIMATION_FLAG)
        except (OSError, RuntimeError, TypeError, ValueError):
            return 0
        if available is not True or busy:
            print(
                f"P{player}_CHARACTER_SELECT_REJECTED unavailable",
                flush=True,
            )
            return 0
        old_member_id = (
            identity.p2_party_member_id if player == 2 else identity.p3_party_member_id
        )
        if member_id == old_member_id:
            print(
                f"P{player}_CHARACTER_SELECT unchanged={name}",
                flush=True,
            )
            return 1

        if member_id == p1_owner_member_id:
            # P2/P3 selected the live P1 actor.  Make the follower's previous
            # actor the new native leader; native_leader_role_permutation then
            # assigns the selected former leader to this follower in the same
            # authenticated movement/HUD transaction.
            result = self.select_p1_leader(owner[2])
            if result:
                print(
                    f"P{player}_CHARACTER_EXCHANGE_REQUEST "
                    f"p1={member_id} follower={old_member_id}",
                    flush=True,
                )
            return result

        magnitude_address = (
            legacy.coop.COMMAND_BLOCK + 0x04
            if player == 2
            else (legacy.coop.third_player_block() + legacy.coop.P3_MAGNITUDE_OFFSET)
        )
        self.process.write(magnitude_address, struct.pack("<f", 0.0))
        p2_member_id = identity.p2_party_member_id
        p3_member_id = identity.p3_party_member_id
        if player == 2:
            if p3_member_id == member_id:
                p3_member_id = p2_member_id
            p2_member_id = member_id
        else:
            if p2_member_id == member_id:
                p2_member_id = p3_member_id
            p3_member_id = member_id
        replacement_identity = session_state.SessionIdentity(
            p1_reserved_member_id,
            p2_member_id,
            p3_member_id,
            identity.player_count,
        )
        session_state.save_identity(replacement_identity, self.session.path)
        self.session.reload(force=True)
        self.role_generation += 1

        self.hard_close_p1_menu()
        self.hard_close_p2_menu()
        self.hard_close_p3_menu()
        self.install_runtime_catalog_identity(
            replacement_identity,
            p1_owner_member_id=p1_owner_member_id,
        )
        self.last_actor_refresh = 0.0
        self.pending_combat_indicators.clear()
        self.observed_native_actions.clear()
        self.observed_native_action_states.clear()
        print(
            f"P{player}_CHARACTER_SELECT "
            f"old={old_member_id} new={member_id} "
            f"name={name} handle={handle:#x}",
            flush=True,
        )
        return 1

    def select_p1_leader(self, target_handle: int) -> int:
        selected = next(
            (
                entry
                for entry in self.active_party
                if entry[2] == (int(target_handle) & 0xFFFFFFFF)
            ),
            None,
        )
        if selected is None:
            print("P1_LEADER_SELECT_REJECTED stale-target", flush=True)
            return 0
        member_id, name, handle, actor = selected
        if member_id not in session_state.MAIN_PARTY_MEMBER_IDS:
            print("P1_LEADER_SELECT_REJECTED guest-or-story-member", flush=True)
            return 0
        available = leader_control_availability(self.process, actor)
        try:
            busy = bool(self.process.u64(actor) & EXECUTION_ANIMATION_FLAG)
            native_leader = legacy.coop.native_party_leader(self.process)
        except (OSError, RuntimeError, struct.error, TypeError, ValueError):
            return 0
        if available is not True or busy:
            print("P1_LEADER_SELECT_REJECTED unavailable", flush=True)
            return 0
        if native_leader == member_id:
            print(f"P1_LEADER_SELECT unchanged={name}", flush=True)
            return 1

        pending_sequence = int(getattr(self, "p1_leader_handoff_sequence", 0))
        if pending_sequence:
            if member_id == getattr(self, "p1_leader_handoff_member_id", None):
                return 1
            print("P1_LEADER_SELECT_REJECTED handoff-pending", flush=True)
            return 0

        try:
            snapshot = self.session_snapshot
            sequence, replacement_identity = (
                legacy.coop.request_native_leader_role_transfer(
                self.process,
                member_id,
                    snapshot.identity,
                    snapshot.current_layout,
                    snapshot.ui_layout,
                    manual_control_mask=legacy.coop.read_manual_control_mask(
                        self.process
                    ),
                )
            )
        except legacy.coop.NativeLeaderRoleAdmissionClosed:
            # CLOSED is sampled before request_native... performs any write.
            # Movement is installing/restarting; rejecting this input edge is
            # normal flow and must not terminate the long-lived menu producer.
            print("P1_LEADER_SELECT_REJECTED movement-restarting", flush=True)
            return 0
        except (OSError, RuntimeError, struct.error, ValueError):
            # A remote write can become visible before its caller observes the
            # error.  Clearing mode here could race the game-thread bridge
            # after it has validated the request but before it publishes
            # CLAIMED.  Leave an ambiguous publication intact: refresh/restart
            # recovery will adopt a complete request, while an incomplete one
            # cannot pass the bridge's request/role/PREPARED validation.
            raise
        self.p1_leader_handoff_sequence = int(sequence)
        self.p1_leader_handoff_member_id = int(member_id)
        self.p1_leader_handoff_identity = replacement_identity
        self.p1_leader_handoff_identity_saved = False
        self.p1_leader_handoff_save_retry_at = 0.0
        self.p1_leader_handoff_deadline = (
            time.monotonic() + NATIVE_LEADER_HANDOFF_TIMEOUT_SECONDS
        )
        print(
            "P1_LEADER_SELECT_REQUEST "
            f"member={member_id} name={name} handle={handle:#x} sequence={sequence}",
            flush=True,
        )
        return 1

    def maintain_p1_leader_handoff(self, now: float | None = None) -> None:
        sequence = int(getattr(self, "p1_leader_handoff_sequence", 0))
        if not sequence:
            return
        timestamp = time.monotonic() if now is None else float(now)
        member_id = int(getattr(self, "p1_leader_handoff_member_id", -1))
        try:
            outcome = legacy.coop.native_leader_role_transfer_outcome(
                self.process,
                sequence,
            )
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            struct.error,
            ValueError,
        ):
            outcome = "pending"
        if outcome == "cancelled":
            try:
                cancellation_ack = (
                    legacy.coop.acknowledge_native_leader_role_cancellation(
                        self.process,
                        sequence,
                    )
                )
            except (OSError, RuntimeError, struct.error, ValueError):
                return
            if cancellation_ack != sequence:
                return
            self.p1_leader_handoff_sequence = 0
            self.p1_leader_handoff_member_id = None
            self.p1_leader_handoff_deadline = 0.0
            self.p1_leader_handoff_identity = None
            self.live_role_identity_override = None
            self.p1_leader_handoff_identity_saved = False
            self.p1_leader_handoff_save_retry_at = 0.0
            self.last_actor_refresh = 0.0
            self.role_generation += 1
            print(
                "P1_LEADER_SELECT_CANCELLED "
                f"member={member_id} sequence={sequence}",
                flush=True,
            )
            return

        # APPLIED is written only after the direct setter and the complete
        # movement/HUD publication.  KO/Stone may immediately promote another
        # native leader; that later promotion must not revoke role persistence.
        native_completed = outcome == "applied"
        desired_identity = getattr(self, "p1_leader_handoff_identity", None)
        if native_completed and desired_identity is not None:
            if getattr(self, "live_role_identity_override", None) != desired_identity:
                self.live_role_identity_override = desired_identity
                if hasattr(self, "menu_theme_signature"):
                    self.menu_theme_signature = None
        identity_saved = bool(
            getattr(self, "p1_leader_handoff_identity_saved", False)
        )
        if (
            native_completed
            and not identity_saved
            and desired_identity is not None
            and timestamp
            >= float(getattr(self, "p1_leader_handoff_save_retry_at", 0.0))
        ):
            try:
                session_state.save_identity(desired_identity, self.session.path)
                self.session.reload(force=True)
            except OSError as error:
                # Native leadership and prepared movement are already committed.
                # Persistence is an idempotent retry, never a reason to revoke
                # the live ownership transaction.
                self.p1_leader_handoff_save_retry_at = timestamp + 0.50
                print(
                    "P1_LEADER_IDENTITY_SAVE_RETRY "
                    f"sequence={sequence} error={type(error).__name__}",
                    flush=True,
                )
            else:
                identity_saved = True
                self.p1_leader_handoff_identity_saved = True
                self.p1_leader_handoff_save_retry_at = 0.0
                self.last_actor_refresh = 0.0

        try:
            movement_committed = legacy.coop.native_leader_role_transfer_committed(
                self.process, sequence
            )
        except (OSError, RuntimeError, struct.error, ValueError):
            movement_committed = False
        completed = bool(native_completed and identity_saved and movement_committed)
        # Published requests are never cancelled from this asynchronous
        # thread.  PREPARED can become visible immediately after any sample,
        # and the game thread can then validate it immediately before CLAIMED;
        # therefore no sampled pre-CLAIMED state is a safe rollback boundary.
        # A timeout only changes this transaction into an indefinite retry.
        if not completed:
            return

        try:
            legacy.coop.publish_native_leader_role_transfer_active(
                self.process, False
            )
        except (OSError, RuntimeError, struct.error, ValueError) as error:
            # The clear can land before the process API reports its failure.
            # Local completion is terminal either way: if it did not land,
            # refresh will rediscover the still-active committed transaction
            # and retry; if it did, no stale local sequence remains stuck.
            print(
                "P1_LEADER_SELECT_CLEAR_RETRY "
                f"sequence={sequence} error={type(error).__name__}",
                flush=True,
            )
        finally:
            self.p1_leader_handoff_sequence = 0
            self.p1_leader_handoff_member_id = None
            self.p1_leader_handoff_deadline = 0.0
            self.p1_leader_handoff_identity = None
            self.p1_leader_handoff_identity_saved = False
            self.p1_leader_handoff_save_retry_at = 0.0
            self.last_actor_refresh = 0.0
        self.role_generation += 1
        print(
            f"P1_LEADER_SELECT_APPLIED member={member_id} sequence={sequence}",
            flush=True,
        )

    def cancel_p1_leader_handoff(self, reason: str) -> None:
        sequence = int(getattr(self, "p1_leader_handoff_sequence", 0))
        if not sequence:
            return
        # Cancellation cannot be decided safely from this thread: the bridge
        # may pass validation between our last read and a mode clear.  Keep
        # retrying/recovering the published transaction until the game-thread
        # commit and movement acknowledgement form one terminal state.
        print(
            "P1_LEADER_SELECT_CANCEL_DEFERRED "
            f"sequence={sequence} reason={reason}",
            flush=True,
        )

    def submit_p1_request(self, request) -> int:
        return self.submit_request(request, player=1)

    def submit_p2_request(self, request) -> int:
        return self.submit_request(request, player=2)

    def submit_p3_request(self, request) -> int:
        return self.submit_request(request, player=3)

    def submit_request(self, request, *, player: int | None = None) -> int:
        if player in (1, 2, 3):
            if player == 1:
                role_model = self.p1_model
            elif player == 2:
                role_model = self.model
            else:
                role_model = self.p3_model
            if not request.issuer or request.issuer != role_model.issuer:
                print(
                    f"P{player}_SUBMIT_REJECTED owner-unavailable-or-stale",
                    flush=True,
                )
                return 0
            issuer_actor = next(
                (
                    actor
                    for _name, handle, actor in getattr(self, "party", ())
                    if handle == request.issuer
                ),
                0,
            )
            if (
                leader_control_availability(self.process, issuer_actor) is not True
            ):
                print(
                    f"P{player}_SUBMIT_REJECTED owner-unavailable-or-stale",
                    flush=True,
                )
                return 0
        if request.action_id == P1_CHANGE_LEADER_ACTION_ID:
            if player != 1 or not request.issuer or request.issuer != self.p1_model.issuer:
                return 0
            return self.select_p1_leader(request.target)
        if request.action_id == P2_CHANGE_CHARACTER_ACTION_ID:
            if player not in (2, 3):
                return 0
            role_model = self.model if player == 2 else self.p3_model
            if not request.issuer or request.issuer != role_model.issuer:
                return 0
            return self.select_coop_character(player, request.target)
        requested_action_id = request.action_id
        action_id = requested_action_id
        if action_id in TWO_LINE_ACTION_IDS:
            self.two_line_message_until = time.monotonic() + TWO_LINE_MESSAGE_SECONDS
        reserve_party_member_id = getattr(
            request,
            "reserve_party_member_id",
            None,
        )
        issuer = request.issuer
        target = request.target
        if action_id == DISMISS_ACTION_ID:
            # Stock Dismiss is issued by the live actor in party slot 3: the
            # materialized Esper, not the summoner whose menu shows the command.
            # Resolve that transient handle at confirmation time.
            self.refresh_actors(force=True)
            esper_handle = next(
                (
                    handle
                    for member_id, _name, handle, _actor in self.active_party
                    if 27 <= int(member_id) <= 39
                ),
                0,
            )
            if not esper_handle:
                print("COOP_DISMISS_REJECTED reason=no-live-esper", flush=True)
                return -1
            issuer = esper_handle
            target = esper_handle
        result = self.native_submitter.submit(
            issuer,
            target,
            action_id,
            reserve_party_member_id=reserve_party_member_id,
        )
        if action_id in ESPER_SUMMON_ACTION_IDS and result >= 0:
            self.remember_summon_owner(request.issuer, player=player)
        if reserve_party_member_id is None and action_id != DISMISS_ACTION_ID:
            self.schedule_native_combat_indicator(issuer, target)
        print(
            "COOP_SUBMIT "
            f"issuer={issuer:#x} target={target:#x} "
            f"action={action_id:#x} requested={requested_action_id:#x} "
            f"reserve={reserve_party_member_id!r} result={result}",
            flush=True,
        )
        return result

    def remember_summon_owner(self, issuer: int, *, player: int | None) -> int | None:
        owner = next(
            (
                int(member_id)
                for member_id, _name, handle, _actor in self.active_party
                if int(handle) == int(issuer)
                and member_id in session_state.MAIN_PARTY_MEMBER_IDS
            ),
            None,
        )
        if owner is None:
            snapshot = self.session_snapshot
            if player == 1:
                owner = snapshot.p1_owner_party_member_id
            elif player == 2:
                owner = snapshot.identity.p2_party_member_id
            elif player == 3:
                owner = snapshot.identity.p3_party_member_id
        if owner not in session_state.MAIN_PARTY_MEMBER_IDS:
            return None
        self.pending_summon_owner_party_member_id = int(owner)
        self.pending_summon_owner_until = (
            time.monotonic() + SUMMON_OWNER_PENDING_SECONDS
        )
        print(
            f"COOP_SUMMON_OWNER_PENDING player={player} member={owner} "
            f"issuer={int(issuer):#x}",
            flush=True,
        )
        return int(owner)

    def resolve_summon_ownership(
        self,
        current_layout: tuple[int, ...] | list[int],
        leader_party_member_id: int | None,
        *,
        now: float,
    ) -> tuple[bool, int | None]:
        summon_active = any(
            member_id in session_state.ESPER_PARTY_MEMBER_IDS
            for member_id in current_layout[:4]
        )
        pending_owner = getattr(
            self,
            "pending_summon_owner_party_member_id",
            None,
        )
        pending_until = float(getattr(self, "pending_summon_owner_until", 0.0))
        was_active = bool(getattr(self, "summon_was_active", False))
        active_owner = getattr(self, "summon_owner_party_member_id", None)
        if summon_active:
            pending_valid = bool(
                pending_owner in session_state.MAIN_PARTY_MEMBER_IDS
                and now <= pending_until
            )
            if not was_active:
                self.summon_owner_party_member_id = (
                    pending_owner
                    if pending_valid
                    else summon_dismiss_owner(
                        current_layout,
                        leader_party_member_id,
                    )
                )
                print(
                    "COOP_SUMMON_OWNER_ACTIVE "
                    f"member={self.summon_owner_party_member_id} "
                    f"source={'request' if pending_valid else 'fallback'}",
                    flush=True,
                )
            elif active_owner not in session_state.MAIN_PARTY_MEMBER_IDS:
                self.summon_owner_party_member_id = summon_dismiss_owner(
                    current_layout,
                    leader_party_member_id,
                )
            self.pending_summon_owner_party_member_id = None
            self.pending_summon_owner_until = 0.0
        else:
            if was_active:
                self.summon_owner_party_member_id = None
                self.pending_summon_owner_party_member_id = None
                self.pending_summon_owner_until = 0.0
            elif now > pending_until:
                self.pending_summon_owner_party_member_id = None
                self.pending_summon_owner_until = 0.0
            self.summon_owner_party_member_id = None
        self.summon_was_active = summon_active
        return summon_active, self.summon_owner_party_member_id

    def observe_native_action(self, player: int) -> None:
        if player not in (2, 3):
            return
        model = self.model if player == 2 else self.p3_model
        source = model.issuer
        actor = next(
            (actor for _name, handle, actor in self.party if handle == source),
            0,
        )
        if not actor or not source:
            self.observed_native_actions.pop(player, None)
            self.observed_native_action_states.pop(player, None)
            return
        try:
            action_state = self.process.read(actor + P2_ACTION_STATE_OFFSET, 1)[0]
            target, action, _flags = struct.unpack(
                "<IHH",
                self.process.read(actor + P2_ACTION_TARGET_OFFSET, 8),
            )
        except OSError:
            return

        self.observed_native_action_states[player] = action_state

        if action_state in (0, 1) or not target or action in P2_ACTION_IDLE_VALUES:
            self.observed_native_actions.pop(player, None)
            return
        if not self.live_actor_for_handle(target):
            self.observed_native_actions.pop(player, None)
            self.cancel_stale_combat_indicator(target, source=source)
            return

        signature = (actor, source, target, action)
        if signature == self.observed_native_actions.get(player):
            return
        self.observed_native_actions[player] = signature
        pending = self.pending_combat_indicators.get(source)
        if pending is not None:
            if pending.origin == "menu":
                return
            if pending.target == target:
                return
        print(
            "COOP_NATIVE_ACTION_OBSERVED "
            f"player={player} issuer={source:#x} target={target:#x} "
            f"action={action:#x} "
            f"state={action_state}",
            flush=True,
        )
        self.schedule_native_combat_indicator(source, target, origin="native")

    def observe_p2_native_action(self) -> None:
        self.observe_native_action(2)

    def observe_p3_native_action(self) -> None:
        self.observe_native_action(3)

    def schedule_native_combat_indicator(
        self, source: int, target: int, *, origin: str = "menu"
    ) -> None:
        if not self.live_actor_for_handle(source, require_living_foe=False):
            return
        if not self.live_actor_for_handle(target):
            self.cancel_stale_combat_indicator(target, source=source)
            return

        now = time.monotonic()
        source &= 0xFFFFFFFF
        target &= 0xFFFFFFFF
        self.pending_combat_indicators[source] = PendingCombatIndicator(
            source=source,
            target=target,
            origin=origin,
            node=0,
            next_try=(now if source == target else now + NATIVE_INDICATOR_FIRST_DELAY),
            attempt_started=0.0,
            deadline=now + NATIVE_INDICATOR_PENDING_SECONDS,
            attempts=0,
        )

    def maintain_native_combat_indicator(self) -> None:
        for source in tuple(self.pending_combat_indicators):
            self._maintain_native_combat_indicator(source)

    def maintain_orphaned_ui_tick_dispatcher(self) -> None:
        if self.orphaned_ui_tick_dispatcher is None:
            return
        recovered = legacy.recover_orphaned_ui_tick_dispatcher(
            self.process,
            self.base,
        )
        if recovered is not None and recovered.endswith(":in-flight"):
            return
        if recovered is not None:
            print(
                f"ORPHANED_UI_TICK_RESTORED dispatcher={recovered}",
                flush=True,
            )
        else:
            print(
                "ORPHANED_UI_TICK_RELEASED_EXTERNALLY "
                f"dispatcher={self.orphaned_ui_tick_dispatcher}",
                flush=True,
            )
        self.orphaned_ui_tick_dispatcher = None

    def maintain_owned_ui_tick_dispatchers(self) -> None:
        """Poll locally timed-out dispatchers until native completion.

        Every entry shares the permanent UI-tick mux.  Each ``maintain`` method
        can release only its own terminal mailbox; armed/claimed state and all
        executable roots/callbacks remain untouched.
        """

        dispatchers = (
            getattr(self, "native_submitter", None),
            getattr(self, "native_combat_indicator", None),
            getattr(getattr(self, "native_p1_target_circle", None), "router", None),
            getattr(getattr(self, "native_target_circle", None), "router", None),
            getattr(getattr(self, "native_p3_target_circle", None), "router", None),
            getattr(self, "native_p1_area_circle", None),
            getattr(self, "native_p2_area_circle", None),
            getattr(self, "native_p3_area_circle", None),
        )
        for dispatcher in dispatchers:
            if dispatcher is not None and getattr(dispatcher, "patched", False):
                dispatcher.maintain()

    def _maintain_native_combat_indicator(self, source: int) -> None:
        pending = self.pending_combat_indicators.get(source)
        if pending is None:
            return
        target = pending.target
        if not self.live_actor_for_handle(
            source, require_living_foe=False
        ) or not self.live_actor_for_handle(target):
            self.cancel_stale_combat_indicator(target, source=source)
            return
        now = time.monotonic()
        if now >= pending.deadline:
            print(
                "COOP_NATIVE_INDICATOR_EXPIRED "
                f"issuer={source:#x} target={target:#x} "
                f"attempts={pending.attempts}",
                flush=True,
            )
            self.pending_combat_indicators.pop(source, None)
            return

        node = pending.node
        if node:
            try:
                relation = self.process.read(
                    node + NATIVE_INDICATOR_SOURCE_OFFSET,
                    NATIVE_INDICATOR_STATE_OFFSET - NATIVE_INDICATOR_SOURCE_OFFSET + 2,
                )
                live_source, live_target = struct.unpack_from("<II", relation, 0)
                flags = struct.unpack_from(
                    "<I",
                    relation,
                    NATIVE_INDICATOR_FLAGS_OFFSET - NATIVE_INDICATOR_SOURCE_OFFSET,
                )[0]
                state = struct.unpack_from(
                    "<H",
                    relation,
                    NATIVE_INDICATOR_STATE_OFFSET - NATIVE_INDICATOR_SOURCE_OFFSET,
                )[0]
            except OSError:
                live_source = live_target = flags = state = 0

            if live_source == source and live_target == target:
                if state == 2 and flags & 1:
                    print(
                        "COOP_NATIVE_INDICATOR_ACTIVE "
                        f"issuer={source:#x} target={target:#x} "
                        f"node={node:#x} attempts="
                        f"{pending.attempts}",
                        flush=True,
                    )
                    self.pending_combat_indicators.pop(source, None)
                    return
                if (
                    state == 1
                    and now - pending.attempt_started < NATIVE_INDICATOR_STATE_GRACE
                ):
                    return

            pending.node = 0
            pending.next_try = now + NATIVE_INDICATOR_RETRY_DELAY
            return

        if now < pending.next_try:
            return
        try:
            node = self.native_combat_indicator.set(source, target)
            pending.attempts += 1
            pending.node = node
            pending.attempt_started = time.monotonic()
            print(
                "COOP_NATIVE_INDICATOR_ATTEMPT "
                f"issuer={source:#x} target={target:#x} node={node:#x} "
                f"attempt={pending.attempts}",
                flush=True,
            )
            if source == target:
                self.pending_combat_indicators.pop(source, None)
        except Exception as error:
            pending.next_try = time.monotonic() + NATIVE_INDICATOR_RETRY_DELAY
            print(
                f"COOP_NATIVE_INDICATOR_ERROR {type(error).__name__}: {error}",
                flush=True,
            )

    def begin_opening(self) -> None:
        self.controller.presenter.reset_visual_state()
        self.transition_kind = "opening"
        self.transition_started = time.monotonic()
        self.closing_image = None
        self.closing_help = None
        self.closing_y_offset = 0

    def p1_leader_selector_active(self) -> bool:
        return bool(
            self.p1_model.mode is MenuMode.TARGET
            and self.p1_model.pending_action is not None
            and self.p1_model.pending_action.action_id == P1_CHANGE_LEADER_ACTION_ID
        )

    def open_p1_leader_selector(self) -> None:
        if self.p1_model.mode is not MenuMode.CLOSED:
            return
        try:
            leader_member_id = legacy.coop.native_party_leader(self.process)
        except (OSError, RuntimeError, struct.error, ValueError):
            leader_member_id = self.session_snapshot.leader_party_member_id
        self.refresh_actors(force=True)
        self.p1_model.stack.clear()
        self.p1_model.pending_action = P1_CHANGE_LEADER_ACTION
        self.p1_model.target_group = TargetGroup.PARTY
        self.p1_model.target_index = 0
        self.p1_model.mode = MenuMode.TARGET
        self.p1_controller.refresh_targets()
        candidates = self.p1_model.valid_candidates()
        selected = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if candidate.party_member_id == leader_member_id
            ),
            None,
        )
        if selected is not None:
            self.p1_model.target_index = selected
        self.begin_p1_opening()
        self.queue_native_sound(NATIVE_SOUND_BY_EVENT["opened"], player=1)
        print(
            "P1_LEADER_SELECTOR_OPEN "
            f"candidates={len(self.p1_model.valid_candidates())}",
            flush=True,
        )

    def close_p1_leader_selector(self) -> None:
        if not self.p1_leader_selector_active():
            return
        self.capture_p1_for_close()
        self.p1_model.close()
        self.begin_p1_closing()
        closed_at = time.monotonic()
        self.p1_menu_reopen_allowed_at = closed_at + MENU_REOPEN_QUIET_SECONDS
        self.p1_confirm_reopen_guard.arm(closed_at)
        self.queue_native_sound(NATIVE_SOUND_BY_EVENT["closed"], player=1)
        print("P1_LEADER_SELECTOR_CLOSED", flush=True)

    def p2_character_selector_active(self) -> bool:
        return bool(
            self.model.mode is MenuMode.TARGET
            and self.model.pending_action is not None
            and self.model.pending_action.action_id == P2_CHANGE_CHARACTER_ACTION_ID
        )

    def follower_character_selector_owner_available(self, player: int) -> bool:
        if player not in (2, 3):
            return False
        snapshot = self.session_snapshot
        owner_member_id = (
            snapshot.identity.p2_party_member_id
            if player == 2
            else snapshot.identity.p3_party_member_id
        )
        if owner_member_id is None:
            return False
        model = self.model if player == 2 else self.p3_model
        if getattr(model, "issuer_party_member_id", None) != owner_member_id:
            return False
        owner = next(
            (
                entry
                for entry in self.active_party
                if entry[0] == owner_member_id
                and entry[2] == getattr(model, "issuer", 0)
            ),
            None,
        )
        if owner is None:
            return False
        role_index = player - 1
        if len(self.party) <= role_index:
            return False
        _name, role_handle, role_actor = self.party[role_index]
        if role_handle != owner[2] or role_actor != owner[3]:
            return False
        return leader_control_availability(self.process, role_actor) is True

    def open_p2_character_selector(self) -> None:
        if self.model.mode is not MenuMode.CLOSED:
            return
        self.refresh_actors(force=True)
        if not self.follower_character_selector_owner_available(2):
            now = time.monotonic()
            guard = getattr(self, "p2_character_direction_guard", None)
            if guard is not None:
                guard.arm(now)
            print("P2_CHARACTER_SELECTOR_REJECTED owner-unavailable", flush=True)
            return
        self.model.stack.clear()
        self.model.pending_action = P2_CHANGE_CHARACTER_ACTION
        self.model.target_group = TargetGroup.PARTY
        self.model.target_index = 0
        self.model.mode = MenuMode.TARGET
        self.controller.refresh_targets()
        candidates = self.model.valid_candidates()
        selected = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if candidate.handle == self.model.issuer
            ),
            None,
        )
        if selected is not None:
            self.model.target_index = selected
        self.begin_opening()
        self.queue_native_sound(NATIVE_SOUND_BY_EVENT["opened"], player=2)
        print(
            "P2_CHARACTER_SELECTOR_OPEN "
            f"candidates={len(self.model.valid_candidates())}",
            flush=True,
        )

    def close_p2_character_selector(self) -> None:
        if not self.p2_character_selector_active():
            return
        self.capture_for_close()
        self.model.close()
        self.begin_closing()
        closed_at = time.monotonic()
        self.menu_reopen_allowed_at = closed_at + MENU_REOPEN_QUIET_SECONDS
        for guard_name in (
            "p2_confirm_reopen_guard",
            "p2_character_direction_guard",
        ):
            guard = getattr(self, guard_name, None)
            if guard is not None:
                guard.arm(closed_at)
        self.queue_native_sound(NATIVE_SOUND_BY_EVENT["closed"], player=2)
        print("P2_CHARACTER_SELECTOR_CLOSED", flush=True)

    def p3_character_selector_active(self) -> bool:
        return bool(
            self.p3_model.mode is MenuMode.TARGET
            and self.p3_model.pending_action is not None
            and self.p3_model.pending_action.action_id == P2_CHANGE_CHARACTER_ACTION_ID
        )

    def open_p3_character_selector(self) -> None:
        if self.p3_model.mode is not MenuMode.CLOSED:
            return
        self.refresh_actors(force=True)
        if not self.follower_character_selector_owner_available(3):
            now = time.monotonic()
            guard = getattr(self, "p3_character_direction_guard", None)
            if guard is not None:
                guard.arm(now)
            print("P3_CHARACTER_SELECTOR_REJECTED owner-unavailable", flush=True)
            return
        self.p3_model.stack.clear()
        self.p3_model.pending_action = P2_CHANGE_CHARACTER_ACTION
        self.p3_model.target_group = TargetGroup.PARTY
        self.p3_model.target_index = 0
        self.p3_model.mode = MenuMode.TARGET
        self.p3_controller.refresh_targets()
        candidates = self.p3_model.valid_candidates()
        selected = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if candidate.handle == self.p3_model.issuer
            ),
            None,
        )
        if selected is not None:
            self.p3_model.target_index = selected
        self.begin_p3_opening()
        self.queue_native_sound(NATIVE_SOUND_BY_EVENT["opened"], player=3)
        print(
            "P3_CHARACTER_SELECTOR_OPEN "
            f"candidates={len(self.p3_model.valid_candidates())}",
            flush=True,
        )

    def close_p3_character_selector(self) -> None:
        if not self.p3_character_selector_active():
            return
        self.capture_p3_for_close()
        self.p3_model.close()
        self.begin_p3_closing()
        closed_at = time.monotonic()
        self.p3_menu_reopen_allowed_at = closed_at + MENU_REOPEN_QUIET_SECONDS
        for guard_name in (
            "p3_confirm_reopen_guard",
            "p3_character_direction_guard",
        ):
            guard = getattr(self, guard_name, None)
            if guard is not None:
                guard.arm(closed_at)
        self.queue_native_sound(NATIVE_SOUND_BY_EVENT["closed"], player=3)
        print("P3_CHARACTER_SELECTOR_CLOSED", flush=True)

    def begin_p1_opening(self) -> None:
        self.p1_controller.presenter.reset_visual_state()
        self.p1_transition_kind = "opening"
        self.p1_transition_started = time.monotonic()
        self.p1_closing_image = None
        self.p1_closing_help = None
        self.p1_closing_y_offset = 0

    def begin_p3_opening(self) -> None:
        self.p3_controller.presenter.reset_visual_state()
        self.p3_transition_kind = "opening"
        self.p3_transition_started = time.monotonic()
        self.p3_closing_image = None
        self.p3_closing_help = None
        self.p3_closing_y_offset = 0

    def menu_vertical_offset(self) -> int:
        if self.model.mode is MenuMode.TARGET:
            return renderer.bottom_anchor_offset(
                target=True,
                row_count=model_target_content_rows(self.model),
            )
        if self.model.mode is MenuMode.LIST:
            page = self.model.page
            visible_rows = min(
                len(page.entries),
                self.controller.presenter.visible_row_limit(page.key),
            )
            if page.title:
                visible_rows += 1
            return renderer.bottom_anchor_offset(row_count=visible_rows)
        return 0

    def p1_menu_vertical_offset(self) -> int:
        if self.p1_model.mode is MenuMode.TARGET:
            return renderer.bottom_anchor_offset(
                target=True,
                row_count=model_target_content_rows(self.p1_model),
            )
        if self.p1_model.mode is MenuMode.LIST:
            page = self.p1_model.page
            visible_rows = min(
                len(page.entries),
                self.p1_controller.presenter.visible_row_limit(page.key),
            )
            if page.title:
                visible_rows += 1
            return renderer.bottom_anchor_offset(row_count=visible_rows)
        return 0

    def p3_menu_vertical_offset(self) -> int:
        if self.p3_model.mode is MenuMode.TARGET:
            return renderer.bottom_anchor_offset(
                target=True,
                row_count=model_target_content_rows(self.p3_model),
            )
        if self.p3_model.mode is MenuMode.LIST:
            page = self.p3_model.page
            visible_rows = min(
                len(page.entries),
                self.p3_controller.presenter.visible_row_limit(page.key),
            )
            if page.title:
                visible_rows += 1
            return renderer.bottom_anchor_offset(row_count=visible_rows)
        return 0

    def capture_for_close(self) -> None:
        if not self.model.is_open:
            return
        now = time.monotonic()
        self.closing_y_offset = self.menu_vertical_offset()
        self.closing_image = self.controller.presenter.render(now=now)
        self.closing_help = (
            None
            if self.model.mode is MenuMode.TARGET
            else self.controller.presenter.render_help(player=2)
        )

    def begin_closing(self) -> None:
        self.transition_kind = "closing"
        self.transition_started = time.monotonic()

    def capture_p1_for_close(self) -> None:
        if not self.p1_model.is_open:
            return
        now = time.monotonic()
        self.p1_closing_y_offset = self.p1_menu_vertical_offset()
        self.p1_closing_image = self.p1_controller.presenter.render(now=now, player=1)
        self.p1_closing_help = (
            None
            if self.p1_model.mode is MenuMode.TARGET
            else self.p1_controller.presenter.render_help(player=1)
        )

    def begin_p1_closing(self) -> None:
        self.p1_transition_kind = "closing"
        self.p1_transition_started = time.monotonic()

    def capture_p3_for_close(self) -> None:
        if not self.p3_model.is_open:
            return
        now = time.monotonic()
        self.p3_closing_y_offset = self.p3_menu_vertical_offset()
        self.p3_closing_image = self.p3_controller.presenter.render(now=now, player=3)
        self.p3_closing_help = (
            None
            if self.p3_model.mode is MenuMode.TARGET
            else self.p3_controller.presenter.render_help(player=3)
        )

    def begin_p3_closing(self) -> None:
        self.p3_transition_kind = "closing"
        self.p3_transition_started = time.monotonic()

    def publish_world_target(self, handle: int) -> None:
        handle &= 0xFFFFFFFF
        if not self.world_target_owned:
            self.world_target_saved = self.process.u32(legacy.P2_WORLD_TARGET_FOCUS)
            self.world_target_owned = True

        current = self.process.u32(legacy.P2_WORLD_TARGET_FOCUS)
        if handle != self.world_target_published or current != handle:
            self.process.write(legacy.P2_WORLD_TARGET_FOCUS, struct.pack("<I", handle))
            self.world_target_published = handle

    def clear_world_target(self) -> None:
        if not self.world_target_owned:
            return
        current = self.process.u32(legacy.P2_WORLD_TARGET_FOCUS)
        if current == self.world_target_published:
            self.process.write(
                legacy.P2_WORLD_TARGET_FOCUS,
                struct.pack("<I", self.world_target_saved),
            )
        self.world_target_owned = False
        self.world_target_saved = 0
        self.world_target_published = 0

    def clear_d3d_target_rings(self) -> None:
        self.process.write(
            self.base + INPROCESS_RING_P1_HANDLE_OFFSET,
            b"\x00" * 8,
        )
        self.process.write(
            self.base + INPROCESS_RING_P3_HANDLE_OFFSET,
            b"\x00" * 4,
        )

    def sync_world_target(self) -> None:
        now = time.monotonic()
        if (
            self.p1_model.mode is MenuMode.TARGET
            and now - self.p1_last_target_refresh >= 1.0 / 60.0
        ):
            self.p1_controller.refresh_targets()
            self.p1_last_target_refresh = now
        if (
            self.model.mode is MenuMode.TARGET
            and now - self.last_target_refresh >= 1.0 / 60.0
        ):
            self.controller.refresh_targets()
            self.last_target_refresh = now
        if (
            self.p3_model.mode is MenuMode.TARGET
            and now - self.p3_last_target_refresh >= 1.0 / 60.0
        ):
            self.p3_controller.refresh_targets()
            self.p3_last_target_refresh = now

        def selected_handle(model: P2MenuModel) -> int:
            if model.mode is not MenuMode.TARGET:
                return 0
            candidates = model.valid_candidates()
            if not candidates:
                return 0
            candidate = candidates[model.target_index % len(candidates)]

            if not self.live_actor_for_handle(
                candidate.handle,
                require_living_foe=candidate.group is not TargetGroup.PARTY,
            ):
                self.cancel_stale_combat_indicator(candidate.handle)
                return 0
            return candidate.handle

        p1_handle = selected_handle(self.p1_model)
        p2_handle = selected_handle(self.model)
        p3_handle = selected_handle(self.p3_model)

        def retain_live_dropout(
            handle: int,
            model: P2MenuModel,
            handle_attribute: str,
            time_attribute: str,
        ) -> int:
            if model.target_group is TargetGroup.RESERVE:
                setattr(self, handle_attribute, 0)
                setattr(self, time_attribute, 0.0)
                return 0
            if handle:
                setattr(self, handle_attribute, handle)
                setattr(self, time_attribute, now)
                return handle
            previous = int(getattr(self, handle_attribute))
            previous_time = float(getattr(self, time_attribute))
            if (
                model.mode is MenuMode.TARGET
                and previous
                and now - previous_time <= TARGET_MARKER_DROPOUT_GRACE_SECONDS
                and self.live_actor_for_handle(
                    previous,
                    require_living_foe=model.target_group is not TargetGroup.PARTY,
                )
            ):
                return previous
            setattr(self, handle_attribute, 0)
            setattr(self, time_attribute, 0.0)
            return 0

        p1_handle = retain_live_dropout(
            p1_handle,
            self.p1_model,
            "native_p1_last_handle",
            "native_p1_last_handle_time",
        )
        p2_handle = retain_live_dropout(
            p2_handle,
            self.model,
            "native_p2_last_handle",
            "native_p2_last_handle_time",
        )
        p3_handle = retain_live_dropout(
            p3_handle,
            self.p3_model,
            "native_p3_last_handle",
            "native_p3_last_handle_time",
        )

        p1_palette = menu_themes.target_circle_palette(self.p1_theme_name)
        p2_palette = menu_themes.target_circle_palette(self.p2_theme_name)
        p3_palette = menu_themes.target_circle_palette(self.p3_theme_name)
        self.native_p1_target_circle.sync(p1_handle, palette=p1_palette)
        self.native_target_circle.sync(p2_handle, palette=p2_palette)
        self.native_p3_target_circle.sync(p3_handle, palette=p3_palette)

        def selected_area_radius(model: P2MenuModel) -> float:
            action = model.pending_action if model.mode is MenuMode.TARGET else None
            return max(0.0, float(action.area_radius)) if action is not None else 0.0

        def collision_radius(handle: int) -> float | None:
            actor = self.live_actor_for_handle(handle) if handle else 0
            if not actor:
                return None
            try:
                root = self.process.u64(actor + 0x10)
                phys = self.process.u64(root + 0xB8) if root else 0
                if phys:
                    return struct.unpack("<f", self.process.read(phys + 0xC4, 4))[0]
            except (OSError, RuntimeError, struct.error):
                pass
            return None

        p1_radius = selected_area_radius(self.p1_model)
        p2_radius = selected_area_radius(self.model)
        p3_radius = selected_area_radius(self.p3_model)
        self.native_p1_area_circle.sync(
            p1_handle,
            p1_radius,
            palette=p1_palette,
            collision_radius=collision_radius(p1_handle),
        )
        self.native_p2_area_circle.sync(
            p2_handle,
            p2_radius,
            palette=p2_palette,
            collision_radius=collision_radius(p2_handle),
        )
        self.native_p3_area_circle.sync(
            p3_handle,
            p3_radius,
            palette=p3_palette,
            collision_radius=collision_radius(p3_handle),
        )
        self.native_area_render_gate.sync(
            bool(
                (p1_handle and p1_radius > 0.0)
                or (p2_handle and p2_radius > 0.0)
                or (p3_handle and p3_radius > 0.0)
            )
        )

        if not p2_handle:
            self.clear_world_target()
            return
        self.publish_world_target(p2_handle)

    def actor_level(self, actor: int) -> int:
        """Nivel cru, sem passar pela revelacao de Libra."""

        if not actor:
            return 0
        try:
            bkbi = self.process.u64(actor + 0x698)
            if not bkbi:
                return 0
            return self.process.read(bkbi + BATTLE_KEEP_LEVEL_OFFSET, 1)[0]
        except (OSError, RuntimeError, struct.error):
            return 0

    def actor_has_libra(self, actor: int) -> bool:
        if not actor:
            return False
        try:
            bkbi = self.process.u64(actor + 0x698)
            if not bkbi:
                return False
            temporary, permanent = status_effect_words(self.process, bkbi)
            return bool((temporary | permanent) & LIBRA_STATUS_MASK)
        except (OSError, RuntimeError, struct.error):
            return False

    def actor_has_anti_libra(self, actor: int) -> bool:
        if not actor:
            return False
        try:
            bkbi = self.process.u64(actor + 0x698)
            if not bkbi:
                return False
            temporary = self.process.read(
                bkbi + TEMPORARY_AUGMENTS_OFFSET + ANTI_LIBRA_AUGMENT_BYTE_INDEX,
                1,
            )[0]
            permanent = self.process.read(
                bkbi + PERMANENT_AUGMENTS_OFFSET + ANTI_LIBRA_AUGMENT_BYTE_INDEX,
                1,
            )[0]
            return bool((temporary | permanent) & ANTI_LIBRA_AUGMENT_MASK)
        except (OSError, RuntimeError, struct.error):
            return False

    def actor_has_flying_info(self, actor: int) -> bool:
        if not actor:
            return False
        try:
            class_record = self.process.u64(actor + ACTOR_CLASS_RECORD_OFFSET)
            if not class_record:
                return False
            has_info = self.process.read(
                class_record + CLASS_HAS_FLYING_INFO_OFFSET, 1
            )[0]
            movement = self.process.read(class_record + CLASS_MOVEMENT_FLAGS_OFFSET, 1)[
                0
            ]
            return bool(
                has_info & CLASS_HAS_FLYING_INFO_MASK
                and movement & CLASS_IS_FLYING_MASK
            )
        except (OSError, RuntimeError, struct.error):
            return False

    def selected_live_target(
        self,
        model: P2MenuModel | None = None,
        owner_actor: int = 0,
    ) -> world_overlay.LiveTargetInfo | None:
        model = model or self.model

        if model.mode is not MenuMode.TARGET:
            return None
        candidates = model.valid_candidates()
        if not candidates:
            return None
        candidate = candidates[model.target_index % len(candidates)]
        actor = self.live_actor_for_handle(
            candidate.handle,
            require_living_foe=candidate.group is not TargetGroup.PARTY,
        )
        if not actor:
            return None
        reveal_stats = candidate.group is not TargetGroup.FOES
        if not reveal_stats:
            for _member_id, _name, _handle, party_actor in self.active_party:
                if self.actor_has_libra(party_actor):
                    reveal_stats = True
                    break
        anti_libra = False
        try:
            position = struct.unpack("<3f", self.process.read(actor + 0xD0, 12))
            collision_height = 1.79
            collision_radius = 0.5
            root = self.process.u64(actor + 0x10)
            phys = self.process.u64(root + 0xB8) if root else 0
            if phys:
                collision_height, collision_radius = struct.unpack(
                    "<2f", self.process.read(phys + 0xC0, 8)
                )
            bkbi = self.process.u64(actor + 0x698)
            if not bkbi:
                return None
            level = self.process.read(bkbi + BATTLE_KEEP_LEVEL_OFFSET, 1)[0]
            maximum = self.process.u32(bkbi + 0x24)
            current = self.process.u32(bkbi + 0x48)
            if candidate.group is TargetGroup.FOES:
                anti_libra = self.actor_has_anti_libra(actor)
                if anti_libra:
                    reveal_stats = False
        except OSError:
            return None
        if not maximum or maximum > 99_999_999 or current > 99_999_999:
            return None

        flying = False
        weak_mask = 0
        status_mask = 0
        try:
            flying = self.actor_has_flying_info(actor)
            battle_keep = self.process.u64(actor + 0x698)
            if battle_keep:
                weak_mask = self.process.read(battle_keep + 0x40, 1)[0]
                temporary, permanent = status_effect_words(self.process, battle_keep)
                status_mask = temporary | permanent
        except OSError:
            pass
        return world_overlay.LiveTargetInfo(
            name=candidate.name,
            actor=actor,
            handle=candidate.handle,
            position=position,
            level=level,
            current_hp=current,
            max_hp=maximum,
            collision_height=collision_height,
            collision_radius=collision_radius,
            reveal_stats=reveal_stats,
            flying=flying,
            anti_libra=anti_libra,
            weak_mask=weak_mask,
            status_mask=status_mask,
            viewer_has_libra=self.actor_has_libra(owner_actor),
            viewer_level=self.actor_level(owner_actor),
            hostile=candidate.group is not TargetGroup.PARTY,
        )

    def paint_p1_target_info(self, foreground: bool) -> None:
        now = time.monotonic()
        target = None
        if (
            foreground
            and self.p1_visual_replacement
            and self.p1_model.mode is MenuMode.TARGET
        ):
            target = self.selected_live_target(
                self.p1_model,
                owner_actor=self.party[0][2] if self.party else 0,
            )
        if target is not None:
            self.p1_target_info_last_target = target
            self.p1_target_info_last_seen = now
        elif (
            foreground
            and self.p1_visual_replacement
            and self.p1_model.mode is MenuMode.TARGET
            and self.p1_target_info_last_target is not None
            and now - self.p1_target_info_last_seen
            <= TARGET_SAMPLE_DROPOUT_GRACE_SECONDS
        ):
            target = self.p1_target_info_last_target

        if target is not None:
            target = self.p1_target_hp_animator.present(target, now)
            if target.handle != self.p1_target_info_handle:
                self.p1_target_info_handle = target.handle
                self.p1_target_info_started = now
                self.p1_target_info_exit_started = 0.0
            key = (
                target.handle,
                target.name,
                target.level,
                target.current_hp,
                target.max_hp,
                target.reveal_stats,
                target.flying,
                target.weak_mask,
                target.status_mask,
                target.viewer_has_libra,
                self.p1_theme_name,
            )
            if key != self.p1_target_info_key:
                self.p1_target_info_image = world_overlay.render_target_info(
                    target,
                    player=1,
                    theme_name=self.p1_theme_name,
                )
                self.p1_target_info_key = key
            progress = min(
                1.0,
                max(0.0, now - self.p1_target_info_started) / TARGET_ENTER_SECONDS,
            )
            opacity = progress * progress * (3.0 - 2.0 * progress)
            if self.p1_target_info_image is not None:
                self.draw_custom_panel(
                    self.p1_target_info_panel,
                    self.p1_target_info_image,
                    int(round(self.p1_target_info_layout_x)),
                    self.target_info_y(
                        P1_TARGET_INFO_Y + self.p1_target_info_layout_y_offset
                    ),
                    opacity=opacity,
                )
            return

        if self.p1_target_info_handle and not self.p1_target_info_exit_started:
            self.p1_target_info_exit_started = now
        if self.p1_target_info_exit_started and self.p1_target_info_image is not None:
            progress = min(
                1.0,
                (now - self.p1_target_info_exit_started) / TARGET_EXIT_SECONDS,
            )
            eased = progress * progress * (3.0 - 2.0 * progress)
            if progress < 1.0:
                self.draw_custom_panel(
                    self.p1_target_info_panel,
                    self.p1_target_info_image,
                    int(round(self.p1_target_info_layout_x)),
                    self.target_info_y(
                        P1_TARGET_INFO_Y + self.p1_target_info_layout_y_offset
                    ),
                    opacity=1.0 - eased,
                )
                return

        self.p1_target_info_panel.hide()
        self.p1_target_info_handle = 0
        self.p1_target_info_started = 0.0
        self.p1_target_info_exit_started = 0.0
        self.p1_target_info_last_target = None
        self.p1_target_info_last_seen = 0.0
        self.p1_target_info_image = None
        self.p1_target_info_key = None
        self.p1_target_hp_animator.clear()

    def clear_p1_target_visual_immediately(self) -> None:
        self.native_p1_last_handle = 0
        self.native_p1_last_handle_time = 0.0
        self.p1_target_info_panel.hide()
        self.p1_target_info_handle = 0
        self.p1_target_info_started = 0.0
        self.p1_target_info_exit_started = 0.0
        self.p1_target_info_last_target = None
        self.p1_target_info_last_seen = 0.0
        self.p1_target_info_image = None
        self.p1_target_info_key = None
        self.p1_target_hp_animator.clear()
        self.native_p1_target_circle.clear()
        self.native_p1_area_circle.clear()
        self.clear_d3d_target_rings()

    def paint_p3_target_info(self, foreground: bool) -> None:
        now = time.monotonic()
        self.p3_target_info_last_tick = now
        target = None
        if foreground and self.p3_model.mode is MenuMode.TARGET:
            target = self.selected_live_target(
                self.p3_model,
                owner_actor=self.party[2][2] if len(self.party) >= 3 else 0,
            )
        if target is not None:
            self.p3_target_info_last_target = target
            self.p3_target_info_last_seen = now
        elif (
            foreground
            and self.p3_model.mode is MenuMode.TARGET
            and self.p3_target_info_last_target is not None
            and now - self.p3_target_info_last_seen
            <= TARGET_SAMPLE_DROPOUT_GRACE_SECONDS
        ):
            target = self.p3_target_info_last_target

        if target is not None:
            target = self.p3_target_hp_animator.present(target, now)
            self.p3_target_info_presented = True
            if target.handle != self.p3_target_info_handle:
                self.p3_target_info_handle = target.handle
                self.p3_target_info_started = now
                self.p3_target_info_exit_started = 0.0
            key = (
                target.handle,
                target.name,
                target.level,
                target.current_hp,
                target.max_hp,
                target.reveal_stats,
                target.flying,
                target.weak_mask,
                target.status_mask,
                target.viewer_has_libra,
                self.p3_theme_name,
            )
            if key != self.p3_target_info_key:
                self.p3_target_info_image = world_overlay.render_target_info(
                    target,
                    player=3,
                    theme_name=self.p3_theme_name,
                )
                self.p3_target_info_key = key
            progress = min(
                1.0,
                max(0.0, now - self.p3_target_info_started) / TARGET_ENTER_SECONDS,
            )
            opacity = progress * progress * (3.0 - 2.0 * progress)
            if self.p3_target_info_image is not None:
                self.draw_custom_panel(
                    self.p3_target_info_panel,
                    self.p3_target_info_image,
                    int(round(self.p3_target_info_layout_x)),
                    self.target_info_y(
                        P3_TARGET_INFO_Y + self.p3_target_info_layout_y_offset
                    ),
                    opacity=opacity,
                )
            return

        if self.p3_target_info_handle and not self.p3_target_info_exit_started:
            self.p3_target_info_exit_started = now
        if self.p3_target_info_exit_started and self.p3_target_info_image is not None:
            progress = min(
                1.0,
                (now - self.p3_target_info_exit_started) / TARGET_EXIT_SECONDS,
            )
            eased = progress * progress * (3.0 - 2.0 * progress)
            if progress < 1.0:
                self.draw_custom_panel(
                    self.p3_target_info_panel,
                    self.p3_target_info_image,
                    int(round(self.p3_target_info_layout_x)),
                    self.target_info_y(
                        P3_TARGET_INFO_Y + self.p3_target_info_layout_y_offset
                    ),
                    opacity=1.0 - eased,
                )
                return

        self.p3_target_info_panel.hide()
        self.p3_target_info_handle = 0
        self.p3_target_info_started = 0.0
        self.p3_target_info_exit_started = 0.0
        self.p3_target_info_last_target = None
        self.p3_target_info_last_seen = 0.0
        self.p3_target_info_image = None
        self.p3_target_info_key = None
        self.p3_target_info_presented = False
        self.p3_target_hp_animator.clear()

    def clear_p3_target_visual_immediately(self) -> None:
        self.native_p3_last_handle = 0
        self.native_p3_last_handle_time = 0.0
        self.native_p3_target_circle.clear()
        self.native_p3_area_circle.clear()
        self.clear_d3d_target_rings()
        self.p3_target_info_panel.hide()
        self.p3_target_info_handle = 0
        self.p3_target_info_started = 0.0
        self.p3_target_info_exit_started = 0.0
        self.p3_target_info_last_target = None
        self.p3_target_info_last_seen = 0.0
        self.p3_target_info_image = None
        self.p3_target_info_key = None
        self.p3_target_info_presented = False
        self.p3_target_hp_animator.clear()

    def clear_target_visual_immediately(self) -> None:
        self.native_p2_last_handle = 0
        self.native_p2_last_handle_time = 0.0
        self.native_target_circle.clear()
        self.native_p2_area_circle.clear()
        self.clear_d3d_target_rings()
        self.clear_world_target()
        self.world_marker_panel.hide()
        self.target_info_panel.hide()
        self.target_visual_handle = 0
        self.target_visual_started = 0.0
        self.target_visual_last_good = None
        self.target_visual_last_good_time = 0.0
        self.target_visual_raw_points = None
        self.target_visual_raw_time = 0.0
        self.target_visual_display_points = None
        self.target_visual_switch_from_points = None
        self.target_visual_entry_from_empty = False
        self.target_visual_raster = None
        self.target_visual_raster_center = None
        self.target_visual_raster_time = 0.0
        self.target_visual_last_info = None
        self.target_visual_info_key = None
        self.target_visual_info_draw_key = None
        self.p2_target_info_presented = False
        self.target_visual_exit_started = 0.0
        self.target_visual_exit_marker = None
        self.target_visual_exit_info = None
        self.target_visual_last_target = None
        self.target_visual_last_target_time = 0.0
        self.p2_target_hp_animator.clear()

    def area_secondary_indices_for_model(
        self,
        model: P2MenuModel,
    ) -> tuple[int, ...]:
        if model.mode is not MenuMode.TARGET or model.pending_action is None:
            return ()
        radius = float(model.pending_action.area_radius)
        candidates = model.valid_candidates()
        if radius <= 0.0 or len(candidates) <= 1:
            return ()

        if model is self.p1_model:
            foe_entries = self.p1_foes
            neutral_entries = self.p1_neutrals
        elif model is self.p3_model:
            foe_entries = self.p3_foes
            neutral_entries = self.p3_neutrals
        else:
            foe_entries = self.foes
            neutral_entries = self.neutrals

        actor_by_handle = {
            int(handle): int(actor)
            for _member_id, _name, handle, actor in self.active_party
            if handle and actor
        }
        actor_by_handle.update(
            {
                int(handle): int(actor)
                for _name, handle, actor in (*foe_entries, *neutral_entries)
                if handle and actor
            }
        )
        positions: dict[int, tuple[float, float, float]] = {}
        for candidate in candidates:
            handle = int(candidate.handle)
            actor = actor_by_handle.get(handle)
            if not actor or handle in positions:
                continue
            try:
                position = struct.unpack("<3f", self.process.read(actor + 0xD0, 12))
            except (OSError, RuntimeError, struct.error, ValueError):
                continue
            if all(math.isfinite(value) and abs(value) < 1.0e8 for value in position):
                positions[handle] = position

        return area_secondary_target_indices(
            candidates,
            model.target_index,
            radius,
            positions,
        )

    def sync_area_target_cursors(self) -> None:
        bindings = (
            (1, self.p1_model, self.p1_controller.presenter),
            (2, self.model, self.controller.presenter),
            (3, self.p3_model, self.p3_controller.presenter),
        )
        for player, model, presenter in bindings:
            indices = self.area_secondary_indices_for_model(model)
            self.party_area_secondary_indices[player] = indices
            presenter.set_secondary_target_indices(indices)

    def selected_party_hud_row(self, model: P2MenuModel) -> int | None:
        if (
            model.mode is not MenuMode.TARGET
            or model.target_group is not TargetGroup.PARTY
            or model.pending_action is None
            or model.pending_action.action_id == P2_CHANGE_CHARACTER_ACTION_ID
        ):
            return None
        candidates = model.valid_candidates()
        if not candidates:
            return None
        candidate = candidates[model.target_index % len(candidates)]
        if candidate.group is not TargetGroup.PARTY or not candidate.handle:
            return None
        member_id = next(
            (
                int(member_id)
                for member_id, _name, handle, _actor in self.active_party
                if int(handle) == int(candidate.handle)
            ),
            None,
        )
        if member_id is None:
            return None
        return party_hud_layout.party_hud_member_row(
            self.session_snapshot.current_layout,
            member_id,
        )

    def paint_party_target_cursors(
        self,
        p1_foreground: bool,
        p2_foreground: bool,
        p3_foreground: bool,
    ) -> None:
        try:
            reduced = party_hud_layout.party_hud_is_reduced(self.process)
        except (OSError, RuntimeError, struct.error, ValueError):
            reduced = False
        now = time.monotonic()
        delta = min(0.05, max(0.0, now - self.party_target_cursor_last_tick))
        self.party_target_cursor_last_tick = now
        jab = renderer.cursor_jab(now)
        area_opacity = renderer.area_cursor_opacity(now)
        summon_active = any(
            27 <= int(member_id) <= 39
            for member_id in self.session_snapshot.current_layout[:4]
        )
        players = (
            (
                1,
                self.p1_party_target_cursor_panel,
                self.p1_model,
                self.p1_theme_name,
                bool(p1_foreground and self.p1_visual_replacement),
            ),
            (
                2,
                self.p2_party_target_cursor_panel,
                self.model,
                self.p2_theme_name,
                bool(p2_foreground),
            ),
            (
                3,
                self.p3_party_target_cursor_panel,
                self.p3_model,
                self.p3_theme_name,
                bool(p3_foreground),
            ),
        )
        for player, panel, model, theme_name, visible in players:
            row = self.selected_party_hud_row(model) if visible else None
            tween = self.party_target_cursor_tweens[player]
            if row is None:
                self.party_target_cursor_active[player] = False
                tween.snap(0.0)
                panel.hide()
                continue
            if not self.party_target_cursor_active[player]:
                tween.snap(float(row))
                self.party_target_cursor_active[player] = True
            else:
                tween.retarget(float(row))
                tween.advance(delta)
            candidates = model.valid_candidates()
            secondary_rows: list[float] = []
            for index in self.party_area_secondary_indices.get(player, ()):
                if not 0 <= int(index) < len(candidates):
                    continue
                candidate = candidates[int(index)]
                if candidate.group is not TargetGroup.PARTY or not candidate.handle:
                    continue
                member_id = next(
                    (
                        int(member_id)
                        for member_id, _name, handle, _actor in self.active_party
                        if int(handle) == int(candidate.handle)
                    ),
                    None,
                )
                if member_id is None:
                    continue
                secondary_row = party_hud_layout.party_hud_member_row(
                    self.session_snapshot.current_layout,
                    member_id,
                )
                if secondary_row is not None and secondary_row != row:
                    secondary_rows.append(float(secondary_row))
            image = renderer.render_party_target_cursor(
                theme_name,
                reduced=reduced,
                position=tween.position,
                trail=tween.visible_trail(),
                secondary_positions=tuple(dict.fromkeys(secondary_rows)),
                secondary_opacity=area_opacity,
            )
            x, y = party_hud_layout.party_hud_cursor_position(
                0,
                reduced=reduced,
                jab=jab,
                esper_active=summon_active,
            )
            panel.draw(image, x, y)

    def paint_world_target(self, foreground: bool) -> None:
        if not foreground:
            self.world_marker_panel.hide()
            self.target_info_panel.hide()
            self.target_visual_info_draw_key = None
            self.p2_target_info_presented = False
            return
        now = time.monotonic()
        self.p2_target_info_last_tick = now
        try:
            inprocess_ring = (
                self.process.u32(self.base + INPROCESS_RING_READY_OFFSET)
                == INPROCESS_RING_READY_MAGIC
            )
        except OSError:
            inprocess_ring = False
        target = (
            self.selected_live_target(
                owner_actor=self.party[1][2] if len(self.party) >= 2 else 0,
            )
            if self.model.mode is MenuMode.TARGET
            else None
        )
        if target is not None:
            self.target_visual_last_target = target
            self.target_visual_last_target_time = now
        elif (
            self.model.mode is MenuMode.TARGET
            and self.target_visual_last_target is not None
            and now - self.target_visual_last_target_time
            <= TARGET_SAMPLE_DROPOUT_GRACE_SECONDS
        ):
            target = self.target_visual_last_target
        if target is not None:
            target = self.p2_target_hp_animator.present(target, now)
            self.p2_target_info_presented = True
            handle_changed = target.handle != self.target_visual_handle
            if handle_changed:
                previous_points = self.target_visual_display_points
                self.target_visual_handle = target.handle
                self.target_visual_started = 0.0
                self.target_visual_switch_from_points = previous_points
                self.target_visual_entry_from_empty = previous_points is None
                self.target_visual_raw_points = None
                self.target_visual_raw_time = 0.0
                self.target_visual_raster = None
                self.target_visual_raster_center = None
                self.target_visual_raster_time = 0.0
            self.target_visual_exit_started = 0.0
            self.target_visual_exit_marker = None
            self.target_visual_exit_info = None

            if inprocess_ring and not self.target_visual_started:
                self.target_visual_started = now

            raw_points = None
            if not inprocess_ring:
                try:
                    view = struct.unpack(
                        "<16f", self.process.read(legacy.coop.CAMERA_VIEW_MATRIX, 64)
                    )
                    projection = struct.unpack(
                        "<16f", self.process.read(CAMERA_PROJECTION_MATRIX, 64)
                    )
                    if not world_overlay.is_probable_minimap_view(view):
                        points = world_overlay.projected_actor_ring(
                            target, view, projection
                        )
                        if points:
                            raw_points = points
                except OSError:
                    raw_points = None

            if raw_points is not None:
                self.target_visual_raw_points = raw_points
                self.target_visual_raw_time = now
                if not self.target_visual_started:
                    self.target_visual_started = now
            elif (
                self.target_visual_raw_points is not None
                and now - self.target_visual_raw_time
                < TARGET_PROJECTION_DROPOUT_GRACE_SECONDS
            ):
                raw_points = self.target_visual_raw_points

            tick_delta = min(0.05, max(0.0, now - self.target_visual_last_tick))
            self.target_visual_last_tick = now
            elapsed = (
                max(0.0, now - self.target_visual_started)
                if self.target_visual_started
                else 0.0
            )
            progress = min(1.0, elapsed / TARGET_ENTER_SECONDS)
            eased = progress * progress * (3.0 - 2.0 * progress)

            display_points = self.target_visual_display_points
            if raw_points is not None:
                source = self.target_visual_switch_from_points
                if source is not None and progress < 1.0:
                    display_points = world_overlay.interpolate_projected_ring(
                        source, raw_points, eased
                    )
                else:
                    self.target_visual_switch_from_points = None
                    if display_points is None or handle_changed:
                        display_points = raw_points
                    else:
                        alpha = 1.0 - math.exp(
                            -tick_delta / TARGET_TRACK_SMOOTHING_SECONDS
                        )
                        display_points = world_overlay.interpolate_projected_ring(
                            display_points, raw_points, alpha
                        )
                self.target_visual_display_points = display_points

            marker = None
            center = (
                world_overlay.projected_ring_center(display_points)
                if display_points is not None
                else None
            )
            if display_points is not None and center is not None:
                if (
                    self.target_visual_raster is None
                    or now - self.target_visual_raster_time >= TARGET_RASTER_INTERVAL
                ):
                    rendered = world_overlay.render_projected_ring(
                        display_points, now=now
                    )
                    if rendered is not None:
                        self.target_visual_raster = rendered
                        self.target_visual_raster_center = center
                        self.target_visual_raster_time = now
                if (
                    self.target_visual_raster is not None
                    and self.target_visual_raster_center is not None
                ):
                    marker = world_overlay.translate_positioned(
                        self.target_visual_raster,
                        center[0] - self.target_visual_raster_center[0],
                        center[1] - self.target_visual_raster_center[1],
                    )
                    self.target_visual_last_good = marker
                    self.target_visual_last_good_time = now

            if (
                marker is None
                and self.target_visual_last_good is not None
                and now - self.target_visual_last_good_time
                <= TARGET_MARKER_DROPOUT_GRACE_SECONDS
            ):
                marker = self.target_visual_last_good

            if inprocess_ring or marker is None:
                self.world_marker_panel.hide()
            else:
                if self.target_visual_entry_from_empty and progress < 1.0:
                    visible = world_overlay.transform_positioned(
                        marker,
                        scale=0.72 + 0.28 * eased,
                        opacity=eased,
                    )
                else:
                    self.target_visual_entry_from_empty = False
                    visible = marker
                self.world_marker_panel.draw(visible.image, visible.x, visible.y)

            info_key = (
                target.handle,
                target.name,
                target.level,
                target.current_hp,
                target.max_hp,
                target.reveal_stats,
                target.flying,
                target.weak_mask,
                target.status_mask,
                target.viewer_has_libra,
                self.p2_theme_name,
            )
            if info_key != self.target_visual_info_key:
                self.target_visual_last_info = world_overlay.render_target_info(
                    target,
                    player=2,
                    theme_name=self.p2_theme_name,
                )
                self.target_visual_info_key = info_key
                self.target_visual_info_draw_key = None
            info_image = self.target_visual_last_info

            info_opacity = eased
            info_x = int(round(self.p2_target_info_layout_x))
            info_y = self.target_info_y(
                TARGET_INFO_Y + self.p2_target_info_layout_y_offset
            )
            info_draw_key = (
                info_key,
                int(round(info_opacity * 255.0)),
                info_x,
                info_y,
            )
            if (
                info_image is not None
                and info_draw_key != self.target_visual_info_draw_key
            ):
                self.draw_custom_panel(
                    self.target_info_panel,
                    info_image,
                    info_x,
                    info_y,
                    opacity=info_opacity,
                )
                self.target_visual_info_draw_key = info_draw_key
            return

        if self.target_visual_handle:
            self.target_visual_handle = 0
            self.target_visual_exit_started = now
            self.target_visual_exit_marker = self.target_visual_last_good
            self.target_visual_exit_info = self.target_visual_last_info

        if self.target_visual_exit_started:
            progress = min(
                1.0,
                (now - self.target_visual_exit_started) / TARGET_EXIT_SECONDS,
            )
            eased = progress * progress * (3.0 - 2.0 * progress)
            opacity = 1.0 - eased
            if inprocess_ring:
                self.world_marker_panel.hide()
            elif self.target_visual_exit_marker is not None:
                marker = world_overlay.transform_positioned(
                    self.target_visual_exit_marker,
                    scale=1.0 + 0.06 * eased,
                    opacity=opacity,
                )
                self.world_marker_panel.draw(marker.image, marker.x, marker.y)
            else:
                self.world_marker_panel.hide()
            if self.target_visual_exit_info is not None:
                self.draw_custom_panel(
                    self.target_info_panel,
                    self.target_visual_exit_info,
                    int(round(self.p2_target_info_layout_x)),
                    self.target_info_y(
                        TARGET_INFO_Y + self.p2_target_info_layout_y_offset
                    ),
                    opacity=opacity,
                )
                self.target_visual_info_draw_key = None
            else:
                self.target_info_panel.hide()
            if progress < 1.0:
                return

        self.world_marker_panel.hide()
        self.target_info_panel.hide()
        self.target_visual_exit_started = 0.0
        self.target_visual_last_good = None
        self.target_visual_last_good_time = 0.0
        self.target_visual_raw_points = None
        self.target_visual_raw_time = 0.0
        self.target_visual_display_points = None
        self.target_visual_switch_from_points = None
        self.target_visual_entry_from_empty = False
        self.target_visual_raster = None
        self.target_visual_raster_center = None
        self.target_visual_raster_time = 0.0
        self.target_visual_last_info = None
        self.target_visual_info_key = None
        self.target_visual_info_draw_key = None
        self.p2_target_info_presented = False
        self.target_visual_exit_marker = None
        self.target_visual_exit_info = None
        self.target_visual_last_target = None
        self.target_visual_last_target_time = 0.0
        self.p2_target_hp_animator.clear()

    def dispatch(self, command: InputCommand) -> None:
        before_open = self.model.is_open
        input_role_generation = self.role_generation
        if (
            command is InputCommand.TOGGLE
            and not before_open
            and not custom_menu_input_allowed(
                self.session_snapshot.p2_menu_allowed,
                leader_control_availability(
                    self.process,
                    self.party[1][2] if len(self.party) >= 2 else 0,
                ),
            )
        ):
            print("COOP_MENU_REJECTED owner=dead/unavailable", flush=True)
            return
        if command in (InputCommand.BACK, InputCommand.CONFIRM):
            self.capture_for_close()
        try:
            result = self.controller.dispatch(command)
        except Exception as error:
            print(f"COOP_INPUT_ERROR {type(error).__name__}: {error}", flush=True)
            return
        if self.role_generation != input_role_generation:
            return
        if (
            result.event.kind in ("target_group", "target_opened")
            and not self.model.valid_candidates()
        ):
            self.clear_target_visual_immediately()
        if result.event.kind == "toggle_gambits":
            try:
                self.model.gambits_enabled = self.toggle_p2_gambits()
                self.queue_native_sound(
                    NATIVE_SOUND_GAMBITS_ON
                    if self.model.gambits_enabled
                    else NATIVE_SOUND_GAMBITS_OFF,
                    player=2,
                )
                print(
                    "COOP_GAMBITS " + ("ON" if self.model.gambits_enabled else "OFF"),
                    flush=True,
                )
            except Exception as error:
                print(
                    f"COOP_GAMBITS_ERROR {type(error).__name__}: {error}",
                    flush=True,
                )
                return
        if result.event.kind in ("target_opened", "target_group"):
            print(
                "P2_HEADLESS_TARGETS "
                f"group={self.model.target_group.value} "
                f"foes={len(self.model.valid_candidates(TargetGroup.FOES))} "
                f"neutrals={len(self.model.valid_candidates(TargetGroup.NEUTRALS))} "
                f"party={len(self.model.valid_candidates(TargetGroup.PARTY))} "
                f"reserve={len(self.model.valid_candidates(TargetGroup.RESERVE))}",
                flush=True,
            )
        sound_id = (
            NATIVE_SOUND_INVALID
            if (
                command is InputCommand.CONFIRM
                and result.event.kind in ("ignored", "invalid_target")
            )
            else NATIVE_SOUND_BY_EVENT.get(result.event.kind)
        )
        if sound_id is not None:
            self.queue_native_sound(sound_id, player=2)
        if result.event.kind == "opened":
            self.begin_opening()
        elif before_open and result.event.kind in ("closed", "submit"):
            self.begin_closing()
            closed_at = time.monotonic()
            self.menu_reopen_allowed_at = closed_at + MENU_REOPEN_QUIET_SECONDS
            for guard_name in (
                "p2_confirm_reopen_guard",
                "p2_character_direction_guard",
            ):
                guard = getattr(self, guard_name, None)
                if guard is not None:
                    guard.arm(closed_at)

    def dispatch_p1(self, command: InputCommand) -> None:
        before_open = self.p1_model.is_open
        if (
            command is InputCommand.TOGGLE
            and not before_open
            and not custom_menu_input_allowed(
                self.session_snapshot.p1_menu_allowed,
                leader_control_availability(
                    self.process,
                    self.party[0][2] if self.party else 0,
                ),
            )
        ):
            print("P1_HEADLESS_MENU_REJECTED owner=dead/unavailable", flush=True)
            return
        if command in (InputCommand.BACK, InputCommand.CONFIRM):
            self.capture_p1_for_close()
        try:
            result = self.p1_controller.dispatch(command)
        except Exception as error:
            print(
                f"P1_HEADLESS_INPUT_ERROR {type(error).__name__}: {error}",
                flush=True,
            )
            return
        if (
            result.event.kind in ("target_group", "target_opened")
            and not self.p1_model.valid_candidates()
        ):
            self.clear_p1_target_visual_immediately()
        if result.event.kind in ("target_opened", "target_group"):
            print(
                "P1_HEADLESS_TARGETS "
                f"group={self.p1_model.target_group.value} "
                f"foes={len(self.p1_model.valid_candidates(TargetGroup.FOES))} "
                f"neutrals={len(self.p1_model.valid_candidates(TargetGroup.NEUTRALS))} "
                f"party={len(self.p1_model.valid_candidates(TargetGroup.PARTY))} "
                f"reserve={len(self.p1_model.valid_candidates(TargetGroup.RESERVE))}",
                flush=True,
            )
        if result.event.kind == "toggle_gambits":
            try:
                self.p1_model.gambits_enabled = self.toggle_p1_gambits()
                self.queue_native_sound(
                    NATIVE_SOUND_GAMBITS_ON
                    if self.p1_model.gambits_enabled
                    else NATIVE_SOUND_GAMBITS_OFF,
                    player=1,
                )
            except Exception as error:
                print(
                    f"P1_HEADLESS_GAMBITS_ERROR {type(error).__name__}: {error}",
                    flush=True,
                )
                return
        sound_id = (
            NATIVE_SOUND_INVALID
            if (
                command is InputCommand.CONFIRM
                and result.event.kind in ("ignored", "invalid_target")
            )
            else NATIVE_SOUND_BY_EVENT.get(result.event.kind)
        )
        if sound_id is not None:
            self.queue_native_sound(sound_id, player=1)
        if result.event.kind == "opened":
            self.begin_p1_opening()
        elif before_open and result.event.kind in ("closed", "submit"):
            self.begin_p1_closing()
            closed_at = time.monotonic()

            self.p1_menu_reopen_allowed_at = closed_at + MENU_REOPEN_QUIET_SECONDS
            self.p1_confirm_reopen_guard.arm(closed_at)

    def dispatch_p3(self, command: InputCommand) -> None:
        before_open = self.p3_model.is_open
        input_role_generation = self.role_generation
        actor = self.party[2][2] if len(self.party) >= 3 else 0
        p3_allowed = p3_session_menu_allowed(self.session_snapshot, len(self.party))
        if (
            command is InputCommand.TOGGLE
            and not before_open
            and not custom_menu_input_allowed(
                p3_allowed,
                leader_control_availability(self.process, actor),
            )
        ):
            print("P3_MENU_REJECTED owner=dead/unavailable", flush=True)
            return
        if command in (InputCommand.BACK, InputCommand.CONFIRM):
            self.capture_p3_for_close()
        try:
            result = self.p3_controller.dispatch(command)
        except Exception as error:
            print(f"P3_INPUT_ERROR {type(error).__name__}: {error}", flush=True)
            return
        if self.role_generation != input_role_generation:
            return
        if (
            result.event.kind in ("target_group", "target_opened")
            and not self.p3_model.valid_candidates()
        ):
            self.clear_p3_target_visual_immediately()
        if result.event.kind == "toggle_gambits":
            try:
                self.p3_model.gambits_enabled = self.toggle_p3_gambits()
                self.queue_native_sound(
                    NATIVE_SOUND_GAMBITS_ON
                    if self.p3_model.gambits_enabled
                    else NATIVE_SOUND_GAMBITS_OFF,
                    player=3,
                )
            except Exception as error:
                print(
                    f"P3_GAMBITS_ERROR {type(error).__name__}: {error}",
                    flush=True,
                )
                return
        sound_id = (
            NATIVE_SOUND_INVALID
            if (
                command is InputCommand.CONFIRM
                and result.event.kind in ("ignored", "invalid_target")
            )
            else NATIVE_SOUND_BY_EVENT.get(result.event.kind)
        )
        if sound_id is not None:
            self.queue_native_sound(sound_id, player=3)
        if result.event.kind == "opened":
            self.begin_p3_opening()
        elif before_open and result.event.kind in ("closed", "submit"):
            self.begin_p3_closing()
            closed_at = time.monotonic()
            self.p3_menu_reopen_allowed_at = closed_at + MENU_REOPEN_QUIET_SECONDS
            for guard_name in (
                "p3_confirm_reopen_guard",
                "p3_character_direction_guard",
            ):
                guard = getattr(self, guard_name, None)
                if guard is not None:
                    guard.arm(closed_at)

    def p1_native_lifecycle(self) -> tuple[int, int, int] | None:
        try:
            manager = self.process.u64(UI_MANAGER_GLOBAL)
            if not manager:
                return None
            menu = manager + P1_MENU_OFFSET
            if self.process.u64(menu) != P1_MENU_CALLBACK:
                return None
            tree = self.process.u64(menu + 0x18)
            if not tree:
                return None
            active = self.process.u64(P1_ACTIVE_MENU_GLOBAL)
            return menu, tree, active
        except OSError:
            return None

    def reset_p1_visual_state(self) -> None:
        self.p1_panel.hide()
        self.p1_help_panel.hide()
        self.p1_transition_kind = None
        self.p1_active_lifecycle = None
        self.p1_last_image = None
        self.p1_last_help = None
        self.p1_help_fade_image = None
        self.p1_help_fade_opacity = 0.0
        self.p1_last_vertical_offset = 0
        self.p1_last_paint = time.monotonic()
        self.p1_mirror.reset()

    def paint_p1_custom_menu(self, foreground: bool) -> None:
        now = time.monotonic()
        delta = min(0.05, max(0.0, now - self.p1_last_paint))
        self.p1_last_paint = now
        if not self.p1_visual_replacement or not foreground:
            self.reset_p1_visual_state()
            return

        if (
            self.p1_model.mode is MenuMode.CLOSED
            and self.p1_transition_kind != "closing"
        ):
            # A submitter/presenter exception may close the model before the
            # opening lifecycle is committed.  Rendering a CLOSED model raises
            # and used to restart the helper immediately before circle upkeep.
            if self.p1_transition_kind is not None:
                self.reset_p1_visual_state()
                return
            self.p1_panel.hide()
            self.p1_help_panel.hide()
            self.p1_help_fade_image = None
            self.p1_help_fade_opacity = 0.0
            return

        if self.p1_transition_kind == "closing":
            image = self.p1_closing_image
            help_image = self.p1_closing_help
            vertical_offset = self.p1_closing_y_offset
        else:
            image = self.p1_controller.presenter.render(
                now=now,
                delta_seconds=delta,
                player=1,
                compose_layers=not self.three_player_layout(),
            )
            vertical_offset = self.p1_menu_vertical_offset()
            help_image = (
                None
                if self.p1_model.mode is MenuMode.TARGET
                else self.p1_controller.presenter.render_help(player=1)
            )
        if image is None:
            self.p1_panel.hide()
            self.p1_help_panel.hide()
            self.p1_help_fade_image = None
            self.p1_help_fade_opacity = 0.0
            return

        self.p1_last_image = image
        self.p1_last_help = help_image
        self.p1_last_vertical_offset = vertical_offset

        offset_x = 0.0
        opacity = 1.0
        if self.p1_transition_kind:
            progress = min(
                1.0,
                (now - self.p1_transition_started) / renderer.TRANSITION_SECONDS,
            )
            transition = renderer.transition(
                image,
                progress,
                opening=self.p1_transition_kind == "opening",
                p2_mirrored=False,
            )
            image = transition.image
            offset_x = transition.offset_x
            opacity = transition.opacity
            if progress >= 1.0:
                closing = self.p1_transition_kind == "closing"
                self.p1_transition_kind = None
                if closing:
                    self.p1_closing_image = None
                    self.p1_closing_help = None
                    self.p1_panel.hide()
                    self.p1_help_panel.hide()
                    self.p1_help_fade_image = None
                    self.p1_help_fade_opacity = 0.0
                    return
        self.draw_custom_panel(
            self.p1_panel,
            image,
            self.menu_panel_x(1)
            + offset_x
            * (
                renderer.THREE_PLAYER_PANEL_SCALE if self.three_player_layout() else 1.0
            ),
            P1_OVERLAY_Y + vertical_offset,
            bottom_anchor=True,
            vertical_offset=vertical_offset,
            opacity=opacity,
        )
        help_requested = bool(
            help_image is not None and not self.info_help_target_suppressed[0]
        )
        if help_requested:
            self.p1_help_fade_image = help_image
        self.p1_help_fade_opacity = advance_info_opacity(
            self.p1_help_fade_opacity,
            help_requested,
            delta,
            immediate=self.p1_transition_kind != "closing",
        )
        if self.p1_help_fade_image is not None and self.p1_help_fade_opacity > 0.0:
            self.draw_custom_panel(
                self.p1_help_panel,
                self.p1_help_fade_image,
                P1_HELP_X,
                int(round(self.p1_help_layout_y)),
                opacity=opacity * self.p1_help_fade_opacity,
            )
        else:
            self.p1_help_panel.hide()
            if not help_requested:
                self.p1_help_fade_image = None

    def queue_native_sound(self, sound_id: int, *, player: int | None = None) -> None:
        if player is not None and not bool(
            getattr(self, "battle_menu_sounds", {}).get(f"p{player}", True)
        ):
            return
        self.pending_native_sounds.append(int(sound_id))

    def flush_one_native_sound(self) -> None:
        if not self.pending_native_sounds:
            return
        sound_id = self.pending_native_sounds.popleft()
        # UI feedback is transient, not a reliable-delivery queue.  If several
        # actions were coalesced into one frame, playing the remainder on later
        # frames creates an input-independent burst.  Keep at most the first
        # sound for this native pump opportunity and discard every stale extra.
        self.pending_native_sounds.clear()
        try:
            self.native_sound_player.play(sound_id)
        except Exception as error:
            print(
                f"COOP_SOUND_ERROR {type(error).__name__}: {error}",
                flush=True,
            )

    def input_and_flush_one_native_sound(
        self,
        foreground: bool,
        *,
        native_pause_open: bool,
    ) -> None:
        """Dispatch one input frame and never strand a queued sound on error."""

        try:
            self.input(foreground, native_pause_open=native_pause_open)
        finally:
            self.flush_one_native_sound()

    def hard_close_p1_menu(self) -> None:
        self.p1_model.close()
        self.p1_controller.presenter.reset_visual_state()
        self.p1_transition_kind = None
        self.p1_closing_image = None
        self.p1_closing_help = None
        self.p1_closing_y_offset = 0
        self.p1_panel.hide()
        self.p1_help_panel.hide()
        self.p1_target_info_panel.hide()
        self.native_p1_target_circle.clear()
        self.native_p1_area_circle.clear()
        self.clear_d3d_target_rings()
        self.reset_p1_visual_state()

    def hard_close_p2_menu(self) -> None:
        self.model.close()
        self.controller.presenter.reset_visual_state()
        self.transition_kind = None
        self.closing_image = None
        self.closing_help = None
        self.closing_y_offset = 0
        self.panel.hide()
        self.help_panel.hide()
        self.target_info_panel.hide()
        self.world_marker_panel.hide()
        self.native_target_circle.clear()
        self.native_p2_area_circle.clear()
        self.clear_d3d_target_rings()
        self.clear_world_target()

    def hard_close_p3_menu(self) -> None:
        self.p3_model.close()
        self.p3_controller.presenter.reset_visual_state()
        self.p3_transition_kind = None
        self.p3_closing_image = None
        self.p3_closing_help = None
        self.p3_closing_y_offset = 0
        self.p3_panel.hide()
        self.p3_help_panel.hide()
        self.clear_p3_target_visual_immediately()

    def hide_custom_menu_visuals_preserving_state(self) -> None:
        for panel in (
            self.p1_panel,
            self.p1_help_panel,
            self.p1_target_info_panel,
            self.panel,
            self.help_panel,
            self.target_info_backdrop_panel,
            self.target_info_panel,
            self.world_marker_panel,
            self.interaction_prompt_panel,
            self.p3_panel,
            self.p3_help_panel,
            self.p3_target_info_panel,
            self.p3_interaction_prompt_panel,
        ):
            panel.hide()
        self.native_p1_target_circle.suspend_for_native_frontend()
        self.native_p1_area_circle.clear()
        self.native_target_circle.suspend_for_native_frontend()
        self.native_p2_area_circle.clear()
        self.native_p3_target_circle.suspend_for_native_frontend()
        self.native_p3_area_circle.clear()
        self.native_area_render_gate.clear()
        self.clear_d3d_target_rings()
        self.clear_world_target()
        self.publish_p1_help_badge(False)

    def suspend_custom_menus_for_native_frontend(
        self,
        source: str,
        *,
        now: float | None = None,
    ) -> None:
        when = time.monotonic() if now is None else float(now)
        if not self.native_frontend_custom_suspended:
            self.p1_frontend_deferred_open_until = 0.0
            self.native_frontend_open_candidate_since = None
            self.native_frontend_custom_suspended = True
            self.native_frontend_suspended_since = when
            self.native_frontend_suspension_source = str(source)
            snapshot = getattr(self, "session_snapshot", None)
            self.native_frontend_suspended_battle_info = int(
                getattr(snapshot, "battle_info", 0) or 0
            )
            self.native_frontend_suspended_role_generation = int(
                getattr(self, "role_generation", 0)
            )
            print(
                f"CUSTOM_MENUS_SUSPENDED native={source}",
                flush=True,
            )
        self.hide_custom_menu_visuals_preserving_state()

    def resume_custom_menus_after_native_frontend(
        self,
        *,
        now: float | None = None,
    ) -> None:
        if not self.native_frontend_custom_suspended:
            return
        when = time.monotonic() if now is None else float(now)
        duration = max(0.0, when - float(self.native_frontend_suspended_since))

        for kind_name, started_name in (
            ("p1_transition_kind", "p1_transition_started"),
            ("transition_kind", "transition_started"),
            ("p3_transition_kind", "p3_transition_started"),
        ):
            if getattr(self, kind_name, None) is not None:
                setattr(
                    self,
                    started_name,
                    float(getattr(self, started_name, when)) + duration,
                )
        self.p1_last_paint = when
        self.last_paint = when
        self.p3_last_paint = when
        source = self.native_frontend_suspension_source
        self.native_frontend_custom_suspended = False
        self.native_frontend_suspended_since = 0.0
        self.native_frontend_observed_open = False
        self.native_frontend_open_candidate_since = None
        self.native_frontend_close_candidate_since = 0.0
        self.native_frontend_suspension_source = ""
        self.native_frontend_suspended_battle_info = 0
        self.native_frontend_suspended_role_generation = 0
        self.native_party_menu_active = False
        print(
            f"CUSTOM_MENUS_RESUMED native={source}",
            flush=True,
        )

    def abort_native_frontend_suspension(self, reason: str) -> None:
        if not self.native_frontend_custom_suspended:
            return
        preserve_party_capture = bool(
            reason == "role_generation" and self.native_party_menu_active
        )
        self.hard_close_p1_menu()
        self.hard_close_p2_menu()
        self.hard_close_p3_menu()
        self.native_frontend_custom_suspended = False
        self.native_frontend_suspended_since = 0.0
        self.native_frontend_observed_open = False
        self.native_frontend_open_candidate_since = None
        self.native_frontend_close_candidate_since = 0.0
        self.native_frontend_suspension_source = ""
        self.native_frontend_suspended_battle_info = 0
        self.native_frontend_suspended_role_generation = 0
        self.native_party_menu_active = False
        self.p1_frontend_deferred_open_until = 0.0
        self._clear_native_party_menu_request()
        if not preserve_party_capture:
            self.party_edit_capture.cancel()
        self.native_dialog_bridge.set_party_menu_blocked(False)
        self.native_wait_mode_bridge.maintain(False)
        print(
            f"CUSTOM_MENUS_SUSPENSION_ABORTED reason={reason}",
            flush=True,
        )

    def maintain_native_frontend_suspension(
        self,
        snapshot: session_state.SessionSnapshot,
        *,
        now: float,
    ) -> None:
        if not snapshot.native_menu_open:
            self.unexpected_native_menu_cancel_latched = False
            self.unexpected_native_menu_cancel_retry_at = 0.0
        if snapshot.loading or snapshot.cutscene:
            self.native_frontend_open_candidate_since = None
            if self.native_frontend_custom_suspended:
                reason = "loading" if snapshot.loading else "cutscene"
                self.abort_native_frontend_suspension(reason)
            return
        if self.native_frontend_custom_suspended:
            battle_info = int(snapshot.battle_info or 0)
            generation_changed = bool(
                self.native_frontend_suspended_battle_info
                and battle_info != self.native_frontend_suspended_battle_info
            )
            roles_changed = bool(
                int(getattr(self, "role_generation", 0))
                != self.native_frontend_suspended_role_generation
            )
            if (
                roles_changed
                and self.native_frontend_suspension_source
                == "dead_party_recovery"
            ):
                # Moving a living reserve into an active slot belongs to the
                # same stock recovery transaction and must retain native input.
                self.native_frontend_suspended_role_generation = int(
                    getattr(self, "role_generation", 0)
                )
                roles_changed = False
            if generation_changed or roles_changed:
                reason = (
                    "battle_generation" if generation_changed else "role_generation"
                )
                self.abort_native_frontend_suspension(reason)
                return
        if snapshot.native_menu_open:
            # Once an unproven native frontend appears inside a forbidden
            # lifecycle window, that *same open instance* can never acquire
            # external provenance merely because the close/release guard later
            # expires.  Only observing the native bit clear starts a new
            # provenance transaction.
            if getattr(self, "unexpected_native_menu_cancel_latched", False):
                self.native_frontend_open_candidate_since = None
                return
            party_request_pending = bool(
                getattr(self, "native_party_menu_request_pending", False)
            )
            party_menu_active = bool(
                getattr(self, "native_party_menu_active", False)
            )
            party_transfer = bool(
                party_request_pending
                or (
                    party_menu_active
                    and not self.native_frontend_close_candidate_since
                )
            )
            already_trusted_external = bool(
                self.native_frontend_custom_suspended
                and self.native_frontend_suspension_source == "external"
                and self.native_frontend_observed_open
                and not self.native_frontend_close_candidate_since
            )
            if party_transfer:
                # The transactional Party request owns acknowledgement in
                # maintain_native_party_menu_request(); do not promote a raw
                # native-menu bit before that exact route is proven.
                if (
                    party_menu_active
                    and not self.native_frontend_close_candidate_since
                ):
                    self.native_frontend_observed_open = True
                return
            if already_trusted_external:
                self.native_frontend_close_candidate_since = 0.0
                return
            if self.native_frontend_quarantine_required(now):
                self.native_frontend_open_candidate_since = None
                return
            if not self.native_frontend_custom_suspended:
                open_candidate_since = getattr(
                    self, "native_frontend_open_candidate_since", None
                )
                if open_candidate_since is None:
                    self.native_frontend_open_candidate_since = float(now)
                    return
                if (
                    float(now) - open_candidate_since
                    < NATIVE_FRONTEND_OPEN_SETTLE_SECONDS
                ):
                    return
                self.suspend_custom_menus_for_native_frontend("external", now=now)
            self.native_frontend_observed_open = True
            self.native_frontend_close_candidate_since = 0.0
            return
        self.native_frontend_open_candidate_since = None
        if (
            not self.native_frontend_custom_suspended
            or self.native_party_menu_request_pending
            or not self.native_frontend_observed_open
        ):
            return

        if (
            snapshot.native_modal_active
            or not snapshot.user_control_enabled
            or snapshot.loading
            or snapshot.cutscene
        ):
            self.native_frontend_close_candidate_since = 0.0
            return
        if not self.native_frontend_close_candidate_since:
            self.native_frontend_close_candidate_since = float(now)
            return
        if (
            float(now) - self.native_frontend_close_candidate_since
            >= NATIVE_FRONTEND_CLOSE_SETTLE_SECONDS
        ):
            self.resume_custom_menus_after_native_frontend(now=now)

    def native_frontend_quarantine_required(self, now: float) -> bool:
        """Reject a native frontend that appeared inside a co-op input guard."""

        now = float(now)
        models = tuple(
            getattr(self, name, None) for name in ("p1_model", "model", "p3_model")
        )
        transitions = tuple(
            getattr(self, name, None)
            for name in ("p1_transition_kind", "transition_kind", "p3_transition_kind")
        )
        release_guard = getattr(self, "custom_directional_release_guard", None)
        release_active = bool(
            release_guard is not None
            and (
                bool(getattr(release_guard, "requires_release", False))
                or now < float(getattr(release_guard, "blocked_until", 0.0))
            )
        )
        quiet_active = any(
            now < float(getattr(self, name, 0.0))
            for name in (
                "p1_menu_reopen_allowed_at",
                "menu_reopen_allowed_at",
                "p3_menu_reopen_allowed_at",
            )
        )
        confirm_guard = getattr(self, "p1_confirm_reopen_guard", None)
        confirm_blocked = bool(
            confirm_guard is not None and not confirm_guard.can_open(now)
        )
        close_settle = bool(
            self.native_frontend_custom_suspended
            and self.native_frontend_close_candidate_since
        )
        command_tree = self.p1_native_lifecycle()
        command_tree_open = bool(
            command_tree is not None and command_tree[2] == command_tree[0]
        )
        return bool(
            any(
                getattr(model, "mode", MenuMode.CLOSED) is not MenuMode.CLOSED
                for model in models
                if model is not None
            )
            or any(transition is not None for transition in transitions)
            or release_active
            or quiet_active
            or confirm_blocked
            or close_settle
            or command_tree_open
            or bool(getattr(getattr(self, "dead_leader_menu_gate", None), "pending", False))
        )

    def native_frontend_input_trusted(self, snapshot) -> bool:
        if not snapshot.native_menu_open:
            return False
        if getattr(self, "unexpected_native_menu_cancel_latched", False):
            return False
        if not (
            self.native_frontend_custom_suspended
            and self.native_frontend_observed_open
            and not self.native_frontend_close_candidate_since
        ):
            return False
        return bool(
            self.native_party_menu_active
            or self.native_frontend_suspension_source == "external"
        )

    def _clear_native_party_menu_request(self) -> None:
        self.native_party_menu_request_pending = False
        self.native_party_menu_request_pulsed = False
        self.native_party_menu_request_source = ""
        self.native_party_menu_request_deadline = 0.0
        self.native_party_command_drain_pending = False

    def cancel_native_party_menu_request(
        self,
        reason: str,
        *,
        publish_policy: bool = True,
    ) -> None:
        if not self.native_party_menu_request_pending:
            return
        source = self.native_party_menu_request_source
        if publish_policy:
            self.native_dialog_bridge.set_party_menu_blocked(False)
        self._clear_native_party_menu_request()
        if not self.native_frontend_observed_open:
            self.resume_custom_menus_after_native_frontend()
        print(
            f"NATIVE_PARTY_MENU_CANCELLED source={source} reason={reason}",
            flush=True,
        )

    def request_native_party_menu(self, source: str, now: float) -> bool:
        if self.native_party_menu_request_pending:
            return False

        # Commit the resumable request before any remote/window operation.  A
        # transient failure while hiding custom panels, publishing the native
        # policy or releasing Wait mode must leave maintain_native_party_menu_request()
        # a durable transaction to resume; otherwise the suspension latch is set
        # but the initiating edge is gone, permanently stranding P1/P2/P3.
        self.native_party_menu_request_pending = True
        self.native_party_menu_request_pulsed = False
        self.native_party_menu_request_source = str(source)
        self.native_party_menu_request_deadline = (
            float(now) + NATIVE_PARTY_MENU_TRANSFER_TIMEOUT_SECONDS
        )
        self.native_party_command_drain_pending = False
        self.suspend_custom_menus_for_native_frontend(source, now=now)
        self._publish_native_input_contract(
            # The synthetic Party pulse bypasses policy masks.  Keep COMMAND
            # and leader directions closed until the native screen is actually
            # observed open; releasing them at request time lets a held alias
            # open Battle/Leader during the asynchronous transfer.
            p1_replacement=True,
            non_p1_command_blocked=True,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=True,
            quickening_block_mask=0,
            leader_replacement=True,
            capture_desktop_events=False,
            suppress_mouse_look=False,
        )
        self._clear_native_desktop_route()

        self.native_wait_mode_bridge.maintain(False)
        print(
            f"NATIVE_PARTY_MENU_REQUEST source={source}",
            flush=True,
        )
        return True

    def maintain_native_party_menu_request(
        self,
        *,
        foreground: bool,
        snapshot: session_state.SessionSnapshot,
        native_pause_open: bool,
        now: float,
    ) -> bool:
        if not self.native_party_menu_request_pending:
            return False
        source = self.native_party_menu_request_source
        self._publish_native_input_contract(
            p1_replacement=True,
            non_p1_command_blocked=True,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=True,
            quickening_block_mask=0,
            leader_replacement=True,
            capture_desktop_events=False,
            suppress_mouse_look=False,
        )
        self._clear_native_desktop_route()
        wait_close_published = self.native_wait_mode_bridge.maintain(False)

        command_lifecycle = self.p1_native_lifecycle()
        command_tree_open = bool(
            command_lifecycle is not None
            and command_lifecycle[2] == command_lifecycle[0]
        )
        if command_tree_open:
            if not self.native_party_command_drain_pending:
                print(
                    f"NATIVE_PARTY_MENU_DRAIN source={source}",
                    flush=True,
                )
            self.native_party_command_drain_pending = True
        elif self.native_party_command_drain_pending and not snapshot.native_menu_open:
            self.native_party_command_drain_pending = False
            print(
                f"NATIVE_PARTY_MENU_DRAINED source={source}",
                flush=True,
            )
        command_drain_pending = self.native_party_command_drain_pending

        physical_p1_ack = bool(
            "p1" in source.split("+")
            and snapshot.native_menu_open
            and not native_pause_open
            and not command_tree_open
            and not command_drain_pending
        )
        if (
            snapshot.native_menu_open
            and not native_pause_open
            and not command_tree_open
            and not command_drain_pending
            and (self.native_party_menu_request_pulsed or physical_p1_ack)
        ):
            ack_route = (
                "mailbox" if self.native_party_menu_request_pulsed else "physical_p1"
            )

            if snapshot.battle_info:
                self.party_edit_capture.begin(
                    source,
                    battle_info=snapshot.battle_info,
                )
            else:
                self.party_edit_capture.cancel()
                print(
                    "PARTY_ACTIVE_INTENT_SKIP missing battle generation",
                    flush=True,
                )
            # Only the observed native Party frontend receives pass-through.
            # Until this acknowledgement the request policy above stays fully
            # fail-closed except for its synthetic pulse.
            self._publish_native_input_contract(
                p1_replacement=False,
                non_p1_command_blocked=False,
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                party_menu_blocked=False,
                quickening_block_mask=0,
                leader_replacement=False,
                capture_desktop_events=False,
                suppress_mouse_look=False,
            )
            self.native_frontend_observed_open = True
            self.native_party_menu_active = True
            self._clear_native_party_menu_request()
            print(
                f"NATIVE_PARTY_MENU_OPENED source={source} route={ack_route}",
                flush=True,
            )
            return True

        cancel_reason = ""
        if native_pause_open:
            cancel_reason = "native_pause"
        elif (
            snapshot.native_menu_open
            and not command_tree_open
            and not command_drain_pending
        ):
            cancel_reason = "unexpected_native_menu"
        elif snapshot.native_modal_active:
            cancel_reason = "native_modal"
        elif snapshot.loading or snapshot.cutscene or not snapshot.user_control_enabled:
            cancel_reason = "session_gate"
        elif (
            self.custom_ui_suppressed
            and not snapshot.town
            and not command_tree_open
            and not command_drain_pending
        ):
            cancel_reason = "presentation_suppressed"
        elif float(now) >= self.native_party_menu_request_deadline:
            cancel_reason = "timeout"
        if cancel_reason:
            self.cancel_native_party_menu_request(cancel_reason)
            return True

        if command_tree_open:
            if (
                wait_close_published
                and self.native_wait_mode_bridge.lifecycle_close_complete()
            ):
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_CANCEL)
            return True
        if command_drain_pending:
            return True

        if self.native_party_menu_request_pulsed:
            return True
        if (
            not wait_close_published
            or not self.native_wait_mode_bridge.lifecycle_close_complete()
        ):
            return True

        self.native_dialog_bridge.pulse(NATIVE_DIALOG_PARTY_MENU)
        self.native_party_menu_request_pulsed = True
        print(
            "NATIVE_PARTY_MENU_PULSED "
            f"source={source} button={NATIVE_DIALOG_PARTY_MENU:#04x}",
            flush=True,
        )
        return True

    def maintain_party_edit_capture(
        self,
        snapshot: session_state.SessionSnapshot,
        *,
        now: float,
    ) -> None:
        pending = getattr(self, "pending_party_intent_publish", None)
        if pending is not None:
            captured, captured_identity, captured_world = pending
            _members, captured_battle_info, _source = captured
            current_battle_info = int(getattr(snapshot, "battle_info", 0) or 0)
            current_identity = getattr(snapshot, "identity", None)
            current_world = getattr(snapshot, "coherent_world_signature", None)
            if (
                (current_battle_info and current_battle_info != captured_battle_info)
                or current_identity != captured_identity
                or current_world != captured_world
            ):
                self.pending_party_intent_publish = None
                return
        else:
            members = coherent_manual_party_members(self.process, snapshot)
            captured = self.party_edit_capture.observe(
                native_frontend_open=bool(
                    snapshot.native_menu_open
                    or getattr(snapshot, "native_modal_active", False)
                    or getattr(self, "native_frontend_custom_suspended", False)
                ),
                field_stable=members is not None,
                battle_info=snapshot.battle_info,
                active_members=members,
                now=now,
            )
            captured_identity = getattr(snapshot, "identity", None)
            captured_world = getattr(snapshot, "coherent_world_signature", None)
        if captured is None:
            return
        # ExplicitPartyEditCapture consumes its stable observation exactly once.
        # Preserve that immutable payload until the atomic file publisher proves
        # success; otherwise one antivirus/disk race silently loses the user's
        # completed Party-menu transaction forever.
        self.pending_party_intent_publish = (
            captured,
            captured_identity,
            captured_world,
        )
        active_members, battle_info, source = captured
        try:
            record = self.party_intent_publisher.publish(
                active_members,
                battle_info=battle_info,
                source=source,
            )
        except (OSError, struct.error, ValueError) as error:
            print(
                f"PARTY_ACTIVE_INTENT_WRITE_FAILED {type(error).__name__}: {error}",
                flush=True,
            )
            return
        self.pending_party_intent_publish = None
        print(
            "PARTY_ACTIVE_INTENT "
            f"epoch={record.epoch} source={source} "
            f"members={','.join(map(str, active_members))}",
            flush=True,
        )

    def apply_session_gates(
        self,
        snapshot: session_state.SessionSnapshot,
        *,
        presentation_suppressed: bool = False,
    ) -> None:
        self.custom_ui_suppressed = bool(presentation_suppressed)
        process = getattr(self, "process", None)
        base = getattr(self, "base", None)
        presentation_suppressed = bool(presentation_suppressed)
        if (
            process is not None
            and base is not None
            and presentation_suppressed
            != getattr(self, "presentation_suppressed_published", None)
        ):
            try:
                process.write(
                    base + RENDER_PRESENTATION_SUPPRESSED_OFFSET,
                    struct.pack("<I", int(presentation_suppressed)),
                )
                self.presentation_suppressed_published = presentation_suppressed
            except (OSError, RuntimeError, ValueError):
                pass
        preserve_for_native_frontend = bool(
            snapshot.native_menu_open or self.native_frontend_custom_suspended
        )
        if presentation_suppressed:
            self._publish_native_input_contract(
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                capture_desktop_events=False,
                suppress_mouse_look=False,
            )
            self._clear_native_desktop_route()
        p1_menu_allowed = bool(snapshot.p1_menu_allowed and not presentation_suppressed)
        p2_menu_allowed = bool(snapshot.p2_menu_allowed and not presentation_suppressed)
        p3_menu_allowed = bool(
            p3_session_menu_allowed(snapshot, len(self.party))
            and not presentation_suppressed
        )
        session_p3_menu_allowed = p3_session_menu_allowed(snapshot, len(self.party))
        signature = (
            p1_menu_allowed,
            p2_menu_allowed,
            p3_menu_allowed,
            bool(presentation_suppressed),
            preserve_for_native_frontend,
            snapshot.bridge_ownership_allowed,
            snapshot.loading,
            snapshot.cutscene,
            snapshot.town,
            snapshot.native_menu_open,
            snapshot.native_modal_active,
            snapshot.leader_party_member_id,
            snapshot.identity,
        )
        if signature == self.last_session_gate_signature:
            return
        if not p1_menu_allowed and not preserve_for_native_frontend:
            self.hard_close_p1_menu()
        if not p2_menu_allowed and not preserve_for_native_frontend:
            self.hard_close_p2_menu()
        if not p3_menu_allowed and not preserve_for_native_frontend:
            self.hard_close_p3_menu()
        if presentation_suppressed or snapshot.loading or snapshot.cutscene:
            self.native_p1_last_handle = 0
            self.native_p1_last_handle_time = 0.0
            self.native_p2_last_handle = 0
            self.native_p2_last_handle_time = 0.0
            self.native_p3_last_handle = 0
            self.native_p3_last_handle_time = 0.0
            self.p1_world_interaction_last = None
            self.p1_world_interaction_last_time = 0.0
        if not snapshot.camera_allowed:
            try:
                self.clear_d3d_target_rings()
            except OSError:
                pass
        print(
            "COOP_SESSION "
            f"reason={snapshot.reason} "
            f"leader={snapshot.leader_party_member_id} "
            f"p1_menu={int(p1_menu_allowed)} "
            f"p2_menu={int(p2_menu_allowed)} "
            f"p3_menu={int(p3_menu_allowed)} "
            f"native_p3_menu={int(session_p3_menu_allowed)} "
            f"ui_suppressed={int(bool(presentation_suppressed))} "
            f"camera={int(snapshot.camera_allowed)}",
            flush=True,
        )
        # Commit only after every idempotent close/clear step completed.  If a
        # loading, modal or KO rebuild invalidates one remote write, retaining
        # the old signature makes the next frame finish the same transition
        # instead of permanently accepting a half-closed P1/P2/P3 state.
        self.last_session_gate_signature = signature

    def _route_native_frontend_input(
        self,
        *,
        foreground: bool,
        snapshot: session_state.SessionSnapshot,
        p1_native_command_open: bool,
        now: float,
        p1: PlayerMenuInput,
        p2: PlayerMenuInput,
        p3: PlayerMenuInput,
    ) -> bool:
        party_menu_input = bool(
            self.native_party_menu_active and self.native_frontend_custom_suspended
        )
        modal_active = bool(snapshot.native_modal_active)
        follower_input = bool(
            party_menu_input or snapshot.native_menu_open or modal_active
        )

        follower_button = None
        if follower_input:
            follower_button = native_party_follower_button(
                p2_up=p2.character_up,
                p2_down=p2.character_down,
                p2_left=self.bindings.repeat("p2", "menu_left"),
                p2_right=self.bindings.repeat("p2", "menu_right"),
                p2_confirm=p2.confirm_edge,
                p2_cancel=p2.cancel_edge,
                p2_party_menu=p2.native_party_menu,
                p2_previous=p2.native_previous,
                p2_next=p2.native_next,
                p3_up=p3.character_up,
                p3_down=p3.character_down,
                p3_left=self.bindings.repeat("p3", "menu_left"),
                p3_right=self.bindings.repeat("p3", "menu_right"),
                p3_confirm=p3.confirm_edge,
                p3_cancel=p3.cancel_edge,
                p3_party_menu=p3.native_party_menu,
                p3_previous=p3.native_previous,
                p3_next=p3.native_next,
            )

        close_handoff = p1_frontend_close_handoff_active(
            suspended=self.native_frontend_custom_suspended,
            observed_open=self.native_frontend_observed_open,
            close_candidate_since=self.native_frontend_close_candidate_since,
            native_menu_open=snapshot.native_menu_open,
            native_modal_active=modal_active,
            user_control_enabled=snapshot.user_control_enabled,
            loading=snapshot.loading,
            cutscene=snapshot.cutscene,
            town=snapshot.town,
        )
        if close_handoff and p1.confirm_edge and self.p1_model.mode is MenuMode.CLOSED:
            self.p1_frontend_deferred_open_until = (
                now + P1_FRONTEND_DEFERRED_OPEN_SECONDS
            )
            self.p1_confirm_reopen_guard.arm(now)
            print("P1_FRONTEND_CONFIRM_DEFERRED", flush=True)

        if party_menu_input:
            if (
                snapshot.native_menu_open or snapshot.native_modal_active
            ) and follower_button is not None:
                player, button = follower_button
                self.native_dialog_bridge.pulse(button)
                print(
                    f"NATIVE_PARTY_MENU_INPUT source={player} button={button:#04x}",
                    flush=True,
                )
            return True
        if modal_active:
            if follower_button is not None:
                player, button = follower_button
                self.native_dialog_bridge.pulse(button)
                print(
                    f"NATIVE_FOLLOWER_MODAL_INPUT source={player} button={button:#04x}",
                    flush=True,
                )
            elif p2.up or p1.up:
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_UP)
            elif p2.down or p1.down:
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_DOWN)
            elif p1.confirm_edge:
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_CONFIRM)
            elif p1.cancel_edge:
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_CANCEL)
            return True
        if snapshot.native_menu_open:
            if p1_native_command_open:
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_CANCEL)
            elif follower_button is not None:
                player, button = follower_button
                self.native_dialog_bridge.pulse(button)
                print(
                    f"NATIVE_FOLLOWER_MENU_INPUT source={player} button={button:#04x}",
                    flush=True,
                )
            return True
        return bool(self.custom_ui_suppressed or self.native_frontend_custom_suspended)

    def _route_native_quickening_input(
        self,
        *,
        foreground: bool,
        p2_manual_control: bool,
        p3_manual_control: bool,
        block_mask: int | None = None,
    ) -> bool:
        active = bool(self.coop_runtime_active and self.native_quickening_active)
        if block_mask is None:
            snapshot = getattr(self, "session_snapshot", None)
            identity = getattr(snapshot, "identity", None)
            p2_occupied = bool(
                len(self.party) >= 2
                and (
                    identity is None
                    or identity.p2_party_member_id is not None
                )
            )
            p3_occupied = bool(
                len(self.party) >= 3
                and (
                    identity is None
                    or identity.p3_party_member_id is not None
                )
            )
            block_mask = quickening_button_block_mask(
                active=active,
                party_size=len(self.party),
                p2_manual_control=bool(p2_manual_control and p2_occupied),
                p3_manual_control=bool(p3_manual_control and p3_occupied),
            )
        if not active:
            return False
        if block_mask & QUICKENING_BLOCK_COMMAND and self.bindings.edge(
            "p2", "quickening"
        ):
            self.native_dialog_bridge.pulse(NATIVE_DIALOG_COMMAND)
            print("QUICKENING_INPUT source=p2 action=quickening", flush=True)
        elif (
            block_mask & QUICKENING_BLOCK_CONFIRM
            and self.bindings.edge("p3", "quickening")
        ):
            self.native_dialog_bridge.pulse(NATIVE_DIALOG_CONFIRM)
            print("QUICKENING_INPUT source=p3 action=quickening", flush=True)
        return True

    def _route_coop_disabled_input(
        self,
        *,
        foreground: bool,
        circle_decides: bool,
        snapshot: session_state.SessionSnapshot,
        p2_character_up: bool,
        p2_character_down: bool,
        p3_character_up: bool,
        p3_character_down: bool,
        native_party_menu_edges: dict[str, bool],
    ) -> None:
        self.cancel_p1_leader_handoff("coop-disabled")
        self._publish_native_input_contract(
            p1_replacement=False,
            non_p1_command_blocked=False,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=False,
            quickening_block_mask=0,
            leader_replacement=False,
            capture_desktop_events=False,
            suppress_mouse_look=False,
        )
        self.native_wait_mode_bridge.maintain(False)

        for model, close in (
            (self.p1_model, self.hard_close_p1_menu),
            (self.model, self.hard_close_p2_menu),
            (self.p3_model, self.hard_close_p3_menu),
        ):
            if model.mode is not MenuMode.CLOSED:
                close()

        button = native_party_follower_button(
            p2_up=p2_character_up,
            p2_down=p2_character_down,
            p2_left=self.bindings.repeat("p2", "menu_left"),
            p2_right=self.bindings.repeat("p2", "menu_right"),
            p2_confirm=self.bindings.native_action_edge(
                "p2", "confirm", circle_decides
            ),
            p2_cancel=self.bindings.native_action_edge(
                "p2", "cancel", circle_decides
            ),
            p2_party_menu=native_party_menu_edges["p2"],
            p2_previous=self.bindings.edge("p2", "target_previous"),
            p2_next=self.bindings.edge("p2", "target_next"),
            p3_up=p3_character_up,
            p3_down=p3_character_down,
            p3_left=self.bindings.repeat("p3", "menu_left"),
            p3_right=self.bindings.repeat("p3", "menu_right"),
            p3_confirm=self.bindings.native_action_edge(
                "p3", "confirm", circle_decides
            ),
            p3_cancel=self.bindings.native_action_edge(
                "p3", "cancel", circle_decides
            ),
            p3_party_menu=native_party_menu_edges["p3"],
            p3_previous=self.bindings.edge("p3", "target_previous"),
            p3_next=self.bindings.edge("p3", "target_next"),
        )
        if button is None:
            return

        _player, native_button = button
        frontend_open = bool(snapshot.native_menu_open or snapshot.native_modal_active)
        if native_button == NATIVE_DIALOG_CONFIRM and not frontend_open:
            native_button = NATIVE_DIALOG_COMMAND
        elif not frontend_open and native_button != NATIVE_DIALOG_PARTY_MENU:
            return
        self.native_dialog_bridge.pulse(native_button)

    def _route_native_pause_input(
        self,
        *,
        foreground: bool,
        circle_decides: bool,
        p2_character_up: bool,
        p2_character_down: bool,
        p3_character_up: bool,
        p3_character_down: bool,
        native_party_menu_edges: dict[str, bool],
    ) -> None:
        button = native_party_follower_button(
            p2_up=p2_character_up,
            p2_down=p2_character_down,
            p2_left=self.bindings.repeat("p2", "menu_left"),
            p2_right=self.bindings.repeat("p2", "menu_right"),
            p2_confirm=self.bindings.native_action_edge(
                "p2", "confirm", circle_decides
            ),
            p2_cancel=self.bindings.native_action_edge(
                "p2", "cancel", circle_decides
            ),
            p2_party_menu=native_party_menu_edges["p2"],
            p2_previous=self.bindings.edge("p2", "target_previous"),
            p2_next=self.bindings.edge("p2", "target_next"),
            p3_up=p3_character_up,
            p3_down=p3_character_down,
            p3_left=self.bindings.repeat("p3", "menu_left"),
            p3_right=self.bindings.repeat("p3", "menu_right"),
            p3_confirm=self.bindings.native_action_edge(
                "p3", "confirm", circle_decides
            ),
            p3_cancel=self.bindings.native_action_edge(
                "p3", "cancel", circle_decides
            ),
            p3_party_menu=native_party_menu_edges["p3"],
            p3_previous=self.bindings.edge("p3", "target_previous"),
            p3_next=self.bindings.edge("p3", "target_next"),
        )
        if button is None:
            return
        player, native_button = button
        self.native_dialog_bridge.pulse(native_button)
        print(
            f"NATIVE_FOLLOWER_PAUSE_INPUT source={player} button={native_button:#04x}",
            flush=True,
        )

    def _world_interactions(self) -> WorldInteractions:
        p1 = (
            self.native_p1_interaction()
            if self.p1_model.mode is MenuMode.CLOSED
            else None
        )
        p2 = (
            self.private_p2_interaction()
            if self.model.mode is MenuMode.CLOSED
            else None
        )
        if p2 is None and self.model.mode is MenuMode.CLOSED:
            p2 = self.native_interaction_router.prompt_interaction(2)
        p3 = (
            self.private_p3_interaction()
            if self.p3_model.mode is MenuMode.CLOSED
            else None
        )
        if p3 is None and self.p3_model.mode is MenuMode.CLOSED:
            p3 = self.native_interaction_router.prompt_interaction(3)
        if interaction_matches_live_foe(p1, self.p1_foes):
            p1 = None
        if interaction_matches_live_foe(p2, self.foes):
            p2 = None
        if interaction_matches_live_foe(p3, self.p3_foes):
            p3 = None

        return WorldInteractions(
            p1=p1,
            p2=p2,
            p3=p3,
            p1_ready=bool(p1 is not None and p1[0] >= 2),
            p2_present=bool(p2 is not None and p2[0] >= 2),
            p3_present=bool(p3 is not None and p3[0] >= 2),
        )

    def _release_native_wait_presentation_for_treasure(
        self,
        interaction: tuple[int, int, int, int] | None,
    ) -> None:
        if not self.interaction_is_treasure(interaction):
            return
        if not any(
            model.mode is not MenuMode.CLOSED
            for model in (self.p1_model, self.model, self.p3_model)
        ):
            return

        self.native_wait_mode_bridge.maintain(
            True,
            native_presentation=False,
        )

    def _handle_p1_battle_input(
        self,
        *,
        now: float,
        actor: int,
        native_open: bool,
        binding_enabled: bool,
        custom_menu_allowed: bool,
        confirm_reopen_ready: bool,
        controls: PlayerMenuInput,
        interactions: WorldInteractions,
    ) -> bool:
        if native_open:
            self.native_dialog_bridge.pulse(NATIVE_DIALOG_CANCEL)
            return False
        if not binding_enabled:
            return False
        if self.p1_model.mode is not MenuMode.CLOSED:
            if not custom_menu_allowed:
                return False
            if self.p1_leader_selector_active():
                # This is a rootless modal, not an ordinary action TARGET page.
                # Cancel uses its transactional close path. Horizontal aliases
                # remain inert here; P1 LB is independently left native so the
                # game can still toggle Turbo while this selector is visible.
                if controls.cancel:
                    self.close_p1_leader_selector()
                    return False
                if controls.confirm:
                    self.dispatch_p1(InputCommand.CONFIRM)
                    return False
                if controls.up and not controls.down:
                    self.dispatch_p1(vertical_input_command(-1, controls.up_edge))
                elif controls.down and not controls.up:
                    self.dispatch_p1(vertical_input_command(1, controls.down_edge))
                return False
            if controls.page_up and not controls.page_down:
                self.dispatch_p1(InputCommand.PAGE_UP)
            elif controls.page_down and not controls.page_up:
                self.dispatch_p1(InputCommand.PAGE_DOWN)
            elif controls.up:
                self.dispatch_p1(vertical_input_command(-1, controls.up_edge))
            elif controls.down:
                self.dispatch_p1(vertical_input_command(1, controls.down_edge))
            if controls.target_group:
                self.dispatch_p1(InputCommand.CYCLE_TARGET_GROUP)
            if controls.confirm:
                self.dispatch_p1(InputCommand.CONFIRM)
            if controls.cancel:
                self.dispatch_p1(InputCommand.BACK)
            return False
        if not controls.confirm:
            return False
        if not confirm_reopen_ready:
            return True
        if interactions.p1_ready and interactions.p1 is not None:
            self._release_native_wait_presentation_for_treasure(interactions.p1)
            self.native_interaction_router.trigger_native_p1(
                interactions.p1[1],
                owner_actor=actor,
            )
            return True
        if custom_menu_allowed and now >= self.p1_menu_reopen_allowed_at:
            self.dispatch_p1(InputCommand.TOGGLE)
        return False

    def _handle_p3_battle_input(
        self,
        *,
        input_role_generation: int,
        now: float,
        actor: int,
        leader_actor: int,
        binding_enabled: bool,
        custom_menu_allowed: bool,
        confirm_reopen_ready: bool,
        controls: PlayerMenuInput,
        interactions: WorldInteractions,
        battle_menu_edge: bool = False,
    ) -> bool:
        if not binding_enabled:
            return False
        if self.p3_model.mode is MenuMode.CLOSED:
            if battle_menu_edge:
                # FF12's keyboard layout distinguishes Square/Battle Menu from
                # Cross/Confirm.  Keep that distinction here: F may open P3's
                # custom command tree, but it must never interact with the
                # world or become a confirm after the tree is already open.
                if (
                    custom_menu_allowed
                    and not self.native_interaction_router.active
                    and now >= self.p3_menu_reopen_allowed_at
                ):
                    self.dispatch_p3(InputCommand.TOGGLE)
                    return True
                return False
            if custom_menu_allowed and controls.character_direction_ready and (
                controls.character_up or controls.character_down
            ):
                # The opening direction belongs to the transition into the
                # selector.  Advancing here makes the very first press skip
                # the actor that the selector deliberately anchors to.
                self.open_p3_character_selector()
                return True
            if not controls.confirm:
                return False
            if self.native_interaction_router.active:
                return True
            if (
                interactions.p3_present
                and not self.interaction_is_treasure(interactions.p3)
                and not self.p3_world_interaction_allowed()
            ):
                print(
                    "P3_INTERACTION_REJECTED_LEADER_ANIMATION",
                    flush=True,
                )
                return True
            if interactions.p3_present and interactions.p3 is not None:
                self._release_native_wait_presentation_for_treasure(interactions.p3)
                try:
                    block = legacy.coop.third_player_block()
                    source_handle = self.process.u32(
                        block + legacy.coop.P3_LOOT_HANDLE_OFFSET
                    )
                    private = self.process.u64(
                        block + legacy.coop.P3_INTERACTION_STATE_POINTER_OFFSET
                    )
                except (OSError, RuntimeError, struct.error):
                    return True
                if interactions.p3[3] < 0:
                    self.native_interaction_router.trigger_atomic_follower_npc(
                        owner_actor=actor,
                        source_handle=source_handle,
                        interaction_owner=INTERACTION_OWNER_P3,
                    )
                else:
                    self.native_interaction_router.trigger(
                        interactions.p3[1],
                        interactions.p3[3],
                        owner_actor=actor,
                        leader_actor=leader_actor,
                        source_handle=source_handle,
                        private_state_pointer=private,
                        interaction_owner=INTERACTION_OWNER_P3,
                    )
                return True
            try:
                p3_handle = self.process.u32(
                    legacy.coop.third_player_block()
                    + legacy.coop.P3_LOOT_HANDLE_OFFSET
                )
            except (OSError, RuntimeError, struct.error):
                p3_handle = 0
            if self.native_interaction_router.trigger_atomic_follower_npc(
                owner_actor=actor,
                source_handle=p3_handle,
                interaction_owner=INTERACTION_OWNER_P3,
            ):
                return True
            if (
                custom_menu_allowed
                and now >= self.p3_menu_reopen_allowed_at
                and confirm_reopen_ready
            ):
                self.dispatch_p3(InputCommand.TOGGLE)
            return False

        if not custom_menu_allowed:
            return False
        if self.p3_character_selector_active():
            # The character selector is a rootless vertical modal.  Do not let
            # page, horizontal or target-group aliases mutate it underneath
            # its transactional lifecycle.
            if controls.cancel:
                self.close_p3_character_selector()
                return True
            if controls.confirm:
                self.dispatch_p3(InputCommand.CONFIRM)
                return self.role_generation != input_role_generation
            if controls.up and not controls.down:
                self.dispatch_p3(vertical_input_command(-1, controls.up_edge))
            elif controls.down and not controls.up:
                self.dispatch_p3(vertical_input_command(1, controls.down_edge))
            return False
        if controls.page_up and not controls.page_down:
            self.dispatch_p3(InputCommand.PAGE_UP)
        elif controls.page_down and not controls.page_up:
            self.dispatch_p3(InputCommand.PAGE_DOWN)
        elif controls.up:
            self.dispatch_p3(vertical_input_command(-1, controls.up_edge))
        elif controls.down:
            self.dispatch_p3(vertical_input_command(1, controls.down_edge))
        if controls.target_group_direction < 0:
            self.dispatch_p3(InputCommand.CYCLE_TARGET_GROUP_PREVIOUS)
        elif controls.target_group_direction > 0 or controls.target_group:
            self.dispatch_p3(InputCommand.CYCLE_TARGET_GROUP)
        if controls.confirm:
            self.dispatch_p3(InputCommand.CONFIRM)
            if self.role_generation != input_role_generation:
                return True
        if controls.cancel:
            self.dispatch_p3(InputCommand.BACK)
        return False

    def _handle_p2_battle_input(
        self,
        *,
        input_role_generation: int,
        now: float,
        actor: int,
        leader_actor: int,
        binding_enabled: bool,
        custom_menu_allowed: bool,
        confirm_reopen_ready: bool,
        interaction_consumed: bool,
        controls: PlayerMenuInput,
        interactions: WorldInteractions,
        battle_menu_edge: bool = False,
    ) -> None:
        if not binding_enabled:
            return
        if self.model.mode is MenuMode.CLOSED:
            if battle_menu_edge:
                # This is the keyboard Square/Battle Menu semantic, not
                # Confirm.  In particular it must not activate a nearby NPC,
                # treasure or an already-open menu item.
                if (
                    custom_menu_allowed
                    and not self.native_interaction_router.active
                    and now >= self.menu_reopen_allowed_at
                ):
                    self.dispatch(InputCommand.TOGGLE)
                return
            if self.native_interaction_router.active:
                return
            if custom_menu_allowed and controls.character_direction_ready and (
                controls.character_up or controls.character_down
            ):
                # Match P1/P3 and the wheel route: opening starts on the
                # current owner; only a later repeat/press may navigate.
                self.open_p2_character_selector()
                return
            if not controls.confirm:
                return
            if interaction_consumed:
                return
            if (
                interactions.p2_present
                and not self.interaction_is_treasure(interactions.p2)
                and not self.p2_world_interaction_allowed()
            ):
                print(
                    "COOP_INTERACTION_REJECTED_LEADER_ANIMATION",
                    flush=True,
                )
                return
            if interactions.p2_present and interactions.p2 is not None:
                self._release_native_wait_presentation_for_treasure(interactions.p2)
                if interactions.p2[3] < 0:
                    try:
                        p2_handle = self.process.u32(
                            legacy.coop.COMMAND_BLOCK
                            + legacy.coop.P2_LOOT_HANDLE_OFFSET
                        )
                    except (OSError, RuntimeError, struct.error):
                        p2_handle = 0
                    self.native_interaction_router.trigger_atomic_follower_npc(
                        owner_actor=actor,
                        source_handle=p2_handle,
                        interaction_owner=INTERACTION_OWNER_P2,
                    )
                else:
                    self.native_interaction_router.trigger(
                        interactions.p2[1],
                        interactions.p2[3],
                        owner_actor=actor,
                        leader_actor=leader_actor,
                    )
                return
            try:
                p2_handle = self.process.u32(
                    legacy.coop.COMMAND_BLOCK + legacy.coop.P2_LOOT_HANDLE_OFFSET
                )
            except (OSError, RuntimeError, struct.error):
                p2_handle = 0
            if self.native_interaction_router.trigger_atomic_follower_npc(
                owner_actor=actor,
                source_handle=p2_handle,
                interaction_owner=INTERACTION_OWNER_P2,
            ):
                return
            if now < self.menu_reopen_allowed_at:
                return
            if custom_menu_allowed and confirm_reopen_ready:
                self.dispatch(InputCommand.TOGGLE)
            return

        if not custom_menu_allowed:
            return
        if self.p2_character_selector_active():
            # Match P1/P3: this synthetic selector accepts only Cancel,
            # Confirm and one unambiguous vertical action per frame.
            if controls.cancel:
                self.close_p2_character_selector()
                return
            if controls.confirm:
                self.dispatch(InputCommand.CONFIRM)
                return
            if controls.up and not controls.down:
                self.dispatch(vertical_input_command(-1, controls.up_edge))
            elif controls.down and not controls.up:
                self.dispatch(vertical_input_command(1, controls.down_edge))
            return
        if controls.page_up and not controls.page_down:
            self.dispatch(InputCommand.PAGE_UP)
        elif controls.page_down and not controls.page_up:
            self.dispatch(InputCommand.PAGE_DOWN)
        elif controls.up:
            self.dispatch(vertical_input_command(-1, controls.up_edge))
        elif controls.down:
            self.dispatch(vertical_input_command(1, controls.down_edge))
        if controls.target_group_direction < 0:
            self.dispatch(InputCommand.CYCLE_TARGET_GROUP_PREVIOUS)
        elif controls.target_group_direction > 0 or controls.target_group:
            self.dispatch(InputCommand.CYCLE_TARGET_GROUP)
        if controls.confirm:
            self.dispatch(InputCommand.CONFIRM)
            if self.role_generation != input_role_generation:
                return
        if controls.cancel:
            self.dispatch(InputCommand.BACK)

    def _desktop_owner(self) -> str | None:
        owner_reader = getattr(self.bindings, "desktop_owner", None)
        owner = owner_reader() if callable(owner_reader) else None
        if owner not in ("p1", "p2", "p3"):
            return None
        ownership_check = getattr(self.bindings, "owns_desktop", None)
        if callable(ownership_check) and not ownership_check(owner):
            return None
        return owner

    def _clear_native_desktop_route(self) -> None:
        self.native_desktop_capture_owner = None
        self.native_desktop_capture_epoch = None
        wheel_gate = getattr(self, "native_desktop_wheel_gate", None)
        if wheel_gate is not None:
            wheel_gate.reset()
        pending = getattr(self, "native_desktop_pending_events", None)
        if pending is not None:
            pending.clear()

    def _collect_native_desktop_events(
        self,
        *,
        foreground: bool,
        desktop_owner: str | None,
        expected_generation: int | None = None,
    ) -> tuple[NativeDesktopInputEvent, ...]:
        pending = getattr(self, "native_desktop_pending_events", None)
        if pending is None:
            pending = deque()
            self.native_desktop_pending_events = pending
        # This deque is retained as a compatibility/debug surface only. Events
        # are never carried across frames or menu lifecycles.
        pending.clear()
        captured_owner = getattr(self, "native_desktop_capture_owner", None)
        route_valid = bool(
            foreground
            and desktop_owner is not None
            and captured_owner == desktop_owner
        )
        drain = getattr(self.native_dialog_bridge, "drain_desktop_events", None)
        try:
            events = drain() if callable(drain) else ()
            overflow = int(
                getattr(self.native_dialog_bridge, "event_overflow_count", 0)
            )
        except (OSError, struct.error, ValueError) as error:
            # The native desktop ring is supplemental to BindingPoller's direct
            # controller samples.  A torn/temporarily unreadable consumer must
            # not abort this frame before P1/P2/P3 gamepads are dispatched.
            # The remote consumer remains uncommitted.  Force a new policy
            # generation before the next drain so those unread records are
            # consumed but rejected as belonging to the failed route; no old
            # gesture may replay after I/O recovery.
            self.native_desktop_route_signature = None
            self._report_frame_subsystem_failure("desktop-event-broker", error)
            return ()
        self._clear_frame_subsystem_failure("desktop-event-broker")
        if overflow and not int(
            getattr(self, "native_desktop_overflow_reported", 0)
        ):
            self.native_desktop_overflow_reported = overflow
            print(f"NATIVE_DESKTOP_INPUT_OVERFLOW count={overflow}", flush=True)

        if not route_valid:
            return ()

        generation_reader = getattr(
            self.native_dialog_bridge,
            "committed_policy_generation",
            None,
        )
        if callable(generation_reader):
            committed_generation = int(generation_reader()) & 0xFFFFFFFF
        else:
            committed_generation = int(
                getattr(self.native_dialog_bridge, "policy_generation", 0)
            ) & 0xFFFFFFFF
        if expected_generation is not None and committed_generation != (
            int(expected_generation) & 0xFFFFFFFF
        ):
            return ()
        return tuple(
            event
            for event in events
            if event.kind in (NATIVE_INPUT_EVENT_COMMAND, NATIVE_INPUT_EVENT_WHEEL)
            and int(event.policy_generation) == committed_generation
        )

    def _publish_native_input_contract(self, **contract) -> NativeInputPolicy | None:
        publisher = getattr(self.native_dialog_bridge, "publish_contract", None)
        if callable(publisher):
            return publisher(**contract)

        # Compatibility for narrow unit doubles.  Production always takes the
        # single inactive-slot publication path above.
        setter_names = {
            "p1_replacement": "set_p1_replacement",
            "non_p1_command_blocked": "set_non_p1_command_blocked",
            "custom_directional_owner": "set_custom_directional_owner",
            "p1_menu_owns_lb": "set_p1_menu_owns_lb",
            "party_menu_blocked": "set_party_menu_blocked",
            "quickening_block_mask": "set_quickening_block_mask",
            "leader_replacement": "set_leader_replacement",
            "capture_desktop_events": "set_desktop_events_enabled",
            "suppress_mouse_look": "set_mouse_look_suppressed",
        }
        for field, setter_name in setter_names.items():
            setter = getattr(self.native_dialog_bridge, setter_name, None)
            if field in contract and callable(setter):
                setter(contract[field])
        return None

    def _route_desktop_wheel_step(
        self,
        owner: str,
        direction: int,
        *,
        p1_enabled: bool,
        p2_enabled: bool,
        p3_enabled: bool,
    ) -> bool:
        command = InputCommand.UP if int(direction) < 0 else InputCommand.DOWN
        if owner == "p1" and p1_enabled:
            if self.p1_model.mode is MenuMode.CLOSED:
                self.open_p1_leader_selector()
                return self.p1_model.mode is not MenuMode.CLOSED
            if self.p1_model.mode is not MenuMode.CLOSED:
                self.dispatch_p1(command)
                return True
        elif owner == "p2" and p2_enabled:
            if self.model.mode is MenuMode.CLOSED:
                self.open_p2_character_selector()
                return self.model.mode is not MenuMode.CLOSED
            if self.model.mode is not MenuMode.CLOSED:
                self.dispatch(command)
                return True
        elif owner == "p3" and p3_enabled:
            if self.p3_model.mode is MenuMode.CLOSED:
                self.open_p3_character_selector()
                return self.p3_model.mode is not MenuMode.CLOSED
            if self.p3_model.mode is not MenuMode.CLOSED:
                self.dispatch_p3(command)
                return True
        return False

    def input(
        self,
        foreground: bool,
        *,
        native_pause_open: bool | None = None,
    ) -> None:
        input_role_generation = self.role_generation
        # Poll every assigned controller in the background, but never consult
        # GetAsyncKeyState unless FF12 owns the foreground.  This keeps the
        # requested pad-only background control without stealing desktop keys.
        self.bindings.begin_frame(True, keyboard_enabled=foreground)
        desktop_owner = self._desktop_owner()
        p1_assignment = self.bindings.effective_gamepad_assignment("p1")
        p2_assignment = self.bindings.effective_gamepad_assignment("p2")
        p3_assignment = self.bindings.effective_gamepad_assignment("p3")
        try:
            self.native_dialog_bridge.set_p1_controller(
                p1_assignment,
                publish=False,
            )
        except TypeError:
            self.native_dialog_bridge.set_p1_controller(p1_assignment)
        gamepad_slot_mask = native_gamepad_slot_mask(
            p1_assignment,
            p2_assignment,
            p3_assignment,
        )
        stage_gamepad_slots = getattr(
            self.native_dialog_bridge,
            "set_native_gamepad_slot_mask",
            None,
        )
        if callable(stage_gamepad_slots):
            try:
                stage_gamepad_slots(gamepad_slot_mask, publish=False)
            except TypeError:
                stage_gamepad_slots(gamepad_slot_mask)
        input_now = time.monotonic()
        self.maintain_p1_leader_handoff(input_now)
        if input_now >= self.native_decide_button_refresh_at:
            self.native_decide_button = native_decide_button(
                self.process,
                self.native_decide_button,
            )
            self.native_decide_button_refresh_at = (
                input_now + NATIVE_DECIDE_BUTTON_REFRESH_SECONDS
            )
        circle_decides = self.native_decide_button == NATIVE_DECIDE_BUTTON_CIRCLE
        p1_confirm_edge = self.bindings.native_action_edge(
            "p1",
            "confirm",
            circle_decides,
            excluded_keyboard_vks=(0x01,),
        )
        fast_scroll = {
            player: bool(
                model.mode is MenuMode.LIST
                and self.bindings.down(player, "native_party_menu")
            )
            for player, model in (
                ("p1", self.p1_model),
                ("p2", self.model),
                ("p3", self.p3_model),
            )
        }
        # Sample P1 directional histories before every early-return gate.
        # Otherwise a direction held through Pause/native frontend suspension
        # looks like a fresh edge/repeat when the custom route resumes.
        p1_direction_repeat = {
            action: (
                self.bindings.repeat("p1", action, accelerated=True)
                if fast_scroll["p1"] and action in ("menu_up", "menu_down")
                else self.bindings.repeat("p1", action)
            )
            for action in ("menu_up", "menu_down", "menu_left", "menu_right")
        }
        p1_direction_edge = {
            action: self.bindings.edge("p1", action)
            for action in ("menu_up", "menu_down")
        }
        p2_direction_edge = {
            action: self.bindings.edge("p2", action)
            for action in ("menu_up", "menu_down")
        }
        p3_direction_edge = {
            action: self.bindings.edge("p3", action)
            for action in ("menu_up", "menu_down")
        }

        p2_character_dpad_up = (
            self.bindings.repeat("p2", "menu_up", accelerated=True)
            if fast_scroll["p2"]
            else self.bindings.repeat("p2", "menu_up")
        )
        p2_character_dpad_down = (
            self.bindings.repeat("p2", "menu_down", accelerated=True)
            if fast_scroll["p2"]
            else self.bindings.repeat("p2", "menu_down")
        )
        p3_character_dpad_up = (
            self.bindings.repeat("p3", "menu_up", accelerated=True)
            if fast_scroll["p3"]
            else self.bindings.repeat("p3", "menu_up")
        )
        p3_character_dpad_down = (
            self.bindings.repeat("p3", "menu_down", accelerated=True)
            if fast_scroll["p3"]
            else self.bindings.repeat("p3", "menu_down")
        )
        p2_character_direction_down = bool(
            self.bindings.down("p2", "menu_up") or self.bindings.down("p2", "menu_down")
        )
        p3_character_direction_down = bool(
            self.bindings.down("p3", "menu_up") or self.bindings.down("p3", "menu_down")
        )

        native_party_menu_edges = {
            player: self.bindings.edge(player, "native_party_menu")
            for player in ("p1", "p2", "p3")
        }
        # Triangle/Y is a stock fast-scroll modifier while a battle list owns
        # input.  Consume its edge here so the same press cannot also request
        # the native Party/Licences screen underneath the custom menu.
        for player, active in fast_scroll.items():
            if active:
                native_party_menu_edges[player] = False
        battle_menu_edges = {
            player: self.bindings.edge(player, "battle_menu")
            for player in ("p2", "p3")
        }
        native_pause_edges = {
            player: self.bindings.edge(player, "native_pause")
            for player in ("p1", "p2", "p3")
        }
        self.p1_controller.presenter.set_target_prompts(
            *self.bindings.target_prompt_pair("p1")
        )
        self.controller.presenter.set_target_prompts(
            *self.bindings.target_prompt_pair("p2")
        )
        self.p3_controller.presenter.set_target_prompts(
            *self.bindings.target_prompt_pair("p3")
        )
        snapshot = self.session_snapshot
        manual_control_mask = legacy.coop.read_manual_control_mask(self.process)
        p2_manual_control_enabled = bool(
            manual_control_mask & legacy.coop.P2_MANUAL_CONTROL_BIT
        )
        p3_manual_control_enabled = bool(
            manual_control_mask & legacy.coop.P3_MANUAL_CONTROL_BIT
        )
        if self.p2_manual_control_enabled and not p2_manual_control_enabled:
            self.hard_close_p2_menu()
            print("P2_MENU_DISABLED manual-control=off", flush=True)
        if self.p3_manual_control_enabled and not p3_manual_control_enabled:
            self.hard_close_p3_menu()
            print("P3_MENU_DISABLED manual-control=off", flush=True)
        self.p2_manual_control_enabled = p2_manual_control_enabled
        self.p3_manual_control_enabled = p3_manual_control_enabled
        if native_pause_open is None:
            native_pause_open = native_pause_screen_open(self.process)
        if native_pause_open:
            self.cancel_native_party_menu_request(
                "native_pause",
                publish_policy=False,
            )
            self._publish_native_input_contract(
                p1_replacement=False,
                non_p1_command_blocked=False,
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                party_menu_blocked=False,
                quickening_block_mask=0,
                leader_replacement=False,
                capture_desktop_events=False,
                suppress_mouse_look=False,
            )
            self._clear_native_desktop_route()
            follower_pause_sources = tuple(
                player for player in ("p2", "p3") if native_pause_edges[player]
            )
            p1_physical_pause = bool(native_pause_edges["p1"])
            if follower_pause_sources and not p1_physical_pause:
                # P1's physical Start remains on FF12's stock path.  The
                # follower keyboard is deliberately filtered, so Esc must be
                # returned through the already-proven native Pause semantic in
                # order to close the same screen without double-pulsing P1.
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_PAUSE)
                print(
                    "NATIVE_PAUSE_PULSED "
                    f"source={'+'.join(follower_pause_sources)} "
                    f"button={NATIVE_DIALOG_PAUSE:#04x}",
                    flush=True,
                )
            else:
                self._route_native_pause_input(
                    foreground=foreground,
                    circle_decides=circle_decides,
                    p2_character_up=p2_character_dpad_up,
                    p2_character_down=p2_character_dpad_down,
                    p3_character_up=p3_character_dpad_up,
                    p3_character_down=p3_character_dpad_down,
                    native_party_menu_edges=native_party_menu_edges,
                )
            if self.p1_model.mode is not MenuMode.CLOSED:
                self.hard_close_p1_menu()
            if self.model.mode is not MenuMode.CLOSED:
                self.hard_close_p2_menu()
            if self.p3_model.mode is not MenuMode.CLOSED:
                self.hard_close_p3_menu()
            if not self.p1_pause_suspended:
                self.p1_pause_suspended = True
                print("P1_HEADLESS_MENU SUSPENDED_BY_PAUSE", flush=True)
            return

        if not self.coop_runtime_active:
            self._publish_native_input_contract(
                p1_replacement=False,
                non_p1_command_blocked=False,
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                party_menu_blocked=False,
                quickening_block_mask=0,
                leader_replacement=False,
                capture_desktop_events=False,
                suppress_mouse_look=False,
            )
            self._clear_native_desktop_route()
            self._route_coop_disabled_input(
                foreground=foreground,
                circle_decides=circle_decides,
                snapshot=snapshot,
                p2_character_up=p2_character_dpad_up,
                p2_character_down=p2_character_dpad_down,
                p3_character_up=p3_character_dpad_up,
                p3_character_down=p3_character_dpad_down,
                native_party_menu_edges=native_party_menu_edges,
            )
            return

        native_pause_sources = tuple(
            player
            # P1 Start is deliberately never masked by the resident semantic
            # firewall and reaches FF12 physically.  Only follower controls are
            # filtered and therefore require a synthetic native Pause pulse.
            for player in ("p2", "p3")
            if native_pause_edges[player]
        )
        p1_physical_pause = bool(native_pause_edges["p1"])
        if (
            (p1_physical_pause or native_pause_sources)
            and native_pause_request_allowed(snapshot)
        ):
            pause_visibility_gate = getattr(
                self,
                "native_pause_visibility_gate",
                None,
            )
            if pause_visibility_gate is not None:
                pause_visibility_gate.arm(now=input_now, foreground=foreground)
            self._publish_native_input_contract(
                # A synthetic Pause pulse is evaluated before these masks.
                # Preserve replacement firewalls until native_pause_open is
                # observed on a later frame, then the branch above publishes
                # the intentional all-native Pause contract.
                p1_replacement=True,
                non_p1_command_blocked=True,
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                party_menu_blocked=True,
                quickening_block_mask=0,
                leader_replacement=True,
                capture_desktop_events=False,
                suppress_mouse_look=False,
            )
            self._clear_native_desktop_route()
            self.native_wait_mode_bridge.maintain(False)
            self.hard_close_p1_menu()
            self.hard_close_p2_menu()
            self.hard_close_p3_menu()
            if native_pause_sources and not p1_physical_pause:
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_PAUSE)
                print(
                    "NATIVE_PAUSE_PULSED "
                    f"source={'+'.join(native_pause_sources)} "
                    f"button={NATIVE_DIALOG_PAUSE:#04x}",
                    flush=True,
                )
            else:
                sources = "+".join(("p1", *native_pause_sources))
                print(f"NATIVE_PAUSE_PHYSICAL source={sources}", flush=True)
            return

        p1_actor = self.party[0][2] if self.party else 0
        p2_actor = self.party[1][2] if len(self.party) >= 2 else 0
        p3_actor = self.party[2][2] if len(self.party) >= 3 else 0
        p1_actor_alive = controlled_actor_alive(self.process, p1_actor)
        p2_actor_alive = controlled_actor_alive(self.process, p2_actor)
        p3_actor_alive = controlled_actor_alive(self.process, p3_actor)
        p1_leader_available = leader_control_availability(self.process, p1_actor)
        p2_actor_available = leader_control_availability(self.process, p2_actor)
        p3_actor_available = leader_control_availability(self.process, p3_actor)
        p1_owner_member_id = getattr(
            snapshot,
            "p1_owner_party_member_id",
            snapshot.identity.p1_party_member_id,
        )
        native_leader_actor = native_leader_actor_for(
            self.active_party,
            snapshot.leader_party_member_id,
        )
        native_leader_available = leader_control_availability(
            self.process,
            native_leader_actor,
        )
        leader_gate_was_pending = self.dead_leader_menu_gate.pending
        leader_selection_pending = self.dead_leader_menu_gate.update(
            owner_alive=p1_leader_available,
            owner_member_id=p1_owner_member_id,
            native_leader_member_id=snapshot.leader_party_member_id,
            native_leader_alive=native_leader_available,
            user_control_enabled=snapshot.user_control_enabled,
        )
        if leader_selection_pending:
            if self.native_party_menu_request_pending:
                self.cancel_native_party_menu_request("leader_reselection")
            any_menu_open = any(
                model.mode is not MenuMode.CLOSED
                for model in (self.p1_model, self.model, self.p3_model)
            )
            any_transition = any(
                transition is not None
                for transition in (
                    self.p1_transition_kind,
                    self.transition_kind,
                    self.p3_transition_kind,
                )
            )
            if not leader_gate_was_pending or any_menu_open or any_transition:
                self.hard_close_p1_menu()
                self.hard_close_p2_menu()
                self.hard_close_p3_menu()
            self.native_wait_mode_bridge.maintain(False)
            if not leader_gate_was_pending:
                print(
                    "ALL_HEADLESS_MENUS_CLOSED leader=dead waiting=native_reselection",
                    flush=True,
                )
        elif leader_gate_was_pending:
            print(
                "HEADLESS_MENUS_REENABLED leader=reselected",
                flush=True,
            )
        active_actor_alive = (p1_actor_alive,)
        if snapshot.identity.p2_party_member_id is not None:
            active_actor_alive += (p2_actor_alive,)
        if snapshot.identity.p3_party_member_id is not None:
            active_actor_alive += (p3_actor_alive,)
        active_party_dead = native_party_recovery_required(active_actor_alive)
        recovery_was_latched = bool(
            getattr(self, "native_dead_party_recovery_passthrough", False)
        )
        if active_party_dead and not recovery_was_latched:
            self.native_dead_party_recovery_observed_open = False
        if snapshot.native_menu_open and recovery_was_latched:
            self.native_dead_party_recovery_observed_open = True
        dead_party_recovery = native_party_recovery_passthrough_required(
            active_actor_alive,
            latched=recovery_was_latched,
            native_menu_observed=bool(
                getattr(
                    self,
                    "native_dead_party_recovery_observed_open",
                    False,
                )
            ),
            native_menu_open=bool(snapshot.native_menu_open),
            native_frontend_suspended=bool(
                self.native_frontend_custom_suspended
            ),
            suspension_source=str(self.native_frontend_suspension_source),
        )
        if dead_party_recovery:
            if self.native_party_menu_request_pending:
                self.cancel_native_party_menu_request(
                    "dead_party_recovery",
                    publish_policy=False,
                )
            if snapshot.native_menu_open:
                self.native_dead_party_recovery_observed_open = True
                self.suspend_custom_menus_for_native_frontend(
                    "dead_party_recovery",
                    now=input_now,
                )
                self.native_frontend_observed_open = True
                self.native_frontend_close_candidate_since = 0.0
                self.native_party_menu_active = True
            self._publish_native_input_contract(
                p1_replacement=False,
                non_p1_command_blocked=False,
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                party_menu_blocked=False,
                quickening_block_mask=0,
                leader_replacement=False,
                capture_desktop_events=False,
                suppress_mouse_look=False,
                native_gamepad_slot_mask=gamepad_slot_mask,
                force=not bool(
                    getattr(self, "native_dead_party_recovery_passthrough", False)
                ),
            )
            self._clear_native_desktop_route()
            self.native_wait_mode_bridge.maintain(False)
            if not bool(
                getattr(self, "native_dead_party_recovery_passthrough", False)
            ):
                print(
                    "NATIVE_PARTY_RECOVERY_INPUT_RELEASED active_party=dead",
                    flush=True,
                )
            self.native_dead_party_recovery_passthrough = True
            return
        if bool(getattr(self, "native_dead_party_recovery_passthrough", False)):
            self.native_dead_party_recovery_passthrough = False
            self.native_dead_party_recovery_observed_open = False
            print("NATIVE_PARTY_RECOVERY_INPUT_RECLAIMED", flush=True)
        native_party_menu_now = time.monotonic()
        if (
            snapshot.native_menu_open
            and not self.native_party_menu_request_pending
            and not self.native_party_menu_active
            and native_party_menu_edges["p1"]
        ):
            source = "p1"
            self.suspend_custom_menus_for_native_frontend(
                source, now=native_party_menu_now
            )
            self.native_frontend_observed_open = True
            self.native_party_menu_active = True
            if snapshot.battle_info:
                self.party_edit_capture.begin(source, battle_info=snapshot.battle_info)
            print(
                f"NATIVE_PARTY_MENU_ADOPTED source={source}",
                flush=True,
            )
        if (
            not self.native_party_menu_request_pending
            and not leader_selection_pending
            and not self.native_frontend_custom_suspended
            and (not self.custom_ui_suppressed or snapshot.town)
        ):
            sources = tuple(
                player
                for player, pressed in native_party_menu_edges.items()
                if pressed
                and not (
                    player == "p1"
                    and p1_native_party_request_blocked(
                        self.p1_model.mode,
                        self.p1_transition_kind,
                        p1_confirm_edge,
                    )
                )
                and native_party_menu_source_allowed(
                    snapshot,
                    player,
                    len(self.party),
                    coop_active=self.coop_runtime_active,
                    p2_manual_control=p2_manual_control_enabled,
                    p3_manual_control=p3_manual_control_enabled,
                )
            )
            if sources:
                self.request_native_party_menu("+".join(sources), native_party_menu_now)
        if self.maintain_native_party_menu_request(
            foreground=foreground,
            snapshot=snapshot,
            native_pause_open=native_pause_open,
            now=native_party_menu_now,
        ):
            return
        native_command_lifecycle = (
            self.p1_native_lifecycle() if self.p1_visual_replacement else None
        )
        wait_presentation_owned = (
            self.native_wait_mode_bridge.owns_native_presentation()
        )
        unexpected_native_command_open = bool(
            not wait_presentation_owned
            and native_command_lifecycle is not None
            and native_command_lifecycle[2] == native_command_lifecycle[0]
        )
        trusted_native_menu = self.native_frontend_input_trusted(snapshot)
        unproven_native_menu = bool(
            snapshot.native_menu_open
            and not trusted_native_menu
            and not snapshot.native_modal_active
            and not snapshot.town
            and not getattr(snapshot, "chocobo_mode_active", False)
            and not self.native_quickening_active
        )
        if unproven_native_menu:
            quarantine = bool(
                unexpected_native_command_open
                or self.native_frontend_quarantine_required(native_party_menu_now)
            )
            self._publish_native_input_contract(
                p1_replacement=True,
                non_p1_command_blocked=True,
                custom_directional_owner=False,
                p1_menu_owns_lb=False,
                party_menu_blocked=True,
                quickening_block_mask=0,
                leader_replacement=True,
                capture_desktop_events=False,
                suppress_mouse_look=False,
            )
            self._clear_native_desktop_route()
            cancel_latched = bool(
                getattr(self, "unexpected_native_menu_cancel_latched", False)
            )
            cancel_retry_at = float(
                getattr(self, "unexpected_native_menu_cancel_retry_at", 0.0)
            )
            retry_forced_command = bool(
                unexpected_native_command_open
                and cancel_latched
                and native_party_menu_now >= cancel_retry_at
            )
            if quarantine and (not cancel_latched or retry_forced_command):
                self.native_dialog_bridge.pulse(NATIVE_DIALOG_CANCEL)
                self.unexpected_native_menu_cancel_latched = True
                self.unexpected_native_menu_cancel_retry_at = (
                    native_party_menu_now
                    + UNEXPECTED_NATIVE_COMMAND_CANCEL_RETRY_SECONDS
                )
                if (
                    unexpected_native_command_open
                    and not cancel_latched
                    and self.p1_model.mode is MenuMode.CLOSED
                ):
                    # Story/tutorial code can open COMMAND directly without a
                    # physical button edge. Convert that forced native open into
                    # the custom P1 command tree as soon as the native lifecycle
                    # acknowledges the cancel, so the player never needs F8 to
                    # escape the tutorial frontend.
                    self.p1_frontend_deferred_open_until = (
                        native_party_menu_now + P1_FRONTEND_DEFERRED_OPEN_SECONDS
                    )
                    self.p1_confirm_reopen_guard.arm(native_party_menu_now)
                    print(
                        "UNEXPECTED_NATIVE_COMMAND_CUSTOM_OPEN_DEFERRED",
                        flush=True,
                    )
                print(
                    "UNEXPECTED_NATIVE_MENU_CANCEL_PULSED "
                    f"retry={int(retry_forced_command)}",
                    flush=True,
                )
            # A stable external frontend (for example Map) is adopted only by
            # maintain_native_frontend_suspension after its settle interval.
            # Until then the raw generic bit cannot lower any resident mask.
            return
        if not snapshot.native_menu_open:
            self.unexpected_native_menu_cancel_latched = False
            self.unexpected_native_menu_cancel_retry_at = 0.0
        p1_route_available = bool(
            p1_actor
            and snapshot.p1_gameplay_ready
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
        )
        p1_custom_menu_allowed = custom_menu_input_allowed(
            snapshot.p1_menu_allowed
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
            and not leader_selection_pending,
            p1_leader_available,
        )
        p2_custom_menu_allowed = custom_menu_input_allowed(
            snapshot.p2_menu_allowed
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
            and not leader_selection_pending,
            p2_actor_available,
        )
        p3_custom_menu_allowed = custom_menu_input_allowed(
            p3_session_menu_allowed(snapshot, len(self.party))
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
            and not leader_selection_pending,
            p3_actor_available,
        )
        if (
            p1_leader_available is not True
            and self.p1_model.mode is not MenuMode.CLOSED
        ):
            self.hard_close_p1_menu()
            print("P1_HEADLESS_MENU_CLOSED owner=unavailable", flush=True)
        if p2_actor_available is not True and self.model.mode is not MenuMode.CLOSED:
            self.hard_close_p2_menu()
            print("COOP_MENU_CLOSED owner=unavailable", flush=True)
        if (
            p3_actor_available is not True
            and self.p3_model.mode is not MenuMode.CLOSED
        ):
            self.hard_close_p3_menu()
            print("P3_MENU_CLOSED owner=unavailable", flush=True)
        p1_native_command_open = bool(
            not wait_presentation_owned
            and native_command_lifecycle is not None
            and native_command_lifecycle[2] == native_command_lifecycle[0]
        )
        pause_suspended = p1_pause_suspension_required(
            snapshot.native_menu_open,
            p1_native_command_open,
            native_pause_open,
        )
        if pause_suspended != self.p1_pause_suspended:
            self.p1_pause_suspended = pause_suspended
            if pause_suspended:
                self.hard_close_p1_menu()
                print("P1_HEADLESS_MENU SUSPENDED_BY_PAUSE", flush=True)
            elif self.p1_visual_replacement:
                print("P1_HEADLESS_MENU RESUMED_AFTER_PAUSE", flush=True)
        effective_p1_replacement = bool(
            self.p1_visual_replacement
            and not pause_suspended
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
        )
        bridge_owned = bool(
            effective_p1_replacement
            and snapshot.bridge_ownership_allowed
            and p1_route_available
        )
        p1_binding_enabled = bool(bridge_owned and not native_pause_open)
        p1_enabled = p1_binding_enabled and p1_custom_menu_allowed

        mask_now = time.monotonic()
        p1_close_guard_active = bool(
            self.p1_transition_kind == "closing"
            or mask_now < self.p1_menu_reopen_allowed_at
            or not self.p1_confirm_reopen_guard.can_open(mask_now)
        )
        p1_mask_requested = p1_command_mask_requested(
            self.p1_visual_replacement,
            user_control_enabled=snapshot.user_control_enabled,
            native_command_open=p1_native_command_open,
            close_guard_active=p1_close_guard_active,
            native_pause_open=native_pause_open,
            native_modal_active=snapshot.native_modal_active,
            town=snapshot.town,
            chocobo_mode_active=getattr(snapshot, "chocobo_mode_active", False),
            game_thread_handoff=True,
        )
        custom_directional_lifecycle_owned = custom_directional_input_owned(
            self.p1_model.mode,
            self.model.mode,
            self.p3_model.mode,
            p1_transition_kind=self.p1_transition_kind,
            p2_transition_kind=self.transition_kind,
            p3_transition_kind=self.p3_transition_kind,
        )
        custom_directional_release_active = (
            self.custom_directional_release_guard.update(
                lifecycle_owned=custom_directional_lifecycle_owned,
                directional_down=custom_directional_control_down(self.bindings),
                now=mask_now,
            )
        )
        custom_directional_owner = custom_directional_input_owned(
            self.p1_model.mode,
            self.model.mode,
            self.p3_model.mode,
            p1_transition_kind=self.p1_transition_kind,
            p2_transition_kind=self.transition_kind,
            p3_transition_kind=self.p3_transition_kind,
            p1_release_guard_active=custom_directional_release_active,
            p2_release_guard_active=(mask_now < self.menu_reopen_allowed_at),
            p3_release_guard_active=(mask_now < self.p3_menu_reopen_allowed_at),
        )

        if not trusted_native_menu:
            self.p1_native_menu_open_since = None
        elif self.p1_native_menu_open_since is None:
            self.p1_native_menu_open_since = mask_now
        p1_native_menu_settled = bool(
            self.p1_native_menu_open_since is not None
            and mask_now - self.p1_native_menu_open_since >= NATIVE_MENU_SETTLE_SECONDS
        )
        p1_replacement_policy = p1_command_mask_enabled(
            p1_mask_requested,
            native_menu_open=(trusted_native_menu and p1_native_menu_settled),
            native_command_open=p1_native_command_open,
            close_guard_active=p1_close_guard_active,
            native_menu_settled=p1_native_menu_settled,
        )
        p1_replacement_policy = p1_command_firewall_state(
            p1_replacement_policy,
            coop_active=self.coop_runtime_active,
            leader_selection_pending=leader_selection_pending,
        )
        custom_directional_policy = custom_directional_bridge_enabled(
            snapshot.bridge_ownership_allowed,
            self.custom_ui_suppressed or self.native_frontend_custom_suspended,
            custom_directional_owner,
        )
        p1_lb_policy = custom_directional_bridge_enabled(
            snapshot.bridge_ownership_allowed,
            self.custom_ui_suppressed or self.native_frontend_custom_suspended,
            p1_menu_owns_lb(
                self.p1_model.mode,
                directional_owner=custom_directional_owner,
                leader_selector_active=self.p1_leader_selector_active(),
            ),
        )
        p2_binding_enabled = bool(
            p2_manual_control_enabled
            and snapshot.gameplay_ready
            and not leader_selection_pending
            and not snapshot.native_menu_open
            and not native_pause_open
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
            and self.transition_kind != "closing"
        )
        p2_enabled = p2_binding_enabled and p2_custom_menu_allowed
        p3_binding_enabled = bool(
            p3_manual_control_enabled
            and snapshot.p2_menu_allowed
            and not leader_selection_pending
            and not snapshot.native_menu_open
            and not native_pause_open
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
            and self.p3_transition_kind != "closing"
            and snapshot.identity.p3_party_member_id is not None
        )
        p3_enabled = p3_binding_enabled and p3_custom_menu_allowed

        chocobo_mode_active = bool(
            getattr(snapshot, "chocobo_mode_active", False)
        )
        # native_frontend_custom_suspended deliberately outlives the observed
        # native menu by a short visual/state settle interval.  It must not be a
        # pass-through authority on its own: once native_menu_open/modal/Pause
        # are false, replacement firewalls have to be armed in that same frame.
        # Presentation suppression is not native-input ownership.  UCOFF,
        # loading/cutscene snapshots, and a locally hidden overlay must keep the
        # co-op COMMAND/leader firewalls armed; only a proven native frontend or
        # an explicit native gameplay contract may receive those semantics.
        native_input_passthrough_authorized = bool(
            native_pause_open
            or snapshot.native_modal_active
            or trusted_native_menu
            or snapshot.town
            or chocobo_mode_active
            or self.native_quickening_active
        )
        all_custom_menus_closed = all(
            model.mode is MenuMode.CLOSED
            for model in (self.p1_model, self.model, self.p3_model)
        )
        no_custom_transition = all(
            transition is None
            for transition in (
                self.p1_transition_kind,
                self.transition_kind,
                self.p3_transition_kind,
            )
        )
        p1_leader_handoff_pending = bool(
            int(getattr(self, "p1_leader_handoff_sequence", 0))
        )
        leader_replacement_base = bool(
            self.coop_runtime_active
            and (
                leader_selection_pending
                or (
                    self.p1_visual_replacement
                    and not native_input_passthrough_authorized
                )
            )
        )
        leader_selector_eligible, leader_replacement_policy = (
            native_leader_replacement_state(
                base_eligible=leader_replacement_base,
                menus_closed=all_custom_menus_closed,
                transitions_clear=no_custom_transition,
                handoff_pending=p1_leader_handoff_pending,
            )
        )
        # Suppression owns the whole custom lifecycle. Opening eligibility is
        # narrower and still requires a controllable P1 route.
        leader_selector_eligible = bool(
            leader_selector_eligible
            and p1_enabled
            and not custom_directional_release_active
        )
        desktop_owner_present = desktop_owner in ("p1", "p2", "p3")
        capture_desktop_events = bool(
            foreground
            and desktop_owner_present
            and self.coop_runtime_active
            and not native_input_passthrough_authorized
            and not self.custom_ui_suppressed
            and snapshot.user_control_enabled
            and not leader_selection_pending
        )
        non_p1_command_policy = non_p1_command_firewall_state(
            coop_active=self.coop_runtime_active,
            # The resident bridge remains the firewall owner across structural
            # loading/UCOFF gaps.  Session readiness governs custom dispatch,
            # not whether stock COMMAND may leak back into the game.
            bridge_ownership_allowed=not native_input_passthrough_authorized,
            native_command_passthrough=native_input_passthrough_authorized,
            leader_selection_pending=leader_selection_pending,
        )
        suppress_mouse_look = bool(
            self.coop_runtime_active and desktop_owner in ("p2", "p3")
        )
        quickening_policy = quickening_button_block_mask(
            active=bool(self.coop_runtime_active and self.native_quickening_active),
            party_size=len(self.party),
            p2_manual_control=bool(
                p2_manual_control_enabled
                and snapshot.identity.p2_party_member_id is not None
            ),
            p3_manual_control=bool(
                p3_manual_control_enabled
                and snapshot.identity.p3_party_member_id is not None
            ),
        )
        party_menu_policy = bool(
            any(
                model.mode is not MenuMode.CLOSED
                for model in (self.p1_model, self.model, self.p3_model)
            )
            or any(
                transition is not None
                for transition in (
                    self.p1_transition_kind,
                    self.transition_kind,
                    self.p3_transition_kind,
                )
            )
            or self.native_party_menu_request_pending
        )
        if native_input_passthrough_authorized or self.custom_ui_suppressed:
            custom_directional_policy = False
            p1_lb_policy = False
            party_menu_policy = False
        route_signature = (
            desktop_owner,
            int(self.role_generation),
            self.p1_model.mode,
            self.model.mode,
            self.p3_model.mode,
            self.p1_transition_kind,
            self.transition_kind,
            self.p3_transition_kind,
            bool(native_input_passthrough_authorized),
            bool(leader_selection_pending),
            bool(p1_leader_handoff_pending),
            bool(capture_desktop_events),
            bool(p1_enabled),
            bool(p2_enabled),
            bool(p3_enabled),
        )
        route_changed = route_signature != getattr(
            self,
            "native_desktop_route_signature",
            None,
        )
        capture_epoch = (
            (str(desktop_owner), int(self.role_generation))
            if capture_desktop_events and desktop_owner is not None
            else None
        )
        self._publish_native_input_contract(
            p1_replacement=p1_replacement_policy,
            non_p1_command_blocked=non_p1_command_policy,
            custom_directional_owner=custom_directional_policy,
            p1_menu_owns_lb=p1_lb_policy,
            party_menu_blocked=party_menu_policy,
            quickening_block_mask=quickening_policy,
            leader_replacement=leader_replacement_policy,
            capture_desktop_events=capture_desktop_events,
            suppress_mouse_look=suppress_mouse_look,
            native_gamepad_slot_mask=gamepad_slot_mask,
            desktop_capture_epoch=capture_epoch,
            force=route_changed,
        )
        generation_reader = getattr(
            self.native_dialog_bridge,
            "committed_policy_generation",
            None,
        )
        if callable(generation_reader):
            published_generation = int(generation_reader()) & 0xFFFFFFFF
        else:
            published_generation = int(
                getattr(self.native_dialog_bridge, "policy_generation", 0)
            ) & 0xFFFFFFFF
        self.native_desktop_route_signature = route_signature
        if capture_desktop_events:
            previous_capture_epoch = getattr(
                self,
                "native_desktop_capture_epoch",
                None,
            )
            if previous_capture_epoch != capture_epoch:
                wheel_gate = getattr(self, "native_desktop_wheel_gate", None)
                if wheel_gate is not None:
                    wheel_gate.reset()
            self.native_desktop_capture_owner = desktop_owner
            self.native_desktop_capture_epoch = capture_epoch
        else:
            self._clear_native_desktop_route()

        desktop_command_owner: str | None = None
        desktop_events = self._collect_native_desktop_events(
            foreground=foreground,
            desktop_owner=desktop_owner,
            expected_generation=published_generation,
        )
        for event in desktop_events:
            if event.kind == NATIVE_INPUT_EVENT_COMMAND:
                desktop_command_owner = desktop_owner
                break
            if event.kind != NATIVE_INPUT_EVENT_WHEEL:
                continue
            wheel_gate = getattr(self, "native_desktop_wheel_gate", None)
            if wheel_gate is None:
                wheel_gate = DesktopWheelGestureGate()
                self.native_desktop_wheel_gate = wheel_gate
            direction = wheel_gate.consume(event.value, now=input_now)
            if direction is None:
                # The ring has already been drained.  Locked-out/coalesced
                # gestures are deliberately discarded and never replayed.
                continue
            self._route_desktop_wheel_step(
                desktop_owner,
                direction,
                p1_enabled=bool(
                    p1_enabled
                    and (
                        leader_selector_eligible
                        or self.p1_model.mode is not MenuMode.CLOSED
                    )
                ),
                p2_enabled=p2_enabled,
                p3_enabled=p3_enabled,
            )

        share_leader_menu = bool(not self.coop_runtime_active and p1_binding_enabled)
        p1_effective_up_edge = bool(
            p1_direction_edge["menu_up"]
            or (share_leader_menu and p2_direction_edge["menu_up"])
            or (share_leader_menu and p3_direction_edge["menu_up"])
        )
        p1_effective_down_edge = bool(
            p1_direction_edge["menu_down"]
            or (share_leader_menu and p2_direction_edge["menu_down"])
            or (share_leader_menu and p3_direction_edge["menu_down"])
        )
        p2_confirm_edge = self.bindings.native_action_edge(
            "p2",
            "confirm",
            circle_decides,
            excluded_keyboard_vks=(0x01,),
        )
        p2_back_edge = self.bindings.native_action_edge(
            "p2", "cancel", circle_decides
        )
        p3_confirm_edge = self.bindings.native_action_edge(
            "p3",
            "confirm",
            circle_decides,
            excluded_keyboard_vks=(0x01,),
        )
        p3_back_edge = self.bindings.native_action_edge(
            "p3", "cancel", circle_decides
        )
        if desktop_command_owner == "p1":
            p1_confirm_edge = True
        elif desktop_command_owner == "p2":
            p2_confirm_edge = True
        elif desktop_command_owner == "p3":
            p3_confirm_edge = True
        p1_up = bool(
            p1_binding_enabled
            and (
                p1_direction_repeat["menu_up"]
                or (share_leader_menu and p2_character_dpad_up)
                or (share_leader_menu and p3_character_dpad_up)
            )
        )
        p1_down = bool(
            p1_binding_enabled
            and (
                p1_direction_repeat["menu_down"]
                or (share_leader_menu and p2_character_dpad_down)
                or (share_leader_menu and p3_character_dpad_down)
            )
        )
        p1_left = bool(
            p1_binding_enabled
            and (
                p1_direction_repeat["menu_left"]
                or (share_leader_menu and self.bindings.repeat("p2", "menu_left"))
                or (share_leader_menu and self.bindings.repeat("p3", "menu_left"))
            )
        )
        p1_right = bool(
            p1_binding_enabled
            and (
                p1_direction_repeat["menu_right"]
                or (share_leader_menu and self.bindings.repeat("p2", "menu_right"))
                or (share_leader_menu and self.bindings.repeat("p3", "menu_right"))
            )
        )
        # Opening is an edge-triggered vertical action.  menu_left/menu_right
        # intentionally include LB/RB aliases in the base profile, so routing
        # either horizontal semantic here would make those shoulders open the
        # leader selector.
        p1_leader_up_edge = bool(
            p1_binding_enabled and p1_direction_edge["menu_up"]
        )
        p1_leader_down_edge = bool(
            p1_binding_enabled and p1_direction_edge["menu_down"]
        )
        if leader_selector_eligible and self.p1_model.mode is MenuMode.CLOSED:
            leader_direction = None
            if p1_leader_up_edge and not p1_leader_down_edge:
                leader_direction = InputCommand.UP
            elif p1_leader_down_edge and not p1_leader_up_edge:
                leader_direction = InputCommand.DOWN
            if leader_direction is not None:
                self.open_p1_leader_selector()
                p1_up = p1_down = p1_left = p1_right = False
        p1_back_edge = self.bindings.native_action_edge(
            "p1", "cancel", circle_decides
        )
        if share_leader_menu:
            p1_confirm_edge = bool(
                p1_confirm_edge or p2_confirm_edge or p3_confirm_edge
            )
            p1_back_edge = bool(p1_back_edge or p2_back_edge or p3_back_edge)
        p1_confirm = p1_confirm_edge if p1_binding_enabled else False
        p1_back = p1_back_edge if p1_binding_enabled else False
        p1_target_group_toggle = bool(p1_enabled and (p1_left or p1_right))

        up = p2_character_dpad_up if p2_binding_enabled else False
        down = p2_character_dpad_down if p2_binding_enabled else False
        p2_target_previous = self.bindings.edge("p2", "target_previous")
        p2_target_next = self.bindings.edge("p2", "target_next")
        confirm = p2_confirm_edge if p2_binding_enabled else False
        back = p2_back_edge if p2_binding_enabled else False
        p2_menu_left_edge = self.bindings.edge("p2", "menu_left")
        p2_menu_right_edge = self.bindings.edge("p2", "menu_right")
        target_group_binding = bool(
            p2_enabled and (p2_menu_left_edge or p2_menu_right_edge)
        )
        target_group_toggle = bool(
            target_group_binding and not (p2_target_previous or p2_target_next)
        )
        p3_up = p3_character_dpad_up if p3_binding_enabled else False
        p3_down = p3_character_dpad_down if p3_binding_enabled else False
        p3_target_previous = self.bindings.edge("p3", "target_previous")
        p3_target_next = self.bindings.edge("p3", "target_next")
        p3_confirm = p3_confirm_edge if p3_binding_enabled else False
        p3_back = p3_back_edge if p3_binding_enabled else False
        p3_menu_left_edge = self.bindings.edge("p3", "menu_left")
        p3_menu_right_edge = self.bindings.edge("p3", "menu_right")
        p3_target_group_binding = bool(
            p3_enabled and (p3_menu_left_edge or p3_menu_right_edge)
        )
        p3_target_group_toggle = bool(
            p3_target_group_binding and not (p3_target_previous or p3_target_next)
        )

        if self.model.mode is MenuMode.CLOSED:
            self.p2_confirm_reopen_guard.update(
                confirm_down=self.bindings.native_action_down(
                    "p2", "confirm", circle_decides
                ),
                now=input_now,
            )
            self.p2_character_direction_guard.update(
                confirm_down=p2_character_direction_down,
                now=input_now,
            )
        if self.p3_model.mode is MenuMode.CLOSED:
            self.p3_confirm_reopen_guard.update(
                confirm_down=self.bindings.native_action_down(
                    "p3", "confirm", circle_decides
                ),
                now=input_now,
            )
            self.p3_character_direction_guard.update(
                confirm_down=p3_character_direction_down,
                now=input_now,
            )
        p2_confirm_reopen_ready = self.p2_confirm_reopen_guard.can_open(input_now)
        p3_confirm_reopen_ready = self.p3_confirm_reopen_guard.can_open(input_now)
        p2_character_direction_ready = self.p2_character_direction_guard.can_open(
            input_now
        )
        p3_character_direction_ready = self.p3_character_direction_guard.can_open(
            input_now
        )

        p1_input = PlayerMenuInput(
            up=p1_up,
            down=p1_down,
            up_edge=p1_effective_up_edge,
            down_edge=p1_effective_down_edge,
            left=p1_left,
            right=p1_right,
            page_up=bool(self.p1_model.mode is MenuMode.LIST and p1_left),
            page_down=bool(self.p1_model.mode is MenuMode.LIST and p1_right),
            confirm=p1_confirm,
            cancel=p1_back,
            confirm_edge=p1_confirm_edge,
            cancel_edge=p1_back_edge,
            target_group=p1_target_group_toggle,
        )
        p2_input = PlayerMenuInput(
            up=up,
            down=down,
            up_edge=p2_direction_edge["menu_up"],
            down_edge=p2_direction_edge["menu_down"],
            page_up=bool(
                self.model.mode is MenuMode.LIST
                and (p2_target_previous or p2_menu_left_edge)
            ),
            page_down=bool(
                self.model.mode is MenuMode.LIST
                and (p2_target_next or p2_menu_right_edge)
            ),
            confirm=confirm,
            cancel=back,
            confirm_edge=p2_confirm_edge,
            cancel_edge=p2_back_edge,
            target_group=target_group_toggle,
            target_group_direction=(
                -1
                if p2_enabled and p2_target_previous
                else (1 if p2_enabled and p2_target_next else 0)
            ),
            character_up=p2_character_dpad_up,
            character_down=p2_character_dpad_down,
            character_hotkey=False,
            character_direction_ready=p2_character_direction_ready,
            native_party_menu=native_party_menu_edges["p2"],
            native_previous=p2_target_previous,
            native_next=p2_target_next,
        )
        p3_input = PlayerMenuInput(
            up=p3_up,
            down=p3_down,
            up_edge=p3_direction_edge["menu_up"],
            down_edge=p3_direction_edge["menu_down"],
            page_up=bool(
                self.p3_model.mode is MenuMode.LIST
                and (p3_target_previous or p3_menu_left_edge)
            ),
            page_down=bool(
                self.p3_model.mode is MenuMode.LIST
                and (p3_target_next or p3_menu_right_edge)
            ),
            confirm=p3_confirm,
            cancel=p3_back,
            confirm_edge=p3_confirm_edge,
            cancel_edge=p3_back_edge,
            target_group=p3_target_group_toggle,
            target_group_direction=(
                -1
                if p3_enabled and p3_target_previous
                else (1 if p3_enabled and p3_target_next else 0)
            ),
            character_up=p3_character_dpad_up,
            character_down=p3_character_dpad_down,
            character_hotkey=False,
            character_direction_ready=p3_character_direction_ready,
            native_party_menu=native_party_menu_edges["p3"],
            native_previous=p3_target_previous,
            native_next=p3_target_next,
        )
        if self._route_native_quickening_input(
            foreground=foreground,
            p2_manual_control=p2_manual_control_enabled,
            p3_manual_control=p3_manual_control_enabled,
            block_mask=quickening_policy,
        ):
            return
        if self._route_native_frontend_input(
            foreground=foreground,
            snapshot=snapshot,
            p1_native_command_open=p1_native_command_open,
            now=mask_now,
            p1=p1_input,
            p2=p2_input,
            p3=p3_input,
        ):
            return
        now = time.monotonic()
        if self.p1_model.mode is MenuMode.CLOSED and p1_binding_enabled:
            p1_confirm_down = self.bindings.native_action_down(
                "p1", "confirm", circle_decides
            )
            if share_leader_menu:
                p1_confirm_down = bool(
                    p1_confirm_down
                    or self.bindings.native_action_down(
                        "p2", "confirm", circle_decides
                    )
                    or self.bindings.native_action_down(
                        "p3", "confirm", circle_decides
                    )
                )
            self.p1_confirm_reopen_guard.update(
                confirm_down=p1_confirm_down,
                now=now,
            )
        p1_confirm_reopen_ready = self.p1_confirm_reopen_guard.can_open(now)
        deferred_open_until = float(
            getattr(self, "p1_frontend_deferred_open_until", 0.0)
        )
        if deferred_open_until and now > deferred_open_until:
            self.p1_frontend_deferred_open_until = 0.0
        elif (
            deferred_open_until
            and self.p1_model.mode is MenuMode.CLOSED
            and p1_binding_enabled
            and p1_custom_menu_allowed
            and p1_confirm_reopen_ready
        ):
            p1_confirm = True
            self.p1_frontend_deferred_open_until = 0.0
            print("P1_FRONTEND_CONFIRM_RESUMED", flush=True)

        if not snapshot.user_control_enabled:
            return

        p1_input = PlayerMenuInput(
            up=p1_up,
            down=p1_down,
            up_edge=p1_effective_up_edge,
            down_edge=p1_effective_down_edge,
            left=p1_left,
            right=p1_right,
            page_up=bool(self.p1_model.mode is MenuMode.LIST and p1_left),
            page_down=bool(self.p1_model.mode is MenuMode.LIST and p1_right),
            confirm=p1_confirm,
            cancel=p1_back,
            confirm_edge=p1_confirm_edge,
            cancel_edge=p1_back_edge,
            target_group=p1_target_group_toggle,
        )
        interactions = self._world_interactions()
        self.frame_world_interactions = interactions
        interaction_consumed = self._handle_p1_battle_input(
            now=now,
            actor=p1_actor,
            native_open=bool(p1_binding_enabled and p1_native_command_open),
            binding_enabled=p1_binding_enabled,
            custom_menu_allowed=p1_custom_menu_allowed,
            confirm_reopen_ready=p1_confirm_reopen_ready,
            controls=p1_input,
            interactions=interactions,
        )
        if self._handle_p3_battle_input(
            input_role_generation=input_role_generation,
            now=now,
            actor=p3_actor,
            leader_actor=native_leader_actor,
            binding_enabled=p3_binding_enabled,
            custom_menu_allowed=p3_custom_menu_allowed,
            confirm_reopen_ready=p3_confirm_reopen_ready,
            controls=p3_input,
            interactions=interactions,
            battle_menu_edge=battle_menu_edges["p3"],
        ):
            return
        self._handle_p2_battle_input(
            input_role_generation=input_role_generation,
            now=now,
            actor=p2_actor,
            leader_actor=native_leader_actor,
            binding_enabled=p2_binding_enabled,
            custom_menu_allowed=p2_custom_menu_allowed,
            confirm_reopen_ready=p2_confirm_reopen_ready,
            interaction_consumed=interaction_consumed,
            controls=p2_input,
            interactions=interactions,
            battle_menu_edge=battle_menu_edges["p2"],
        )

    def update_upper_info_layout(self) -> None:
        self.upper_info_layout_offset_y = -float(UPPER_INFO_IDLE_LIFT_PX) + (
            float(BOSS_GAUGE_INFO_DROP_PX) if self.boss_gauge_visible else 0.0
        )
        self.upper_info_layout_initialized = True
        self.target_info_extra_offset_y = 0.0
        self.upper_info_layout_last_tick = time.monotonic()

    def upper_info_y(self, base_y: float) -> int:
        return int(round(base_y + self.upper_info_screen_offset_y()))

    def upper_info_screen_offset_y(self) -> float:
        offset = float(self.upper_info_layout_offset_y)
        if self.combat_log_on_screen:
            offset = max(offset, float(COMBAT_LOG_CLEARANCE_PX))
            offset += float(COMBAT_LOG_ACTIVE_INFO_NUDGE_PX)
        return offset

    def target_info_y(self, base_y: float) -> int:
        return int(
            round(
                base_y
                + self.upper_info_layout_offset_y
                + self.target_info_extra_offset_y
            )
        )

    @staticmethod
    def _target_info_transition_opacity(
        handle: int,
        started: float,
        exit_started: float,
        now: float,
    ) -> float:
        if exit_started:
            if started <= 0.0:
                return 0.0
            progress = min(1.0, max(0.0, now - exit_started) / TARGET_EXIT_SECONDS)
            return 1.0 - progress * progress * (3.0 - 2.0 * progress)
        if not handle or started <= 0.0:
            return 0.0
        progress = min(1.0, max(0.0, now - started) / TARGET_ENTER_SECONDS)
        return progress * progress * (3.0 - 2.0 * progress)

    def paint_target_info_backdrop(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        delta = min(
            0.05,
            max(0.0, now - self.target_info_backdrop_last_tick),
        )
        self.target_info_backdrop_last_tick = now

        states = (
            (
                bool(self.p1_target_info_handle or self.p1_target_info_exit_started),
                self.p1_target_info_layout_x,
                self.p1_target_info_layout_y_offset,
                self.p1_target_info_last_target,
                self._target_info_transition_opacity(
                    self.p1_target_info_handle,
                    self.p1_target_info_started,
                    self.p1_target_info_exit_started,
                    now,
                ),
            ),
            (
                bool(self.p2_target_info_presented or self.target_visual_exit_started),
                self.p2_target_info_layout_x,
                self.p2_target_info_layout_y_offset,
                self.target_visual_last_target,
                self._target_info_transition_opacity(
                    self.target_visual_handle,
                    self.target_visual_started,
                    self.target_visual_exit_started,
                    now,
                ),
            ),
            (
                bool(self.p3_target_info_handle or self.p3_target_info_exit_started),
                self.p3_target_info_layout_x,
                self.p3_target_info_layout_y_offset,
                self.p3_target_info_last_target,
                self._target_info_transition_opacity(
                    self.p3_target_info_handle,
                    self.p3_target_info_started,
                    self.p3_target_info_exit_started,
                    now,
                ),
            ),
        )
        active = [state for state in states if state[0]]
        opacity = max((state[4] for state in active), default=0.0)
        if not active or opacity <= 0.001:
            self.target_info_backdrop_panel.hide()
            self.target_info_backdrop_draw_key = None
            if not active:
                self.target_info_backdrop_width = 0.0
                self.target_info_backdrop_height = 0.0
            return

        right_edge = max(
            state[1] + world_overlay.target_card_visual_width() for state in active
        )
        desired_width = right_edge + world_overlay.TARGET_BACKDROP_FADE_WIDTH
        desired_height = max(
            live_target_panel_height(state[3]) for state in active
        ) - world_overlay.target_backdrop_top_inset() + TARGET_BACKDROP_BOTTOM_PADDING
        desired_height = max(1.0, float(desired_height))

        if self.target_info_backdrop_width <= 0.0:
            self.target_info_backdrop_width = desired_width
            self.target_info_backdrop_height = desired_height
        else:
            blend = 1.0 - math.exp(-delta / TARGET_BACKDROP_LAYOUT_SECONDS)
            self.target_info_backdrop_width += (
                desired_width - self.target_info_backdrop_width
            ) * blend
            self.target_info_backdrop_height += (
                desired_height - self.target_info_backdrop_height
            ) * blend

        width = max(1, int(round(self.target_info_backdrop_width)))
        height = max(1, int(round(self.target_info_backdrop_height)))
        top = min(
            self.target_info_y(P1_TARGET_INFO_Y + state[2]) for state in active
        ) + world_overlay.target_backdrop_top_inset()
        draw_key = (width, height, top, int(round(opacity * 255.0)))
        if draw_key == self.target_info_backdrop_draw_key:
            return
        image = world_overlay.render_target_info_backdrop(width, height)
        self.draw_custom_panel(
            self.target_info_backdrop_panel,
            image,
            0,
            top,
            opacity=opacity,
        )
        self.target_info_backdrop_draw_key = draw_key

    def custom_target_info_presented(self, presentation_visible: bool) -> bool:
        return bool(
            presentation_visible
            and (
                self.p1_target_info_handle
                or self.p1_target_info_exit_started
                or self.p2_target_info_presented
                or self.target_visual_exit_started
                or self.p3_target_info_handle
                or self.p3_target_info_exit_started
            )
        )

    def update_player_info_lanes(self) -> None:
        now = time.monotonic()
        delta = min(
            0.05,
            max(0.0, now - self.info_lane_layout_last_tick),
        )
        self.info_lane_layout_last_tick = now
        models = (self.p1_model, self.model, self.p3_model)
        help_opacity = (
            self.p1_help_fade_opacity,
            self.p2_help_fade_opacity,
            self.p3_help_fade_opacity,
        )
        target_tail = (
            bool(self.p1_target_info_handle or self.p1_target_info_exit_started),
            bool(self.p2_target_info_presented or self.target_visual_exit_started),
            bool(self.p3_target_info_handle or self.p3_target_info_exit_started),
        )
        help_requested = tuple(
            bool(
                (
                    model.mode is not MenuMode.CLOSED
                    and model.mode is not MenuMode.TARGET
                )
                or opacity > 0.0
            )
            for model, opacity in zip(models, help_opacity)
        )

        target_active = tuple(bool(tail) for tail in target_tail)
        help_suppressed = tuple(
            bool(target or (suppressed and self.combat_log_gate_hidden))
            for target, suppressed in zip(
                target_active,
                self.info_help_target_suppressed,
            )
        )
        self.info_help_target_suppressed = help_suppressed
        help_active = tuple(
            bool(requested and not suppressed)
            for requested, suppressed in zip(help_requested, help_suppressed)
        )
        pitch = float(P2_HELP_COMPACT_Y - P1_HELP_Y)

        presented_targets = [
            target
            for target, active in zip(
                (
                    getattr(self, "p1_target_info_last_target", None),
                    getattr(self, "target_visual_last_target", None),
                    getattr(self, "p3_target_info_last_target", None),
                ),
                target_active,
            )
            if active
        ]
        help_start_y = (
            float(
                P1_TARGET_INFO_Y
                + max(
                    (live_target_panel_height(target) for target in presented_targets),
                    default=TARGET_INFO_PANEL_HEIGHT,
                )
                + 4
                if any(target_active)
                else P1_HELP_Y
            )
            + INFO_LANE_TOP_PADDING_PX
            + self.upper_info_screen_offset_y()
        )

        if getattr(self, "combat_log_on_screen", False) and now < getattr(
            self, "two_line_message_until", 0.0
        ):
            help_start_y += COMBAT_LOG_LINE_PX
        desired_help = []
        active_help_count = 0
        for active in help_active:
            desired_help.append(help_start_y + active_help_count * pitch)
            if active:
                active_help_count += 1

        target_pitch = float(world_overlay.panel_pitch())
        desired_target_x = []
        active_target_count = 0
        for active in target_active:
            desired_target_x.append(
                float(P1_TARGET_INFO_X) + active_target_count * target_pitch
            )
            if active:
                active_target_count += 1
        desired_target_y_offset = 0.0

        help_values = [
            self.p1_help_layout_y,
            self.p2_help_layout_y,
            self.p3_help_layout_y,
        ]
        target_x_values = [
            self.p1_target_info_layout_x,
            self.p2_target_info_layout_x,
            self.p3_target_info_layout_x,
        ]
        target_y_values = [
            self.p1_target_info_layout_y_offset,
            self.p2_target_info_layout_y_offset,
            self.p3_target_info_layout_y_offset,
        ]
        for index in range(3):
            help_new = help_active[index] and not self.info_help_active_last[index]
            target_new = (
                target_active[index] and not self.info_target_active_last[index]
            )
            if not self.info_lane_layout_initialized or help_new:
                help_values[index] = desired_help[index]
            else:
                help_values[index] = clearance_layout_axis(
                    help_values[index],
                    desired_help[index],
                    delta,
                )
            if not self.info_lane_layout_initialized or target_new:
                target_x_values[index] = desired_target_x[index]
                target_y_values[index] = desired_target_y_offset
            else:
                target_x_values[index] = smooth_layout_axis(
                    target_x_values[index],
                    desired_target_x[index],
                    delta,
                )
                target_y_values[index] = smooth_layout_axis(
                    target_y_values[index],
                    desired_target_y_offset,
                    delta,
                )

        (
            self.p1_help_layout_y,
            self.p2_help_layout_y,
            self.p3_help_layout_y,
        ) = help_values
        (
            self.p1_target_info_layout_x,
            self.p2_target_info_layout_x,
            self.p3_target_info_layout_x,
        ) = target_x_values
        (
            self.p1_target_info_layout_y_offset,
            self.p2_target_info_layout_y_offset,
            self.p3_target_info_layout_y_offset,
        ) = target_y_values
        self.info_help_active_last = help_active
        self.info_target_active_last = target_active
        self.info_lane_layout_initialized = True

    def three_player_layout(self) -> bool:
        return bool(
            self.session_snapshot.identity.player_count >= 3
            and self.session_snapshot.identity.p2_party_member_id is not None
            and self.session_snapshot.identity.p3_party_member_id is not None
            and len(self.party) >= 3
            and all(entry[2] for entry in self.party[:3])
        )

    def menu_panel_x(self, player: int) -> int:
        if self.three_player_layout():
            return {1: 77, 2: 450, 3: 823}[int(player)]
        return {
            1: P1_OVERLAY_X,
            2: legacy.OVERLAY_X,
            3: P3_OVERLAY_X,
        }[int(player)]

    def draw_custom_panel(
        self,
        panel,
        image: Image.Image,
        x: float,
        y: float,
        *,
        bottom_anchor: bool = False,
        vertical_offset: float = 0.0,
        opacity: float = 1.0,
    ) -> None:
        compact_menu = panel in (self.p1_panel, self.panel, self.p3_panel)
        local_offset_y = float(image.info.get("ff12_local_offset_y", 0.0))
        local_opacity = min(
            1.0,
            max(0.0, float(image.info.get("ff12_local_opacity", 1.0))),
        )
        opacity *= local_opacity
        if not self.three_player_layout() or not compact_menu:
            panel.draw(image, x, y + local_offset_y, opacity)
            return
        cache = getattr(panel, "_compact_image_cache", None)
        if cache is None:
            cache = {}
            panel._compact_image_cache = cache
        entry = cache.pop(id(image), None)
        if entry is not None and entry[0] is image:
            compact = entry[1]
            cache[id(image)] = entry
        else:
            compact = renderer.scale_three_player_panel(image)

            cache[id(image)] = (image, compact)
            while len(cache) > 8:
                cache.pop(next(iter(cache)))
        if bottom_anchor:
            baseline_bottom = renderer.PANEL_TOP + renderer.panel_height(5)
            panel_bottom = baseline_bottom - vertical_offset
            y += panel_bottom * (1.0 - renderer.THREE_PLAYER_PANEL_SCALE)
        panel.draw(
            compact,
            x,
            y + local_offset_y * renderer.THREE_PLAYER_PANEL_SCALE,
            opacity,
        )

    def publish_p1_help_badge(self, foreground: bool) -> None:
        rect = (-10000.0, -10000.0, -9999.0, -9999.0)
        if foreground:
            try:
                manager = self.process.u64(UI_MANAGER_GLOBAL)
                help_object = manager + P1_HELP_OFFSET if manager else 0
                root = self.process.u64(help_object + 0x1048) if help_object else 0
                if (
                    help_object
                    and self.process.u64(help_object) == HELP_CALLBACK
                    and root
                    and self.process.u64(root) == HELP_ROOT_CALLBACK
                ):
                    root_x, root_y = struct.unpack(
                        "<hh", self.process.read(root + 0x98, 4)
                    )
                    if (
                        -64 <= root_x < legacy.REFERENCE_WIDTH
                        and -64 <= root_y < legacy.REFERENCE_HEIGHT
                    ):
                        rect = (
                            float(root_x + 12),
                            float(root_y + 7),
                            float(root_x + 61),
                            float(root_y + 58),
                        )
            except (OSError, RuntimeError, struct.error):
                pass

        if rect == self.help_badge_last_rect:
            return

        self.help_badge_sequence = (self.help_badge_sequence + 2) & 0xFFFFFFFE
        sequence = self.help_badge_sequence
        odd = sequence | 1
        address = self.base + P1_HELP_BADGE_FEED_OFFSET
        self.process.write(address + 0x04, struct.pack("<I", odd))
        self.process.write(address + 0x18, struct.pack("<I", odd))
        self.process.write(address, struct.pack("<I", P1_HELP_BADGE_MAGIC))
        self.process.write(address + 0x08, struct.pack("<4f", *rect))
        self.process.write(address + 0x18, struct.pack("<I", sequence))
        self.process.write(address + 0x04, struct.pack("<I", sequence))
        self.help_badge_last_rect = rect

    def paint_p3(self, foreground: bool) -> None:
        now = time.monotonic()
        delta = min(0.05, max(0.0, now - self.p3_last_paint))
        self.p3_last_paint = now
        if not foreground:
            self.p3_panel.hide()
            self.p3_help_panel.hide()
            return
        if self.p3_model.mode is MenuMode.CLOSED and not self.p3_transition_kind:
            self.p3_panel.hide()
            self.p3_help_panel.hide()
            self.p3_help_fade_image = None
            self.p3_help_fade_opacity = 0.0
            self.p3_help_presented = False
            return

        if self.p3_transition_kind == "closing":
            image = self.p3_closing_image
            help_image = self.p3_closing_help
            vertical_offset = self.p3_closing_y_offset
        else:
            image = self.p3_controller.presenter.render(
                now=now,
                delta_seconds=delta,
                player=3,
                compose_layers=not self.three_player_layout(),
            )
            vertical_offset = self.p3_menu_vertical_offset()
            help_image = (
                None
                if self.p3_model.mode is MenuMode.TARGET
                else self.p3_controller.presenter.render_help(player=3)
            )
        if image is None:
            self.p3_panel.hide()
            self.p3_help_panel.hide()
            return

        offset_x = 0.0
        opacity = 1.0
        if self.p3_transition_kind:
            progress = min(
                1.0,
                (now - self.p3_transition_started) / renderer.TRANSITION_SECONDS,
            )
            frame = renderer.transition(
                image,
                progress,
                opening=self.p3_transition_kind == "opening",
                p2_mirrored=True,
            )
            image = frame.image
            offset_x = frame.offset_x
            opacity = frame.opacity
            if progress >= 1.0:
                closing = self.p3_transition_kind == "closing"
                self.p3_transition_kind = None
                if closing:
                    self.p3_closing_image = None
                    self.p3_closing_help = None
                    self.p3_panel.hide()
                    self.p3_help_panel.hide()
                    self.p3_help_fade_image = None
                    self.p3_help_fade_opacity = 0.0
                    self.p3_help_presented = False
                    return

        self.draw_custom_panel(
            self.p3_panel,
            image,
            self.menu_panel_x(3)
            + offset_x
            * (
                renderer.THREE_PLAYER_PANEL_SCALE if self.three_player_layout() else 1.0
            ),
            P3_OVERLAY_Y + vertical_offset,
            bottom_anchor=True,
            vertical_offset=vertical_offset,
            opacity=opacity,
        )
        help_requested = bool(
            help_image is not None and not self.info_help_target_suppressed[2]
        )
        if help_requested:
            self.p3_help_fade_image = help_image
        self.p3_help_fade_opacity = advance_info_opacity(
            self.p3_help_fade_opacity,
            help_requested,
            delta,
            immediate=self.p3_transition_kind != "closing",
        )
        self.p3_help_presented = bool(help_requested or self.p3_help_fade_opacity > 0.0)
        if self.p3_help_fade_image is not None and self.p3_help_fade_opacity > 0.0:
            self.draw_custom_panel(
                self.p3_help_panel,
                self.p3_help_fade_image,
                legacy.HELP_X + HELP_PANEL_X_COMPENSATION,
                int(round(self.p3_help_layout_y)),
                opacity=opacity * self.p3_help_fade_opacity,
            )
        else:
            self.p3_help_panel.hide()
            if not help_requested:
                self.p3_help_fade_image = None
                self.p3_help_presented = False

    def paint(self, foreground: bool) -> None:
        now = time.monotonic()
        delta = min(0.05, max(0.0, now - self.last_paint))
        self.last_paint = now
        if not foreground:
            self.panel.hide()
            self.help_panel.hide()
            return

        if self.model.mode is MenuMode.CLOSED and not self.transition_kind:
            self.panel.hide()
            self.help_panel.hide()
            self.p2_help_fade_image = None
            self.p2_help_fade_opacity = 0.0
            self.p2_help_presented = False
            return

        if self.transition_kind == "closing":
            image = self.closing_image
            help_image = self.closing_help
            vertical_offset = self.closing_y_offset
        else:
            image = self.controller.presenter.render(
                now=now,
                delta_seconds=delta,
                compose_layers=not self.three_player_layout(),
            )
            vertical_offset = self.menu_vertical_offset()
            help_image = (
                None
                if self.model.mode is MenuMode.TARGET
                else self.controller.presenter.render_help(player=2)
            )
        if image is None:
            self.panel.hide()
            self.help_panel.hide()
            self.p2_help_fade_image = None
            self.p2_help_fade_opacity = 0.0
            self.p2_help_presented = False
            return

        offset_x = 0.0
        opacity = 1.0
        if self.transition_kind:
            progress = min(
                1.0,
                (now - self.transition_started) / renderer.TRANSITION_SECONDS,
            )
            frame = renderer.transition(
                image,
                progress,
                opening=self.transition_kind == "opening",
                p2_mirrored=True,
            )
            image = frame.image
            offset_x = frame.offset_x
            opacity = frame.opacity
            if progress >= 1.0:
                closing = self.transition_kind == "closing"
                self.transition_kind = None
                if closing:
                    self.closing_image = None
                    self.closing_help = None
                    self.panel.hide()
                    self.help_panel.hide()
                    self.p2_help_fade_image = None
                    self.p2_help_fade_opacity = 0.0
                    self.p2_help_presented = False
                    return

        self.draw_custom_panel(
            self.panel,
            image,
            self.menu_panel_x(2)
            + offset_x
            * (
                renderer.THREE_PLAYER_PANEL_SCALE if self.three_player_layout() else 1.0
            ),
            legacy.OVERLAY_Y + vertical_offset,
            bottom_anchor=True,
            vertical_offset=vertical_offset,
            opacity=opacity,
        )
        help_requested = bool(
            help_image is not None and not self.info_help_target_suppressed[1]
        )
        if help_requested:
            self.p2_help_fade_image = help_image
        self.p2_help_fade_opacity = advance_info_opacity(
            self.p2_help_fade_opacity,
            help_requested,
            delta,
            immediate=self.transition_kind != "closing",
        )
        self.p2_help_presented = bool(help_requested or self.p2_help_fade_opacity > 0.0)
        if self.p2_help_fade_image is not None and self.p2_help_fade_opacity > 0.0:
            self.draw_custom_panel(
                self.help_panel,
                self.p2_help_fade_image,
                legacy.HELP_X + HELP_PANEL_X_COMPENSATION,
                int(round(self.p2_help_layout_y)),
                opacity=opacity * self.p2_help_fade_opacity,
            )
        else:
            self.help_panel.hide()
            if not help_requested:
                self.p2_help_fade_image = None

    def update_damage_meter(
        self,
        snapshot: session_state.SessionSnapshot,
        *,
        visible: bool,
        foreground: bool,
        now: float,
    ) -> None:
        self.live_damage_meter.update(
            snapshot,
            self.active_party,
            self.menu_theme_tracker.selection,
            presentation_visible=visible,
            foreground=foreground,
            now=now,
        )

    def maintain_loot_ownership(self, now: float) -> None:
        bridge = self.loot_ownership
        if bridge is None or bridge.installed or now < self.loot_ownership_retry_at:
            return
        try:
            bridge.maintain(True)
        except (OSError, RuntimeError, ValueError) as error:
            self.loot_ownership_retry_at = now + 0.5
            signature = type(error).__name__, str(error)
            if signature != self.loot_ownership_last_error:
                print(
                    f"COOP_LOOT_OWNERSHIP WAITING {signature[0]}: {signature[1]}",
                    flush=True,
                )
                self.loot_ownership_last_error = signature
            return
        self.loot_ownership_last_error = None
        print("COOP_LOOT_OWNERSHIP ON", flush=True)

    @staticmethod
    def _safe_frame_diagnostic(message: str) -> None:
        """Keep a broken diagnostic stream from defeating frame recovery."""

        try:
            print(message, flush=True)
        except Exception:
            pass

    @staticmethod
    def _frame_error_description(error: BaseException) -> str:
        try:
            return str(error)
        except Exception:
            return f"<unprintable {type(error).__name__}>"

    def _report_frame_subsystem_failure(
        self,
        subsystem: str,
        error: BaseException,
    ) -> None:
        failures = getattr(self, "frame_subsystem_failures", None)
        if failures is None:
            failures = {}
            self.frame_subsystem_failures = failures
        description = self._frame_error_description(error)
        signature = type(error).__name__, description
        if failures.get(subsystem) == signature:
            return
        failures[subsystem] = signature
        self._safe_frame_diagnostic(
            "HEADLESS_MENU_FRAME_SUBSYSTEM_RETRY "
            f"subsystem={subsystem} error={signature[0]}: {description}"
        )

    def _clear_frame_subsystem_failure(self, subsystem: str) -> None:
        failures = getattr(self, "frame_subsystem_failures", None)
        if failures is not None:
            failures.pop(subsystem, None)

    def _pump_window_messages(self, message: wt.MSG) -> None:
        """Drain a bounded Win32 batch without sacrificing the input frame."""

        try:
            for _ in range(WINDOW_MESSAGE_BUDGET_PER_FRAME):
                if not legacy.ui.user32.PeekMessageW(
                    ct.byref(message), None, 0, 0, legacy.ui.PM_REMOVE
                ):
                    break
                legacy.ui.user32.TranslateMessage(ct.byref(message))
                legacy.ui.user32.DispatchMessageW(ct.byref(message))
        except (OSError, struct.error, ValueError) as error:
            self._report_frame_subsystem_failure("windows-message-pump", error)
            return
        self._clear_frame_subsystem_failure("windows-message-pump")

    def _sample_game_foreground(self) -> bool:
        """Fail keyboard ownership closed while preserving background pads."""

        try:
            foreground = bool(legacy.coop.process_is_foreground(self.pid))
        except (OSError, struct.error, ValueError) as error:
            self._report_frame_subsystem_failure("foreground-sample", error)
            return False
        self._clear_frame_subsystem_failure("foreground-sample")
        return foreground

    def _maintain_panel_transport_heartbeat(self) -> None:
        """Keep the recoverable D3D publisher alive without blocking GDI."""

        panel = getattr(self, "panel", None)
        transport = getattr(panel, "d3d_transport", None)
        if transport is None:
            return
        try:
            available = bool(transport.touch())
        except (OSError, struct.error, ValueError) as error:
            self._report_frame_subsystem_failure("panel-heartbeat", error)
            available = False
        if not available:
            if "panel-heartbeat" not in getattr(
                self,
                "frame_subsystem_failures",
                {},
            ):
                self._report_frame_subsystem_failure(
                    "panel-heartbeat",
                    OSError("shared panel transport temporarily unavailable"),
                )
            # Every LayeredPanel for this PID shares this recoverable object.
            # Retain its identity so publish() can reopen the mapping after its
            # bounded backoff, but invalidate the local D3D fast path so each
            # panel paints through GDI immediately on this same frame.
            panels = list(getattr(self, "overlay_panels", ()))
            damage_panel = getattr(
                getattr(self, "live_damage_meter", None),
                "panel",
                None,
            )
            if damage_panel is not None:
                panels.append(damage_panel)
            for candidate in panels:
                if getattr(candidate, "d3d_transport", None) is not transport:
                    continue
                candidate.d3d_visible = False
                candidate._last_d3d_draw_key = None
            return
        self._clear_frame_subsystem_failure("panel-heartbeat")

    def _set_overlay_foreground_resilient(self, foreground: bool) -> None:
        """Keep one damaged presentation window out of the control path."""

        failed = False
        for overlay_panel in self.overlay_panels:
            try:
                overlay_panel.set_game_foreground(foreground)
            except (OSError, struct.error, ValueError) as error:
                failed = True
                self._report_frame_subsystem_failure(
                    "overlay-foreground",
                    error,
                )
        if not failed:
            self._clear_frame_subsystem_failure("overlay-foreground")

    def update_frame(self, foreground: bool) -> None:
        frame_now = time.monotonic()
        self.maintain_orphaned_ui_tick_dispatcher()
        self.maintain_owned_ui_tick_dispatchers()
        self.maintain_loot_ownership(frame_now)
        self.frame_world_interactions = None
        self.frame_interaction_projection = None
        self.battle_menu_sounds = self.battle_menu_sound_tracker.reload()
        overlay_visible = self.panel.game_can_present()
        self._set_overlay_foreground_resilient(foreground)
        snapshot = self.refresh_session_state()
        interaction_transition_ready = self.interaction_transition_gate.allows(
            snapshot, frame_now
        )
        self.maintain_native_frontend_suspension(snapshot, now=frame_now)
        raw_native_pause_open = native_pause_screen_open(self.process)
        native_pause_open = self.native_pause_visibility_gate.update(
            raw_native_pause_open,
            foreground=foreground,
            now=frame_now,
        )
        native_action_ids = native_party_action_ids(self.process, self.party)
        self.native_quickening_active = bool(
            native_action_ids & QUICKENING_PRESENTATION_ACTION_IDS
        )
        presentation_suppressed = custom_ui_suppression_required(
            snapshot,
            native_ui_suppressed=native_ui_render_suppressed(self.process),
            native_pause_open=native_pause_open,
            mist_presentation_active=bool(
                native_action_ids & MIST_PRESENTATION_ACTION_IDS
            ),
            owned_wait_presentation=(snapshot.reason == "custom Wait command menu"),
        )
        self.apply_session_gates(
            snapshot,
            presentation_suppressed=presentation_suppressed,
        )
        self.native_dialog_bridge.maintain()
        self.native_interaction_router.maintain(
            modal_active=snapshot.native_modal_active,
            now=frame_now,
        )
        self.native_interaction_router.maintain_prompt_scans(
            enabled=bool(
                interaction_transition_ready
                and snapshot.user_control_enabled
                and not snapshot.native_menu_open
                and not snapshot.native_modal_active
                and not self.dead_leader_menu_gate.pending
            ),
            candidates=self.follower_prompt_scan_candidates(),
            now=frame_now,
        )
        self.observe_p2_native_action()
        self.observe_p3_native_action()
        self.maintain_native_combat_indicator()
        # Input owns the sound queue.  Its finally-path flushes before fallible
        # raster/presenter work and even if input itself encounters transient
        # remote I/O, so old navigation sounds cannot become a later burst.
        self.input_and_flush_one_native_sound(
            foreground,
            native_pause_open=native_pause_open,
        )
        self.refresh_actors(snapshot=snapshot)
        self.refresh_runtime_catalogs(snapshot)
        self.maintain_party_edit_capture(
            self.session_snapshot,
            now=time.monotonic(),
        )
        self.sync_presenter_character_ids()
        self.maintain_menu_raster_warmup(
            snapshot,
            now=time.monotonic(),
        )
        custom_command_menu_logically_open = bool(
            self.p1_model.mode is not MenuMode.CLOSED
            or self.model.mode is not MenuMode.CLOSED
            or self.p3_model.mode is not MenuMode.CLOSED
        )
        custom_command_menu_open = bool(
            custom_command_menu_logically_open
            and not self.native_frontend_custom_suspended
            and not self.dead_leader_menu_gate.pending
        )
        custom_command_transition_active = bool(
            not self.native_frontend_custom_suspended
            and not self.dead_leader_menu_gate.pending
            and (
                self.p1_transition_kind is not None
                or self.transition_kind is not None
                or self.p3_transition_kind is not None
            )
        )
        self.native_dialog_bridge.set_party_menu_blocked(
            custom_command_menu_open
            or custom_command_transition_active
            or self.native_party_menu_request_pending
        )
        presentation_visible = bool(
            overlay_visible
            and self.coop_runtime_active
            and not self.custom_ui_suppressed
            and not self.native_frontend_custom_suspended
        )
        # Preserve FF12's native resting position. Opening any custom command
        # panel moves the log to the co-op lane; the transition keeps ownership
        # until the panel has visually finished closing.
        self.boss_gauge_visible = native_boss_gauge_visible(self.process)
        self.combat_log_layout.maintain(
            combat_log_raise_requested(
                snapshot,
                custom_menu_open=custom_command_menu_open,
                custom_menu_transition_active=custom_command_transition_active,
            ),
            boss_gauge_visible=self.boss_gauge_visible,
        )
        combat_log_authored_visible = self.combat_log_visibility.sample()
        self.combat_log_gate_hidden = bool(
            self.native_combat_log_target_gate is not None
            and self.native_combat_log_target_gate.applied_hidden
        )
        self.combat_log_on_screen = bool(
            combat_log_authored_visible and not self.combat_log_gate_hidden
        )
        self.update_upper_info_layout()
        self.update_player_info_lanes()
        p1_visual_visible = bool(presentation_visible and snapshot.p1_menu_allowed)
        p2_visual_visible = bool(
            presentation_visible
            and self.p2_manual_control_enabled
            and snapshot.p2_menu_allowed
        )
        p3_visual_visible = bool(
            presentation_visible
            and self.p3_manual_control_enabled
            and p3_session_menu_allowed(snapshot, len(self.party))
        )
        self.native_wait_mode_bridge.maintain(
            custom_command_menu_open,
            native_presentation=not custom_menu_leaves_free_player(
                self.p1_model.mode,
                self.model.mode,
                self.p3_model.mode,
                p2_enabled=p2_visual_visible,
                p3_enabled=p3_visual_visible,
            ),
        )
        interaction_visible = bool(
            presentation_visible
            and interaction_transition_ready
            and snapshot.gameplay_ready
            and snapshot.user_control_enabled
            and not snapshot.native_menu_open
            and not snapshot.native_modal_active
            and not self.dead_leader_menu_gate.pending
        )
        p2_interaction_visible = bool(
            interaction_visible
            and self.p2_manual_control_enabled
            and self.ko_promoted_follower_role != "p2"
        )
        p3_interaction_visible = bool(
            interaction_visible
            and self.p3_manual_control_enabled
            and self.ko_promoted_follower_role != "p3"
        )
        if self.native_frontend_custom_suspended:
            self.hide_custom_menu_visuals_preserving_state()
        elif self.custom_ui_suppressed:
            self.hide_custom_menu_visuals_preserving_state()
        else:
            self.sync_area_target_cursors()
            # Native circle upkeep is control-state work, not panel painting.
            # Publish it before any raster presenter so a damaged/closed menu
            # model cannot starve all three circles for the remainder of the
            # frame (or repeatedly across the frame retry boundary).
            self.sync_world_target()
            self.paint_p1_custom_menu(p1_visual_visible)
            self.paint_p1_target_info(p1_visual_visible)
            self.paint_world_target(p2_visual_visible)
            self.paint_p3_target_info(p3_visual_visible)
            self.paint_target_info_backdrop()
            self.paint_party_target_cursors(
                p1_visual_visible,
                p2_visual_visible,
                p3_visual_visible,
            )
            self.paint_p2_interaction_prompt(p2_interaction_visible)
            self.paint_p3_interaction_prompt(p3_interaction_visible)
            self.paint(p2_visual_visible)
            self.paint_p3(p3_visual_visible)
            self.publish_p1_help_badge(p1_visual_visible)
        self.update_damage_meter(
            snapshot,
            visible=presentation_visible,
            foreground=foreground,
            now=time.monotonic(),
        )
        if self.native_combat_log_target_gate is not None:
            self.native_combat_log_target_gate.request_hidden(
                self.custom_target_info_presented(presentation_visible)
            )

    def _update_frame_resilient(self, foreground: bool, update=None) -> None:
        """Quarantine process-memory failures without losing the worker.

        Party rebuilds and area handoffs can invalidate one sampled pointer after
        the surrounding session snapshot was read.  A single failed remote write
        must not tear down every menu/input hook.  Loading, KO/Stone rebuilds and
        a suspended game can all last longer than an arbitrary wall-clock retry
        window, so an ``OSError``, torn ``struct.error`` or incoherent decoded
        ``ValueError`` sample is never promoted into worker shutdown while the
        runtime itself has not requested shutdown. Structural runtime errors
        remain uncaught and promptly expose incompatible code.
        """

        if update is None:
            update = self.update_frame
            update_arguments = (foreground,)
        else:
            update_arguments = ()
        try:
            update(*update_arguments)
        except (OSError, struct.error, ValueError) as error:
            if runtime_shutdown.requested(force=True):
                self.running = False
                return

            native_dialog_bridge = getattr(self, "native_dialog_bridge", None)
            if bool(
                getattr(self, "coop_runtime_active", False)
                and native_dialog_bridge is not None
                and getattr(native_dialog_bridge, "installed", False)
            ):
                try:
                    native_dialog_bridge.fail_closed_for_recovery()
                except BaseException as firewall_error:
                    self._report_frame_subsystem_failure(
                        "input-firewall-recovery",
                        firewall_error,
                    )
                else:
                    self._clear_frame_subsystem_failure(
                        "input-firewall-recovery"
                    )

            now = time.monotonic()
            error_number = getattr(error, "errno", None)
            description = self._frame_error_description(error)
            started = getattr(self, "frame_io_retry_started", None)
            if started is None:
                self.frame_io_retry_started = now
                self.frame_io_retry_signature = (error_number, description)
                self.frame_io_retry_last_log = now
                self._safe_frame_diagnostic(
                    "HEADLESS_MENU_FRAME_IO_RETRY "
                    f"errno={error_number!r} error={description}"
                )
                return
            last_log = getattr(self, "frame_io_retry_last_log", started)
            if now - last_log >= FRAME_IO_RETRY_WINDOW_SECONDS:
                self.frame_io_retry_last_log = now
                self._safe_frame_diagnostic(
                    "HEADLESS_MENU_FRAME_IO_STILL_RETRYING "
                    f"seconds={max(0.0, now - started):.3f} "
                    f"errno={error_number!r} error={description}"
                )
            return

        started = getattr(self, "frame_io_retry_started", None)
        if started is not None:
            elapsed = max(0.0, time.monotonic() - started)
            self._safe_frame_diagnostic(
                f"HEADLESS_MENU_FRAME_IO_RECOVERED seconds={elapsed:.3f}"
            )
            self.frame_io_retry_started = None
            self.frame_io_retry_signature = None
            self.frame_io_retry_last_log = None

    def _cleanup_after_run(self, *, preserve_input_firewall: bool = False) -> None:
        """Restore every independently owned resource even if one restore fails."""

        if getattr(self, "_runtime_cleanup_complete", False):
            return
        self._runtime_cleanup_complete = True
        failures: list[str] = []

        def attempt(label: str, callback) -> None:
            try:
                callback()
            except BaseException as error:
                description = self._frame_error_description(error)
                failures.append(
                    f"{label}: {type(error).__name__}: {description}"
                )

        def release(name: str, method: str) -> None:
            resource = getattr(self, name, None)
            if resource is None:
                return
            attempt(f"{name}.{method}", lambda: getattr(resource, method)())

        if preserve_input_firewall:
            # A fatal/restart exit transfers ownership to a replacement shell.
            # Do not restore, cancel or clear any process-resident router,
            # panel, handoff or firewall: every such write creates a visible
            # native-input gap.  Only force the dialog bridge fail-closed and
            # release this worker's local handles; process exit releases the
            # remaining OS handles without mutating the game.
            release("native_dialog_bridge", "fail_closed_for_recovery")
            # Retry the idempotent publication before releasing local handles.
            # A transient first WPM failure must not leave the adopted bridge in
            # an unknown state while the replacement worker is taking over.
            release("native_dialog_bridge", "fail_closed_for_recovery")
            release("process", "close")
            session_mutex = getattr(self, "session_mutex", None)
            if session_mutex:
                attempt(
                    "session_mutex.CloseHandle",
                    lambda: kernel32.CloseHandle(session_mutex),
                )
                self.session_mutex = None
            for failure in failures:
                try:
                    print(
                        f"HEADLESS_MENU_CLEANUP_WARNING {failure}",
                        flush=True,
                    )
                except BaseException:
                    pass
            return

        # Pending Party-menu transactions belong to this world/session and this
        # worker only.  Revoke their local latches before native ownership is
        # restored so no teardown callback can observe a publishable stale edit.
        self.pending_party_intent_publish = None
        self._clear_native_party_menu_request()
        release("party_edit_capture", "cancel")

        release("live_damage_meter", "close")

        target_gate = getattr(self, "native_combat_log_target_gate", None)
        if target_gate is not None:
            def restore_target_gate() -> None:
                if not target_gate.restore():
                    print(
                        "NATIVE_COMBAT_LOG_TARGET_GATE_DEFERRED_RESTORE",
                        flush=True,
                    )

            attempt("native_combat_log_target_gate.restore", restore_target_gate)

        release("native_stock_field_hud", "restore")
        release("combat_log_layout", "restore")
        release("native_wait_mode_bridge", "restore")
        release("loot_ownership", "restore")
        release("native_treasure_log", "restore")
        release("native_p1_target_circle", "restore")
        release("native_target_circle", "restore")
        release("native_p3_target_circle", "restore")
        release("native_area_render_gate", "restore")
        release("native_p1_area_circle", "restore")
        release("native_p2_area_circle", "restore")
        release("native_p3_area_circle", "restore")
        release("native_target_circle_capacity", "restore")

        for label, callback in (
            ("clear_world_target", self.clear_world_target),
            ("clear_d3d_target_rings", self.clear_d3d_target_rings),
            ("publish_p1_help_badge", lambda: self.publish_p1_help_badge(False)),
        ):
            attempt(label, callback)

        release("native_submitter", "restore")
        release("native_combat_indicator", "restore")
        release("native_action_line_theme_router", "restore")
        release("native_sound_player", "restore")
        attempt(
            "cancel_p1_leader_handoff",
            lambda: self.cancel_p1_leader_handoff("shutdown"),
        )
        release("native_interaction_router", "shutdown")
        release("native_p1_list_bridge", "restore")
        if preserve_input_firewall:
            # Retry after the slower native/UI restores as well.  The first
            # publication is what closes the gap; this idempotent second chance
            # handles a short remote-memory instability during fatal teardown.
            release("native_dialog_bridge", "fail_closed_for_recovery")
        else:
            release("native_dialog_bridge", "restore")

        for name in (
            "world_marker_panel",
            "interaction_prompt_panel",
            "p3_interaction_prompt_panel",
            "panel",
            "help_panel",
            "target_info_panel",
            "p1_target_info_panel",
            "p1_panel",
            "p1_help_panel",
            "p3_panel",
            "p3_help_panel",
            "p3_target_info_panel",
            "p1_party_target_cursor_panel",
            "p2_party_target_cursor_panel",
            "p3_party_target_cursor_panel",
        ):
            release(name, "close")

        release("process", "close")
        session_mutex = getattr(self, "session_mutex", None)
        if session_mutex:
            attempt(
                "session_mutex.CloseHandle",
                lambda: kernel32.CloseHandle(session_mutex),
            )
            self.session_mutex = None

        for failure in failures:
            try:
                print(f"HEADLESS_MENU_CLEANUP_WARNING {failure}", flush=True)
            except BaseException:
                pass

    def run(self) -> None:
        message = wt.MSG()
        next_frame = time.perf_counter()
        normal_shutdown = False
        self._safe_frame_diagnostic(
            f"HEADLESS_MENU_V2_READY pid={self.pid} cave={self.base:#x} "
            "F=battle Space/Enter=confirm I/K=navigate Q/E=group "
            "C/Backspace=back Esc=pause"
        )
        try:
            self._update_frame_resilient(
                False,
                update=lambda: self.native_stock_field_hud.maintain(
                    hide_combat_log=False,
                ),
            )
            while self.running:
                if runtime_shutdown.requested():
                    normal_shutdown = True
                    self.running = False
                    break
                self._pump_window_messages(message)
                foreground = self._sample_game_foreground()
                self._maintain_panel_transport_heartbeat()

                self._update_frame_resilient(foreground)
                if not self.running:
                    break

                next_frame += FRAME_INTERVAL
                remaining = next_frame - time.perf_counter()
                if remaining > 0.0:
                    time.sleep(remaining)
                elif remaining < -FRAME_INTERVAL * 4.0:
                    next_frame = time.perf_counter()
        finally:
            # A supervisor replacement adopts the resident bridge.  Keep the
            # command/leader/Party firewalls closed across an abnormal worker
            # exit; only an explicit runtime shutdown restores native input.
            if runtime_shutdown.preserve_resident_runtime(force=True):
                self._cleanup_after_run(preserve_input_firewall=True)
            elif normal_shutdown or runtime_shutdown.requested(force=True):
                self._cleanup_after_run()
            else:
                self._cleanup_after_run(preserve_input_firewall=True)

def summon_dismiss_owner(
    current_layout: tuple[int, ...] | list[int],
    leader_party_member_id: int | None,
) -> int | None:
    """Return the live main-party member that owns an active summon."""
    if leader_party_member_id in session_state.MAIN_PARTY_MEMBER_IDS:
        return int(leader_party_member_id)
    return next(
        (
            int(member_id)
            for member_id in current_layout[:4]
            if member_id in session_state.MAIN_PARTY_MEMBER_IDS
        ),
        None,
    )


if __name__ == "__main__":
    CoopMenuRuntime().run()
