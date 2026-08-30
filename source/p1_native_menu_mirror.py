from __future__ import annotations

from dataclasses import dataclass

import headless_menu_renderer as renderer
from headless_menu_presenter import CursorTween
from native_action_catalog import NativeActionCatalog
from native_type0b_entry import decode_type0b_entry


ROOT_ROWS = {
    0x00: ("Attack", "Attack with weapon in hand."),
    0x12: ("Magicks & Technicks", "Use magicks and technicks."),
    0x13: ("Mist", "Use Mist abilities."),
    0x0D: ("Gambits", "Toggle gambits."),
    0x03: ("Items", "Use items."),
}

MAGICK_ROWS = {
    0: ("White Magicks", "Use white magicks."),
    1: ("Black Magicks", "Use black magicks."),
    2: ("Time Magicks", "Use time magicks."),
    3: ("Green Magicks", "Use green magicks."),
    4: ("Arcane Magicks", "Use arcane magicks."),
}


@dataclass(frozen=True)
class NativePageSnapshot:
    generation: tuple[int, ...]
    type_id: int
    actor_token: int
    variant: int
    subcategory: int
    context: int
    count: int
    index: int
    pitch: int
    entries: tuple[bytes, ...]


@dataclass(frozen=True)
class MirrorFrame:
    view: renderer.ListView
    help_text: str
    page_key: str


class P1NativeMirrorPresenter:
    def __init__(
        self,
        action_catalog: NativeActionCatalog,
        *,
        max_visible_rows: int = 5,
    ) -> None:
        self.action_catalog = action_catalog
        self.max_visible_rows = max(1, int(max_visible_rows))
        self.cursor = CursorTween()
        self._generation: tuple[int, ...] | None = None
        self._page_key: str | None = None
        self._first_by_page: dict[str, int] = {}

        self._next_action_title: str | None = None

    def reset(self) -> None:
        self._generation = None
        self._page_key = None
        self._first_by_page.clear()
        self._next_action_title = None
        self.cursor.snap(0.0)

    @staticmethod
    def _enabled_category(raw: bytes) -> bool:
        return len(raw) == 8 and not bool(raw[3] & 0x02)

    @staticmethod
    def _category(raw: bytes) -> tuple[str, str] | None:
        if len(raw) != 8:
            return None
        dialog_id = int.from_bytes(raw[:2], "little")
        if raw[3] & 0x04:
            return MAGICK_ROWS.get(dialog_id)
        if dialog_id == 2:
            return "Technicks", "Use technicks."
        return None

    def _first_visible(self, key: str, selected: int, count: int, limit: int) -> int:
        visible = min(count, max(1, limit))
        maximum = max(0, count - visible)
        first = min(max(0, self._first_by_page.get(key, 0)), maximum)
        if selected < first:
            first = selected
        elif selected >= first + visible:
            first = selected - visible + 1
        self._first_by_page[key] = first
        return first

    def _cursor_state(
        self,
        snapshot: NativePageSnapshot,
        page_key: str,
        delta_seconds: float,
    ) -> tuple[float, tuple[tuple[float, float], ...]]:
        if snapshot.generation != self._generation or page_key != self._page_key:
            self._generation = snapshot.generation
            self._page_key = page_key
            self.cursor.snap(float(snapshot.index))
        else:
            self.cursor.retarget(float(snapshot.index))
            self.cursor.advance(delta_seconds)
        return self.cursor.position, self.cursor.visible_trail()

    def _decode_root(
        self, snapshot: NativePageSnapshot, gambits_enabled: bool
    ) -> (
        tuple[
            str, tuple[str, ...], tuple[str, ...], tuple[bool, ...], dict[int, str], str
        ]
        | None
    ):
        rows: list[str] = []
        descriptions: list[str] = []
        enabled: list[bool] = []
        right: dict[int, str] = {}
        for ordinal, raw in enumerate(snapshot.entries):
            if len(raw) != 8:
                return None
            identity = ROOT_ROWS.get(int.from_bytes(raw[:2], "little"))
            if identity is None:
                return None
            name, description = identity
            rows.append(name)
            descriptions.append(description)
            enabled.append(self._enabled_category(raw))
            if name == "Gambits":
                right[ordinal] = "ON" if gambits_enabled else "OFF"
        selected_name = rows[snapshot.index]
        if selected_name in ("Items", "Mist"):
            self._next_action_title = selected_name
        return "root", tuple(rows), tuple(descriptions), tuple(enabled), right, ""

    def _decode_categories(
        self, snapshot: NativePageSnapshot
    ) -> (
        tuple[
            str, tuple[str, ...], tuple[str, ...], tuple[bool, ...], dict[int, str], str
        ]
        | None
    ):
        rows: list[str] = []
        descriptions: list[str] = []
        enabled: list[bool] = []
        for raw in snapshot.entries:
            identity = self._category(raw)
            if identity is None:
                return None
            name, description = identity
            rows.append(name)
            descriptions.append(description)
            enabled.append(self._enabled_category(raw))
        self._next_action_title = rows[snapshot.index]
        return (
            "magicks",
            tuple(rows),
            tuple(descriptions),
            tuple(enabled),
            {},
            "Magicks & Technicks",
        )

    def _fallback_action_title(self, action_ids: tuple[int, ...]) -> str | None:
        categories = {
            self.action_catalog.category(action_id) for action_id in action_ids
        }
        if len(categories) != 1:
            return None
        category = next(iter(categories))
        return None if category in ("Other", "Green / Arcane Magicks") else category

    def _decode_actions(
        self, snapshot: NativePageSnapshot
    ) -> (
        tuple[
            str, tuple[str, ...], tuple[str, ...], tuple[bool, ...], dict[int, str], str
        ]
        | None
    ):
        try:
            decoded = tuple(
                decode_type0b_entry(raw, discriminator=snapshot.variant)
                for raw in snapshot.entries
            )
            action_ids = tuple(entry.action_id for entry in decoded)
            records = tuple(
                self.action_catalog.record(action_id) for action_id in action_ids
            )
        except (IndexError, KeyError, TypeError, ValueError):
            return None
        if snapshot.variant == 3:
            title = "Items"
        elif snapshot.variant == 2:
            title = "Technicks"
        elif snapshot.variant == 1:
            title = self._next_action_title or self._fallback_action_title(action_ids)
            if title not in {name for name, _description in MAGICK_ROWS.values()}:
                return None
        else:
            return None
        rows = tuple(record.name for record in records)
        descriptions = tuple(record.description for record in records)
        enabled = tuple(entry.enabled for entry in decoded)
        right: dict[int, str] = {}
        if snapshot.variant == 3:
            for ordinal, entry in enumerate(decoded):
                quantity = entry.inventory_quantity
                if quantity is not None:
                    right[ordinal] = str(quantity)
        page_key = f"actions:{snapshot.variant}:{title}"
        return page_key, rows, descriptions, enabled, right, title

    def present(
        self,
        snapshot: NativePageSnapshot,
        *,
        gambits_enabled: bool,
        delta_seconds: float,
    ) -> MirrorFrame | None:
        if (
            snapshot.count <= 0
            or snapshot.count != len(snapshot.entries)
            or not 0 <= snapshot.index < snapshot.count
            or any(len(raw) != 8 for raw in snapshot.entries)
        ):
            return None
        if snapshot.type_id == 0x06:
            decoded = self._decode_root(snapshot, gambits_enabled)
        elif snapshot.type_id == 0x08:
            decoded = self._decode_categories(snapshot)
        elif snapshot.type_id == 0x0B:
            decoded = self._decode_actions(snapshot)
        else:
            return None
        if decoded is None:
            return None
        page_key, rows, descriptions, enabled, right, title = decoded
        if not rows or len(descriptions) != len(rows) or len(enabled) != len(rows):
            return None
        limit = 6 if page_key == "magicks" else self.max_visible_rows
        first = self._first_visible(page_key, snapshot.index, len(rows), limit)
        position, trail = self._cursor_state(snapshot, page_key, delta_seconds)
        view = renderer.ListView(
            entries=rows,
            selected_index=snapshot.index,
            title=title,
            right_labels=right,
            enabled=enabled,
            first_visible=first,
            max_visible_rows=limit,
            cursor_position=position,
            cursor_trail=trail,
        )
        return MirrorFrame(view, descriptions[snapshot.index], page_key)


__all__ = [
    "MirrorFrame",
    "NativePageSnapshot",
    "P1NativeMirrorPresenter",
]
