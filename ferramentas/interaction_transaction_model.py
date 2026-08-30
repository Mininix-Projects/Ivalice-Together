from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class InteractionPhase(Enum):
    IDLE = auto()
    DISPATCH = auto()
    ACTIVE = auto()
    MODAL = auto()
    ABORTING = auto()


@dataclass(frozen=True)
class InteractionSignals:
    control_enabled: bool = True
    modal_active: bool = False
    owner_has_target: bool = False
    wrong_owner_has_target: bool = False
    owner_busy: bool = False
    private_alive: bool = True
    object_alive: bool = True
    loading: bool = False
    allow_long_native: bool = False


@dataclass(frozen=True)
class InteractionDecision:
    hold_owner: bool
    complete: bool = False
    pulse_cancel: bool = False
    reason: str = ""


class InteractionLifecycle:
    ACCEPT_TIMEOUT = 1.50
    ACTIVE_TIMEOUT = 10.0
    SETTLE_SECONDS = 0.12
    CANCEL_INTERVAL = 0.35
    ABORT_TIMEOUT = 3.0

    def __init__(self) -> None:
        self.phase = InteractionPhase.IDLE
        self.started_at = 0.0
        self.phase_started_at = 0.0
        self.settle_started_at = 0.0
        self.last_cancel_at = -1.0e9
        self.accepted = False
        self.saw_control_loss = False
        self.saw_owner_busy = False
        self.saw_modal = False

    @property
    def active(self) -> bool:
        return self.phase is not InteractionPhase.IDLE

    def start(self, now: float) -> None:
        self.phase = InteractionPhase.DISPATCH
        self.started_at = now
        self.phase_started_at = now
        self.settle_started_at = 0.0
        self.last_cancel_at = -1.0e9
        self.accepted = False
        self.saw_control_loss = False
        self.saw_owner_busy = False
        self.saw_modal = False

    def reset(self) -> None:
        self.__init__()

    def _abort(
        self, now: float, signals: InteractionSignals, reason: str
    ) -> InteractionDecision:
        if self.phase is not InteractionPhase.ABORTING:
            self.phase = InteractionPhase.ABORTING
            self.phase_started_at = now
            self.settle_started_at = 0.0
        if not signals.modal_active:
            return InteractionDecision(False, complete=True, reason=reason)
        pulse = now - self.last_cancel_at >= self.CANCEL_INTERVAL
        if pulse:
            self.last_cancel_at = now
        if now - self.phase_started_at >= self.ABORT_TIMEOUT:
            return InteractionDecision(
                False,
                complete=True,
                pulse_cancel=pulse,
                reason=f"{reason}: abort timeout",
            )
        return InteractionDecision(True, pulse_cancel=pulse, reason=reason)

    def update(self, signals: InteractionSignals, now: float) -> InteractionDecision:
        if self.phase is InteractionPhase.IDLE:
            return InteractionDecision(False, complete=True, reason="idle")

        if signals.loading:
            return InteractionDecision(
                False, complete=True, reason="loading generation changed"
            )

        if signals.wrong_owner_has_target:
            return self._abort(now, signals, "native target escaped to P1")

        self.saw_control_loss |= not signals.control_enabled
        self.saw_owner_busy |= signals.owner_busy
        self.saw_modal |= signals.modal_active
        newly_accepted = bool(
            signals.modal_active
            or not signals.control_enabled
            or signals.owner_has_target
            or signals.owner_busy
            or not signals.private_alive
            or not signals.object_alive
        )
        self.accepted |= newly_accepted

        if signals.modal_active:
            if self.phase is not InteractionPhase.MODAL:
                self.phase = InteractionPhase.MODAL
                self.phase_started_at = now
            self.settle_started_at = 0.0
            return InteractionDecision(True, reason="native modal active")

        if self.phase is InteractionPhase.ABORTING:
            return self._abort(now, signals, "native abort")

        if self.accepted and self.phase is InteractionPhase.DISPATCH:
            self.phase = InteractionPhase.ACTIVE
            self.phase_started_at = now

        if self.phase is InteractionPhase.DISPATCH:
            if now - self.started_at >= self.ACCEPT_TIMEOUT:
                return self._abort(now, signals, "native consumer did not accept")
            return InteractionDecision(True, reason="dispatching")

        returned_from_native = bool(
            signals.control_enabled
            and not signals.owner_busy
            and (self.saw_modal or self.saw_control_loss)
        )
        nonmodal_finished = bool(
            signals.control_enabled
            and not signals.owner_busy
            and self.accepted
            and (not signals.private_alive or not signals.object_alive)
        )
        if returned_from_native or nonmodal_finished:
            if not self.settle_started_at:
                self.settle_started_at = now
            if now - self.settle_started_at >= self.SETTLE_SECONDS:
                return InteractionDecision(
                    False, complete=True, reason="native interaction complete"
                )
        else:
            self.settle_started_at = 0.0

        if (
            not signals.allow_long_native
            and now - self.started_at >= self.ACTIVE_TIMEOUT
        ):
            return self._abort(now, signals, "native interaction stalled")
        return InteractionDecision(True, reason="native interaction active")


WEAPON_DRAWN_FLAG = 1 << 23
WEAPON_HANDLING_FLAG = 1 << 24
EXECUTION_ANIMATION_FLAG = 1 << 46
IDLE_ACTIONS = frozenset((0xFFFF, 0x4021))
FOLLOWER_INTERACTION_LEADER_BLOCK_MASK = WEAPON_HANDLING_FLAG | EXECUTION_ANIMATION_FLAG


def interaction_press_allowed(
    actor_flags: int,
    actor_state: int,
    actor_action: int,
) -> bool:
    return bool(
        not (int(actor_flags) & WEAPON_HANDLING_FLAG)
        and not (int(actor_flags) & EXECUTION_ANIMATION_FLAG)
        and int(actor_state) in (0, 1)
        and int(actor_action) in IDLE_ACTIONS
    )


def follower_interaction_leader_allows(actor_flags: int) -> bool:
    return not bool(int(actor_flags) & FOLLOWER_INTERACTION_LEADER_BLOCK_MASK)


__all__ = [
    "InteractionDecision",
    "InteractionLifecycle",
    "InteractionPhase",
    "InteractionSignals",
    "WEAPON_DRAWN_FLAG",
    "WEAPON_HANDLING_FLAG",
    "EXECUTION_ANIMATION_FLAG",
    "FOLLOWER_INTERACTION_LEADER_BLOCK_MASK",
    "follower_interaction_leader_allows",
    "interaction_press_allowed",
]
