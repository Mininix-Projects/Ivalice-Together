from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
from PIL import Image

import headless_menu_renderer as renderer
from headless_menu_model import (
    MenuMode,
    PageDefinition,
    P2MenuModel,
    TargetGroup,
)


@dataclass
class CursorTween:
    position: float = 0.0
    target: float = 0.0
    velocity: float = 0.0
    response: float = 42.0
    trail_lifetime: float = 0.075
    emit_trail: bool = True
    trail: list[tuple[float, float]] | None = None

    def __post_init__(self) -> None:
        if self.trail is None:
            self.trail = []

    def snap(self, value: float) -> None:
        self.position = self.target = float(value)
        self.velocity = 0.0
        self.trail.clear()

    def retarget(self, value: float) -> None:
        value = float(value)
        if value != self.target:
            self.target = value

    def advance(self, delta_seconds: float) -> float:
        delta_seconds = max(0.0, float(delta_seconds))
        self.trail[:] = [
            (position, remaining - delta_seconds)
            for position, remaining in self.trail
            if remaining > delta_seconds
        ]
        if abs(self.position - self.target) < 0.0001 and abs(self.velocity) < 0.001:
            self.position = self.target
            self.velocity = 0.0
            return self.position
        previous = self.position

        omega = max(0.001, float(self.response))
        displacement = self.position - self.target
        coefficient = self.velocity + omega * displacement
        decay = math.exp(-omega * delta_seconds)
        displacement = (displacement + coefficient * delta_seconds) * decay
        self.velocity = (self.velocity - omega * coefficient * delta_seconds) * decay
        self.position = self.target + displacement
        if abs(self.target - self.position) < 0.0001 and abs(self.velocity) < 0.001:
            self.position = self.target
            self.velocity = 0.0
        movement = abs(self.position - previous)
        if self.emit_trail and self.trail_lifetime > 0.0 and movement >= 0.01:
            midpoint = (previous + self.position) * 0.5
            self.trail.append((previous, self.trail_lifetime * 0.72))
            self.trail.append((midpoint, self.trail_lifetime))
            del self.trail[:-6]
        return self.position

    def visible_trail(self) -> tuple[tuple[float, float], ...]:
        if not self.emit_trail or self.trail_lifetime <= 0.0:
            return ()
        return tuple(
            (position, min(1.0, remaining / self.trail_lifetime))
            for position, remaining in self.trail
        )


LONG_LIST_VISIBLE_ROWS = 8


class HeadlessMenuPresenter:
    def __init__(self, model: P2MenuModel, *, max_visible_rows: int = 5) -> None:
        self.model = model
        self.max_visible_rows = max(1, int(max_visible_rows))
        self.cursor = CursorTween()
        self._page_key: str | None = None
        self._first_by_page: dict[str, int] = {}
        self._scroll_by_page: dict[str, CursorTween] = {}
        self._last_list_page_key: str | None = None
        self._last_list_stack_depth = 0
        self._last_list_image: Image.Image | None = None
        self._page_transition_from: Image.Image | None = None
        self._page_transition_started = 0.0
        self._page_transition_forward = True
        self._last_target_group: str | None = None
        self._last_target_vertical_offset = 0
        self._target_transition_started = 0.0
        self._target_transition_delta_y = 0.0
        self._target_transition_active = False
        self.target_prompts = ("Q", "E")
        self.party_member_id: int | None = None
        self.theme_name: str | None = None
        self.secondary_target_indices: tuple[int, ...] = ()

        self._raster_key: tuple | None = None
        self._raster_image: Image.Image | None = None
        self._raster_cache: OrderedDict[tuple, Image.Image] = OrderedDict()

    def set_party_member_id(self, party_member_id: int | None) -> None:
        normalized = None if party_member_id is None else int(party_member_id)
        if normalized == self.party_member_id:
            return
        self.party_member_id = normalized
        self._raster_key = None
        self._raster_image = None
        self._raster_cache.clear()

    def set_theme_name(self, theme_name: str | None) -> None:
        normalized = None if theme_name is None else str(theme_name)
        if normalized == self.theme_name:
            return
        self.theme_name = normalized

        self._last_list_image = None
        self._page_transition_from = None
        self._raster_key = None
        self._raster_image = None
        self._raster_cache.clear()

    def set_target_prompts(self, left: str, right: str) -> None:
        normalized = (left or "?", right or "?")
        if normalized == self.target_prompts:
            return
        self.target_prompts = normalized
        self._raster_key = None
        self._raster_image = None
        self._raster_cache.clear()

    def set_secondary_target_indices(self, indices) -> None:
        normalized = tuple(sorted({int(index) for index in indices if int(index) >= 0}))
        if normalized == self.secondary_target_indices:
            return
        self.secondary_target_indices = normalized
        self._raster_key = None
        self._raster_image = None

    @staticmethod
    def _list_raster_key(
        view: renderer.ListView,
        *,
        now: float,
        player: int,
        party_member_id: int | None,
        theme_name: str | None,
        compose_layers: bool,
    ) -> tuple:
        return (
            "list",
            view.entries,
            int(view.selected_index),
            view.title,
            tuple(sorted((view.right_labels or {}).items())),
            view.right_header,
            view.enabled,
            int(view.first_visible),
            (
                None
                if view.scroll_position is None
                else round(float(view.scroll_position), 4)
            ),
            int(view.max_visible_rows),
            (
                None
                if view.cursor_position is None
                else round(float(view.cursor_position), 4)
            ),
            tuple(
                (round(float(position), 4), round(float(opacity), 3))
                for position, opacity in view.cursor_trail
            ),
            int(round(renderer.cursor_jab(now))),
            int(player),
            party_member_id,
            theme_name,
            bool(compose_layers),
        )

    @staticmethod
    def _target_raster_key(
        view: renderer.TargetView,
        *,
        now: float,
        player: int,
        party_member_id: int | None,
        theme_name: str | None,
    ) -> tuple:
        cursor_visible = not (
            view.group == "party" and view.has_target and not view.show_party_names
        )
        no_target_pulse = (
            int(round((0.28 + 0.72 * (0.5 + 0.5 * math.sin(now * 1.55))) * 31.0))
            if not view.has_target
            else -1
        )
        return (
            "target",
            view.action_name,
            view.target_name,
            bool(view.has_target),
            view.group,
            view.target_names,
            int(view.selected_index),
            view.left_prompt,
            view.right_prompt,
            view.available_groups,
            bool(view.show_party_names),
            view.secondary_indices,
            (
                int(round(renderer.area_cursor_opacity(now) * 20.0))
                if view.secondary_indices
                else -1
            ),
            (int(round(renderer.cursor_jab(now))) if cursor_visible else 0),
            no_target_pulse,
            int(player),
            party_member_id,
            theme_name,
        )

    def _cached_raster(self, key: tuple, factory) -> Image.Image:
        if self._raster_image is not None and key == self._raster_key:
            return self._raster_image
        cached = self._raster_cache.pop(key, None)
        if cached is None:
            cached = factory()
        self._raster_cache[key] = cached
        while len(self._raster_cache) > 8:
            self._raster_cache.popitem(last=False)
        self._raster_image = cached
        self._raster_key = key
        return self._raster_image

    def _sync_cursor(self, delta_seconds: float) -> float:
        page_key = self.model.page.key
        if page_key != self._page_key:
            self._page_key = page_key
            self.cursor.snap(float(self.model.index))
        else:
            self.cursor.retarget(float(self.model.index))
            self.cursor.advance(delta_seconds)
        return self.cursor.position

    def _first_visible(self, page_key: str, selected: int, count: int) -> int:
        visible = min(count, self.visible_row_limit(page_key))
        maximum = max(0, count - visible)
        first = min(max(0, self._first_by_page.get(page_key, 0)), maximum)
        if selected < first:
            first = selected
        elif selected >= first + visible:
            first = selected - visible + 1
        self._first_by_page[page_key] = first
        return first

    def _sync_scroll(
        self,
        page_key: str,
        first_visible: int,
        delta_seconds: float,
    ) -> float:
        tween = self._scroll_by_page.get(page_key)
        if tween is None:
            tween = CursorTween(emit_trail=False)
            tween.snap(float(first_visible))
            self._scroll_by_page[page_key] = tween
        else:
            tween.retarget(float(first_visible))
            tween.advance(delta_seconds)
        return tween.position

    def visible_row_limit(self, page_key: str) -> int:
        if page_key == "magicks":
            return 6
        if (
            page_key in {"items", "technicks", "mix"}
            or page_key.startswith("magick:")
            or page_key.startswith("mix:")
        ):
            return LONG_LIST_VISIBLE_ROWS
        return self.max_visible_rows

    def _right_labels(self, page: PageDefinition) -> tuple[dict[int, str], str]:
        labels: dict[int, str] = {}
        header = ""
        for index, entry in enumerate(page.entries):
            if entry.action is not None and entry.action.quantity is not None:
                # The game shows only the number under "AMT"; vanilla has no "x".
                labels[index] = str(entry.action.quantity)
                header = "AMT"
            elif (
                page.key.startswith("magick:")
                and entry.action is not None
                and entry.action.mp_cost is not None
            ):
                labels[index] = str(entry.action.mp_cost)
                header = "MP"
            elif entry.name == "Gambits":
                labels[index] = "ON" if self.model.gambits_enabled else "OFF"
        return labels, header

    def list_view(self, *, delta_seconds: float = 0.0) -> renderer.ListView:
        if self.model.mode is not MenuMode.LIST:
            raise RuntimeError("P2 model is not displaying a list")
        page = self.model.page
        entries = page.entries
        position = self._sync_cursor(delta_seconds)
        first = self._first_visible(page.key, self.model.index, len(entries))
        scroll_position = self._sync_scroll(
            page.key,
            first,
            delta_seconds,
        )
        right_labels, right_header = self._right_labels(page)
        return renderer.ListView(
            entries=tuple(entry.name for entry in entries),
            selected_index=self.model.index,
            title=page.title,
            right_labels=right_labels,
            right_header=right_header,
            enabled=tuple(entry.enabled for entry in entries),
            first_visible=first,
            scroll_position=scroll_position,
            max_visible_rows=self.visible_row_limit(page.key),
            cursor_position=position,
            cursor_trail=self.cursor.visible_trail(),
        )

    def prewarm_page(
        self,
        page_key: str,
        *,
        player: int = 2,
        compose_layers: bool = True,
    ) -> bool:
        page = self.model.pages.get(page_key)
        if page is None:
            return False
        entries = page.entries
        right_labels, right_header = self._right_labels(page)
        view = renderer.ListView(
            entries=tuple(entry.name for entry in entries),
            selected_index=0,
            title=page.title,
            right_labels=right_labels,
            right_header=right_header,
            enabled=tuple(entry.enabled for entry in entries),
            first_visible=0,
            scroll_position=0.0,
            max_visible_rows=self.visible_row_limit(page.key),
            cursor_position=0.0,
        )
        image = renderer.render_list(
            view,
            now=0.0,
            player=player,
            party_member_id=self.party_member_id,
            theme_name=self.theme_name,
            compose_layers=compose_layers,
        )
        if not compose_layers:
            renderer.scale_three_player_panel(image)
        return True

    def target_view(self) -> renderer.TargetView:
        if self.model.mode is not MenuMode.TARGET or not self.model.pending_action:
            raise RuntimeError("P2 model is not targeting")
        candidates = self.model.valid_candidates()
        show_party_names = bool(
            self.model.pending_action.revive_only
            or self.model.target_group is TargetGroup.RESERVE
        )
        show_target_names = (
            self.model.target_group is not TargetGroup.PARTY or show_party_names
        )
        target_name = ""
        if candidates and show_target_names:
            target_name = candidates[self.model.target_index % len(candidates)].name
        available_groups = tuple(
            group.value
            for group in self.model.pending_action.target_groups
            if group is self.model.target_group or self.model.valid_candidates(group)
        )
        return renderer.TargetView(
            action_name=self.model.pending_action.name,
            target_name=target_name,
            has_target=bool(candidates),
            group=self.model.target_group.value,
            target_names=(
                tuple(candidate.name for candidate in candidates)
                if show_target_names
                else ()
            ),
            selected_index=self.model.target_index,
            left_prompt=self.target_prompts[0],
            right_prompt=self.target_prompts[1],
            available_groups=available_groups,
            show_party_names=show_party_names,
            secondary_indices=tuple(
                index
                for index in self.secondary_target_indices
                if 0 <= index < len(candidates)
                and index != self.model.target_index % len(candidates)
            )
            if candidates
            else (),
        )

    def render(
        self,
        *,
        now: float = 0.0,
        delta_seconds: float = 0.0,
        player: int = 2,
        compose_layers: bool = True,
    ) -> Image.Image:
        if self.model.mode is MenuMode.CLOSED:
            return Image.new("RGBA", renderer.CANVAS_SIZE, (0, 0, 0, 0))
        if self.model.mode is MenuMode.TARGET:
            target_view = self.target_view()
            target_key = self._target_raster_key(
                target_view,
                now=now,
                player=player,
                party_member_id=self.party_member_id,
                theme_name=self.theme_name,
            )
            current = self._cached_raster(
                target_key,
                lambda: renderer.render_target(
                    target_view,
                    now=now,
                    player=player,
                    party_member_id=self.party_member_id,
                    theme_name=self.theme_name,
                ),
            )
            candidates = self.model.valid_candidates()
            content_rows = renderer.target_content_rows(
                target_view.group,
                len(candidates),
                show_party_names=target_view.show_party_names,
            )
            vertical_offset = renderer.bottom_anchor_offset(
                target=True,
                row_count=content_rows,
            )
            group = target_view.group
            if self._last_target_group is not None and group != self._last_target_group:
                self._target_transition_started = now
                self._target_transition_delta_y = (
                    self._last_target_vertical_offset - vertical_offset
                )
                self._target_transition_active = True
            rendered = current
            if self._target_transition_active:
                progress = (
                    (now - self._target_transition_started)
                    / renderer.TARGET_GROUP_TRANSITION_SECONDS
                    if now
                    else 1.0
                )
                if progress < 1.0:
                    rendered = renderer.target_group_transition(
                        current,
                        progress,
                        initial_offset_y=self._target_transition_delta_y,
                    )
                else:
                    self._target_transition_delta_y = 0.0
                    self._target_transition_active = False
            self._last_target_group = group
            self._last_target_vertical_offset = vertical_offset
            return rendered
        list_view = self.list_view(delta_seconds=delta_seconds)
        list_key = self._list_raster_key(
            list_view,
            now=now,
            player=player,
            party_member_id=self.party_member_id,
            theme_name=self.theme_name,
            compose_layers=compose_layers,
        )
        current = self._cached_raster(
            list_key,
            lambda: renderer.render_list(
                list_view,
                now=now,
                player=player,
                party_member_id=self.party_member_id,
                theme_name=self.theme_name,
                compose_layers=compose_layers,
            ),
        )
        page_key = self.model.page.key
        stack_depth = len(self.model.stack)
        if (
            self._last_list_page_key is not None
            and page_key != self._last_list_page_key
            and self._last_list_image is not None
        ):
            self._page_transition_from = self._last_list_image
            self._page_transition_started = now
            self._page_transition_forward = stack_depth > self._last_list_stack_depth

        rendered = current
        if self._page_transition_from is not None:
            progress = (
                (now - self._page_transition_started) / renderer.PAGE_TRANSITION_SECONDS
                if now
                else 1.0
            )
            if progress < 1.0:
                rendered = renderer.page_transition(
                    self._page_transition_from,
                    current,
                    progress,
                    forward=self._page_transition_forward,
                )
            else:
                self._page_transition_from = None

        self._last_list_page_key = page_key
        self._last_list_stack_depth = stack_depth

        self._last_list_image = rendered
        return rendered

    def render_help(self, *, player: int = 2) -> Image.Image:
        return renderer.render_help(
            self.model.help_text,
            player=player,
            theme_name=self.theme_name,
        )

    def reset_visual_state(self) -> None:
        self._page_key = None
        self._first_by_page.clear()
        self._scroll_by_page.clear()
        self._last_list_page_key = None
        self._last_list_stack_depth = 0
        self._last_list_image = None
        self._page_transition_from = None
        self._page_transition_started = 0.0
        self._last_target_group = None
        self._last_target_vertical_offset = 0
        self._target_transition_started = 0.0
        self._target_transition_delta_y = 0.0
        self._target_transition_active = False
        self.secondary_target_indices = ()
        self._raster_key = None
        self._raster_image = None
        self._raster_cache.clear()
        self.cursor.snap(0.0)


__all__ = [
    "CursorTween",
    "HeadlessMenuPresenter",
    "LONG_LIST_VISIBLE_ROWS",
]
