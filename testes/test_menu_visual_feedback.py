"""Turn each visual-feedback item into a verifiable regression test."""

import colorsys
import unittest

import headless_menu_renderer as renderer
import headless_world_target_overlay as world_overlay
import menu_themes
import native_ui_assets as native
from headless_menu_model import ActionDefinition, MenuEntry, PageDefinition
from headless_menu_model import EntryKind, TargetGroup


THEMES = ("gold", "sapphire", "emerald", "amethyst", "pink", "ruby", "bronze")


class EmptyTargetListTests(unittest.TestCase):
    """An empty target list must not appear to contain one entry."""

    def test_empty_list_reserves_one_message_row(self):
        for group in ("foes", "neutrals", "party"):
            with self.subTest(group=group):
                self.assertEqual(renderer.target_content_rows(group, 0), 1)

    def test_empty_list_panel_has_room_for_warning(self):
        rows = renderer.target_content_rows("party", 0)
        self.assertEqual(
            renderer.target_panel_height(rows), renderer.TARGET_PANEL_HEIGHT
        )

    def test_ally_group_still_hides_the_list_when_it_has_names(self):
        self.assertEqual(renderer.target_content_rows("party", 3), 0)

    def test_revive_still_shows_the_party_names(self):
        self.assertEqual(
            renderer.target_content_rows("party", 3, show_party_names=True), 3
        )

    def test_foe_counts_are_untouched(self):
        self.assertEqual(renderer.target_content_rows("foes", 1), 1)
        self.assertEqual(renderer.target_content_rows("foes", 4), 4)
        self.assertEqual(
            renderer.target_content_rows("foes", 99),
            renderer.MAX_TARGET_VISIBLE_ROWS,
        )


class ItemQuantityTests(unittest.TestCase):
    """The AMT column displays only the number, matching the game."""

    def test_quantity_label_has_no_x_prefix(self):
        from headless_menu_presenter import HeadlessMenuPresenter

        action = ActionDefinition(
            0x10,
            "Potion",
            "Restores HP.",
            (TargetGroup.PARTY,),
            TargetGroup.PARTY,
            "Items",
            quantity=12,
        )
        page = PageDefinition(
            "items",
            "Items",
            (MenuEntry("Potion", "Restores HP.", EntryKind.ACTION, action=action),),
        )
        labels, header = HeadlessMenuPresenter._right_labels(_PresenterStub(), page)
        self.assertEqual(labels[0], "12")
        self.assertEqual(header, "AMT")
        self.assertNotIn("x", labels[0])


class _PresenterStub:
    """Provide only enough state to exercise _right_labels offline."""

    class _Model:
        gambits_enabled = False

    model = _Model()


class TargetInfoScaleTests(unittest.TestCase):
    """The panel grew by roughly five percent and still fits side by side."""

    def test_scale_grew_but_stays_under_one(self):
        self.assertGreater(world_overlay.FRAMED_SCALE, 0.825)
        self.assertLessEqual(world_overlay.FRAMED_SCALE, 1.0)

    def test_three_cards_still_fit_the_screen(self):
        from headless_menu_shell import P1_TARGET_INFO_X

        span = P1_TARGET_INFO_X + 3 * world_overlay.panel_pitch()
        self.assertLess(span, 1920)

    def test_slab_stays_inside_the_card(self):
        # The card gap is the slab space left inside the owned width.
        slab = round(world_overlay.CARD_BOX_WIDTH * world_overlay.panel_scale())
        self.assertLessEqual(slab, world_overlay.panel_pitch())


class ScrollbarInsideFrameTests(unittest.TestCase):
    """The scrollbar stays inside the frame instead of covering its border."""

    def panel_body_right_edge(self) -> int:
        panel = native.native_panel(
            (renderer.PANEL_WIDTH, renderer.panel_height(6)), palette="gold"
        )
        pixels = panel.load()
        row = panel.height // 2
        origin = renderer.PANEL_LEFT - 3
        # The drawn body begins at the first opaque pixel from the outside in;
        # preceding pixels belong only to the sprite's outer glow.
        for local in range(panel.width - 1, -1, -1):
            if pixels[local, row][3] >= 200:
                return origin + local
        raise AssertionError("borda do painel nao encontrada")

    def test_scrollbar_right_edge_is_inside_the_panel_body(self):
        layer, left, _top = renderer._list_scrollbar_layer(200, 400, 240, 300, "gold")
        right = left + layer.width - 1
        self.assertLess(right, self.panel_body_right_edge())

    def test_scrollbar_does_not_cover_the_right_labels(self):
        layer, left, _top = renderer._list_scrollbar_layer(200, 400, 240, 300, "gold")
        self.assertGreater(left, renderer.PANEL_LEFT + renderer.RIGHT_LABEL_ANCHOR - 60)


class MenuColourVibrancyTests(unittest.TestCase):
    """No theme may regress to the washed-out yellow formerly used by gold."""

    def test_every_theme_has_a_chromatic_edge_distinct_from_its_core(self):
        for name in THEMES:
            with self.subTest(theme=name):
                style = renderer._menu_text_style(name)
                _hue, saturation, _value = colorsys.rgb_to_hsv(
                    *(channel / 255 for channel in style.edge[:3])
                )
                contrast = abs(
                    native.relative_luminance(style.core)
                    - native.relative_luminance(style.edge)
                )
                self.assertGreaterEqual(saturation, 0.60)
                self.assertGreaterEqual(contrast, 110.0)
                self.assertAlmostEqual(
                    native.relative_luminance(style.edge),
                    renderer.MENU_EDGE_TARGET_LUMA,
                    delta=1.0,
                )

    def test_perceptual_edge_preserves_theme_hue(self):
        for name in THEMES:
            with self.subTest(theme=name):
                theme = menu_themes.get_theme(name)
                after = renderer._maximum_chroma_menu_edge(theme.accent)
                hue_a, _sat_a, _ = colorsys.rgb_to_hsv(
                    *(channel / 255 for channel in theme.accent[:3])
                )
                hue_b, _sat_b, _ = colorsys.rgb_to_hsv(
                    *(channel / 255 for channel in after[:3])
                )
                self.assertAlmostEqual(hue_a, hue_b, places=2)

    def test_non_gold_cores_are_near_white_and_subtly_theme_tinted(self):
        for name in THEMES:
            if name == "gold":
                continue
            with self.subTest(theme=name):
                style = renderer._menu_text_style(name)
                self.assertGreaterEqual(min(style.core[:3]), 237)
                self.assertNotEqual(style.core[:3], (255, 255, 255))

    def test_gold_keeps_a_warm_white_core_and_a_distinct_saturated_edge(self):
        core = renderer.MENU_GOLD_TEXT.core
        edge = renderer.MENU_GOLD_TEXT.edge
        _hue, saturation, _value = colorsys.rgb_to_hsv(
            *(channel / 255 for channel in edge[:3])
        )
        core_luma = native.relative_luminance(core)
        edge_luma = native.relative_luminance(edge)
        pink_edge_luma = native.relative_luminance(
            renderer._menu_text_style("pink").edge
        )
        self.assertGreaterEqual(min(core[:3]), 230)
        self.assertGreater(saturation, 0.95)
        self.assertGreater(core_luma - edge_luma, 100.0)
        self.assertAlmostEqual(edge_luma, pink_edge_luma, delta=3.0)
        self.assertLess(renderer.MENU_GOLD_TEXT.glow[3], 255)


class PageTransitionTests(unittest.TestCase):
    """Page transitions must not jump."""

    def test_average_speed_is_gentle(self):
        speed = renderer.PAGE_TRANSITION_SLIDE_PX / renderer.PAGE_TRANSITION_SECONDS
        self.assertLess(speed, 120.0)

    def test_slide_ends_exactly_at_rest(self):
        finished = renderer.page_transition(
            renderer.Image.new("RGBA", (8, 8)),
            renderer.Image.new("RGBA", (8, 8)),
            1.0,
            forward=True,
        )
        self.assertAlmostEqual(finished.info["ff12_local_offset_y"], 0.0)
        self.assertAlmostEqual(finished.info["ff12_local_opacity"], 1.0)


class CombatLogClearanceTests(unittest.TestCase):
    """Help text must clear the log even when the message came from the game."""

    def test_clearance_covers_three_log_lines(self):
        from headless_menu_shell import COMBAT_LOG_CLEARANCE_PX, COMBAT_LOG_LINE_PX

        self.assertGreaterEqual(COMBAT_LOG_CLEARANCE_PX, 3 * COMBAT_LOG_LINE_PX)

    def test_clearance_beats_the_idle_lift(self):
        from headless_menu_shell import COMBAT_LOG_CLEARANCE_PX, UPPER_INFO_IDLE_LIFT_PX

        self.assertGreater(COMBAT_LOG_CLEARANCE_PX, UPPER_INFO_IDLE_LIFT_PX)


if __name__ == "__main__":
    unittest.main()
