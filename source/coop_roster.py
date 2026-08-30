from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import atomic_files
import runtime_paths


CONFIG_PATH = runtime_paths.config_path(__file__, "coop_roster_config.json")
CONFIG_VERSION = 2

VAAN_ID = 0
ASHE_ID = 1
FRAN_ID = 2
BALTHIER_ID = 3
BASCH_ID = 4
PENELO_ID = 5


SELECTABLE_MEMBERS: tuple[tuple[int, str], ...] = (
    (BALTHIER_ID, "Balthier"),
    (FRAN_ID, "Fran"),
    (BASCH_ID, "Basch"),
    (ASHE_ID, "Ashe"),
    (PENELO_ID, "Penelo"),
)

SELECTABLE_MEMBER_IDS: tuple[int, ...] = tuple(
    member_id for member_id, _name in SELECTABLE_MEMBERS
)

MEMBER_NAMES: dict[int, str] = dict(SELECTABLE_MEMBERS)

DEFAULT_P2 = BASCH_ID
DEFAULT_P3 = BALTHIER_ID
DEFAULT_START_WITH_ALL = True


_NATIVE_MAIN_MEMBER_IDS = frozenset(range(6))


class CoopRoster:
    __slots__ = ("p2", "p3", "start_with_all")

    def __init__(
        self,
        p2: int | None = DEFAULT_P2,
        p3: int | None = DEFAULT_P3,
        start_with_all: bool = False,
    ):
        self.p2 = p2
        self.p3 = p3
        self.start_with_all = bool(start_with_all)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CoopRoster):
            return NotImplemented
        return (self.p2, self.p3, self.start_with_all) == (
            other.p2,
            other.p3,
            other.start_with_all,
        )

    def __repr__(self) -> str:
        return (
            "CoopRoster("
            f"p2={self.p2!r}, p3={self.p3!r}, "
            f"start_with_all={self.start_with_all!r})"
        )

    @property
    def role_members(self) -> tuple[int, ...]:
        return tuple(
            dict.fromkeys(
                member_id for member_id in (self.p2, self.p3) if member_id is not None
            )
        )

    @property
    def members(self) -> tuple[int, ...]:
        if not self.start_with_all:
            return ()
        return SELECTABLE_MEMBER_IDS

    def with_start_all(self, enabled: bool = True) -> "CoopRoster":
        return CoopRoster(self.p2, self.p3, bool(enabled))

    def role_of(self, member_id: int) -> str | None:
        if self.p2 is not None and member_id == self.p2:
            return "p2"
        if self.p3 is not None and member_id == self.p3:
            return "p3"
        return None

    def assign(self, role: str, member_id: int | None) -> "CoopRoster":
        if role not in ("p2", "p3"):
            raise ValueError("role must be 'p2' or 'p3'")
        p2, p3 = self.p2, self.p3
        if member_id is None:
            if role == "p2":
                p2 = None
            else:
                p3 = None
            return CoopRoster(p2, p3, self.start_with_all)
        if member_id not in SELECTABLE_MEMBER_IDS:
            raise ValueError(f"member {member_id} is not selectable")
        if role == "p2":
            if p3 == member_id:
                p3 = None
            p2 = member_id
        else:
            if p2 == member_id:
                p2 = None
            p3 = member_id
        return CoopRoster(p2, p3, self.start_with_all)


def default_roster() -> CoopRoster:
    return CoopRoster(DEFAULT_P2, DEFAULT_P3, DEFAULT_START_WITH_ALL)


def _clean(value: Any) -> int | None:
    try:
        member_id = int(value)
    except (TypeError, ValueError):
        return None
    return member_id if member_id in SELECTABLE_MEMBER_IDS else None


def normalize(payload: Any) -> CoopRoster:
    if not isinstance(payload, dict):
        return default_roster()
    players = payload.get("players")
    if not isinstance(players, dict):
        return default_roster()
    p2 = _clean(players["p2"]) if "p2" in players else DEFAULT_P2
    p3 = _clean(players["p3"]) if "p3" in players else DEFAULT_P3
    start_with_all = payload.get("start_with_all") is True
    if p2 is not None and p2 == p3:
        p3 = None
    return CoopRoster(p2, p3, start_with_all)


def load(path: Path = CONFIG_PATH) -> CoopRoster:
    try:
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return default_roster()


def save(roster: CoopRoster, path: Path = CONFIG_PATH) -> CoopRoster:
    payload = {
        "version": CONFIG_VERSION,
        "start_with_all": roster.start_with_all,
        "players": {"p2": roster.p2, "p3": roster.p3},
        "names": {
            role: MEMBER_NAMES.get(member_id)
            for role, member_id in (("p2", roster.p2), ("p3", roster.p3))
            if member_id is not None
        },
    }
    atomic_files.write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return load(path)


def initial_members(path: Path = CONFIG_PATH) -> tuple[int, ...]:
    return load(path).members


def normalize_party_layout(
    slots: Sequence[int],
    protected_members: Sequence[int],
    *,
    empty: int,
    prefer_active: bool,
) -> tuple[int, ...]:
    if len(slots) != 9:
        raise ValueError("party layout must contain exactly nine slots")

    protected = tuple(
        dict.fromkeys(
            member_id for member_id in protected_members if member_id != empty
        )
    )
    seen: set[int] = set()
    cleaned: list[int] = [empty] * 9
    guest_member = slots[3]
    scan_order = tuple(range(9))
    if guest_member != empty and guest_member not in _NATIVE_MAIN_MEMBER_IDS:
        scan_order = (3, 0, 1, 2, 4, 5, 6, 7, 8)
    for index in scan_order:
        member_id = slots[index]
        if member_id == empty or member_id in seen:
            continue
        seen.add(member_id)
        cleaned[index] = member_id

    missing = [member_id for member_id in protected if member_id not in seen]

    destinations: list[int] = []
    if prefer_active:
        destinations.extend(index for index in range(3) if cleaned[index] == empty)
    destinations.extend(index for index in range(4, 9) if cleaned[index] == empty)

    if len(destinations) < len(missing):
        return tuple(cleaned)
    for index, member_id in zip(destinations, missing):
        cleaned[index] = member_id
    return tuple(cleaned)


def normalize_live_party_layout(
    slots: Sequence[int],
    protected_members: Sequence[int],
    *,
    empty: int,
    prefer_active: bool,
) -> tuple[int, ...]:
    if len(slots) != 9:
        raise ValueError("party layout must contain exactly nine slots")

    working = list(
        normalize_party_layout(
            slots,
            protected_members,
            empty=empty,
            prefer_active=prefer_active,
        )
    )

    def canonicalize_active_guests(layout: list[int]) -> None:
        for active_index in range(3):
            member_id = layout[active_index]
            if member_id == empty or member_id in _NATIVE_MAIN_MEMBER_IDS:
                continue

            if layout[3] == empty:
                layout[3] = member_id
                layout[active_index] = empty
                continue
            if layout[3] == member_id or member_id in layout[4:]:
                layout[active_index] = empty
                continue

            reserve_index = next(
                (index for index in range(4, 9) if layout[index] == empty),
                None,
            )
            if reserve_index is not None:
                layout[reserve_index] = member_id
                layout[active_index] = empty

    normalized = working

    for _pass in range(9):
        before = tuple(normalized)
        normalized[:] = normalize_party_layout(
            normalized,
            protected_members,
            empty=empty,
            prefer_active=prefer_active,
        )
        canonicalize_active_guests(normalized)
        if tuple(normalized) == before:
            break

    return tuple(normalized)


__all__ = [
    "CONFIG_PATH",
    "CoopRoster",
    "DEFAULT_P2",
    "DEFAULT_P3",
    "DEFAULT_START_WITH_ALL",
    "default_roster",
    "MEMBER_NAMES",
    "SELECTABLE_MEMBERS",
    "SELECTABLE_MEMBER_IDS",
    "initial_members",
    "load",
    "normalize",
    "normalize_party_layout",
    "normalize_live_party_layout",
    "save",
]
