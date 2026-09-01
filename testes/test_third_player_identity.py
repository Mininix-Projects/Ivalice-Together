"""Contract for the optional third co-op role.

Two players must remain the supported default: most story sections have fewer
than three live members, so a third role can never be required for a session
to be valid.  These tests pin that, and pin how P3 is resolved when it is.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import controller_bindings
import session_state
from session_state import SessionIdentity, available_identity


EMPTY = session_state.EMPTY_PARTY_ID


def layout(*members):
    values = list(members) + [EMPTY] * (9 - len(members))
    return tuple(values[:9])


class IdentityDefaultsTests(unittest.TestCase):
    def test_two_players_remain_the_default(self):
        identity = SessionIdentity()
        self.assertIsNone(identity.p3_party_member_id)
        self.assertEqual(identity.player_count, 2)

    def test_a_config_without_p3_stays_two_player(self):
        config = session_state.normalize_config(
            {
                "version": 1,
                "players": {
                    "p1": {"party_member_id": 0},
                    "p2": {"party_member_id": 5},
                },
            }
        )
        identity = SessionIdentity.from_config(config)
        self.assertIsNone(identity.p3_party_member_id)

    def test_a_config_with_p3_is_accepted(self):
        config = session_state.normalize_config(
            {
                "version": 1,
                "players": {
                    "p1": {"party_member_id": 0},
                    "p2": {"party_member_id": 5},
                    "p3": {"party_member_id": 1},
                },
            }
        )
        identity = SessionIdentity.from_config(config)
        self.assertEqual(identity.p3_party_member_id, 1)
        self.assertEqual(identity.player_count, 3)
        self.assertEqual(identity.name("p3"), session_state.PARTY_NAMES[1])

    def test_an_unknown_member_still_gets_a_readable_name(self):
        identity = SessionIdentity(0, 5, 4242)
        self.assertEqual(identity.name("p3"), "Party 4242")

    def test_story_variants_and_guests_have_real_display_names(self):
        self.assertEqual(SessionIdentity(0, 5, 9).name("p3"), "Basch")
        self.assertEqual(SessionIdentity(0, 5, 11).name("p3"), "Vossler")
        self.assertEqual(SessionIdentity(0, 5, 13).name("p3"), "Larsa")

    def test_a_missing_third_role_names_itself_none(self):
        self.assertEqual(SessionIdentity(0, 5).name("p3"), "None")

    def test_member_id_lookup_covers_every_role(self):
        identity = SessionIdentity(0, 5, 1)
        self.assertEqual(identity.member_id("p1"), 0)
        self.assertEqual(identity.member_id("p2"), 5)
        self.assertEqual(identity.member_id("p3"), 1)
        self.assertIsNone(identity.member_id("p4"))


class LiveResolutionTests(unittest.TestCase):
    def test_p3_takes_the_remaining_live_member(self):
        configured = SessionIdentity(0, 5, 1)
        resolved = available_identity(configured, layout(0, 5, 1), layout(0, 5, 1), 0)
        self.assertEqual(
            (
                resolved.p1_party_member_id,
                resolved.p2_party_member_id,
                resolved.p3_party_member_id,
            ),
            (0, 5, 1),
        )

    def test_p3_disappears_when_the_party_shrinks(self):
        configured = SessionIdentity(0, 5, 1)
        resolved = available_identity(configured, layout(0, 5), layout(0, 5), 0)
        self.assertIsNone(resolved.p3_party_member_id)
        self.assertEqual(resolved.p1_party_member_id, 0)
        self.assertEqual(resolved.p2_party_member_id, 5)

    def test_p3_never_aliases_another_role(self):
        configured = SessionIdentity(0, 5, 5)
        resolved = available_identity(configured, layout(0, 5, 2), layout(0, 5, 2), 0)
        self.assertNotIn(
            resolved.p3_party_member_id,
            (resolved.p1_party_member_id, resolved.p2_party_member_id),
        )

    def test_p3_follows_a_leader_change_without_collision(self):
        configured = SessionIdentity(0, 5, 1)
        resolved = available_identity(configured, layout(0, 5, 1), layout(0, 5, 1), 1)
        roles = (
            resolved.p1_party_member_id,
            resolved.p2_party_member_id,
            resolved.p3_party_member_id,
        )
        self.assertEqual(resolved.p1_party_member_id, 1)
        self.assertEqual(len(set(roles)), 3)

    def test_a_two_player_config_is_unaffected_by_a_third_member(self):
        configured = SessionIdentity(0, 5)
        resolved = available_identity(configured, layout(0, 5, 1), layout(0, 5, 1), 0)
        self.assertIsNone(resolved.p3_party_member_id)

    def test_guest_never_becomes_a_follower_role_during_reconcile(self):
        configured = SessionIdentity(0, 5, 1)
        resolved = available_identity(configured, layout(0, 5, 11), layout(0, 5, 11), 0)
        self.assertEqual(resolved, SessionIdentity(0, 5, None, 3))

    def test_party_shrink_releases_the_persisted_third_role(self):
        snapshot = SimpleNamespace(
            identity=SessionIdentity(0, 5, 1),
            battle_info=1,
            user_control_enabled=True,
            native_menu_open=False,
            native_modal_active=False,
            current_location=7,
            ui_location=7,
            current_layout=layout(0, 5),
            ui_layout=layout(0, 5),
            leader_party_member_id=5,
        )
        replacement = session_state.replacement_identity_for_live_party(
            snapshot,
        )
        self.assertEqual(replacement, SessionIdentity(5, 0, None, 3))


class PersistenceTests(unittest.TestCase):
    def test_p3_round_trips_through_the_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_config.json"
            session_state.save_identity(SessionIdentity(0, 5, 1), path)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["players"]["p3"]["party_member_id"], 1)
            reloaded = SessionIdentity.from_config(
                session_state.normalize_config(written)
            )
            self.assertEqual(reloaded.p3_party_member_id, 1)

    def test_empty_three_player_seats_round_trip_without_placeholder_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_config.json"
            expected = SessionIdentity(0, None, None, 3)
            session_state.save_identity(expected, path)
            written = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(written["player_count"], 3)
            self.assertIsNone(written["players"]["p2"]["party_member_id"])
            self.assertIsNone(written["players"]["p3"]["party_member_id"])
            self.assertEqual(
                SessionIdentity.from_config(session_state.normalize_config(written)),
                expected,
            )

    def test_a_two_player_identity_writes_no_p3_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_config.json"
            session_state.save_identity(SessionIdentity(0, 5), path)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("p3", written["players"])


class BindingTests(unittest.TestCase):
    def test_p3_uses_the_shared_wasd_desktop_profile(self):
        config = controller_bindings.default_config()
        p3 = config["players"]["p3"]

        def keys(action):
            return {
                entry["vk"] for entry in p3[action] if entry.get("device") == "keyboard"
            }

        self.assertEqual(keys("move_up"), {ord("W")})
        self.assertEqual(keys("move_left"), {ord("A")})
        self.assertEqual(keys("move_down"), {ord("S")})
        self.assertEqual(keys("move_right"), {ord("D")})

    def test_shared_movement_profile_keeps_one_exclusive_desktop_owner(self):
        config = controller_bindings.default_config()
        players = config["players"]

        def movement_keys(player):
            found = set()
            for action in ("move_up", "move_down", "move_left", "move_right"):
                for entry in players[player].get(action, ()):
                    if entry.get("device") == "keyboard":
                        found.add(entry["vk"])
            return found

        self.assertEqual(movement_keys("p2"), movement_keys("p3"))
        self.assertEqual(controller_bindings.desktop_owner(config), "p2")

        displaced = controller_bindings.set_desktop_owner(config, "p3")

        self.assertEqual(displaced, "p2")
        self.assertEqual(controller_bindings.desktop_owner(config), "p3")
        self.assertFalse(controller_bindings.player_owns_desktop(config, "p2"))

    def test_p3_has_its_own_assignable_pad_slot(self):
        config = controller_bindings.default_config()
        self.assertEqual(set(config["devices"]), {"p1", "p2", "p3"})
        self.assertIsNone(config["devices"]["p3"])
        config["devices"]["p3"] = 2
        self.assertEqual(config["devices"]["p3"], 2)

    def test_p3_exposes_the_same_actions_as_p2(self):
        actions = controller_bindings.PLAYER_ACTIONS
        self.assertEqual(
            [name for name, _label in actions["p3"]],
            [name for name, _label in actions["p2"]],
        )


if __name__ == "__main__":
    unittest.main()
