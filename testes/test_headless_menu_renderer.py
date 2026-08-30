from __future__ import annotations

import gc
import unittest
import weakref
from unittest import mock

import headless_menu_renderer as renderer
from PIL import Image, ImageChops


class RendererTests(unittest.TestCase):
    def test_cursor_move_reuses_the_complete_list_base(self):
        common = dict(
            entries=tuple(f"Item {index}" for index in range(12)),
            title="Items",
            right_labels={index: f"x{index + 1}" for index in range(12)},
            right_header="AMT",
            enabled=(True,) * 12,
            first_visible=0,
            scroll_position=0.0,
            max_visible_rows=8,
        )
        first = renderer.render_list(
            renderer.ListView(selected_index=0, cursor_position=0.0, **common),
            now=0.0,
        )
        second = renderer.render_list(
            renderer.ListView(selected_index=1, cursor_position=0.5, **common),
            now=0.01,
        )

        self.assertIs(
            first.info["ff12_dynamic_layers"][0],
            second.info["ff12_dynamic_layers"][0],
        )

    def test_deferred_three_player_list_matches_the_composed_pixels(self):
        view = renderer.ListView(
            entries=tuple(f"Item {index}" for index in range(16)),
            selected_index=10,
            title="Items",
            right_labels={index: f"x{index + 1}" for index in range(16)},
            right_header="AMT",
            enabled=(True,) * 16,
            first_visible=3,
            scroll_position=2.5,
            max_visible_rows=8,
            cursor_position=9.5,
        )
        composed = renderer.scale_three_player_panel(
            renderer.render_list(view, now=1.0, compose_layers=True)
        )
        deferred = renderer.scale_three_player_panel(
            renderer.render_list(view, now=1.0, compose_layers=False)
        )

        self.assertIsNone(ImageChops.difference(composed, deferred).getbbox())

    def test_command_letters_use_native_outline_and_glow(self):
        self.assertEqual(
            renderer.MENU_BLUE_TEXT.edge_radius,
            renderer.native.BLUE_TEXT.edge_radius,
        )
        self.assertEqual(
            renderer.MENU_BLUE_TEXT.glow_radius,
            renderer.native.BLUE_TEXT.glow_radius,
        )
        self.assertEqual(
            renderer.MENU_GOLD_TEXT.edge_radius,
            renderer.native.GOLD_TEXT.edge_radius,
        )
        self.assertEqual(renderer.MENU_BLUE_TEXT.edge[3], 255)
        self.assertEqual(renderer.COMMAND_TEXT_SCALE, 0.99)

    def test_letter_glow_gain_changes_opacity_not_thickness(self):
        self.assertEqual(renderer.LETTER_GLOW_GAIN, 1.25)
        self.assertEqual(
            renderer.MENU_BLUE_TEXT.glow[3],
            min(
                255,
                round(renderer.native.BLUE_TEXT.glow[3] * 1.25),
            ),
        )
        self.assertEqual(
            renderer.MENU_BLUE_TEXT.glow_radius,
            renderer.native.BLUE_TEXT.glow_radius,
        )

    def test_command_core_is_thinner_without_changing_edge_geometry(self):
        self.assertEqual(renderer.COMMAND_CORE_INSET, 0.45)
        self.assertEqual(
            renderer.MENU_BLUE_TEXT.core_inset,
            renderer.COMMAND_CORE_INSET,
        )
        self.assertEqual(
            renderer.MENU_BLUE_TEXT.edge_radius,
            renderer.native.BLUE_TEXT.edge_radius,
        )

    def test_three_player_panel_is_exactly_three_quarters_with_fringe_compensation(
        self,
    ):
        source = Image.new("RGBA", (520, 560), (0, 0, 0, 0))
        source.putpixel((100, 100), (255, 245, 230, 96))
        compact = renderer.scale_three_player_panel(source)
        plain = source.resize(compact.size, Image.Resampling.LANCZOS)
        self.assertEqual(compact.size, (390, 420))
        self.assertEqual(renderer.THREE_PLAYER_PANEL_SCALE, 0.75)
        self.assertGreaterEqual(
            max(compact.getchannel("A").get_flattened_data()),
            max(plain.getchannel("A").get_flattened_data()),
        )

    def test_three_player_panel_preserves_small_dynamic_cursor_layers(self):
        base = Image.new("RGBA", (520, 560), (4, 8, 12, 255))
        cursor = Image.new("RGBA", (12, 8), (255, 255, 255, 220))
        source = base.copy()
        source.alpha_composite(cursor, (80, 120))
        source.info["ff12_dynamic_layers"] = (base, ((cursor, 80, 120),))
        compact = renderer.scale_three_player_panel(source)
        compact_base, layers = compact.info["ff12_dynamic_layers"]
        self.assertEqual(compact_base.size, (390, 420))
        self.assertEqual(len(layers), 1)
        compact_cursor, x, y = layers[0]
        self.assertEqual((x, y), (60, 90))
        self.assertEqual(compact_cursor.size, (9, 6))

    def test_transient_three_player_layers_are_not_kept_alive_by_cache(self):
        renderer._THREE_PLAYER_LAYER_CACHE.clear()
        layer = Image.new("RGBA", (48, 32), (255, 255, 255, 180))
        reference = weakref.ref(layer)

        renderer._cached_three_player_layer(layer)
        del layer
        gc.collect()

        self.assertIsNone(reference())
        self.assertEqual(len(renderer._THREE_PLAYER_LAYER_CACHE), 0)

    def test_help_info_uses_combat_log_dark_outline(self):
        self.assertGreater(min(renderer.HELP_INFO_TEXT.core[:3]), 240)
        self.assertLess(max(renderer.HELP_INFO_TEXT.edge[:3]), 32)
        self.assertLess(max(renderer.HELP_INFO_TEXT.glow[:3]), 8)

    def test_help_badge_matches_native_size_and_baseline(self):
        self.assertEqual(
            renderer.INFO_ATLAS_PATH,
            renderer.RUNTIME_UI_PNG_ROOT / "plik_003.png",
        )
        self.assertTrue(renderer.INFO_ATLAS_PATH.is_file())
        badge = renderer._native_question_badge()
        self.assertIsNotNone(badge)
        self.assertEqual(badge.size, renderer.HELP_BADGE_SIZE)
        self.assertEqual(renderer.HELP_BADGE_SIZE, (56, 57))
        self.assertEqual(renderer.HELP_BADGE_Y, 0)
        self.assertEqual(renderer.HELP_TEXT_Y, 2)

        missing = renderer.RUNTIME_UI_PNG_ROOT / "__missing_info_atlas__.png"
        self.assertFalse(missing.exists())
        try:
            with mock.patch.object(renderer, "INFO_ATLAS_PATH", missing):
                renderer._native_question_badge.cache_clear()
                renderer._native_question_badge_theme.cache_clear()
                renderer.render_help.cache_clear()
                self.assertIsNone(renderer._native_question_badge())
                fallback = renderer.render_help(
                    "Fallback badge",
                    player=2,
                    theme_name="gold",
                )
                self.assertIsNotNone(fallback.getbbox())
        finally:
            renderer._native_question_badge.cache_clear()
            renderer._native_question_badge_theme.cache_clear()
            renderer.render_help.cache_clear()

    def test_shorter_menus_share_the_native_bottom_baseline(self):
        self.assertEqual(renderer.bottom_anchor_offset(row_count=5), 0)
        self.assertEqual(renderer.bottom_anchor_offset(row_count=4), renderer.ROW_PITCH)
        baseline = renderer.PANEL_TOP + renderer.panel_height(5)
        self.assertEqual(
            renderer.PANEL_TOP
            + renderer.TARGET_PANEL_HEIGHT
            + renderer.bottom_anchor_offset(target=True, row_count=1),
            baseline,
        )
        self.assertEqual(
            renderer.PANEL_TOP
            + renderer.target_panel_height(0)
            + renderer.bottom_anchor_offset(target=True, row_count=0),
            baseline,
        )
        self.assertEqual(
            renderer.bottom_anchor_offset(row_count=7),
            -2 * renderer.ROW_PITCH,
        )

    def test_revive_party_names_own_real_rows_for_bottom_anchoring(self):
        self.assertEqual(
            renderer.target_content_rows("party", 3, show_party_names=False),
            0,
        )
        self.assertEqual(
            renderer.target_content_rows("party", 3, show_party_names=True),
            3,
        )
        baseline = renderer.PANEL_TOP + renderer.panel_height(5)
        rows = renderer.target_content_rows("party", 3, show_party_names=True)
        self.assertEqual(
            renderer.PANEL_TOP
            + renderer.target_panel_height(rows)
            + renderer.bottom_anchor_offset(target=True, row_count=rows),
            baseline,
        )

    def test_root_compensates_skin_alpha_margin(self):
        # The supplied nine-slice has a three-pixel transparent/antialias
        # margin. Extra native bottom material prevents the rounded border
        # from being clipped by the transparent overlay surface.
        self.assertEqual(renderer.panel_height(4), 232)
        image = renderer.render_list(
            renderer.ListView(("Attack", "Magicks & Technicks", "Gambits", "Items"))
        )
        self.assertEqual(image.mode, "RGBA")
        self.assertIsNotNone(image.getbbox())

    def test_five_rows_expand_without_clipping(self):
        image = renderer.render_list(
            renderer.ListView(
                ("Attack", "Magicks & Technicks", "Mist", "Gambits", "Items"), 4
            )
        )
        self.assertEqual(renderer.panel_height(5), 278)
        self.assertGreater(image.getbbox()[3], renderer.PANEL_TOP + 250)

    def test_long_list_uses_scrolling_viewport(self):
        image = renderer.render_list(
            renderer.ListView(
                tuple(f"Action {index}" for index in range(12)),
                selected_index=8,
                first_visible=4,
                max_visible_rows=5,
            )
        )
        self.assertEqual(renderer.panel_height(5), 278)
        self.assertLessEqual(image.getbbox()[3], renderer.CANVAS_SIZE[1])

    def test_eight_item_rows_and_title_fit_without_clipping(self):
        image = renderer.render_list(
            renderer.ListView(
                tuple(f"Item {index}" for index in range(12)),
                title="Items",
                right_header="AMT",
                right_labels={index: "x10" for index in range(12)},
                max_visible_rows=8,
            )
        )
        self.assertEqual(renderer.panel_height(9), 462)
        self.assertLess(image.getbbox()[3], renderer.CANVAS_SIZE[1])

    def test_scrollbar_thumb_is_proportional_and_tracks_viewport(self):
        top = renderer.scrollbar_geometry(16, 8, 0, track_top=100, track_bottom=300)
        middle = renderer.scrollbar_geometry(16, 8, 4, track_top=100, track_bottom=300)
        bottom = renderer.scrollbar_geometry(16, 8, 8, track_top=100, track_bottom=300)
        self.assertIsNotNone(top)
        self.assertEqual(top.thumb_bottom - top.thumb_top, 100)
        self.assertLess(top.thumb_top, middle.thumb_top)
        self.assertLess(middle.thumb_top, bottom.thumb_top)
        self.assertEqual(bottom.thumb_bottom, bottom.track_bottom)
        self.assertIsNone(
            renderer.scrollbar_geometry(8, 8, 0, track_top=100, track_bottom=300)
        )

    def test_overflow_cue_changes_only_when_more_rows_follow(self):
        exact = renderer.render_list(
            renderer.ListView(
                tuple(f"Item {index}" for index in range(8)),
                title="Items",
                max_visible_rows=8,
            )
        )
        overflow = renderer.render_list(
            renderer.ListView(
                tuple(f"Item {index}" for index in range(9)),
                title="Items",
                max_visible_rows=8,
            )
        )
        self.assertNotEqual(exact.tobytes(), overflow.tobytes())

    def test_subpage_title_owns_a_real_panel_row(self):
        view = renderer.ListView(("Cure",), title="White Magicks")
        image = renderer.render_list(view)
        self.assertEqual(renderer.panel_height(2), 140)
        self.assertGreater(image.getbbox()[3], renderer.PANEL_TOP + 110)

    def test_target_warning(self):
        warning = renderer.render_target(
            renderer.TargetView("Attack", has_target=False), now=1.0
        )
        self.assertIsNotNone(warning.getbbox())

    def test_single_family_action_has_no_group_switch_footer(self):
        view = renderer.TargetView(
            "Steal",
            target_names=("Wolf",),
            available_groups=("foes",),
        )
        with mock.patch.object(renderer, "draw_keycap") as keycap:
            renderer._STATIC_TARGET_BASES.clear()
            renderer.render_target(view)
        self.assertEqual(keycap.call_count, 0)

    def test_area_targets_add_translucent_hands_without_weakening_primary(self):
        view = renderer.TargetView(
            "Fira",
            target_names=("Wolf A", "Wolf B", "Wolf C"),
            selected_index=0,
            secondary_indices=(1, 2),
            available_groups=("foes",),
        )
        renderer._STATIC_TARGET_BASES.clear()
        image = renderer.render_target(view, now=0.0)
        _base, layers = image.info["ff12_dynamic_layers"]
        self.assertEqual(len(layers), 3)
        secondary_alpha = [
            layer.getchannel("A").getextrema()[1] for layer, _x, _y in layers[:2]
        ]
        primary_alpha = layers[-1][0].getchannel("A").getextrema()[1]
        self.assertEqual(secondary_alpha, [153, 153])
        self.assertEqual(primary_alpha, 255)

    def test_two_family_action_shows_a_real_switch_affordance(self):
        view = renderer.TargetView(
            "Float",
            group="party",
            target_names=(),
            available_groups=("party", "neutrals"),
        )
        with mock.patch.object(renderer, "draw_keycap") as keycap:
            renderer._STATIC_TARGET_BASES.clear()
            renderer.render_target(view)
        self.assertEqual(keycap.call_count, 2)

    def test_target_names_are_lettered_in_the_players_own_colour(self):
        """A foe's name is drawn exactly like "Gambits" in that menu.

        The rows used to be a fixed white, so every player's target window
        looked the same and belonged to none of them.
        """

        view = renderer.TargetView("Attack", target_names=("Wolf A",))
        renderer._STATIC_TARGET_BASES.clear()
        blue = renderer.render_target(view, player=2, theme_name="blue")
        renderer._STATIC_TARGET_BASES.clear()
        pink = renderer.render_target(view, player=2, theme_name="pink")
        self.assertNotEqual(blue.tobytes(), pink.tobytes())

        # Not merely different per theme: the row is the very sprite the
        # command list draws, so the two can never drift apart.  Marked rather
        # than sampled, because the panel behind the row is opaque and would
        # answer for the lettering.
        marker = Image.new("RGBA", (6, 6), (7, 9, 11, 255))
        with mock.patch.object(renderer, "_text", return_value=marker):
            renderer._STATIC_TARGET_BASES.clear()
            marked = renderer.render_target(view, player=2, theme_name="blue")
        renderer._STATIC_TARGET_BASES.clear()

        placed = [
            (x, y)
            for y in range(marked.height)
            for x in range(marked.width)
            if marked.getpixel((x, y)) == (7, 9, 11, 255)
        ]
        self.assertTrue(placed, "the target row did not come from _text")
        self.assertEqual(
            (min(x for x, _y in placed), min(y for _x, y in placed)),
            (renderer.PANEL_LEFT + 37, renderer.PANEL_TOP + 58),
        )

    def test_named_labels_take_their_own_horizontal_nudge(self):
        """One label may sit off the shared column; the rest must not move."""

        self.assertEqual(renderer.LABEL_X_NUDGE.get("Gambits"), -2)

        view = renderer.ListView(("Attack", "Gambits"))
        # The label sprite is marked instead of measured: the panel behind the
        # rows is opaque, so a bounding box would report the panel's edge and
        # move for neither label.  The hand is mocked away for the same reason
        # , it is composited last and covers the very column being read.
        marker = Image.new("RGBA", (6, 6), (7, 9, 11, 255))
        blank = Image.new("RGBA", (1, 1), (0, 0, 0, 0))

        def left_edges(nudges):
            with (
                mock.patch.object(renderer, "_text", return_value=marker),
                mock.patch.object(renderer, "_cursor", return_value=blank),
                mock.patch.object(renderer, "LABEL_X_NUDGE", nudges),
            ):
                renderer._STATIC_LIST_BASES.clear()
                image = renderer.render_list(view, player=2, theme_name="blue")
            found: dict[int, int] = {}
            for y in range(image.height):
                for x in range(image.width):
                    if image.getpixel((x, y)) == (7, 9, 11, 255):
                        found.setdefault(y, x)
            self.assertTrue(found, "no label was drawn")
            # One entry per row band, in row order.
            return [found[y] for y in sorted(found) if y - 1 not in found]

        nudged = left_edges(renderer.LABEL_X_NUDGE)
        plain = left_edges({})
        renderer._STATIC_LIST_BASES.clear()

        self.assertEqual(len(plain), 2)
        # Attack is not in the table, so it does not budge; Gambits moves left.
        self.assertEqual(nudged[0], plain[0])
        self.assertEqual(nudged[1], plain[1] - 2)

    def test_no_target_warning_has_no_selectable_cursor(self):
        cursor_color = (1, 2, 3, 255)
        cursor = Image.new("RGBA", (4, 4), cursor_color)
        with mock.patch.object(renderer, "_cursor", return_value=cursor):
            image = renderer.render_target(
                renderer.TargetView("Warp Mote", has_target=False),
                now=0.0,
            )
        cursor_pixels = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if image.getpixel((x, y)) == cursor_color
        ]
        self.assertFalse(cursor_pixels)
        _base, dynamic_layers = image.info["ff12_dynamic_layers"]
        self.assertEqual(len(dynamic_layers), 1)  # single pulsing warning

    def test_fran_ears_fit_canvas_without_changing_other_portrait_scales(self):
        fran = renderer._portrait(player=2, party_member_id=2)
        fran_position = renderer._portrait_position(
            player=2,
            anchor_y=renderer.PORTRAIT_Y_OFFSET,
            party_member_id=2,
        )
        self.assertGreaterEqual(fran_position[1], 0)
        self.assertEqual(fran.size, (52, 94))
        penelo = renderer._portrait(player=2, party_member_id=5)
        self.assertEqual(penelo.size, (72, 87))

    def test_orange_command_flag_follows_any_p1_leader_portrait(self):
        leader_flag = Image.new("RGBA", (4, 4), (255, 96, 0, 255))
        canvas = Image.new("RGBA", renderer.CANVAS_SIZE, (0, 0, 0, 0))
        with mock.patch.object(
            renderer, "_leader_command_flag", return_value=leader_flag
        ):
            renderer._paste_leader_flag(
                canvas,
                player=1,
                anchor_y=renderer.PORTRAIT_Y_OFFSET,
                party_member_id=4,
            )
        self.assertIsNotNone(canvas.getbbox())

        p2_canvas = Image.new("RGBA", renderer.CANVAS_SIZE, (0, 0, 0, 0))
        with mock.patch.object(
            renderer, "_leader_command_flag", return_value=leader_flag
        ):
            renderer._paste_leader_flag(
                p2_canvas,
                player=2,
                anchor_y=renderer.PORTRAIT_Y_OFFSET,
                party_member_id=4,
            )
        self.assertIsNone(p2_canvas.getbbox())

    def test_party_targets_use_title_only_panel_without_warning_row(self):
        view = renderer.TargetView(
            "Potion",
            has_target=True,
            group="party",
            target_names=(),
        )
        image = renderer.render_target(view, now=1.0)
        self.assertIsNotNone(image.getbbox())
        self.assertEqual(
            renderer.target_panel_height(0),
            renderer.TARGET_TITLE_ONLY_HEIGHT,
        )

    def test_transition_is_mirrored_and_monotonic(self):
        image = renderer.render_list(renderer.ListView(("Attack",)))
        opening = [
            renderer.transition(image, p, opening=True) for p in (0, 0.25, 0.5, 1)
        ]
        self.assertTrue(
            all(a.opacity <= b.opacity for a, b in zip(opening, opening[1:]))
        )
        self.assertGreater(opening[0].offset_x, opening[-1].offset_x)
        closing = [
            renderer.transition(image, p, opening=False) for p in (0, 0.25, 0.5, 1)
        ]
        self.assertTrue(
            all(a.opacity >= b.opacity for a, b in zip(closing, closing[1:]))
        )
        self.assertLess(closing[0].offset_x, closing[-1].offset_x)

    def test_nested_page_transition_smoothly_settles_the_new_page(self):
        old = renderer.render_list(renderer.ListView(("Magicks & Technicks",)))
        new = renderer.render_list(
            renderer.ListView(
                ("Cure",),
                title="White Magicks",
                right_header="MP",
                right_labels={0: "8"},
            )
        )
        start = renderer.page_transition(old, new, 0.0, forward=True)
        middle = renderer.page_transition(old, new, 0.5, forward=True)
        end = renderer.page_transition(old, new, 1.0, forward=True)
        self.assertEqual(middle.mode, "RGBA")
        self.assertIsNotNone(middle.getbbox())
        self.assertEqual(
            start.info["ff12_local_offset_y"],
            renderer.PAGE_TRANSITION_SLIDE_PX,
        )
        self.assertGreater(middle.info["ff12_local_offset_y"], 0.0)
        self.assertLess(
            middle.info["ff12_local_offset_y"],
            renderer.PAGE_TRANSITION_SLIDE_PX,
        )
        self.assertEqual(end.info["ff12_local_offset_y"], 0.0)
        self.assertLess(start.info["ff12_local_opacity"], 1.0)
        self.assertEqual(end.info["ff12_local_opacity"], 1.0)

    def test_fractional_scroll_moves_list_pixels_between_integer_rows(self):
        entries = tuple(f"Action {index}" for index in range(10))
        top = renderer.render_list(
            renderer.ListView(
                entries,
                selected_index=5,
                first_visible=1,
                scroll_position=1.0,
                max_visible_rows=5,
            )
        )
        moving = renderer.render_list(
            renderer.ListView(
                entries,
                selected_index=5,
                first_visible=1,
                scroll_position=0.5,
                max_visible_rows=5,
            )
        )
        self.assertIsNotNone(ImageChops.difference(top, moving).getbbox())

    def test_target_group_transition_preserves_old_bottom_anchor_then_settles(self):
        image = renderer.render_target(
            renderer.TargetView("Attack", group="party", target_names=())
        )
        start = renderer.target_group_transition(
            image,
            0.0,
            initial_offset_y=renderer.ROW_PITCH,
        )
        end = renderer.target_group_transition(
            image,
            1.0,
            initial_offset_y=renderer.ROW_PITCH,
        )
        self.assertEqual(start.info["ff12_local_offset_y"], renderer.ROW_PITCH)
        self.assertEqual(end.info["ff12_local_offset_y"], 0.0)
        self.assertLess(start.info["ff12_local_opacity"], 1.0)
        self.assertEqual(end.info["ff12_local_opacity"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
