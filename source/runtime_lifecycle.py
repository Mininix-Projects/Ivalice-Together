from __future__ import annotations

import math


CAMERA_NATIVE_READY_SAMPLES = 3


def native_desktop_filter_required(coop_active, desktop_owner):
    """Filter native desktop input only while a follower owns the desktop.

    P1 must retain the stock keyboard/mouse path, and disabling co-op must
    release the filter immediately regardless of the last sampled owner.
    """

    return bool(coop_active and desktop_owner != "p1")


def follower_role_owns_movement(
    enabled,
    actor_alive,
    native_leader_member_id,
    role_member_id,
    *,
    actor_present=True,
    identity_current=True,
    layout_coherent=True,
    allow_native_leader=False,
):
    return bool(
        enabled
        and actor_present
        and identity_current
        and layout_coherent
        and actor_alive is not False
        and role_member_id is not None
        and (native_leader_member_id != role_member_id or allow_native_leader)
    )


def retain_owner_during_mapping_gap(
    *,
    mapping_unknown,
    coop_active,
    manual_control,
    current_owner,
    role_member_id,
    native_leader_member_id,
    world_epoch,
    allow_native_leader=False,
):
    """Retain only the same explicitly requested owner across a scan gap."""

    return bool(
        mapping_unknown
        and coop_active
        and manual_control
        and current_owner is not None
        and role_member_id is not None
        and current_owner.member_id == role_member_id
        and current_owner.world_epoch == world_epoch
        and (
            native_leader_member_id != role_member_id
            or allow_native_leader
        )
    )


def retain_owner_during_liveness_gap(
    *,
    actor_alive,
    coop_active,
    manual_control,
    current_owner,
    candidate_owner,
    role_member_id,
    native_leader_member_id,
    world_epoch,
    allow_native_leader=False,
):
    """Retain an exact owner while one coherent status sample is unavailable."""

    return bool(
        actor_alive is None
        and coop_active
        and manual_control
        and current_owner is not None
        and candidate_owner is not None
        and current_owner == candidate_owner
        and role_member_id is not None
        and current_owner.member_id == role_member_id
        and current_owner.world_epoch == world_epoch
        and (
            native_leader_member_id != role_member_id
            or allow_native_leader
        )
    )


def role_member_changed(resolved_identity, current_identity, player):
    if resolved_identity is None:
        return True
    return resolved_identity.member_id(player) != current_identity.member_id(player)


def role_member_layout_coherent(member_id, current_layout, ui_layout):
    if member_id is None:
        return False
    return bool(
        member_id in tuple(current_layout[:4]) and member_id in tuple(ui_layout[:4])
    )


def role_cache_survives_scan_gap(
    *,
    member_changed,
    member_id,
    current_layout,
    ui_layout,
    actor_validity,
):
    return bool(
        not member_changed
        and member_id is not None
        and role_member_layout_coherent(member_id, current_layout, ui_layout)
        and actor_validity is not False
    )


def fresh_role_actor_mapping(entry):
    if entry is None:
        return 0, 0
    return int(entry[1]), int(entry[2])


def follower_movement_master_enabled(base_active, p2_owned, p3_owned):
    return bool(base_active and (p2_owned or p3_owned))


def berserk_leader_owns_movement(
    base_movement_active,
    p1_alive,
    actor_present,
    native_leader_member_id,
    p1_owner_member_id,
    has_berserk,
    accepts_manual_movement,
):
    return bool(
        base_movement_active
        and p1_alive is True
        and actor_present
        and native_leader_member_id == p1_owner_member_id
        and has_berserk is True
        and accepts_manual_movement is True
    )


def coop_lifecycle_flags(
    enabled,
    p1_alive,
    p2_alive,
    p3_alive=None,
    *,
    p2_present=True,
    p3_present=None,
):
    logical = bool(enabled)
    if p3_present is None:
        p3_present = p3_alive is not None
    p2_available = bool(p2_present and p2_alive is not False)
    p3_available = bool(p3_present and p3_alive is not False)
    movement = bool(enabled and (p2_available or p3_available))
    camera_role_count = sum((p1_alive is True, p2_available, p3_available))
    shared_camera = bool(enabled and camera_role_count >= 2)
    return logical, movement, shared_camera


def coop_movement_control_flags(
    lifecycle_movement_active,
    session_movement_allowed,
    custom_wait_owned,
    battle_mode_active,
):
    input_allowed = bool(
        lifecycle_movement_active
        and (session_movement_allowed or (custom_wait_owned and battle_mode_active))
    )
    ownership_active = bool(
        lifecycle_movement_active and (session_movement_allowed or custom_wait_owned)
    )
    return ownership_active, input_allowed


def follower_interaction_routing_active(
    logical_active,
    session_movement_allowed,
    p2_movement_owned,
    p3_movement_owned,
    *,
    town,
):
    """Allow private scans only for a live, controllable field follower."""

    return bool(
        logical_active
        and session_movement_allowed
        and not town
        and (p2_movement_owned or p3_movement_owned)
    )


def follower_session_movement_allowed(
    snapshot,
    *,
    p2_present: bool,
    p3_present: bool,
):
    if snapshot.movement_allowed:
        return True
    identity = snapshot.identity
    follower_ready = bool(
        (
            p2_present
            and role_member_layout_coherent(
                identity.p2_party_member_id,
                snapshot.current_layout,
                snapshot.ui_layout,
            )
        )
        or (
            p3_present
            and role_member_layout_coherent(
                identity.p3_party_member_id,
                snapshot.current_layout,
                snapshot.ui_layout,
            )
        )
    )
    return bool(
        snapshot.p1_gameplay_ready
        and snapshot.user_control_enabled
        and not snapshot.native_menu_open
        and not snapshot.native_modal_active
        and not snapshot.loading
        and not snapshot.cutscene
        and follower_ready
    )


def p1_session_movement_allowed(snapshot):
    return bool(
        snapshot.p1_gameplay_ready
        and snapshot.user_control_enabled
        and not snapshot.native_menu_open
        and not snapshot.native_modal_active
        and not snapshot.loading
        and not snapshot.cutscene
    )


def camera_session_gate(
    lifecycle_camera_active,
    session_camera_allowed,
    gameplay_ready,
    loading,
    interaction_owner,
):
    interaction_hold = bool(interaction_owner and gameplay_ready and not loading)
    return bool(lifecycle_camera_active and (session_camera_allowed or interaction_hold))


def advance_camera_reacquire_gate(
    requested,
    was_requested,
    pending,
    ready_samples,
    native_ready,
    required_samples=CAMERA_NATIVE_READY_SAMPLES,
):
    if not requested:
        return False, False, 0
    if not was_requested:
        pending = True
        ready_samples = 0
    if not pending:
        return True, False, ready_samples
    ready_samples = ready_samples + 1 if native_ready else 0
    if ready_samples >= max(1, int(required_samples)):
        return True, False, ready_samples
    return False, True, ready_samples


def camera_party_positions_ready(*positions):
    try:
        party = [
            tuple(float(value) for value in position[:3]) for position in positions
        ]
    except (TypeError, ValueError):
        return False
    if len(party) < 2 or any(len(position) != 3 for position in party):
        return False
    if not all(math.isfinite(value) for position in party for value in position):
        return False
    epsilon_squared = 1.0e-8
    return all(
        sum(value * value for value in position) > epsilon_squared
        for position in party
    )
