from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Mapping


class QueuePhase(Enum):
    IDLE = auto()
    CHARGING = auto()
    EXECUTING = auto()
    UNKNOWN_ACTIVE = auto()


class SubmissionDisposition(Enum):
    START = auto()
    REPLACE_CHARGING = auto()
    QUEUE_AFTER_CURRENT = auto()
    DEFER_TO_NATIVE = auto()


@dataclass(frozen=True)
class NativeQueueSnapshot:
    actor_flags_000: int
    state_6b4: int
    descriptor_708: int
    target_710: int
    current_action_714: int
    charge_current_768: int
    charge_required_76c: int
    requested_action_ba0: int
    request_flags_ba4: int
    requested_target_bb8: int

    @property
    def phase(self) -> QueuePhase:
        if self.state_6b4 in (7, 10):
            return QueuePhase.EXECUTING
        if self.state_6b4 == 4:
            return QueuePhase.CHARGING
        if self.state_6b4 == 1:
            return QueuePhase.IDLE
        return QueuePhase.UNKNOWN_ACTIVE

    @property
    def manual_request_pulse(self) -> bool:
        mask = (1 << 14) | (1 << 32)
        return (self.actor_flags_000 & mask) == mask

    @property
    def has_pending_request(self) -> bool:
        return self.manual_request_pulse

    @classmethod
    def from_mapping(cls, values: Mapping[int, int | float]) -> "NativeQueueSnapshot":
        return cls(
            actor_flags_000=int(values.get(0x000, 0)),
            state_6b4=int(values.get(0x6B4, 0)),
            descriptor_708=int(values.get(0x708, 0)),
            target_710=int(values.get(0x710, 0)),
            current_action_714=int(values.get(0x714, 0)),
            charge_current_768=int(values.get(0x768, 0)),
            charge_required_76c=int(values.get(0x76C, 0)),
            requested_action_ba0=int(values.get(0xBA0, 0)),
            request_flags_ba4=int(values.get(0xBA4, 0)),
            requested_target_bb8=int(values.get(0xBB8, 0)),
        )


def predict_native_disposition(snapshot: NativeQueueSnapshot) -> SubmissionDisposition:
    if snapshot.phase is QueuePhase.IDLE:
        return SubmissionDisposition.START
    if snapshot.phase is QueuePhase.CHARGING:
        return SubmissionDisposition.REPLACE_CHARGING
    if snapshot.phase is QueuePhase.EXECUTING:
        return SubmissionDisposition.QUEUE_AFTER_CURRENT
    return SubmissionDisposition.DEFER_TO_NATIVE


@dataclass(frozen=True)
class QueuePresentation:
    phase: QueuePhase
    current_action: int
    requested_action: int
    charge_fraction: float
    accepted_this_frame: bool

    @classmethod
    def from_native(cls, snapshot: NativeQueueSnapshot) -> "QueuePresentation":
        required = max(0, snapshot.charge_required_76c)
        fraction = (
            min(1.0, max(0.0, snapshot.charge_current_768 / required))
            if required
            else 0.0
        )
        return cls(
            snapshot.phase,
            snapshot.current_action_714,
            snapshot.requested_action_ba0,
            fraction,
            snapshot.manual_request_pulse,
        )
