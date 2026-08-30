from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from headless_menu_model import (
    ActionDefinition,
    EntryKind,
    MenuEntry,
    MenuMode,
    P2MenuModel,
    PageDefinition,
)


EntryIdentity = tuple[str, object]


@dataclass(frozen=True)
class CatalogCommitResult:
    changed: bool
    retained_stack_depth: int
    target_preserved: bool
    target_cancelled: bool


def entry_identity(entry: MenuEntry) -> EntryIdentity:
    if entry.kind is EntryKind.ACTION and entry.action is not None:
        return (
            "action",
            (entry.action.action_id, entry.action.visual_variant),
        )
    if entry.kind is EntryKind.PAGE:
        return ("page", entry.page_key or "")
    if entry.kind is EntryKind.TOGGLE:
        return ("toggle", entry.name.casefold())
    return ("label", entry.name.casefold())


def validate_page_tree(
    pages: Mapping[str, PageDefinition],
) -> dict[str, PageDefinition]:
    candidate = dict(pages)
    if "root" not in candidate:
        raise ValueError("catalog has no root page")
    for key, page in candidate.items():
        if page.key != key:
            raise ValueError(
                f"page mapping key {key!r} != PageDefinition.key {page.key!r}"
            )
        for ordinal, entry in enumerate(page.entries):
            if entry.kind is EntryKind.PAGE:
                if not entry.page_key:
                    raise ValueError(
                        f"{key}[{ordinal}] is a page entry without page_key"
                    )
                if entry.page_key not in candidate:
                    raise ValueError(
                        f"{key}[{ordinal}] references missing page {entry.page_key!r}"
                    )
            elif entry.kind is EntryKind.ACTION and entry.action is None:
                raise ValueError(
                    f"{key}[{ordinal}] is an action entry without an action"
                )
    return candidate


def _selected_identity(
    pages: Mapping[str, PageDefinition], key: str, index: int
) -> EntryIdentity | None:
    page = pages.get(key)
    if page is None or not page.entries:
        return None
    return entry_identity(page.entries[min(max(index, 0), len(page.entries) - 1)])


def _reconciled_index(
    page: PageDefinition,
    identity: EntryIdentity | None,
    old_index: int,
) -> int:
    if not page.entries:
        return 0
    if identity is not None:
        for index, entry in enumerate(page.entries):
            if entry_identity(entry) == identity:
                return index

    start = min(max(old_index, 0), len(page.entries) - 1)
    for offset in range(len(page.entries)):
        index = (start + offset) % len(page.entries)
        if page.entries[index].kind is not EntryKind.LABEL:
            return index
    return start


def _find_action(
    pages: Mapping[str, PageDefinition],
    action_id: int,
    visual_variant: str | None = None,
) -> ActionDefinition | None:
    for page in pages.values():
        for entry in page.entries:
            if (
                entry.kind is EntryKind.ACTION
                and entry.action is not None
                and entry.action.action_id == action_id
                and entry.action.visual_variant == visual_variant
                and entry.enabled
                and entry.action.enabled
            ):
                return entry.action
    return None


def commit_pages_atomically(
    model: P2MenuModel,
    pages: Mapping[str, PageDefinition],
) -> CatalogCommitResult:
    candidate = validate_page_tree(pages)
    was_target = model.mode is MenuMode.TARGET
    old_pages = model.pages
    old_states = list(model.stack)
    old_identities = [
        _selected_identity(old_pages, state.key, state.index) for state in old_states
    ]

    retained = 0
    reconciled_indices: list[int] = []
    for position, state in enumerate(old_states):
        page = candidate.get(state.key)
        if page is None:
            break
        if position:
            parent = candidate[old_states[position - 1].key]
            if not any(
                entry.kind is EntryKind.PAGE and entry.page_key == state.key
                for entry in parent.entries
            ):
                break
        reconciled_indices.append(
            _reconciled_index(page, old_identities[position], state.index)
        )
        retained += 1

    target_preserved = False
    target_cancelled = False
    replacement_action: ActionDefinition | None = None
    if model.mode is MenuMode.TARGET and model.pending_action is not None:
        replacement_action = _find_action(
            candidate,
            model.pending_action.action_id,
            model.pending_action.visual_variant,
        )
        if replacement_action is not None:
            target_preserved = True
        else:
            target_cancelled = True

    changed = candidate != old_pages
    model.pages = candidate

    if model.mode is not MenuMode.CLOSED:
        del model.stack[retained:]
        for state, index in zip(model.stack, reconciled_indices):
            state.index = index

        if not model.stack:
            model.close()
            target_cancelled = target_cancelled or was_target
            target_preserved = False

    if target_preserved and replacement_action is not None:
        model.pending_action = replacement_action
        if model.target_group not in replacement_action.target_groups:
            model.target_group = replacement_action.default_group
            model.target_index = 0
        candidates = model.valid_candidates(model.target_group)
        model.target_index = min(model.target_index, max(0, len(candidates) - 1))
    elif target_cancelled and model.mode is MenuMode.TARGET:
        model.mode = MenuMode.LIST
        model.pending_action = None
        model.target_index = 0

    return CatalogCommitResult(
        changed=changed,
        retained_stack_depth=len(model.stack),
        target_preserved=target_preserved,
        target_cancelled=target_cancelled,
    )


__all__ = [
    "CatalogCommitResult",
    "commit_pages_atomically",
    "entry_identity",
    "validate_page_tree",
]
