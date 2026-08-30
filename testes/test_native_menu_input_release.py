"""The command mask must never trap the player in a native menu.

Reproduces the live failure: the leader-select screen stayed open while the
close guard was latched, so Confirm and Cancel were withheld from both the
keyboard and the pad and the screen could not be left.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import headless_menu_shell as live


class NativeMenuInputReleaseTests(unittest.TestCase):
    @staticmethod
    def session_snapshot():
        return SimpleNamespace(
            loading=False,
            cutscene=False,
            user_control_enabled=True,
            native_menu_open=False,
            native_modal_active=False,
            p1_gameplay_ready=True,
            gameplay_ready=True,
            identity=SimpleNamespace(p3_party_member_id=2),
        )

    def test_settled_native_menu_takes_its_buttons_back(self):
        # The exact trap: guard latched, a real full-screen menu open.
        self.assertFalse(
            live.p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=False,
                close_guard_active=True,
                native_menu_settled=True,
            )
        )

    def test_guard_still_covers_a_transient_reading(self):
        # What the guard was added for: a snapshot that flickers open for a
        # frame must not release the mask and let a held Confirm through.
        self.assertTrue(
            live.p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=False,
                close_guard_active=True,
                native_menu_settled=False,
            )
        )

    def test_native_command_tree_stays_masked_however_long_it_is_open(self):
        # An accidental command tree is the one native menu that must stay
        # masked, so it can be cancelled deterministically.
        self.assertTrue(
            live.p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=True,
                close_guard_active=False,
                native_menu_settled=True,
            )
        )

    def test_settled_menu_without_the_guard_was_already_released(self):
        self.assertFalse(
            live.p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=False,
                close_guard_active=False,
                native_menu_settled=True,
            )
        )

    def test_mask_holds_while_no_native_menu_is_open(self):
        self.assertTrue(
            live.p1_command_mask_enabled(
                True,
                native_menu_open=False,
                native_command_open=False,
                close_guard_active=False,
                native_menu_settled=False,
            )
        )

    def test_f6_off_never_masks(self):
        self.assertFalse(
            live.p1_command_mask_enabled(
                False,
                native_menu_open=False,
                native_command_open=False,
                close_guard_active=True,
                native_menu_settled=False,
            )
        )

    def test_settle_window_is_short_enough_to_feel_immediate(self):
        self.assertLessEqual(live.NATIVE_MENU_SETTLE_SECONDS, 0.5)
        self.assertGreater(live.NATIVE_MENU_SETTLE_SECONDS, 0.0)

    def test_single_native_menu_sample_does_not_release_custom_input(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_frontend_custom_suspended = False
        shell.native_frontend_open_candidate_since = None
        shell.native_frontend_observed_open = False
        shell.native_frontend_close_candidate_since = 0.0
        shell.native_party_menu_request_pending = False
        shell.suspend_custom_menus_for_native_frontend = mock.Mock()
        shell.p1_native_lifecycle = mock.Mock(return_value=None)
        snapshot = SimpleNamespace(
            loading=False,
            cutscene=False,
            native_menu_open=True,
            native_modal_active=False,
            user_control_enabled=True,
        )

        shell.maintain_native_frontend_suspension(snapshot, now=10.0)
        shell.maintain_native_frontend_suspension(snapshot, now=10.02)

        shell.suspend_custom_menus_for_native_frontend.assert_not_called()
        self.assertEqual(shell.native_frontend_open_candidate_since, 10.0)

    def test_continuous_native_menu_sample_is_accepted_quickly(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_frontend_custom_suspended = False
        shell.native_frontend_open_candidate_since = None
        shell.native_frontend_observed_open = False
        shell.native_frontend_close_candidate_since = 0.0
        shell.native_party_menu_request_pending = False
        shell.suspend_custom_menus_for_native_frontend = mock.Mock()
        shell.p1_native_lifecycle = mock.Mock(return_value=None)
        snapshot = SimpleNamespace(
            loading=False,
            cutscene=False,
            native_menu_open=True,
            native_modal_active=False,
            user_control_enabled=True,
        )

        shell.maintain_native_frontend_suspension(snapshot, now=10.0)
        shell.maintain_native_frontend_suspension(snapshot, now=10.05)

        shell.suspend_custom_menus_for_native_frontend.assert_called_once_with(
            "external", now=10.05
        )
        self.assertTrue(shell.native_frontend_observed_open)

    def test_followers_share_the_leaders_native_menu_when_coop_is_off(self):
        snapshot = self.session_snapshot()
        for player in ("p2", "p3"):
            with self.subTest(player=player):
                self.assertTrue(
                    live.native_party_menu_source_allowed(
                        snapshot,
                        player,
                        1,
                        coop_active=False,
                        p2_manual_control=False,
                        p3_manual_control=False,
                    )
                )

    def test_disabled_follower_cannot_open_its_native_menu_during_coop(self):
        self.assertFalse(
            live.native_party_menu_source_allowed(
                self.session_snapshot(),
                "p2",
                3,
                coop_active=True,
                p2_manual_control=False,
                p3_manual_control=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
