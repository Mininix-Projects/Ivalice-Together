"""The authored co-op roster and the story insertion that consumes it."""

import inspect
import json
import tempfile
import unittest
from pathlib import Path

import controller_config_gui
import coop_roster
import penelo_story_guard
import story_party_patch
from story_party_patch import EMPTY, normalize_story_party


BALTHIER, FRAN, BASCH, ASHE, PENELO = 3, 2, 4, 1, 5


class RosterSelectionTests(unittest.TestCase):
    def test_first_run_default_matches_the_release_configuration(self):
        roster = coop_roster.default_roster()
        self.assertEqual(roster.role_members, (BASCH, BALTHIER))
        self.assertTrue(roster.start_with_all)
        self.assertEqual(roster.members, (BALTHIER, FRAN, BASCH, ASHE, PENELO))

    def test_explicit_roster_still_defaults_to_story_safe_mode(self):
        roster = coop_roster.CoopRoster()
        self.assertEqual(roster.role_members, (BASCH, BALTHIER))
        self.assertFalse(roster.start_with_all)
        self.assertEqual(roster.members, ())

    def test_start_all_unlocks_every_companion_in_canonical_order(self):
        roster = coop_roster.CoopRoster(PENELO, BALTHIER).with_start_all()
        self.assertEqual(roster.role_members, (PENELO, BALTHIER))
        self.assertEqual(
            roster.members,
            (BALTHIER, FRAN, BASCH, ASHE, PENELO),
        )
        self.assertEqual(set(roster.members), set(range(1, 6)))

    def test_start_all_members_remain_unique_for_a_malformed_direct_pair(self):
        roster = coop_roster.CoopRoster(ASHE, ASHE).with_start_all()
        self.assertEqual(roster.role_members, (ASHE,))
        self.assertEqual(len(roster.members), 5)
        self.assertEqual(set(roster.members), set(range(1, 6)))

    def test_toggling_start_all_preserves_both_roles(self):
        original = coop_roster.CoopRoster(ASHE, FRAN)
        enabled = original.with_start_all()
        disabled = enabled.with_start_all(False)
        self.assertEqual((enabled.p2, enabled.p3), (ASHE, FRAN))
        self.assertTrue(enabled.start_with_all)
        self.assertEqual(disabled, original)

    def test_assigning_a_role_preserves_start_all(self):
        roster = coop_roster.CoopRoster(PENELO, BALTHIER).with_start_all()
        changed = roster.assign("p2", ASHE).assign("p3", None)
        self.assertTrue(changed.start_with_all)
        self.assertEqual(changed.role_members, (ASHE,))
        self.assertEqual(
            changed.members,
            (BALTHIER, FRAN, BASCH, ASHE, PENELO),
        )
        self.assertEqual(set(changed.members), set(range(1, 6)))

    def test_vaan_is_not_selectable(self):
        self.assertNotIn(0, coop_roster.SELECTABLE_MEMBER_IDS)
        with self.assertRaises(ValueError):
            coop_roster.CoopRoster().assign("p2", 0)

    def test_guests_are_not_selectable(self):
        for guest in (6, 12, 13, 14):
            self.assertNotIn(guest, coop_roster.SELECTABLE_MEMBER_IDS)

    def test_taking_a_seat_frees_it_from_the_previous_holder(self):
        roster = coop_roster.CoopRoster(PENELO, BALTHIER)
        roster = roster.assign("p2", FRAN)
        self.assertEqual(roster.p2, FRAN)
        self.assertIsNone(roster.role_of(PENELO))
        self.assertEqual(roster.p3, BALTHIER)

    def test_taking_the_other_seat_of_the_same_character_moves_it(self):
        # Checking P3 on whoever already holds P2 must not seat them twice.
        roster = coop_roster.CoopRoster(PENELO, BALTHIER)
        roster = roster.assign("p3", PENELO)
        self.assertEqual(roster.p3, PENELO)
        self.assertIsNone(roster.p2)
        self.assertEqual(roster.members, ())

    def test_a_seat_can_be_released_for_two_player_co_op(self):
        roster = coop_roster.CoopRoster(PENELO, BALTHIER).assign("p3", None)
        self.assertIsNone(roster.p3)
        self.assertEqual(roster.members, ())

    def test_the_two_seats_can_never_hold_the_same_member(self):
        roster = coop_roster.CoopRoster(PENELO, BALTHIER)
        for role, member in (("p2", BASCH), ("p3", BASCH), ("p2", ASHE)):
            roster = roster.assign(role, member)
            self.assertNotEqual(roster.p2, roster.p3, f"{role}->{member} seated twice")

    def test_a_corrupt_file_falls_back_instead_of_seating_a_duplicate(self):
        self.assertEqual(
            coop_roster.normalize({"players": {"p2": ASHE, "p3": ASHE}}),
            coop_roster.CoopRoster(ASHE, None),
        )
        self.assertEqual(
            coop_roster.normalize({"players": {"p2": 99, "p3": 0}}),
            coop_roster.CoopRoster(None, None),
        )
        self.assertEqual(
            coop_roster.normalize("nonsense"), coop_roster.default_roster()
        )

    def test_a_missing_file_uses_the_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.json"
            self.assertEqual(coop_roster.load(path), coop_roster.default_roster())

    def test_missing_player_keys_keep_first_run_defaults(self):
        self.assertEqual(
            coop_roster.normalize({"players": {}}),
            coop_roster.CoopRoster(BASCH, BALTHIER),
        )
        self.assertEqual(
            coop_roster.normalize({"players": {"p2": ASHE}}),
            coop_roster.CoopRoster(ASHE, BALTHIER),
        )

    def test_schema_v1_migrates_without_enabling_start_all(self):
        old_payload = {
            "version": 1,
            "players": {"p2": ASHE, "p3": FRAN},
        }
        roster = coop_roster.normalize(old_payload)
        self.assertEqual(roster.role_members, (ASHE, FRAN))
        self.assertEqual(roster.members, ())
        self.assertFalse(roster.start_with_all)

    def test_start_all_requires_a_json_boolean_true(self):
        base = {"version": 2, "players": {"p2": ASHE, "p3": FRAN}}
        for invalid in (1, "true", [True], {"enabled": True}):
            with self.subTest(invalid=invalid):
                roster = coop_roster.normalize({**base, "start_with_all": invalid})
                self.assertFalse(roster.start_with_all)
        self.assertTrue(
            coop_roster.normalize({**base, "start_with_all": True}).start_with_all
        )

    def test_save_round_trips_and_records_readable_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roster.json"
            roster = coop_roster.CoopRoster(ASHE, FRAN).with_start_all()
            saved = coop_roster.save(roster, path)
            self.assertEqual(saved, roster)
            self.assertEqual(coop_roster.load(path), saved)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertIs(payload["start_with_all"], True)
            self.assertEqual(payload["names"], {"p2": "Ashe", "p3": "Fran"})

    def test_initial_members_reads_all_five_from_a_v2_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roster.json"
            coop_roster.save(
                coop_roster.CoopRoster(PENELO, BALTHIER).with_start_all(),
                path,
            )
            self.assertEqual(
                coop_roster.initial_members(path),
                (BALTHIER, FRAN, BASCH, ASHE, PENELO),
            )


class StoryInsertionTests(unittest.TestCase):
    def first_record(self, members):
        return normalize_story_party(bytes((0,) + (EMPTY,) * 8), members)

    def test_an_explicit_pair_is_unlocked_in_reserve(self):
        self.assertEqual(
            self.first_record((ASHE, FRAN)),
            bytes((0, EMPTY, EMPTY, EMPTY, ASHE, FRAN, EMPTY, EMPTY, EMPTY)),
        )

    def test_a_legacy_default_pair_also_stays_in_reserve(self):
        self.assertEqual(
            self.first_record((PENELO, BALTHIER)),
            bytes((0, EMPTY, EMPTY, EMPTY, PENELO, BALTHIER, EMPTY, EMPTY, EMPTY)),
        )

    def test_start_all_leaves_vaan_solo_and_fills_only_reserve(self):
        members = coop_roster.CoopRoster().with_start_all().members
        expected = bytes(
            (
                0,
                EMPTY,
                EMPTY,
                EMPTY,
                BALTHIER,
                FRAN,
                BASCH,
                ASHE,
                PENELO,
            )
        )
        once = self.first_record(members)
        self.assertEqual(once, expected)
        self.assertEqual(normalize_story_party(once, members), expected)

    def test_a_single_unlocked_companion_goes_to_reserve(self):
        self.assertEqual(
            self.first_record((BASCH,)),
            bytes((0, EMPTY, EMPTY, EMPTY, BASCH, EMPTY, EMPTY, EMPTY, EMPTY)),
        )

    def test_an_empty_roster_never_rewrites_a_story_record(self):
        original = bytes((0, 1, 2, EMPTY, 3, 4, 5, EMPTY, EMPTY))
        self.assertEqual(normalize_story_party(original, ()), original)

    def test_insertion_is_idempotent_after_the_first_pass(self):
        # Records are rescanned several times a second. The initial write must
        # already be the fixed point so timing cannot expose two party shapes.
        pair = (BASCH, FRAN)
        once = self.first_record(pair)
        twice = normalize_story_party(once, pair)
        self.assertEqual(twice, once)
        self.assertEqual(normalize_story_party(twice, pair), twice)
        self.assertEqual(
            sorted(value for value in twice if value != EMPTY), sorted((0, BASCH, FRAN))
        )

    def test_the_default_pair_is_also_a_first_pass_fixed_point(self):
        pair = (PENELO, BALTHIER)
        once = self.first_record(pair)
        self.assertEqual(normalize_story_party(once, pair), once)

    def test_guest_slot_and_existing_main_party_survive_story_restoration(self):
        original = (0, ASHE, FRAN, 11, BASCH, EMPTY, EMPTY, EMPTY, EMPTY)
        result = coop_roster.normalize_party_layout(
            original,
            (PENELO, BALTHIER),
            empty=EMPTY,
            prefer_active=False,
        )
        self.assertEqual(result[:4], original[:4])
        self.assertEqual(result[3], 11)
        self.assertEqual(result.count(ASHE), 1)
        self.assertEqual(result.count(PENELO), 1)
        self.assertEqual(result.count(BALTHIER), 1)

    def test_full_custom_roster_fails_closed_without_evicting_a_member(self):
        original = tuple(range(9))
        result = coop_roster.normalize_party_layout(
            original,
            (99,),
            empty=EMPTY,
            prefer_active=False,
        )
        self.assertEqual(result, original)

    def test_live_guest_is_moved_out_of_the_selectable_trio(self):
        original = (0, 11, ASHE, EMPTY, PENELO, FRAN, EMPTY, EMPTY, EMPTY)
        result = coop_roster.normalize_live_party_layout(
            original,
            (PENELO, ASHE),
            empty=EMPTY,
            prefer_active=False,
        )
        self.assertEqual(result[:4], (0, EMPTY, ASHE, 11))
        self.assertEqual(result.count(11), 1)
        self.assertEqual(result.count(PENELO), 1)

    def test_second_live_guest_is_preserved_in_reserve(self):
        original = (0, 12, ASHE, 11, PENELO, FRAN, EMPTY, EMPTY, EMPTY)
        result = coop_roster.normalize_live_party_layout(
            original,
            (PENELO, ASHE),
            empty=EMPTY,
            prefer_active=False,
        )
        self.assertEqual(result[:4], (0, EMPTY, ASHE, 11))
        self.assertEqual(result[6], 12)
        self.assertEqual(result.count(11), 1)
        self.assertEqual(result.count(12), 1)

    def test_live_guest_move_fails_closed_when_no_destination_exists(self):
        original = (0, 12, ASHE, 11, PENELO, FRAN, 3, 4, 13)
        result = coop_roster.normalize_live_party_layout(
            original,
            (PENELO, ASHE),
            empty=EMPTY,
            prefer_active=False,
        )
        self.assertEqual(result, original)

    def test_live_guest_repair_is_a_single_pass_fixed_point(self):
        original = (ASHE, FRAN, 7, 12, BASCH, 0, 3, 12, BASCH)
        once = coop_roster.normalize_live_party_layout(
            original,
            (PENELO, ASHE),
            empty=EMPTY,
            prefer_active=True,
        )
        twice = coop_roster.normalize_live_party_layout(
            once,
            (PENELO, ASHE),
            empty=EMPTY,
            prefer_active=True,
        )
        self.assertEqual(twice, once)
        self.assertEqual(
            len([value for value in once if value != EMPTY]),
            len(set(value for value in once if value != EMPTY)),
        )
        self.assertTrue(all(value == EMPTY or value < 6 for value in once[:3]))

    def test_the_module_default_comes_from_the_authored_roster(self):
        self.assertEqual(
            story_party_patch.INITIAL_COOP_MEMBERS,
            coop_roster.initial_members(),
        )


class GuardWiringTests(unittest.TestCase):
    def test_the_guard_protects_the_authored_roster(self):
        self.assertEqual(
            penelo_story_guard._protected_member_ids(),
            coop_roster.initial_members(),
        )

    def test_the_guard_never_rewrites_player_gambits(self):
        source = inspect.getsource(penelo_story_guard)
        self.assertNotIn("configure_coop_default_gambits", source)
        self.assertNotIn("FOE_LOWEST_HP", source)


class PickerUiTests(unittest.TestCase):
    def test_seat_boxes_are_built_only_for_selectable_characters(self):
        source = inspect.getsource(controller_config_gui.Configurator._make_theme_card)
        self.assertIn(
            "if profile.member_id in coop_roster.SELECTABLE_MEMBER_IDS:",
            source,
        )

    def test_toggling_a_seat_rewrites_every_box_from_the_roster(self):
        # Both boxes of every card are recomputed, which is what makes the
        # previous holder visibly clear instead of leaving two ticks on screen.
        toggled = inspect.getsource(controller_config_gui.Configurator._roster_toggled)
        self.assertIn("self._refresh_roster_boxes()", toggled)
        refresh = inspect.getsource(
            controller_config_gui.Configurator._refresh_roster_boxes
        )
        self.assertIn("for (member_id, role), variable in", refresh)

    def test_saving_themes_also_persists_the_roster(self):
        source = inspect.getsource(controller_config_gui.Configurator.save_themes)
        self.assertIn("coop_roster.save(self.coop_roster)", source)


if __name__ == "__main__":
    unittest.main()
