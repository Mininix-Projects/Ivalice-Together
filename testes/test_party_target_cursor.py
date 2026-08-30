import unittest
from types import SimpleNamespace

import numpy as np

import headless_menu_renderer as renderer
import headless_menu_shell as shell
import party_hud_layout as hud
from headless_menu_model import (
    ActionDefinition,
    MenuMode,
    P2MenuModel,
    TargetCandidate,
    TargetGroup,
    make_demo_catalog,
)


class PartyTargetCursorTests(unittest.TestCase):
    def test_native_cursor_geometry_and_center_are_preserved(self):
        original = renderer._cursor()
        pink = renderer.party_target_cursor(False)
        self.assertEqual(pink.size, original.size)
        original_pixels = np.asarray(original)
        pink_pixels = np.asarray(pink)
        changed = np.any(original_pixels != pink_pixels, axis=2)
        self.assertGreater(int(changed.sum()), 750)
        self.assertTrue(np.all(pink_pixels[changed, :3] == (255, 47, 155)))
        self.assertTrue(np.array_equal(original_pixels[25, 25], pink_pixels[25, 25]))
        self.assertTrue(np.array_equal(original_pixels[50], pink_pixels[50]))

    def test_reduced_cursor_matches_the_native_hud_scale(self):
        self.assertEqual(renderer.party_target_cursor(True).size, (48, 40))

    def test_every_theme_gets_its_own_outline(self):
        original = np.asarray(renderer._cursor())
        for theme_name, expected in renderer.PARTY_CURSOR_OUTLINE_BY_THEME.items():
            with self.subTest(theme=theme_name):
                themed = np.asarray(
                    renderer.party_target_cursor(False, theme_name=theme_name)
                )
                changed = np.any(original != themed, axis=2)
                self.assertTrue(np.all(themed[changed, :3] == expected))

    def test_afterimage_occupies_the_previous_and_current_rows(self):
        image = renderer.render_party_target_cursor(
            "pink",
            reduced=False,
            position=2.0,
            trail=((1.0, 1.0),),
        )
        alpha = np.asarray(image)[..., 3]
        self.assertGreater(int(alpha[60:114].max()), 0)
        self.assertGreater(int(alpha[120:174].max()), 0)

    def test_area_hands_are_translucent_and_leave_the_primary_opaque(self):
        image = renderer.render_party_target_cursor(
            "pink",
            reduced=False,
            position=2.0,
            secondary_positions=(0.0, 1.0),
            secondary_opacity=0.40,
        )
        alpha = np.asarray(image)[..., 3]
        self.assertLessEqual(int(alpha[0:54].max()), 102)
        self.assertLessEqual(int(alpha[60:114].max()), 102)
        self.assertEqual(int(alpha[120:174].max()), 255)

    def test_area_hand_pulse_runs_from_twenty_to_sixty_percent_at_five_hertz(self):
        self.assertAlmostEqual(renderer.area_cursor_opacity(0.0), 0.60)
        self.assertAlmostEqual(renderer.area_cursor_opacity(0.10), 0.20)
        self.assertAlmostEqual(renderer.area_cursor_opacity(0.20), 0.60)

    def test_visual_rows_are_canonical_and_bottom_anchored(self):
        layout = (5, 2, 1, hud.EMPTY_PARTY_MEMBER_ID)
        self.assertEqual(hud.party_hud_member_row(layout, 2), 1)
        self.assertEqual(hud.party_hud_member_row(layout, 1), 2)
        self.assertEqual(hud.party_hud_member_row(layout, 5), 3)
        self.assertIsNone(hud.party_hud_member_row(layout, 4))

    def test_cursor_tip_tracks_full_and_reduced_hud_geometry(self):
        full = hud.party_hud_cursor_position(3, reduced=False, jab=4.5)
        reduced = hud.party_hud_cursor_position(3, reduced=True, jab=4.5)
        summoned = hud.party_hud_cursor_position(
            3,
            reduced=False,
            jab=4.5,
            esper_active=True,
        )
        self.assertEqual(full, (1188.5, 978.0))
        self.assertAlmostEqual(reduced[0], 1384.375)
        self.assertAlmostEqual(reduced[1], 991.0)
        self.assertEqual(summoned, (1188.5, 981.0))

    def test_area_resolution_uses_native_horizontal_radius_only(self):
        candidates = (
            TargetCandidate(0x100, "Wolf A", TargetGroup.FOES),
            TargetCandidate(0x101, "Wolf B", TargetGroup.FOES),
            TargetCandidate(0x102, "Wolf C", TargetGroup.FOES),
            TargetCandidate(0x103, "Wolf D", TargetGroup.FOES),
        )
        positions = {
            0x100: (0.0, 0.0, 0.0),
            0x101: (6.0, 999.0, 0.0),
            0x102: (8.01, 0.0, 0.0),
            0x103: (0.0, 0.0, 8.0),
        }
        self.assertEqual(
            shell.area_secondary_target_indices(candidates, 0, 8.0, positions),
            (1, 3),
        )
        self.assertEqual(
            shell.area_secondary_target_indices(candidates, 0, 0.0, positions),
            (),
        )

    def test_reserve_area_resolution_uses_the_logical_target_group(self):
        candidates = (
            TargetCandidate(0, "Vaan", TargetGroup.RESERVE, party_member_id=0),
            TargetCandidate(0, "Fran", TargetGroup.RESERVE, party_member_id=2),
            TargetCandidate(0, "Basch", TargetGroup.RESERVE, party_member_id=4),
        )
        self.assertEqual(
            shell.area_secondary_target_indices(candidates, 1, 10.0, {}),
            (0, 2),
        )

    def test_only_a_materialized_party_action_resolves_a_hud_row(self):
        runtime = shell.CoopMenuRuntime.__new__(shell.CoopMenuRuntime)
        runtime.active_party = [
            (5, "Penelo", 0x505, 0x5000),
            (2, "Fran", 0x202, 0x2000),
            (1, "Ashe", 0x101, 0x1000),
        ]
        runtime.session_snapshot = SimpleNamespace(
            current_layout=(5, 2, 1, hud.EMPTY_PARTY_MEMBER_ID)
        )
        model = P2MenuModel(0x101, make_demo_catalog())
        model.mode = MenuMode.TARGET
        model.target_group = TargetGroup.PARTY
        model.pending_action = ActionDefinition(
            0x31,
            "Potion",
            "Restore HP.",
            (TargetGroup.PARTY,),
            TargetGroup.PARTY,
            "Items",
        )
        model.candidates[TargetGroup.PARTY] = (
            TargetCandidate(0x505, "Penelo", TargetGroup.PARTY),
            TargetCandidate(0x101, "Ashe", TargetGroup.PARTY),
        )

        model.target_index = 0
        self.assertEqual(runtime.selected_party_hud_row(model), 3)
        model.target_index = 1
        self.assertEqual(runtime.selected_party_hud_row(model), 2)

        model.pending_action = ActionDefinition(
            shell.P2_CHANGE_CHARACTER_ACTION_ID,
            "Change",
            "Change character.",
            (TargetGroup.PARTY,),
            TargetGroup.PARTY,
            "Party",
        )
        self.assertIsNone(runtime.selected_party_hud_row(model))

    def test_party_navigation_and_circle_selection_follow_hud_top_to_bottom(self):
        active = [
            (5, "Penelo", 0x505, 0x5000),
            (1, "Ashe", 0x101, 0x1000),
            (3, "Balthier", 0x303, 0x3000),
        ]
        targets = [
            ("Penelo", 0x505, 0x5000),
            ("Ashe", 0x101, 0x1000),
            ("Balthier", 0x303, 0x3000),
        ]
        layout = (5, 1, 3, hud.EMPTY_PARTY_MEMBER_ID)
        ordered = shell.party_targets_in_hud_order(targets, active, layout)
        self.assertEqual(tuple(entry[1] for entry in ordered), (0x303, 0x101, 0x505))

        model = P2MenuModel(0x101, make_demo_catalog())
        model.mode = MenuMode.TARGET
        model.target_group = TargetGroup.PARTY
        model.pending_action = ActionDefinition(
            0x31,
            "Potion",
            "Restore HP.",
            (TargetGroup.PARTY,),
            TargetGroup.PARTY,
            "Items",
        )
        model.preserve_candidate_on_refresh = False
        model.set_candidates(
            TargetCandidate(handle, name, TargetGroup.PARTY)
            for name, handle, _actor in ordered
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, 0x101)
        model.navigate(1)
        # This handle drives both the HUD hand and sync_world_target's native
        # circle, so Down cannot leave the two presentations out of sync.
        self.assertEqual(model.valid_candidates()[model.target_index].handle, 0x505)


if __name__ == "__main__":
    unittest.main()
