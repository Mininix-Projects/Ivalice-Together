from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ferramentas"))
from unittest import mock

import headless_menu_renderer as renderer
from headless_menu_model import (
    ActionDefinition,
    EntryKind,
    MenuEntry,
    MenuMode,
    P2MenuModel,
    PageDefinition,
    TargetCandidate,
    TargetGroup,
    make_demo_catalog,
)
from headless_menu_presenter import CursorTween, HeadlessMenuPresenter
from native_queue_analysis import NativeQueueSnapshot, QueuePhase, QueuePresentation


ISSUER = 0x100008


class PresenterTests(unittest.TestCase):
    def test_cursor_tween_is_independent_of_frame_partitioning(self):
        one_tick = CursorTween()
        four_ticks = CursorTween()
        one_tick.snap(0.0)
        four_ticks.snap(0.0)
        one_tick.retarget(1.0)
        four_ticks.retarget(1.0)
        one_tick.advance(0.08)
        for _ in range(4):
            four_ticks.advance(0.02)
        self.assertAlmostEqual(one_tick.position, four_ticks.position, places=7)

    def test_cursor_travels_with_temporal_afterimages(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.open()
        presenter = HeadlessMenuPresenter(model)
        presenter.list_view(delta_seconds=1 / 120)
        model.navigate(1)
        moving = presenter.list_view(delta_seconds=1 / 120)
        self.assertGreater(moving.cursor_position, 0.0)
        self.assertLess(moving.cursor_position, 1.0)
        self.assertTrue(moving.cursor_trail)
        for _ in range(30):
            settled = presenter.list_view(delta_seconds=1 / 120)
        self.assertAlmostEqual(settled.cursor_position, 1.0, places=3)

    def test_static_menu_reuses_the_exact_raster_between_animation_ticks(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.open()
        presenter = HeadlessMenuPresenter(model)
        # Both samples are outside the short idle-hand jab. No menu, cursor or
        # text pixel changes, so a 120-Hz shell must reuse one RGBA generation.
        first = presenter.render(now=0.40, delta_seconds=1 / 120)
        second = presenter.render(now=0.50, delta_seconds=1 / 120)
        self.assertIs(first, second)

    def test_cursor_wrap_travels_across_root_instead_of_teleporting(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.open()
        presenter = HeadlessMenuPresenter(model)
        presenter.list_view(delta_seconds=1 / 120)
        last_index = len(model.page.entries) - 1
        model.navigate(-1)
        moving = presenter.list_view(delta_seconds=1 / 120)
        self.assertGreater(moving.cursor_position, 0.0)
        self.assertLess(moving.cursor_position, float(last_index))
        self.assertTrue(moving.cursor_trail)
        for _ in range(40):
            settled = presenter.list_view(delta_seconds=1 / 120)
        self.assertAlmostEqual(settled.cursor_position, float(last_index), places=3)

    def test_long_action_page_scrolls_in_compact_five_row_viewport(self):
        actions = tuple(
            MenuEntry(
                f"Action {index}",
                f"Description {index}",
                EntryKind.ACTION,
                action=ActionDefinition(
                    index,
                    f"Action {index}",
                    f"Description {index}",
                    (TargetGroup.FOES,),
                    TargetGroup.FOES,
                    "Test",
                ),
            )
            for index in range(12)
        )
        pages = {
            "root": PageDefinition(
                "root", "", (MenuEntry("Long", "", EntryKind.PAGE, page_key="long"),)
            ),
            "long": PageDefinition("long", "Long", actions),
        }
        model = P2MenuModel(ISSUER, pages)
        model.open()
        model.confirm()
        for _ in range(8):
            model.navigate(1)
        presenter = HeadlessMenuPresenter(model)
        view = presenter.list_view()
        self.assertEqual(view.selected_index, 8)
        self.assertEqual(view.first_visible, 4)
        self.assertEqual(view.max_visible_rows, 5)
        self.assertIsNotNone(presenter.render(now=1.0).getbbox())

    def test_long_list_viewport_glides_instead_of_teleporting(self):
        actions = tuple(
            MenuEntry(
                f"Action {index}",
                "",
                EntryKind.ACTION,
                action=ActionDefinition(
                    index,
                    f"Action {index}",
                    "",
                    (TargetGroup.FOES,),
                    TargetGroup.FOES,
                    "Test",
                ),
            )
            for index in range(12)
        )
        pages = {
            "root": PageDefinition(
                "root",
                "",
                (MenuEntry("Long", "", EntryKind.PAGE, page_key="long"),),
            ),
            "long": PageDefinition("long", "Long", actions),
        }
        model = P2MenuModel(ISSUER, pages)
        model.open()
        model.confirm()
        presenter = HeadlessMenuPresenter(model)
        presenter.list_view(delta_seconds=1 / 120)
        for _ in range(5):
            model.navigate(1)
        moving = presenter.list_view(delta_seconds=1 / 120)
        self.assertEqual(moving.first_visible, 1)
        self.assertGreater(moving.scroll_position, 0.0)
        self.assertLess(moving.scroll_position, 1.0)
        for _ in range(45):
            settled = presenter.list_view(delta_seconds=1 / 120)
        self.assertAlmostEqual(settled.scroll_position, 1.0, places=3)
        self.assertAlmostEqual(
            settled.cursor_position - settled.scroll_position,
            4.0,
            places=3,
        )

    def test_items_use_native_eight_row_viewport(self):
        actions = tuple(
            MenuEntry(
                f"Item {index}",
                f"Description {index}",
                EntryKind.ACTION,
                action=ActionDefinition(
                    0x52 + index,
                    f"Item {index}",
                    f"Description {index}",
                    (TargetGroup.PARTY,),
                    TargetGroup.PARTY,
                    "Items",
                    quantity=10,
                ),
            )
            for index in range(12)
        )
        pages = {
            "root": PageDefinition(
                "root",
                "",
                (MenuEntry("Items", "", EntryKind.PAGE, page_key="items"),),
            ),
            "items": PageDefinition("items", "Items", actions),
        }
        model = P2MenuModel(ISSUER, pages)
        model.open()
        model.confirm()
        for _ in range(8):
            model.navigate(1)
        view = HeadlessMenuPresenter(model).list_view()
        self.assertEqual(view.selected_index, 8)
        self.assertEqual(view.first_visible, 1)
        self.assertEqual(view.max_visible_rows, 8)

    def test_item_prewarm_does_not_open_or_move_the_real_menu(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        presenter = HeadlessMenuPresenter(model)
        presenter.set_party_member_id(5)
        presenter.set_theme_name("gold")
        warmed_image = renderer.Image.new("RGBA", renderer.CANVAS_SIZE)

        with (
            mock.patch.object(
                renderer,
                "render_list",
                return_value=warmed_image,
            ) as render_list,
            mock.patch.object(renderer, "scale_three_player_panel") as scale,
        ):
            self.assertTrue(
                presenter.prewarm_page(
                    "items",
                    player=1,
                    compose_layers=False,
                )
            )

        self.assertIs(model.mode, MenuMode.CLOSED)
        self.assertEqual(model.stack, [])
        view = render_list.call_args.args[0]
        self.assertEqual(view.title, "Items")
        self.assertEqual(view.max_visible_rows, 8)
        self.assertEqual(view.selected_index, 0)
        self.assertEqual(render_list.call_args.kwargs["party_member_id"], 5)
        self.assertEqual(render_list.call_args.kwargs["theme_name"], "gold")
        scale.assert_called_once_with(warmed_image)

    def test_action_subpages_use_eight_rows_but_category_page_stays_six(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        presenter = HeadlessMenuPresenter(model)
        self.assertEqual(presenter.visible_row_limit("magicks"), 6)
        self.assertEqual(presenter.visible_row_limit("magick:white_magicks"), 8)
        self.assertEqual(presenter.visible_row_limit("technicks"), 8)
        self.assertEqual(presenter.visible_row_limit("mist"), 5)

    def test_target_projection_uses_dynamic_native_name(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.set_candidates(
            (TargetCandidate(0x200027, "Battery Mimic B", TargetGroup.FOES),)
        )
        model.open()
        model.confirm()
        view = HeadlessMenuPresenter(model).target_view()
        self.assertTrue(view.has_target)
        self.assertEqual(view.target_name, "Battery Mimic B")

    def test_target_projection_has_native_no_target_state(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.open()
        model.confirm()
        view = HeadlessMenuPresenter(model).target_view()
        self.assertFalse(view.has_target)

    def test_target_projection_carries_only_valid_secondary_area_indices(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.set_candidates(
            (
                TargetCandidate(0x200027, "Wolf A", TargetGroup.FOES),
                TargetCandidate(0x200028, "Wolf B", TargetGroup.FOES),
                TargetCandidate(0x200029, "Wolf C", TargetGroup.FOES),
            )
        )
        model.open()
        model.confirm()
        presenter = HeadlessMenuPresenter(model)
        presenter.set_secondary_target_indices((0, 1, 7, -1, 1))
        view = presenter.target_view()
        self.assertEqual(view.secondary_indices, (1,))

    def test_target_projection_advertises_only_legal_live_families(self):
        steal = ActionDefinition(
            0xA9,
            "Steal",
            "Steal from one foe.",
            (TargetGroup.FOES,),
            TargetGroup.FOES,
            "Technicks",
        )
        pages = {
            "root": PageDefinition(
                "root",
                "",
                (MenuEntry("Steal", "", EntryKind.ACTION, action=steal),),
            )
        }
        model = P2MenuModel(ISSUER, pages)
        model.open()
        model.confirm()
        view = HeadlessMenuPresenter(model).target_view()
        self.assertEqual(view.available_groups, ("foes",))

    def test_no_target_cursor_animation_participates_in_raster_cache_key(self):
        view = renderer.TargetView("Warp Mote", has_target=False)
        with mock.patch.object(
            renderer,
            "cursor_jab",
            side_effect=(0.0, 5.0),
        ):
            first = HeadlessMenuPresenter._target_raster_key(
                view,
                now=0.0,
                player=2,
                party_member_id=4,
                theme_name="emerald",
            )
            second = HeadlessMenuPresenter._target_raster_key(
                view,
                now=0.1,
                player=2,
                party_member_id=4,
                theme_name="emerald",
            )
        self.assertNotEqual(first, second)

    def test_party_targets_remain_selectable_but_names_are_world_only(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.set_candidates(
            (
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
                TargetCandidate(0x10000D, "Vaan", TargetGroup.PARTY),
            )
        )
        model.open()
        model.confirm()
        model.target_group = TargetGroup.PARTY
        view = HeadlessMenuPresenter(model).target_view()
        self.assertTrue(view.has_target)
        self.assertEqual(view.target_name, "")
        self.assertEqual(view.target_names, ())
        self.assertEqual(len(model.valid_candidates()), 2)

    def test_foes_party_switch_uses_lightweight_bottom_anchor_transition(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.set_candidates(
            (
                TargetCandidate(0x200027, "Wolf", TargetGroup.FOES),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )
        model.open()
        model.confirm()
        presenter = HeadlessMenuPresenter(model)
        presenter.render(now=1.0)
        model.switch_target_group(TargetGroup.PARTY)
        changing = presenter.render(now=1.01)
        self.assertIn("ff12_local_offset_y", changing.info)
        self.assertIn("ff12_local_opacity", changing.info)
        settled = presenter.render(now=1.011 + renderer.TARGET_GROUP_TRANSITION_SECONDS)
        self.assertNotIn("ff12_local_offset_y", settled.info)

    def test_revival_party_target_shows_dead_actor_name(self):
        revive = ActionDefinition(
            0x58,
            "Phoenix Down",
            "Revive one KO'd ally.",
            (TargetGroup.PARTY,),
            TargetGroup.PARTY,
            "Items",
            revive_only=True,
        )
        pages = {
            "root": PageDefinition(
                "root",
                "",
                (
                    MenuEntry(
                        revive.name,
                        revive.description,
                        EntryKind.ACTION,
                        action=revive,
                    ),
                ),
            ),
        }
        model = P2MenuModel(ISSUER, pages)
        model.set_candidates((TargetCandidate(0x200011, "Penelo", TargetGroup.PARTY),))
        model.open()
        model.confirm()
        view = HeadlessMenuPresenter(model).target_view()
        self.assertEqual(view.target_name, "Penelo")
        self.assertEqual(view.target_names, ("Penelo",))
        self.assertTrue(view.show_party_names)

    def test_help_tracks_p2_selection_only(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.open()
        presenter = HeadlessMenuPresenter(model)
        self.assertEqual(model.help_text, "Attack with weapon in hand.")
        model.navigate(1)
        self.assertEqual(model.help_text, "Use magicks and technicks.")
        self.assertIsNotNone(presenter.render_help().getbbox())

    def test_queue_projection_is_read_only_and_normalized(self):
        snapshot = NativeQueueSnapshot(
            actor_flags_000=(1 << 14) | (1 << 32),
            state_6b4=4,
            descriptor_708=1,
            target_710=2,
            current_action_714=0x96,
            charge_current_768=11200,
            charge_required_76c=17040,
            requested_action_ba0=0x52,
            request_flags_ba4=0x8000,
            requested_target_bb8=3,
        )
        view = QueuePresentation.from_native(snapshot)
        self.assertEqual(view.phase, QueuePhase.CHARGING)
        self.assertAlmostEqual(view.charge_fraction, 11200 / 17040)
        self.assertTrue(view.accepted_this_frame)

    def test_gambit_status_tracks_the_live_model(self):
        model = P2MenuModel(ISSUER, make_demo_catalog(), gambits_enabled=False)
        model.open()
        view = HeadlessMenuPresenter(model).list_view()
        gambits = next(
            index
            for index, entry in enumerate(model.page.entries)
            if entry.name == "Gambits"
        )
        self.assertEqual(view.right_labels[gambits], "OFF")

    def test_item_amount_and_magick_cost_use_native_column_headers(self):
        model = P2MenuModel(ISSUER, make_demo_catalog())
        model.open()
        model.navigate(1)
        model.confirm()
        model.confirm()
        magick_view = HeadlessMenuPresenter(model).list_view()
        self.assertEqual(magick_view.right_header, "MP")
        self.assertEqual(magick_view.right_labels[0], "8")

        model.close()
        model.open()
        while model.selected_entry and model.selected_entry.name != "Items":
            model.navigate(1)
        model.confirm()
        item_view = HeadlessMenuPresenter(model).list_view()
        self.assertEqual(item_view.right_header, "AMT")
        # The AMT column displays only the number, like the native menu. The
        # prefix previously added by the mod does not exist in the game.
        self.assertTrue(
            all(label.isdigit() for label in item_view.right_labels.values())
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
