"""Pure ownership and native-follow handoff policy.

No process reads, writes or clocks live here.  Callers supply observations and
time, receive ordered effects, and execute those effects only after revalidating
the same owner token at the process boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping


FOLLOW_ACTION = 0x4124
NATIVE_IDLE_ACTION = 0x4021
FOLLOW_REPAIR_MAX_ATTEMPTS = 4
FOLLOW_REPAIR_RETRY_SECONDS = 0.100
FOLLOW_REPAIR_DEADLINE_SECONDS = 1.000


@dataclass(frozen=True)
class OwnerToken:
    role: str
    member_id: int
    actor: int
    phys: int
    actor_generation: int
    world_epoch: int
    native_handle: int = 0

    @property
    def incarnation_key(self) -> tuple[int, int, int]:
        return self.member_id, self.actor_generation, self.world_epoch


@dataclass(frozen=True)
class FollowerObservation:
    token: OwnerToken
    sample: int
    available: bool
    alive: bool | None
    materialized: bool
    layout_coherent: bool
    leader_member_id: int | None
    cutscene: bool
    status_blocked: bool | None = False
    action_state: int | None = None
    target: int | None = None
    action: int | None = None
    descriptor_action: int | None = None
    follows_native_list: bool = False

    @property
    def confirmed(self) -> bool:
        return bool(
            self.available
            and self.alive is True
            and self.materialized
            and self.layout_coherent
            and self.token.native_handle
            and self.status_blocked is False
            and self.leader_member_id is not None
            and self.leader_member_id != self.token.member_id
            and self.action == FOLLOW_ACTION
            and self.follows_native_list
        )

    @property
    def safe_to_request(self) -> bool:
        return bool(
            self.available
            and self.alive is True
            and self.materialized
            and self.layout_coherent
            and self.token.native_handle
            and not self.cutscene
            and self.status_blocked is False
            and self.leader_member_id is not None
            and self.leader_member_id != self.token.member_id
            and self.action_state == 1
            and self.target == 0
            and self.action == NATIVE_IDLE_ACTION
            and self.descriptor_action == FOLLOW_ACTION
            and self.follows_native_list
        )

    @property
    def hard_cancel(self) -> bool:
        if self.cutscene:
            return True
        if not self.materialized or not self.layout_coherent:
            return True
        # Death and temporary native leadership are eligibility gates, not
        # proof that this actor identity was destroyed. Keep only a write-free
        # intent while either is observed; actor/physics/handle/layout/epoch
        # changes above still prove reincarnation and cancel the stale token.
        # Unknown reads and valid native actions are transient for the same
        # reason.
        return False


@dataclass(frozen=True)
class FollowRepair:
    token: OwnerToken
    created_at: float
    deadline: float
    attempts: int = 0
    next_attempt_at: float = 0.0
    requested_sample: int | None = None
    passive_deferral_observed: bool = False


class EffectKind(str, Enum):
    DISARM_OLD = "disarm_old"
    CLEAR_OLD_METADATA = "clear_old_metadata"
    REQUEST_NATIVE_FOLLOW = "request_native_follow"
    ACQUIRE_NEW = "acquire_new"
    CONFIRM_NATIVE_FOLLOW = "confirm_native_follow"
    CANCEL_NATIVE_FOLLOW = "cancel_native_follow"


@dataclass(frozen=True)
class HandoffEffect:
    kind: EffectKind
    token: OwnerToken
    reason: str = ""


@dataclass(frozen=True)
class HandoffState:
    owners: tuple[tuple[str, OwnerToken], ...] = ()
    repairs: tuple[FollowRepair, ...] = ()

    def owner_map(self) -> dict[str, OwnerToken]:
        return dict(self.owners)


def _sorted_owners(
    owners: Mapping[str, OwnerToken],
) -> tuple[tuple[str, OwnerToken], ...]:
    return tuple(sorted(owners.items()))


def _repair_key(repair: FollowRepair) -> tuple[int, int, int]:
    return repair.token.incarnation_key


def _advance_one(
    repair: FollowRepair,
    observation: FollowerObservation | None,
    *,
    now: float,
    world_epoch: int,
) -> tuple[FollowRepair | None, tuple[HandoffEffect, ...]]:
    token = repair.token
    if token.world_epoch != world_epoch:
        return None, (
            HandoffEffect(EffectKind.CANCEL_NATIVE_FOLLOW, token, "world_epoch"),
        )
    if observation is not None and observation.token != token:
        return None, (
            HandoffEffect(EffectKind.CANCEL_NATIVE_FOLLOW, token, "generation"),
        )
    if observation is not None and observation.hard_cancel:
        return None, (
            HandoffEffect(EffectKind.CANCEL_NATIVE_FOLLOW, token, "ineligible"),
        )
    if (
        observation is not None
        and observation.confirmed
        and repair.requested_sample is None
    ):
        return None, (
            HandoffEffect(EffectKind.CONFIRM_NATIVE_FOLLOW, token, "already_native"),
        )
    if (
        observation is not None
        and observation.confirmed
        and repair.requested_sample is not None
        and observation.sample > repair.requested_sample
    ):
        return None, (
            HandoffEffect(EffectKind.CONFIRM_NATIVE_FOLLOW, token, "accepted"),
        )
    native_eligibility_deferral = bool(
        observation is not None
        and (
            observation.alive is False
            or observation.status_blocked is True
            or observation.leader_member_id == token.member_id
        )
    )
    uncertain_sample = bool(
        observation is not None
        and (
            not observation.available
            or observation.alive is None
            or observation.status_blocked is None
        )
    )
    if native_eligibility_deferral:
        # No native request is made and therefore no bounded request attempt is
        # consumed. KO, Stone, other native command gates and temporary native
        # leadership can legitimately outlive every wall-clock retry window.
        return replace(
            repair,
            deadline=max(repair.deadline, now + FOLLOW_REPAIR_DEADLINE_SECONDS),
            passive_deferral_observed=True,
        ), ()
    if uncertain_sample:
        # Transport/read gaps are also write-free, but they do not turn an
        # unrelated protected action into an unbounded repair. Preserve only a
        # native-status deferral that had already been proven.
        return replace(
            repair,
            deadline=max(repair.deadline, now + FOLLOW_REPAIR_DEADLINE_SECONDS),
        ), ()
    elif repair.passive_deferral_observed:
        if observation is None or not observation.safe_to_request:
            # Status/action publication is not atomic. The status word can
            # clear arbitrarily earlier than the action packet and follower
            # list settle. Retain a passive exact-incarnation intent until the
            # complete safe contract appears; this performs no process write.
            return replace(
                repair,
                deadline=max(
                    repair.deadline,
                    now + FOLLOW_REPAIR_DEADLINE_SECONDS,
                ),
            ), ()
        # A newly eligible episode receives its own bounded request window.
        # Attempts issued before a later KO/status gate cannot acknowledge this
        # post-clear native state and therefore must not consume the new window.
        repair = replace(
            repair,
            deadline=now + FOLLOW_REPAIR_DEADLINE_SECONDS,
            attempts=0,
            next_attempt_at=now,
            requested_sample=None,
            passive_deferral_observed=False,
        )
    if now >= repair.deadline:
        passive_deferral = bool(
            observation is not None
            and not observation.hard_cancel
            and (
                not observation.available
                or observation.alive is not True
                or observation.leader_member_id is None
                or observation.status_blocked is not False
            )
        )
        if passive_deferral:
            # A read gap proves neither ineligibility nor native completion. A
            # native command-blocking status can also outlive any safe wall-clock
            # deadline (Stone persists until cured). Keep a passive, write-free
            # intent for this exact incarnation while attempts remain bounded;
            # world/generation/layout checks above still cancel stale pointers.
            return replace(
                repair,
                deadline=now + FOLLOW_REPAIR_DEADLINE_SECONDS,
            ), ()
        return None, (HandoffEffect(EffectKind.CANCEL_NATIVE_FOLLOW, token, "bounded"),)
    if repair.attempts >= FOLLOW_REPAIR_MAX_ATTEMPTS:
        # The fourth request owns the same acknowledgement window as every
        # earlier request.  A fast runtime tick must not cancel it before the
        # next native sample has had the configured retry interval to land.
        if now < repair.next_attempt_at:
            return repair, ()
        return None, (HandoffEffect(EffectKind.CANCEL_NATIVE_FOLLOW, token, "bounded"),)
    if (
        observation is None
        or not observation.safe_to_request
        or now < repair.next_attempt_at
    ):
        return repair, ()

    advanced = replace(
        repair,
        attempts=repair.attempts + 1,
        next_attempt_at=now + FOLLOW_REPAIR_RETRY_SECONDS,
        requested_sample=observation.sample,
    )
    return advanced, (
        HandoffEffect(EffectKind.REQUEST_NATIVE_FOLLOW, token, "eligible"),
    )


def reconcile_owners(
    state: HandoffState,
    desired: Mapping[str, OwnerToken | None],
    *,
    acquire_eligible: Mapping[str, bool],
    observations: Mapping[tuple[int, int, int], FollowerObservation],
    now: float,
    world_epoch: int,
) -> tuple[HandoffState, tuple[HandoffEffect, ...]]:
    """Atomically plan all role changes, including P2/P3 swaps.

    Every old role is disarmed and cleared before any new role is acquired.
    An incarnation transferred directly to another manual role is not asked to
    follow during the transfer.
    """

    owners = state.owner_map()
    # A desired-but-ineligible transfer has not acquired anything yet.  Do not
    # suppress the old incarnation's follow repair across that gap: if the
    # desired mapping disappears (KO/loading/layout churn), otherwise neither
    # native follow nor manual ownership would remain.  Exact retained owners
    # still win over stale repairs even while their liveness is temporarily
    # unknown.
    replacement_keys = {
        token.incarnation_key
        for role, token in desired.items()
        if token is not None and acquire_eligible.get(role, False)
    }
    replacement_keys.update(
        token.incarnation_key
        for role, token in owners.items()
        if desired.get(role) == token
    )
    repairs = {
        _repair_key(item): item
        for item in state.repairs
        if _repair_key(item) not in replacement_keys
    }
    changed = tuple(
        role
        for role in sorted(set(owners) | set(desired))
        if owners.get(role) != desired.get(role)
    )
    if not changed:
        cleaned = HandoffState(state.owners, tuple(repairs.values()))
        return cleaned, ()

    effects: list[HandoffEffect] = []
    old_tokens = tuple(owners[role] for role in changed if role in owners)
    old_keys = {token.incarnation_key for token in old_tokens}
    for token in old_tokens:
        effects.append(HandoffEffect(EffectKind.DISARM_OLD, token))
    for token in old_tokens:
        effects.append(HandoffEffect(EffectKind.CLEAR_OLD_METADATA, token))
    # Reacquisition wins over repair even when the role was temporarily empty:
    # no later retry may ask a newly controlled actor to enter native follow.
    for token in old_tokens:
        if token.incarnation_key in replacement_keys:
            continue
        repair = FollowRepair(
            token,
            created_at=now,
            deadline=now + FOLLOW_REPAIR_DEADLINE_SECONDS,
            next_attempt_at=now,
        )
        observation = observations.get(token.incarnation_key)
        repair, repair_effects = _advance_one(
            repair,
            observation,
            now=now,
            world_epoch=world_epoch,
        )
        effects.extend(repair_effects)
        if repair is not None:
            repairs[_repair_key(repair)] = repair

    for role in changed:
        owners.pop(role, None)
    for role in changed:
        token = desired.get(role)
        if token is not None and acquire_eligible.get(role, False):
            owners[role] = token
            effects.append(
                HandoffEffect(
                    EffectKind.ACQUIRE_NEW,
                    token,
                    "transfer" if token.incarnation_key in old_keys else "",
                )
            )

    return (
        HandoffState(_sorted_owners(owners), tuple(repairs.values())),
        tuple(effects),
    )


def reject_acquisition(
    state: HandoffState,
    token: OwnerToken,
    *,
    restore_repair: bool = False,
    now: float = 0.0,
) -> HandoffState:
    """Drop a failed acquire and restore a suppressed transfer repair."""

    owners = state.owner_map()
    if owners.get(token.role) == token:
        owners.pop(token.role)
    repairs = {_repair_key(repair): repair for repair in state.repairs}
    if restore_repair and token.incarnation_key not in repairs:
        repairs[token.incarnation_key] = FollowRepair(
            token,
            created_at=now,
            deadline=now + FOLLOW_REPAIR_DEADLINE_SECONDS,
            next_attempt_at=now,
        )
    return HandoffState(_sorted_owners(owners), tuple(repairs.values()))


def advance_repairs(
    state: HandoffState,
    observations: Mapping[tuple[int, int, int], FollowerObservation],
    *,
    now: float,
    world_epoch: int,
) -> tuple[HandoffState, tuple[HandoffEffect, ...]]:
    repairs: list[FollowRepair] = []
    effects: list[HandoffEffect] = []
    for repair in state.repairs:
        advanced, emitted = _advance_one(
            repair,
            observations.get(repair.token.incarnation_key),
            now=now,
            world_epoch=world_epoch,
        )
        effects.extend(emitted)
        if advanced is not None:
            repairs.append(advanced)
    return HandoffState(state.owners, tuple(repairs)), tuple(effects)


def cancel_repairs(
    state: HandoffState,
    *,
    reason: str,
) -> tuple[HandoffState, tuple[HandoffEffect, ...]]:
    """Explicitly terminate every still-pending repair at a lifecycle boundary."""

    effects = tuple(
        HandoffEffect(EffectKind.CANCEL_NATIVE_FOLLOW, repair.token, reason)
        for repair in state.repairs
    )
    return HandoffState(state.owners, ()), effects


class WorldEpochTracker:
    def __init__(self) -> None:
        self.epoch = 0
        self._signature: object | None = None
        self._invalid_since_valid = False

    def observe(self, signature: object | None, *, valid: bool) -> int:
        if not valid or signature is None:
            if self.epoch:
                self._invalid_since_valid = True
            return self.epoch
        if not self.epoch or signature != self._signature or self._invalid_since_valid:
            self.epoch += 1
        self._signature = signature
        self._invalid_since_valid = False
        return self.epoch


class ActorGenerationTracker:
    """Assign generations without treating a transient scan gap as reincarnation."""

    def __init__(self) -> None:
        self._records: dict[int, tuple[int, int, int, int, bool, bool]] = {}
        self._next: dict[int, int] = {}
        self._epochs: dict[int, int] = {}

    def observe(
        self,
        role: str,
        member_id: int | None,
        actor: int,
        phys: int,
        *,
        native_handle: int = 0,
        world_epoch: int,
        alive: bool | None,
        structurally_present: bool,
    ) -> OwnerToken | None:
        if member_id is None:
            return None
        member_id = int(member_id)
        previous = self._records.get(member_id)
        if not structurally_present:
            if previous is not None:
                actor0, phys0, handle0, generation0, alive0, _present0 = previous
                self._records[member_id] = (
                    actor0,
                    phys0,
                    handle0,
                    generation0,
                    alive0,
                    False,
                )
            return None
        if not actor or not phys:
            # Coherent membership plus a missing scan is UNKNOWN, not a new actor.
            return None

        observed_handle = int(native_handle)
        stable_handle = observed_handle
        if (
            not observed_handle
            and previous is not None
            and previous[0] == int(actor)
            and previous[1] == int(phys)
        ):
            # A failed generation-handle read must not manufacture a new actor
            # incarnation. Preserve the last coherent handle until a nonzero
            # sample can either confirm it or prove a real generation change.
            stable_handle = previous[2]
        handle_changed = bool(
            previous is not None
            and previous[2]
            and observed_handle
            and previous[2] != observed_handle
        )
        reincarnated = bool(
            previous is None
            or previous[0] != int(actor)
            or previous[1] != int(phys)
            or handle_changed
            or not previous[5]
        )
        if previous is not None:
            previous_token_epoch = self._epochs.get(member_id)
            reincarnated = reincarnated or previous_token_epoch != int(world_epoch)
        if reincarnated:
            generation = self._next.get(member_id, 0) + 1
            self._next[member_id] = generation
        else:
            generation = previous[3]
        self._epochs[member_id] = int(world_epoch)
        self._records[member_id] = (
            int(actor),
            int(phys),
            stable_handle,
            generation,
            bool(alive) if alive is not None else (previous[4] if previous else True),
            True,
        )
        return OwnerToken(
            role,
            member_id,
            int(actor),
            int(phys),
            generation,
            int(world_epoch),
            stable_handle,
        )
