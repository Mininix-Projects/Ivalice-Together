"""Pure target-candidate filtering for the headless P2 frontend.

Only Attack's coarse acquisition rule is considered verified so far.  Other
actions must eventually delegate to their native predicates instead of being
guessed from this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # co-op modules


from headless_menu_model import TargetCandidate, TargetGroup


ATTACK_HORIZONTAL_RANGE = 24.0
ATTACKABLE_UNIT_TYPES = frozenset((1, 4, 7))


@dataclass(frozen=True)
class ActorTargetSnapshot:
    handle: int
    name: str
    x: float
    y: float
    z: float
    actor_flags: int = 0
    unit_type: int = 0
    active: bool = True
    hidden: bool = False
    party_member: bool = False
    selectable: bool = True

    def horizontal_distance_to(self, other: "ActorTargetSnapshot") -> float:
        return math.hypot(self.x - other.x, self.z - other.z)


def attack_candidates(
    issuer: ActorTargetSnapshot,
    actors: Iterable[ActorTargetSnapshot],
) -> tuple[TargetCandidate, ...]:
    """Return native-compatible coarse Attack candidates, sorted by distance.

    The 24.0 threshold and X/Z-only distance were established by a synchronized
    native focus/minimap capture.  This deliberately does not claim to model
    action-specific immunity, line-of-sight or special encounter scripts.
    """

    foes: list[TargetCandidate] = []
    party: list[TargetCandidate] = []
    for actor in actors:
        distance = issuer.horizontal_distance_to(actor)
        if actor.party_member:
            party.append(
                TargetCandidate(
                    actor.handle,
                    actor.name,
                    TargetGroup.PARTY,
                    distance,
                    actor.selectable,
                )
            )
            continue
        if (
            not actor.active
            or actor.hidden
            or actor.unit_type not in ATTACKABLE_UNIT_TYPES
            or distance > ATTACK_HORIZONTAL_RANGE
        ):
            continue
        foes.append(
            TargetCandidate(
                actor.handle,
                actor.name,
                TargetGroup.FOES,
                distance,
                actor.selectable,
            )
        )
    foes.sort(key=lambda candidate: candidate.distance or 0.0)
    party.sort(key=lambda candidate: candidate.distance or 0.0)
    return tuple(foes + party)


__all__ = [
    "ATTACK_HORIZONTAL_RANGE",
    "ATTACKABLE_UNIT_TYPES",
    "ActorTargetSnapshot",
    "attack_candidates",
]
