from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterable, Mapping, Sequence


SUBMIT_MANUAL_ACTION = 0x311CD0
SUBMIT_RESERVE_ACTION = 0x311B20
ATTACK_ACTION_ID = 0x96
POTION_ACTION_ID = 0x52


REVIVAL_ACTION_IDS = frozenset((0x05, 0x0F, 0x58, 0xAE))


class MenuMode(Enum):
    CLOSED = auto()
    LIST = auto()
    TARGET = auto()


class EntryKind(Enum):
    PAGE = auto()
    ACTION = auto()
    TOGGLE = auto()
    LABEL = auto()


class TargetGroup(Enum):
    FOES = "foes"
    NEUTRALS = "neutrals"
    PARTY = "party"
    RESERVE = "reserve"


def _status(*bits: int) -> int:
    mask = 0
    for bit in bits:
        mask |= 1 << bit
    return mask


REMEDY_STATUS_MASKS = {
    "antidote": _status(8),
    "eye drops": _status(7),
    "echo herbs": _status(9),
    "handkerchief": _status(11),
    "gold needle": _status(1, 2),
    "smelling salts": _status(5),
    "alarm clock": _status(4),
    "prince's kiss": _status(4),
    "chronos tear": _status(3, 15),
    "nu khai sand": _status(5),
    "serum": _status(16),
    "dispel": _status(15),
    "esuna": _status(1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13, 14, 15, 16),
    "remedy": _status(1, 2, 4, 5, 7, 8, 9, 10, 11, 13, 14, 16),
    "poisona": _status(8),
    "blindna": _status(7),
    "vox": _status(9),
    "stona": _status(1, 2),
    "cleanse": _status(16),
}


def remedy_status_mask(action_name: str) -> int:
    return REMEDY_STATUS_MASKS.get(action_name.strip().lower(), 0)


@dataclass(frozen=True)
class ActionDefinition:
    action_id: int
    name: str
    description: str
    target_groups: tuple[TargetGroup, ...]
    default_group: TargetGroup
    category: str
    enabled: bool = True
    quantity: int | None = None

    self_only: bool = False

    mp_cost: int | None = None

    revive_only: bool = False

    area_radius: float = 0.0

    visual_variant: str | None = None

    @property
    def requires_target(self) -> bool:
        return bool(self.target_groups) and not self.self_only


@dataclass(frozen=True)
class MenuEntry:
    name: str
    description: str
    kind: EntryKind
    page_key: str | None = None
    action: ActionDefinition | None = None
    enabled: bool = True


@dataclass(frozen=True)
class PageDefinition:
    key: str
    title: str
    entries: tuple[MenuEntry, ...]


@dataclass(frozen=True)
class MenuCapabilities:
    magick_pages: tuple[str, ...] = ()
    technicks_unlocked: bool = False
    mist_unlocked: bool = False
    gambits_unlocked: bool = True
    items_available: bool = True


@dataclass(frozen=True)
class TargetCandidate:
    handle: int
    name: str
    group: TargetGroup
    distance: float | None = None
    selectable: bool = True

    status_mask: int = 0
    party_member_id: int | None = None


def target_candidate_identity(candidate: TargetCandidate) -> tuple[str, int]:
    if candidate.party_member_id is not None:
        return "party_member", int(candidate.party_member_id)
    return "handle", int(candidate.handle)


@dataclass(frozen=True)
class SubmitRequest:
    issuer: int
    target: int
    action_id: int
    flag: int = 0
    entrypoint: int = SUBMIT_MANUAL_ACTION
    visual_variant: str | None = None
    reserve_party_member_id: int | None = None


@dataclass(frozen=True)
class MenuEvent:
    kind: str
    request: SubmitRequest | None = None
    value: object | None = None


def default_catalog(
    capabilities: MenuCapabilities,
    *,
    magicks: Mapping[str, Sequence[ActionDefinition]] | None = None,
    technicks: Sequence[ActionDefinition] = (),
    items: Sequence[ActionDefinition] = (),
    mist: Sequence[ActionDefinition] = (),
) -> dict[str, PageDefinition]:
    magicks = magicks or {}
    pages: dict[str, PageDefinition] = {}

    def action_page(key: str, title: str, actions: Sequence[ActionDefinition]) -> None:
        entries = tuple(
            MenuEntry(
                action.name,
                action.description,
                EntryKind.ACTION,
                action=action,
                enabled=action.enabled,
            )
            for action in actions
        )
        if not entries:
            entries = (
                MenuEntry("No actions available", "", EntryKind.LABEL, enabled=False),
            )
        pages[key] = PageDefinition(key, title, entries)

    magick_root: list[MenuEntry] = []
    for category in capabilities.magick_pages:
        key = f"magick:{category.casefold().replace(' ', '_')}"
        actions = tuple(magicks.get(category, ()))
        action_page(key, category, actions)
        magick_root.append(
            MenuEntry(
                category, f"Use {category.casefold()}.", EntryKind.PAGE, page_key=key
            )
        )
    if capabilities.technicks_unlocked:
        action_page("technicks", "Technicks", technicks)
        magick_root.append(
            MenuEntry(
                "Technicks", "Use technicks.", EntryKind.PAGE, page_key="technicks"
            )
        )
    if magick_root:
        pages["magicks"] = PageDefinition(
            "magicks", "Magicks & Technicks", tuple(magick_root)
        )

    if capabilities.items_available:
        action_page("items", "Items", items)
    if capabilities.mist_unlocked:
        action_page("mist", "Mist", mist)

    attack = ActionDefinition(
        ATTACK_ACTION_ID,
        "Attack",
        "Attack with weapon in hand.",
        (TargetGroup.FOES, TargetGroup.NEUTRALS, TargetGroup.PARTY),
        TargetGroup.FOES,
        "Attack",
    )
    root: list[MenuEntry] = [
        MenuEntry(attack.name, attack.description, EntryKind.ACTION, action=attack)
    ]
    if "magicks" in pages:
        root.append(
            MenuEntry(
                "Magicks & Technicks",
                "Use magicks and technicks.",
                EntryKind.PAGE,
                page_key="magicks",
            )
        )
    if capabilities.mist_unlocked:
        root.append(
            MenuEntry("Mist", "Use Mist abilities.", EntryKind.PAGE, page_key="mist")
        )
    if capabilities.gambits_unlocked:
        root.append(MenuEntry("Gambits", "Toggle gambits.", EntryKind.TOGGLE))
    if capabilities.items_available:
        root.append(MenuEntry("Items", "Use items.", EntryKind.PAGE, page_key="items"))
    pages["root"] = PageDefinition("root", "", tuple(root))
    return pages


@dataclass
class _PageState:
    key: str
    index: int = 0


@dataclass
class P2MenuModel:
    issuer: int
    pages: Mapping[str, PageDefinition]
    issuer_party_member_id: int | None = None
    mode: MenuMode = MenuMode.CLOSED
    stack: list[_PageState] = field(default_factory=list)
    pending_action: ActionDefinition | None = None
    target_group: TargetGroup = TargetGroup.FOES
    target_index: int = 0
    gambits_enabled: bool = True
    candidates: dict[TargetGroup, tuple[TargetCandidate, ...]] = field(
        default_factory=lambda: {
            TargetGroup.FOES: (),
            TargetGroup.NEUTRALS: (),
            TargetGroup.PARTY: (),
            TargetGroup.RESERVE: (),
        }
    )
    preserve_candidate_on_refresh: bool = True
    party_target_locked_to_issuer: bool = False

    def candidate_is_issuer(self, candidate: TargetCandidate) -> bool:
        if (
            self.issuer_party_member_id is not None
            and candidate.party_member_id is not None
        ):
            return int(candidate.party_member_id) == int(self.issuer_party_member_id)
        return int(candidate.handle) == int(self.issuer)

    def __post_init__(self) -> None:
        if "root" not in self.pages:
            raise ValueError("catalog has no root page")

    @property
    def is_open(self) -> bool:
        return self.mode is not MenuMode.CLOSED

    @property
    def page(self) -> PageDefinition:
        if not self.stack:
            raise RuntimeError("menu is closed")
        return self.pages[self.stack[-1].key]

    @property
    def index(self) -> int:
        return self.stack[-1].index if self.stack else 0

    @property
    def selected_entry(self) -> MenuEntry | None:
        if self.mode is not MenuMode.LIST or not self.page.entries:
            return None
        return self.page.entries[self.index]

    @property
    def help_text(self) -> str:
        if self.mode is MenuMode.TARGET and self.pending_action:
            return self.pending_action.description
        entry = self.selected_entry
        return entry.description if entry else ""

    def open(self) -> MenuEvent:
        self.mode = MenuMode.LIST
        self.stack[:] = [_PageState("root", 0)]
        self.pending_action = None
        self.target_group = TargetGroup.FOES
        self.target_index = 0
        self.party_target_locked_to_issuer = False
        return MenuEvent("opened")

    def close(self) -> MenuEvent:
        self.mode = MenuMode.CLOSED
        self.stack.clear()
        self.pending_action = None
        self.target_index = 0
        self.party_target_locked_to_issuer = False
        return MenuEvent("closed")

    def toggle(self) -> MenuEvent:
        return self.close() if self.is_open else self.open()

    def navigate(self, delta: int, *, wrap: bool = True) -> MenuEvent:
        if self.mode is MenuMode.TARGET:
            candidates = self.valid_candidates(self.target_group)
            if len(candidates) <= 1:
                return MenuEvent("ignored")
            previous = self.target_index
            if wrap:
                self.target_index = (self.target_index + delta) % len(candidates)
            else:
                self.target_index = min(
                    len(candidates) - 1,
                    max(0, self.target_index + delta),
                )
            if self.target_index == previous:
                return MenuEvent("ignored")
            selected = candidates[self.target_index]
            self.party_target_locked_to_issuer = bool(
                self.target_group is TargetGroup.PARTY
                and self.candidate_is_issuer(selected)
            )
            return MenuEvent("target_moved", value=self.target_index)
        if self.mode is not MenuMode.LIST or not self.page.entries:
            return MenuEvent("ignored")
        state = self.stack[-1]
        count = len(self.page.entries)
        next_index = state.index
        if wrap:
            for _ in range(count):
                next_index = (next_index + delta) % count
                if self.page.entries[next_index].kind is not EntryKind.LABEL:
                    break
        else:
            direction = -1 if delta < 0 else 1
            next_index += direction
            while (
                0 <= next_index < count
                and self.page.entries[next_index].kind is EntryKind.LABEL
            ):
                next_index += direction
            if not 0 <= next_index < count:
                return MenuEvent("ignored")
        if next_index == state.index:
            return MenuEvent("ignored")
        state.index = next_index
        return MenuEvent("cursor_moved", value=state.index)

    def navigate_page(self, direction: int, page_size: int) -> MenuEvent:
        """Move one visible list page without wrapping at either end."""

        if self.mode is not MenuMode.LIST or not self.page.entries:
            return MenuEvent("ignored")
        step = max(1, int(page_size)) * (-1 if int(direction) < 0 else 1)
        state = self.stack[-1]
        last = len(self.page.entries) - 1
        candidate = min(last, max(0, state.index + step))
        if candidate == state.index:
            return MenuEvent("ignored")

        search_direction = -1 if step < 0 else 1
        while (
            0 <= candidate <= last
            and self.page.entries[candidate].kind is EntryKind.LABEL
        ):
            candidate += search_direction
        if not 0 <= candidate <= last:
            candidate = min(last, max(0, state.index + step))
            while (
                0 <= candidate <= last
                and self.page.entries[candidate].kind is EntryKind.LABEL
            ):
                candidate -= search_direction
        if not 0 <= candidate <= last or candidate == state.index:
            return MenuEvent("ignored")
        state.index = candidate
        return MenuEvent("cursor_moved", value=state.index)

    def set_candidates(self, candidates: Iterable[TargetCandidate]) -> None:
        issuer_lock = bool(
            self.party_target_locked_to_issuer
            and self.target_group is TargetGroup.PARTY
        )
        previous_identity = None
        if self.preserve_candidate_on_refresh:
            previous = self.valid_candidates(self.target_group)
            if previous:
                selected = previous[self.target_index % len(previous)]
                previous_identity = target_candidate_identity(selected)
        grouped: dict[TargetGroup, list[TargetCandidate]] = {
            TargetGroup.FOES: [],
            TargetGroup.NEUTRALS: [],
            TargetGroup.PARTY: [],
            TargetGroup.RESERVE: [],
        }
        for candidate in candidates:
            grouped[candidate.group].append(candidate)
        if self.preserve_candidate_on_refresh:
            for group, refreshed_values in grouped.items():
                refreshed_by_identity = {
                    target_candidate_identity(candidate): candidate
                    for candidate in refreshed_values
                }
                stable_values = []
                for candidate in self.candidates.get(group, ()):
                    replacement = refreshed_by_identity.pop(
                        target_candidate_identity(candidate), None
                    )
                    if replacement is not None:
                        stable_values.append(replacement)
                stable_values.extend(refreshed_by_identity.values())
                grouped[group] = stable_values
        self.candidates = {group: tuple(values) for group, values in grouped.items()}
        refreshed = self.valid_candidates(self.target_group)
        fresh_default = not self.preserve_candidate_on_refresh
        self.preserve_candidate_on_refresh = True
        if issuer_lock:
            issuer_index = next(
                (
                    index
                    for index, candidate in enumerate(refreshed)
                    if self.candidate_is_issuer(candidate)
                ),
                None,
            )
            if issuer_index is not None:
                self.target_index = issuer_index
                return
        if fresh_default and self.target_group is TargetGroup.PARTY:
            remedy = (
                remedy_status_mask(self.pending_action.name)
                if self.pending_action
                else 0
            )
            if remedy:
                afflicted_index = next(
                    (
                        index
                        for index, candidate in enumerate(refreshed)
                        if candidate.status_mask & remedy
                    ),
                    None,
                )
                if afflicted_index is not None:
                    self.target_index = afflicted_index
                    self.party_target_locked_to_issuer = False
                    return

            issuer_index = next(
                (
                    index
                    for index, candidate in enumerate(refreshed)
                    if self.candidate_is_issuer(candidate)
                ),
                None,
            )
            if issuer_index is not None:
                self.target_index = issuer_index
                self.party_target_locked_to_issuer = True
                return
        if previous_identity is not None:
            preserved = next(
                (
                    index
                    for index, candidate in enumerate(refreshed)
                    if target_candidate_identity(candidate) == previous_identity
                ),
                None,
            )
            if preserved is not None:
                self.target_index = preserved
                self.party_target_locked_to_issuer = bool(
                    self.target_group is TargetGroup.PARTY
                    and self.candidate_is_issuer(refreshed[preserved])
                )
                return
        self.target_index = self._clamped_target_index()
        if refreshed and not issuer_lock:
            self.party_target_locked_to_issuer = bool(
                self.target_group is TargetGroup.PARTY
                and self.candidate_is_issuer(refreshed[self.target_index])
            )

    def valid_candidates(
        self, group: TargetGroup | None = None
    ) -> tuple[TargetCandidate, ...]:
        group = group or self.target_group
        return tuple(
            candidate
            for candidate in self.candidates.get(group, ())
            if candidate.selectable
        )

    def switch_target_group(self, group: TargetGroup) -> MenuEvent:
        if self.mode is not MenuMode.TARGET or not self.pending_action:
            return MenuEvent("ignored")
        if group not in self.pending_action.target_groups:
            return MenuEvent("ignored")
        if group is self.target_group:
            return MenuEvent("ignored")
        self.target_group = group
        self.target_index = 0
        self.party_target_locked_to_issuer = False
        self.preserve_candidate_on_refresh = False
        return MenuEvent("target_group", value=group)

    def cycle_target_group(self, direction: int = 1) -> MenuEvent:
        if self.mode is not MenuMode.TARGET or not self.pending_action:
            return MenuEvent("ignored")
        ordered = self.pending_action.target_groups
        if not ordered:
            return MenuEvent("ignored")
        try:
            current = ordered.index(self.target_group)
        except ValueError:
            current = 0
        step = 1 if direction >= 0 else -1
        for offset in range(1, len(ordered) + 1):
            group = ordered[(current + step * offset) % len(ordered)]
            if group is self.target_group or not self.valid_candidates(group):
                continue
            self.target_group = group
            self.target_index = 0
            self.party_target_locked_to_issuer = False
            self.preserve_candidate_on_refresh = False
            return MenuEvent("target_group", value=group)
        return MenuEvent("ignored")

    def _clamped_target_index(self) -> int:
        candidates = self.valid_candidates(self.target_group)
        if not candidates:
            return 0
        return min(self.target_index, len(candidates) - 1)

    def confirm(self) -> MenuEvent:
        if self.mode is MenuMode.TARGET:
            candidates = self.valid_candidates(self.target_group)
            if not candidates or not self.pending_action:
                return MenuEvent("invalid_target")
            if (
                self.party_target_locked_to_issuer
                and self.target_group is TargetGroup.PARTY
            ):
                candidate = next(
                    (
                        candidate
                        for candidate in candidates
                        if self.candidate_is_issuer(candidate)
                    ),
                    None,
                )
                if candidate is None:
                    return MenuEvent("invalid_target")
            else:
                candidate = candidates[self.target_index % len(candidates)]
            request = SubmitRequest(
                issuer=self.issuer,
                target=candidate.handle,
                action_id=self.pending_action.action_id,
                visual_variant=self.pending_action.visual_variant,
                reserve_party_member_id=(
                    candidate.party_member_id
                    if candidate.group is TargetGroup.RESERVE
                    else None
                ),
            )
            self.close()
            return MenuEvent("submit", request=request)

        entry = self.selected_entry
        if not entry or not entry.enabled:
            return MenuEvent("ignored")
        if entry.kind is EntryKind.PAGE and entry.page_key:
            self.stack.append(_PageState(entry.page_key, 0))
            return MenuEvent("page_opened", value=entry.page_key)
        if entry.kind is EntryKind.TOGGLE:
            return MenuEvent("toggle_gambits")
        if entry.kind is not EntryKind.ACTION or not entry.action:
            return MenuEvent("ignored")

        action = entry.action
        if action.self_only:
            request = SubmitRequest(
                self.issuer,
                self.issuer,
                action.action_id,
                visual_variant=action.visual_variant,
            )
            self.close()
            return MenuEvent("submit", request=request)
        if not action.requires_target:
            request = SubmitRequest(
                self.issuer,
                self.issuer,
                action.action_id,
                visual_variant=action.visual_variant,
            )
            self.close()
            return MenuEvent("submit", request=request)
        self.pending_action = action
        self.target_group = action.default_group
        self.target_index = 0
        self.party_target_locked_to_issuer = False

        self.preserve_candidate_on_refresh = False
        self.mode = MenuMode.TARGET
        return MenuEvent("target_opened", value=action)

    def back(self) -> MenuEvent:
        if self.mode is MenuMode.CLOSED:
            return MenuEvent("ignored")
        if self.mode is MenuMode.TARGET:
            # Synthetic selectors (leader/controlled-character pickers) are
            # deliberately opened as a rootless TARGET modal.  They have no
            # LIST page to return to.  Failing closed here preserves the model
            # invariant that LIST always owns at least one stack entry, even
            # if a caller misses its selector-specific close path.
            if not self.stack:
                return self.close()
            self.mode = MenuMode.LIST
            self.pending_action = None
            self.target_index = 0
            self.party_target_locked_to_issuer = False
            return MenuEvent("target_closed")
        if len(self.stack) > 1:
            self.stack.pop()
            return MenuEvent("page_closed", value=self.page.key)
        return self.close()


def make_demo_catalog() -> dict[str, PageDefinition]:
    potion = ActionDefinition(
        POTION_ACTION_ID,
        "Potion",
        "Restore HP to one ally.",
        (TargetGroup.PARTY, TargetGroup.NEUTRALS, TargetGroup.FOES),
        TargetGroup.PARTY,
        "Items",
        quantity=3,
    )
    antidote = ActionDefinition(
        0x5B,
        "Antidote",
        "Remove Poison from one ally.",
        (TargetGroup.PARTY, TargetGroup.NEUTRALS, TargetGroup.FOES),
        TargetGroup.PARTY,
        "Items",
        quantity=2,
    )
    cure = ActionDefinition(
        0x00,
        "Cure",
        "Restore HP to one ally.",
        (TargetGroup.PARTY, TargetGroup.NEUTRALS, TargetGroup.FOES),
        TargetGroup.PARTY,
        "White Magicks",
        mp_cost=8,
    )
    return default_catalog(
        MenuCapabilities(
            magick_pages=("White Magicks",),
            technicks_unlocked=True,
            gambits_unlocked=True,
            items_available=True,
        ),
        magicks={"White Magicks": (cure,)},
        items=(potion, antidote),
    )


__all__ = [
    "ATTACK_ACTION_ID",
    "POTION_ACTION_ID",
    "SUBMIT_MANUAL_ACTION",
    "SUBMIT_RESERVE_ACTION",
    "ActionDefinition",
    "EntryKind",
    "MenuCapabilities",
    "MenuEntry",
    "MenuEvent",
    "MenuMode",
    "P2MenuModel",
    "PageDefinition",
    "SubmitRequest",
    "TargetCandidate",
    "TargetGroup",
    "default_catalog",
    "make_demo_catalog",
]
