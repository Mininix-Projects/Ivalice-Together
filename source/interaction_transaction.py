"""Runtime rules for additional-player world interactions."""

from __future__ import annotations


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
    "WEAPON_DRAWN_FLAG",
    "WEAPON_HANDLING_FLAG",
    "EXECUTION_ANIMATION_FLAG",
    "FOLLOWER_INTERACTION_LEADER_BLOCK_MASK",
    "follower_interaction_leader_allows",
    "interaction_press_allowed",
]
