import json
import tempfile
import unittest
from pathlib import Path

import headless_menu_renderer as renderer
import menu_themes
import native_ui_assets as native


PUBLIC_PRESETS = tuple(menu_themes.PRESETS)


class MenuThemeTests(unittest.TestCase):
    def test_fractional_outline_blends_adjacent_pixel_radii(self):
        mask = native.Image.new("L", (9, 9), 0)
        mask.putpixel((4, 4), 255)
        one = native._max_filter(mask, 1.0)
        one_and_half = native._max_filter(mask, 1.5)
        two = native._max_filter(mask, 2.0)
        self.assertEqual(one.getpixel((2, 4)), 0)
        self.assertIn(one_and_half.getpixel((2, 4)), range(126, 130))
        self.assertEqual(two.getpixel((2, 4)), 255)

    def test_fractional_core_inset_blends_mask_contraction(self):
        mask = native.Image.new("L", (9, 9), 255)
        mask.putpixel((0, 4), 0)
        original = native._min_filter(mask, 0.0)
        half = native._min_filter(mask, 0.5)
        one = native._min_filter(mask, 1.0)
        self.assertEqual(original.getpixel((1, 4)), 255)
        self.assertIn(half.getpixel((1, 4)), range(126, 130))
        self.assertEqual(one.getpixel((1, 4)), 0)

    def test_directional_inner_glow_is_side_top_bottom_weighted(self):
        panel = native.Image.new("RGBA", (24, 16), (16, 32, 64, 255))
        reinforced = native._reinforce_directional_skin_edges(panel, "blue")
        self.assertEqual(reinforced.size, panel.size)
        self.assertEqual(
            reinforced.getchannel("A").tobytes(),
            panel.getchannel("A").tobytes(),
        )
        base = sum(panel.getpixel((12, 8))[:3])
        side = sum(reinforced.getpixel((0, 8))[:3]) - base
        top = sum(reinforced.getpixel((12, 0))[:3]) - base
        bottom = sum(reinforced.getpixel((12, 15))[:3]) - base
        self.assertGreater(side, top)
        self.assertGreater(top, bottom)
        self.assertGreater(bottom, 0)

    def test_top_corner_highlight_preserves_authored_alpha(self):
        corner = native._skin_piece("border_tl.png", "blue").resize((36, 30))
        strengthened = native._strengthen_skin_top_corner(corner, "blue")
        self.assertEqual(
            corner.getchannel("A").tobytes(),
            strengthened.getchannel("A").tobytes(),
        )
        self.assertNotEqual(
            corner.convert("RGB").tobytes(),
            strengthened.convert("RGB").tobytes(),
        )

    def test_all_public_presets_render_distinct_native_panels(self):
        signatures = set()
        view = renderer.ListView(("Attack", "Magicks", "Items"), 0)
        for name in PUBLIC_PRESETS:
            image = renderer.render_list(view, theme_name=name)
            self.assertEqual(image.size, renderer.CANVAS_SIZE)
            self.assertIsNotNone(image.getbbox(), name)
            signatures.add(image.convert("RGB").resize((1, 1)).getpixel((0, 0)))
        # Transparent canvas averaging can collapse some themes; the actual
        # menu region must nevertheless retain a unique raster for each one.
        rasters = {
            renderer.render_list(view, theme_name=name)
            .crop((renderer.PANEL_LEFT, renderer.PANEL_TOP, 480, 250))
            .tobytes()
            for name in PUBLIC_PRESETS
        }
        self.assertEqual(len(rasters), len(PUBLIC_PRESETS))

    def test_portuguese_aliases_are_supported(self):
        self.assertEqual(menu_themes.normalize_name("esmeralda"), "emerald")
        self.assertEqual(menu_themes.normalize_name("roxo"), "amethyst")
        self.assertEqual(menu_themes.normalize_name("fogo"), "emerald")
        self.assertEqual(menu_themes.normalize_name("marrom escuro"), "bronze")
        self.assertEqual(menu_themes.normalize_name("white"), "bronze")
        self.assertNotIn("white", menu_themes.PRESETS)

    def test_config_selects_each_player_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            path.write_text(
                json.dumps({"player_1": "preto", "player_2": "rosa"}),
                encoding="utf-8",
            )
            selection = menu_themes.load_selection(path)
        self.assertEqual(selection.player_1, "ruby")
        self.assertEqual(selection.player_2, "pink")
        self.assertEqual(selection.player_3, "pink")
        self.assertEqual(selection.assignment_mode, "character")

    def test_missing_config_uses_default_selection_on_first_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coop" / "menu_theme_config.json"
            selection = menu_themes.load_selection(path)

        self.assertEqual(selection, menu_themes.default_selection())

    def test_legacy_positional_selection_keeps_field_order_and_p3_inherits_p2(self):
        selection = menu_themes.ThemeSelection(
            "black",
            "pink",
            ((4, "fire"),),
        )
        self.assertEqual(selection.player_1, "ruby")
        self.assertEqual(selection.player_2, "pink")
        self.assertEqual(selection.player_3, "pink")
        self.assertEqual(selection.for_character(4), "emerald")

    def test_version_two_defaults_to_character_mode_and_p3_inherits_p2(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "defaults": {"player_1": "ruby", "player_2": "purple"},
                        "characters": {"Basch": "fire"},
                    }
                ),
                encoding="utf-8",
            )
            selection = menu_themes.load_selection(path)
        self.assertEqual(selection.assignment_mode, "character")
        self.assertEqual(selection.player_3, "amethyst")
        self.assertEqual(selection.for_player(2, 4), "emerald")

    def test_player_mode_keeps_colours_with_roles_after_character_swap(self):
        selection = menu_themes.ThemeSelection(
            player_1="blue",
            player_2="pink",
            player_3="emerald",
            assignment_mode="player",
            character_overrides=((1, "ruby"), (4, "fire"), (5, "gold")),
        )
        before = (
            selection.for_player(1, 4),
            selection.for_player(2, 5),
            selection.for_player(3, 1),
        )
        after = (
            selection.for_player(1, 4),
            selection.for_player(2, 1),
            selection.for_player(3, 5),
        )
        self.assertEqual(before, ("sapphire", "pink", "emerald"))
        self.assertEqual(after, before)
        # The inactive character catalog remains intact for switching modes.
        self.assertEqual(selection.for_character(4), "emerald")
        self.assertEqual(selection.for_character(1), "ruby")

    def test_invalid_assignment_mode_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            path.write_text(
                json.dumps({"assignment_mode": "party_slot"}),
                encoding="utf-8",
            )
            selection = menu_themes.load_selection(path)

        self.assertEqual(selection, menu_themes.default_selection())

    def test_truncated_config_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            path.write_text('{"defaults":', encoding="utf-8")
            selection = menu_themes.load_selection(path)

        self.assertEqual(selection, menu_themes.default_selection())

    def test_live_tracker_keeps_last_complete_selection_during_invalid_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            expected = menu_themes.ThemeSelection(
                assignment_mode="character",
                character_overrides=((5, "gold"), (3, "bronze"), (2, "amethyst")),
            )
            menu_themes.save_selection(expected, path)
            tracker = menu_themes.ThemeTracker(path, poll_seconds=0.05)
            self.assertEqual(tracker.selection.for_player(1, 5), "gold")

            path.write_text('{"assignment_mode":', encoding="utf-8")
            retained = tracker.reload(force=True)

        self.assertEqual(retained, tracker.selection)
        self.assertEqual(retained.for_player(1, 5), "gold")
        self.assertEqual(retained.for_player(2, 3), "bronze")
        self.assertEqual(retained.for_player(3, 2), "amethyst")

    def test_live_tracker_first_run_uses_full_character_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker = menu_themes.ThemeTracker(
                Path(folder) / "missing.json", poll_seconds=0.05
            )

        self.assertEqual(tracker.selection, menu_themes.default_selection())

    def test_character_theme_overrides_player_slot_and_accepts_names(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            path.write_text(
                json.dumps(
                    {
                        "defaults": {"player_1": "ruby", "player_2": "gold"},
                        "characters": {"Basch": "fire", "Penelo": "gold"},
                    }
                ),
                encoding="utf-8",
            )
            selection = menu_themes.load_selection(path)
        self.assertEqual(selection.for_player(2, 4), "emerald")
        self.assertEqual(selection.for_player(2, 5), "gold")
        self.assertEqual(selection.for_player(2, 1), "pink")

    def test_story_variants_share_the_playable_character_theme(self):
        selection = menu_themes.ThemeSelection(
            character_overrides=(
                (1, "emerald"),
                (4, "black"),
                (13, "white"),
            )
        )
        self.assertEqual(selection.for_character(7), "emerald")
        self.assertEqual(selection.for_character(8), "ruby")
        self.assertEqual(selection.for_character(9), "ruby")
        self.assertEqual(selection.for_character(10), "bronze")

    def test_target_circle_has_a_valid_palette_for_every_theme(self):
        for theme_name in menu_themes.PRESETS:
            palette = menu_themes.target_circle_palette(theme_name)
            self.assertIn(palette, range(7))
        self.assertEqual(menu_themes.target_circle_palette("blue"), 0)
        self.assertEqual(menu_themes.target_circle_palette("fire"), 3)
        self.assertEqual(menu_themes.target_circle_palette("pink"), 2)
        self.assertEqual(menu_themes.target_circle_palette("emerald"), 3)
        self.assertEqual(menu_themes.target_circle_palette("gold"), 4)
        self.assertEqual(menu_themes.target_circle_palette("purple"), 5)
        self.assertEqual(menu_themes.target_circle_palette("dark_brown"), 6)
        self.assertEqual(menu_themes.target_circle_palette("black"), 1)

    def test_save_selection_round_trips_player_mode_and_all_three_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.json"
            saved = menu_themes.save_selection(
                menu_themes.ThemeSelection(
                    player_1="ruby",
                    player_2="pink",
                    player_3="emerald",
                    assignment_mode="player",
                    character_overrides=((4, "fire"), (7, "purple")),
                ),
                path,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 3)
        self.assertEqual(payload["assignment_mode"], "player")
        self.assertEqual(
            payload["defaults"],
            {"player_1": "ruby", "player_2": "pink", "player_3": "emerald"},
        )
        self.assertEqual(saved.for_player(1, 4), "ruby")
        self.assertEqual(saved.for_player(2, 4), "pink")
        self.assertEqual(saved.for_player(3, 4), "emerald")
        self.assertEqual(saved.for_character(4), "emerald")
        self.assertEqual(saved.for_character(7), "amethyst")

    def test_gauge_palette_preserves_gold_and_builds_emerald_gradient(self):
        self.assertEqual(
            menu_themes.gauge_palette("gold"),
            (127, 127, 58, 127, 127, 108, 18, 127),
        )
        self.assertEqual(
            menu_themes.gauge_palette("fire"),
            (69, 127, 97, 127, 17, 90, 52, 127),
        )

    def test_native_party_names_follow_the_same_theme_catalog(self):
        self.assertEqual(menu_themes.hud_text_color("blue"), (74, 112, 127))
        self.assertEqual(menu_themes.hud_text_color("gold"), (127, 108, 18))
        self.assertEqual(menu_themes.hud_text_color("fire"), (17, 90, 52))

    def test_default_character_roster_has_one_shared_theme_identity(self):
        # The live config is user-editable and may legitimately differ from
        # the built-in roster while this suite runs.
        selection = menu_themes.ThemeSelection(
            character_overrides=tuple(
                sorted(menu_themes.DEFAULT_CHARACTER_THEMES.items())
            )
        )
        expected = {
            0: "sapphire",  # Vaan
            1: "pink",  # Ashe
            2: "amethyst",  # Fran
            3: "bronze",  # Balthier
            4: "emerald",  # Basch
            5: "gold",  # Penelo
            6: "sapphire",  # Reks
            12: "sapphire",  # Vossler
            13: "sapphire",  # Larsa / Lamont
            14: "sapphire",  # Reddas
        }
        for member_id, theme_name in expected.items():
            self.assertEqual(selection.for_player(2, member_id), theme_name)
        self.assertEqual(len(menu_themes.MAIN_CHARACTER_PROFILES), 6)
        self.assertEqual(
            {profile.name for profile in menu_themes.GUEST_CHARACTER_PROFILES},
            {"Reks", "Vossler", "Larsa", "Reddas"},
        )


if __name__ == "__main__":
    unittest.main()
