import inspect
import json
import struct
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import d3d_panel_transport
import headless_menu_shell as live
import session_state


class MemoryProcess:
    def __init__(self):
        self.data = {}

    def put(self, address, payload):
        for offset, value in enumerate(payload):
            self.data[address + offset] = value

    def read(self, address, size):
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class ThirdPlayerMenuTests(unittest.TestCase):
    def test_applied_role_override_updates_hud_identity_before_next_snapshot(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.base = 0x100000
        shell.process = MemoryProcess()
        shell.active_party = [
            (5, "Penelo", 0x100005, 0x70000500),
            (4, "Basch", 0x100004, 0x70000400),
        ]
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(5, 4),
            p1_owner_party_member_id=5,
            current_layout=(5, 4, *([session_state.EMPTY_PARTY_ID] * 7)),
        )
        shell.live_role_identity_override = session_state.SessionIdentity(4, 5)
        shell.process.put(
            shell.base + live.P1_CONTROLLED_ACTOR_OFFSET,
            struct.pack("<Q", 0x70000400),
        )
        shell.process.put(
            shell.base + live.P2_CONTROLLED_ACTOR_OFFSET,
            struct.pack("<Q", 0x70000500),
        )

        self.assertEqual(shell._controlled_member_id(1), 4)
        self.assertEqual(shell._controlled_member_id(2), 5)

    def test_character_selector_keeps_the_players_current_theme_until_commit(self):
        self.assertEqual(
            live.character_selector_theme(
                "pink",
                "bronze",
                selector_active=True,
            ),
            "pink",
        )
        self.assertEqual(
            live.character_selector_theme(
                "pink",
                "bronze",
                selector_active=False,
            ),
            "bronze",
        )

    def test_character_selector_does_not_retain_an_invalid_uninitialized_theme(self):
        self.assertEqual(
            live.character_selector_theme(
                "invalid",
                "bronze",
                selector_active=True,
            ),
            "bronze",
        )

    def test_dismiss_owner_prefers_main_leader_and_falls_back_to_main_member(self):
        self.assertEqual(live.summon_dismiss_owner((5, 33, 0xFFFF), 5), 5)
        self.assertEqual(live.summon_dismiss_owner((5, 33, 0xFFFF), 33), 5)
        self.assertIsNone(live.summon_dismiss_owner((33, 0xFFFF), 33))

    def test_summon_owner_is_the_player_who_submitted_not_the_native_leader(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.active_party = [
            (1, "Balthier", 0x100011, 0x70000000),
            (5, "Penelo", 0x100055, 0x71000000),
            (3, "Basch", 0x100033, 0x72000000),
        ]
        shell.session_snapshot = SimpleNamespace(
            p1_owner_party_member_id=1,
            identity=SimpleNamespace(p2_party_member_id=5, p3_party_member_id=3),
        )
        shell.pending_summon_owner_party_member_id = None
        shell.pending_summon_owner_until = 0.0
        shell.summon_owner_party_member_id = None
        shell.summon_was_active = False

        with mock.patch.object(live.time, "monotonic", return_value=10.0):
            self.assertEqual(shell.remember_summon_owner(0x100055, player=2), 5)
        active, owner = shell.resolve_summon_ownership(
            (1, 5, 3, 33),
            1,
            now=11.0,
        )

        self.assertTrue(active)
        self.assertEqual(owner, 5)

    def test_summon_owner_survives_leader_changes_until_esper_leaves(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.pending_summon_owner_party_member_id = 3
        shell.pending_summon_owner_until = 30.0
        shell.summon_owner_party_member_id = None
        shell.summon_was_active = False

        self.assertEqual(
            shell.resolve_summon_ownership((1, 5, 3, 33), 1, now=11.0),
            (True, 3),
        )
        self.assertEqual(
            shell.resolve_summon_ownership((1, 5, 3, 33), 5, now=12.0),
            (True, 3),
        )
        self.assertEqual(
            shell.resolve_summon_ownership((1, 5, 3, 0xFFFF), 5, now=13.0),
            (False, None),
        )

    def test_dismiss_resolves_live_esper_handle_and_skips_target_indicator(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = mock.Mock()
        shell.refresh_actors = mock.Mock()
        shell.session_snapshot = SimpleNamespace(
            current_layout=(5, 33, 0xFFFF, 0xFFFF),
            leader_party_member_id=5,
        )
        shell.active_party = [
            (5, "Penelo", 0x100077, 0x70000000),
            (33, "Zalera", 0x300088, 0x71000000),
        ]
        shell.party = [("Penelo", 0x100077, 0x70000000)]
        shell.p1_model = SimpleNamespace(issuer=0x100077)
        shell.native_submitter = SimpleNamespace(submit=mock.Mock(return_value=0))
        shell.schedule_native_combat_indicator = mock.Mock()
        request = SimpleNamespace(
            issuer=0x100077,
            target=0x100077,
            action_id=live.DISMISS_ACTION_ID,
            reserve_party_member_id=None,
        )

        with mock.patch.object(
            live, "leader_control_availability", return_value=True
        ):
            result = shell.submit_request(request, player=1)

        self.assertEqual(result, 0)
        shell.refresh_actors.assert_called_once_with(force=True)
        shell.native_submitter.submit.assert_called_once_with(
            0x300088,
            0x300088,
            live.DISMISS_ACTION_ID,
            reserve_party_member_id=None,
        )
        shell.schedule_native_combat_indicator.assert_not_called()

    def test_dismiss_fails_closed_without_a_live_esper(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = mock.Mock()
        shell.refresh_actors = mock.Mock()
        shell.active_party = [(5, "Penelo", 0x100077, 0x70000000)]
        shell.party = [("Penelo", 0x100077, 0x70000000)]
        shell.p1_model = SimpleNamespace(issuer=0x100077)
        shell.native_submitter = SimpleNamespace(submit=mock.Mock(return_value=0))
        shell.schedule_native_combat_indicator = mock.Mock()
        request = SimpleNamespace(
            issuer=0x100077,
            target=0x100077,
            action_id=live.DISMISS_ACTION_ID,
            reserve_party_member_id=None,
        )

        with mock.patch.object(
            live, "leader_control_availability", return_value=True
        ):
            self.assertEqual(shell.submit_request(request, player=1), -1)
        shell.native_submitter.submit.assert_not_called()

    def test_quickening_filter_owns_only_manually_controlled_slots(self) -> None:
        self.assertEqual(
            live.quickening_button_block_mask(
                active=True,
                party_size=3,
                p2_manual_control=True,
                p3_manual_control=True,
            ),
            live.QUICKENING_BLOCK_COMMAND | live.QUICKENING_BLOCK_CONFIRM,
        )
        self.assertEqual(
            live.quickening_button_block_mask(
                active=True,
                party_size=3,
                p2_manual_control=False,
                p3_manual_control=True,
            ),
            live.QUICKENING_BLOCK_CONFIRM,
        )
        self.assertEqual(
            live.quickening_button_block_mask(
                active=False,
                party_size=3,
                p2_manual_control=True,
                p3_manual_control=True,
            ),
            0,
        )

    def test_quickening_routes_p2_x_to_the_middle_native_slot(self) -> None:
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.coop_runtime_active = True
        shell.native_quickening_active = True
        shell.party = [object(), object(), object()]
        shell.native_dialog_bridge = SimpleNamespace(
            set_quickening_block_mask=mock.Mock(),
            pulse=mock.Mock(),
        )
        shell.bindings = SimpleNamespace(
            edge=mock.Mock(
                side_effect=lambda player, action: (player, action)
                == ("p2", "quickening")
            )
        )

        consumed = shell._route_native_quickening_input(
            foreground=True,
            p2_manual_control=True,
            p3_manual_control=True,
        )

        self.assertTrue(consumed)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_COMMAND
        )

    def test_quickening_routes_p3_a_to_the_bottom_native_slot(self) -> None:
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.coop_runtime_active = True
        shell.native_quickening_active = True
        shell.party = [object(), object(), object()]
        shell.native_dialog_bridge = SimpleNamespace(
            set_quickening_block_mask=mock.Mock(),
            pulse=mock.Mock(),
        )
        shell.bindings = SimpleNamespace(
            edge=mock.Mock(
                side_effect=lambda player, action: (player, action)
                == ("p3", "quickening")
            )
        )

        consumed = shell._route_native_quickening_input(
            foreground=True,
            p2_manual_control=True,
            p3_manual_control=True,
        )

        self.assertTrue(consumed)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CONFIRM
        )

    def test_battle_menu_sound_switch_mutes_only_its_player(self) -> None:
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.pending_native_sounds = deque()
        shell.battle_menu_sounds = {"p1": True, "p2": False, "p3": True}

        shell.queue_native_sound(1, player=1)
        shell.queue_native_sound(2, player=2)
        shell.queue_native_sound(3, player=3)
        shell.queue_native_sound(4)

        self.assertEqual(tuple(shell.pending_native_sounds), (1, 3, 4))

    def test_battle_menu_sound_settings_reload_while_runtime_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher_config.json"
            path.write_text(
                json.dumps(
                    {"battle_menu_sounds": {"p1": True, "p2": True, "p3": True}}
                ),
                encoding="utf-8",
            )
            tracker = live.BattleMenuSoundSettingsTracker(path, poll_seconds=0.05)

            path.write_text(
                json.dumps(
                    {"battle_menu_sounds": {"p1": False, "p2": True, "p3": False}}
                ),
                encoding="utf-8",
            )
            settings = tracker.reload(now=time.monotonic() + 1.0)

        self.assertEqual(settings, {"p1": False, "p2": True, "p3": False})

    def test_save_crystal_pause_routes_follower_confirm(self) -> None:
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.bindings = SimpleNamespace(
            repeat=mock.Mock(return_value=False),
            edge=mock.Mock(
                side_effect=lambda player, action: (player, action) == ("p2", "confirm")
            ),
            native_action_edge=mock.Mock(
                side_effect=lambda player, action, _circle: (player, action)
                == ("p2", "confirm")
            ),
        )
        shell.native_dialog_bridge = SimpleNamespace(pulse=mock.Mock())

        shell._route_native_pause_input(
            foreground=True,
            circle_decides=False,
            p2_character_up=False,
            p2_character_down=False,
            p3_character_up=False,
            p3_character_down=False,
            native_party_menu_edges={"p1": False, "p2": False, "p3": False},
        )

        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CONFIRM
        )

    def test_manual_control_state_gates_each_followers_menu(self) -> None:
        source = inspect.getsource(live.CoopMenuRuntime.input)
        self.assertIn("p2_binding_enabled = bool(", source)
        self.assertIn("p2_manual_control_enabled", source)
        self.assertIn("p3_binding_enabled = bool(", source)
        self.assertIn("p3_manual_control_enabled", source)
        self.assertIn("self.hard_close_p2_menu()", source)
        self.assertIn("self.hard_close_p3_menu()", source)

    def test_menu_navigation_reuses_the_character_direction_pulse(self) -> None:
        source = inspect.getsource(live.CoopMenuRuntime.input)
        self.assertIn(
            "up = p2_character_dpad_up if p2_binding_enabled else False",
            source,
        )
        self.assertIn(
            "p3_up = p3_character_dpad_up if p3_binding_enabled else False",
            source,
        )
        self.assertEqual(source.count('repeat("p2", "menu_up")'), 1)
        self.assertEqual(source.count('repeat("p2", "menu_down")'), 1)
        self.assertEqual(source.count('repeat("p3", "menu_up")'), 1)
        self.assertEqual(source.count('repeat("p3", "menu_down")'), 1)
        self.assertEqual(source.count('"p2", "menu_up", accelerated=True'), 1)
        self.assertEqual(source.count('"p2", "menu_down", accelerated=True'), 1)
        self.assertEqual(source.count('"p3", "menu_up", accelerated=True'), 1)
        self.assertEqual(source.count('"p3", "menu_down", accelerated=True'), 1)

    def test_native_frontend_reuses_the_already_sampled_vertical_pulse(self) -> None:
        source = inspect.getsource(live.CoopMenuRuntime._route_native_frontend_input)
        self.assertIn("p2_up=p2.character_up", source)
        self.assertIn("p2_down=p2.character_down", source)
        self.assertIn("p3_up=p3.character_up", source)
        self.assertIn("p3_down=p3.character_down", source)
        self.assertNotIn('repeat("p2", "menu_down")', source)
        self.assertNotIn('repeat("p3", "menu_down")', source)

    def test_native_frontend_receives_y_and_both_shoulder_buttons(self) -> None:
        source = inspect.getsource(live.CoopMenuRuntime.input)
        route = inspect.getsource(live.CoopMenuRuntime._route_native_frontend_input)
        for player in ("p2", "p3"):
            self.assertIn(
                f'native_party_menu=native_party_menu_edges["{player}"]',
                source,
            )
            self.assertIn(f"{player}_previous={player}.native_previous", route)
            self.assertIn(f"{player}_next={player}.native_next", route)

    def test_party_menu_receives_follower_down_while_battle_input_is_gated(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_party_menu_active = True
        shell.native_frontend_custom_suspended = True
        shell.native_frontend_observed_open = True
        shell.native_frontend_close_candidate_since = 0.0
        shell.custom_ui_suppressed = False
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.bindings = SimpleNamespace(repeat=lambda *_args: False)
        shell.native_dialog_bridge = SimpleNamespace(pulse=mock.Mock())
        snapshot = SimpleNamespace(
            native_menu_open=True,
            native_modal_active=False,
            user_control_enabled=True,
            loading=False,
            cutscene=False,
            town=False,
        )

        consumed = shell._route_native_frontend_input(
            foreground=True,
            snapshot=snapshot,
            p1_native_command_open=False,
            now=10.0,
            p1=live.PlayerMenuInput(),
            p2=live.PlayerMenuInput(
                down=False,
                character_down=True,
            ),
            p3=live.PlayerMenuInput(),
        )

        self.assertTrue(consumed)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_DOWN
        )

    @staticmethod
    def ui_snapshot(**changes):
        values = dict(
            battle_info=0x500000,
            loading=False,
            cutscene=False,
            user_control_enabled=True,
            native_menu_open=False,
            native_modal_active=False,
            town=False,
        )
        values.update(changes)
        return SimpleNamespace(**values)

    @staticmethod
    def party_menu_snapshot(**changes):
        values = dict(
            battle_info=0x500000,
            loading=False,
            cutscene=False,
            user_control_enabled=True,
            native_menu_open=False,
            native_modal_active=False,
            town=False,
            p1_gameplay_ready=True,
            gameplay_ready=True,
            identity=session_state.SessionIdentity(0, 2, 3),
        )
        values.update(changes)
        return SimpleNamespace(**values)

    @staticmethod
    def native_party_transfer_shell(close_complete=False):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.suspend_custom_menus_for_native_frontend = mock.Mock()
        shell.resume_custom_menus_after_native_frontend = mock.Mock()
        shell.native_dialog_bridge = SimpleNamespace(
            set_p1_replacement=mock.Mock(),
            set_non_p1_command_blocked=mock.Mock(),
            set_custom_directional_owner=mock.Mock(),
            set_p1_menu_owns_lb=mock.Mock(),
            set_party_menu_blocked=mock.Mock(),
            pulse=mock.Mock(),
        )
        shell.native_wait_mode_bridge = SimpleNamespace(
            maintain=mock.Mock(return_value=True),
            lifecycle_close_complete=mock.Mock(return_value=close_complete),
        )
        shell.p1_native_lifecycle = mock.Mock(return_value=None)
        shell.custom_ui_suppressed = False
        shell.native_frontend_custom_suspended = False
        shell.native_frontend_observed_open = False
        shell.native_party_menu_active = False
        shell.party_edit_capture = SimpleNamespace(
            begin=mock.Mock(),
            cancel=mock.Mock(),
        )
        shell.native_party_menu_request_pending = False
        shell.native_party_menu_request_pulsed = False
        shell.native_party_menu_request_source = ""
        shell.native_party_menu_request_deadline = 0.0
        shell.native_party_command_drain_pending = False
        return shell

    def test_native_party_action_is_available_to_each_materialized_player(self):
        snapshot = self.party_menu_snapshot()
        for player in ("p1", "p2", "p3"):
            with self.subTest(player=player):
                self.assertTrue(
                    live.native_party_menu_request_allowed(snapshot, player, 3)
                )
        self.assertFalse(live.native_party_menu_request_allowed(snapshot, "p3", 2))
        self.assertFalse(
            live.native_party_menu_request_allowed(
                self.party_menu_snapshot(native_menu_open=True), "p2", 3
            )
        )
        # Town closes custom battle menus but deliberately does not disable
        # FF12's native Party/Licenses frontend.
        self.assertTrue(
            live.native_party_menu_request_allowed(
                self.party_menu_snapshot(town=True), "p2", 3
            )
        )

    def test_native_party_request_suspends_without_destroying_custom_state(self):
        shell = self.native_party_transfer_shell()

        self.assertTrue(shell.request_native_party_menu("p3", 10.0))

        shell.suspend_custom_menus_for_native_frontend.assert_called_once_with(
            "p3", now=10.0
        )
        shell.hard_close_p1_menu.assert_not_called()
        shell.hard_close_p2_menu.assert_not_called()
        shell.hard_close_p3_menu.assert_not_called()
        shell.native_dialog_bridge.set_party_menu_blocked.assert_called_once_with(True)
        shell.native_dialog_bridge.set_p1_replacement.assert_called_once_with(True)
        shell.native_dialog_bridge.set_non_p1_command_blocked.assert_called_once_with(True)
        shell.native_dialog_bridge.set_custom_directional_owner.assert_called_once_with(
            False
        )
        shell.native_wait_mode_bridge.maintain.assert_called_once_with(False)
        self.assertTrue(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_request_pulsed)
        self.assertEqual(shell.native_party_menu_request_source, "p3")
        self.assertEqual(
            shell.native_party_menu_request_deadline,
            10.0 + live.NATIVE_PARTY_MENU_TRANSFER_TIMEOUT_SECONDS,
        )

    def test_native_party_transfer_waits_for_close_then_pulses_y_once(self):
        shell = self.native_party_transfer_shell(close_complete=False)
        shell.request_native_party_menu("p2", 10.0)
        snapshot = self.party_menu_snapshot()

        self.assertTrue(
            shell.maintain_native_party_menu_request(
                foreground=True,
                snapshot=snapshot,
                native_pause_open=False,
                now=10.1,
            )
        )
        shell.native_dialog_bridge.pulse.assert_not_called()
        shell.native_wait_mode_bridge.lifecycle_close_complete.return_value = True

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=snapshot,
            native_pause_open=False,
            now=10.2,
        )
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_PARTY_MENU
        )
        self.assertTrue(shell.native_party_menu_request_pulsed)

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=snapshot,
            native_pause_open=False,
            now=10.3,
        )
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_PARTY_MENU
        )

    def test_native_party_transfer_observes_open_and_releases_pending_owner(self):
        shell = self.native_party_transfer_shell(close_complete=True)
        shell.request_native_party_menu("p1", 10.0)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(),
            native_pause_open=False,
            now=10.05,
        )
        self.assertTrue(shell.native_party_menu_request_pulsed)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.1,
        )

        self.assertFalse(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_request_pulsed)
        self.assertTrue(shell.native_party_menu_active)
        self.assertTrue(shell.native_frontend_observed_open)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_PARTY_MENU
        )
        shell.party_edit_capture.begin.assert_called_once_with(
            "p1", battle_info=0x500000
        )

    def test_physical_p1_y_fullscreen_is_ack_before_synthetic_pulse(self):
        shell = self.native_party_transfer_shell(close_complete=False)
        shell.request_native_party_menu("p1", 10.0)

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.05,
        )

        shell.native_dialog_bridge.pulse.assert_not_called()
        self.assertFalse(shell.native_party_menu_request_pending)
        self.assertTrue(shell.native_party_menu_active)
        shell.party_edit_capture.begin.assert_called_once_with(
            "p1", battle_info=0x500000
        )

    def test_stale_custom_command_tree_is_drained_before_party_y(self):
        shell = self.native_party_transfer_shell(close_complete=True)
        shell.request_native_party_menu("p2", 10.0)
        shell.p1_native_lifecycle.return_value = (0xA000, 0, 0xA000)

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.05,
        )

        self.assertTrue(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_request_pulsed)
        self.assertFalse(shell.native_party_menu_active)
        shell.party_edit_capture.begin.assert_not_called()
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CANCEL
        )

        # FF12 can lower the embedded active leaf one frame before its global
        # native-menu state.  This tail is neither Party nor an error.
        shell.p1_native_lifecycle.return_value = (0xA000, 0, 0)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.075,
        )
        self.assertTrue(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_request_pulsed)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CANCEL
        )

        shell.p1_native_lifecycle.return_value = None
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(),
            native_pause_open=False,
            now=10.10,
        )
        self.assertTrue(shell.native_party_menu_request_pulsed)
        self.assertEqual(
            shell.native_dialog_bridge.pulse.call_args_list,
            [
                mock.call(live.NATIVE_DIALOG_CANCEL),
                mock.call(live.NATIVE_DIALOG_PARTY_MENU),
            ],
        )

    def test_stale_command_tree_cannot_fake_physical_p1_party_ack(self):
        shell = self.native_party_transfer_shell(close_complete=True)
        shell.request_native_party_menu("p1", 10.0)
        shell.p1_native_lifecycle.return_value = (0xA000, 0, 0xA000)

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.05,
        )

        self.assertTrue(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_active)
        shell.party_edit_capture.begin.assert_not_called()
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CANCEL
        )

        shell.p1_native_lifecycle.return_value = (0xA000, 0, 0)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.075,
        )
        self.assertTrue(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_active)
        shell.party_edit_capture.begin.assert_not_called()
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CANCEL
        )

    def test_p2_fullscreen_still_requires_the_synthetic_party_pulse(self):
        shell = self.native_party_transfer_shell(close_complete=False)
        shell.request_native_party_menu("p2", 10.0)

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=False,
            now=10.05,
        )

        self.assertFalse(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_active)
        shell.party_edit_capture.begin.assert_not_called()

    def test_pause_cannot_be_mistaken_for_party_menu_ack(self):
        shell = self.native_party_transfer_shell(close_complete=True)
        shell.request_native_party_menu("p2", 10.0)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(),
            native_pause_open=False,
            now=10.05,
        )
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(native_menu_open=True),
            native_pause_open=True,
            now=10.10,
        )

        self.assertFalse(shell.native_party_menu_request_pending)
        shell.party_edit_capture.begin.assert_not_called()

    def test_party_fullscreen_plus_modal_is_a_valid_pulsed_ack(self):
        shell = self.native_party_transfer_shell(close_complete=True)
        shell.request_native_party_menu("p1", 10.0)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(),
            native_pause_open=False,
            now=10.05,
        )

        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(
                native_menu_open=True,
                native_modal_active=True,
            ),
            native_pause_open=False,
            now=10.10,
        )

        self.assertTrue(shell.native_party_menu_active)
        shell.party_edit_capture.begin.assert_called_once_with(
            "p1", battle_info=0x500000
        )

    def test_native_party_transfer_timeout_never_leaves_wait_desired(self):
        shell = self.native_party_transfer_shell(close_complete=False)
        shell.request_native_party_menu("p2", 10.0)
        shell.maintain_native_party_menu_request(
            foreground=True,
            snapshot=self.party_menu_snapshot(),
            native_pause_open=False,
            now=shell.native_party_menu_request_deadline + 0.01,
        )

        self.assertFalse(shell.native_party_menu_request_pending)
        self.assertEqual(
            shell.native_wait_mode_bridge.maintain.call_args_list[-1],
            mock.call(False),
        )
        shell.native_dialog_bridge.pulse.assert_not_called()

    def test_native_party_suspension_returns_exact_model_state_after_settle(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        models = [
            live.P2MenuModel(0x100001 + index, live.make_demo_catalog())
            for index in range(3)
        ]
        for index, model in enumerate(models):
            model.open()
            model.navigate(index + 1)
        models[1].mode = live.MenuMode.TARGET
        models[1].pending_action = models[1].pages["root"].entries[0].action
        models[1].target_index = 2
        shell.p1_model, shell.model, shell.p3_model = models
        before = tuple(
            (
                model.mode,
                tuple((state.key, state.index) for state in model.stack),
                model.pending_action,
                model.target_group,
                model.target_index,
            )
            for model in models
        )
        shell.role_generation = 7
        shell.session_snapshot = self.ui_snapshot()
        shell.native_frontend_custom_suspended = False
        shell.native_frontend_suspended_since = 0.0
        shell.native_frontend_observed_open = False
        shell.native_frontend_close_candidate_since = 0.0
        shell.native_frontend_suspension_source = ""
        shell.native_frontend_suspended_battle_info = 0
        shell.native_frontend_suspended_role_generation = 0
        shell.native_party_menu_active = False
        shell.native_party_menu_request_pending = False
        shell.hide_custom_menu_visuals_preserving_state = mock.Mock()
        shell.p1_transition_kind = None
        shell.transition_kind = None
        shell.p3_transition_kind = None
        shell.p1_last_paint = 0.0
        shell.last_paint = 0.0
        shell.p3_last_paint = 0.0

        shell.suspend_custom_menus_for_native_frontend("p1", now=10.0)
        shell.native_party_menu_active = True
        shell.maintain_native_frontend_suspension(
            self.ui_snapshot(native_menu_open=True), now=10.05
        )
        shell.maintain_native_frontend_suspension(self.ui_snapshot(), now=10.10)
        shell.maintain_native_frontend_suspension(self.ui_snapshot(), now=10.29)
        self.assertTrue(shell.native_frontend_custom_suspended)
        shell.maintain_native_frontend_suspension(self.ui_snapshot(), now=10.31)

        after = tuple(
            (
                model.mode,
                tuple((state.key, state.index) for state in model.stack),
                model.pending_action,
                model.target_group,
                model.target_index,
            )
            for model in models
        )
        self.assertFalse(shell.native_frontend_custom_suspended)
        self.assertEqual(after, before)

    def test_loading_does_not_restart_native_frontend_suspension(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_frontend_custom_suspended = False
        shell.suspend_custom_menus_for_native_frontend = mock.Mock()

        shell.maintain_native_frontend_suspension(
            self.ui_snapshot(loading=True, native_menu_open=True), now=10.0
        )

        shell.suspend_custom_menus_for_native_frontend.assert_not_called()

    def test_party_capture_stays_open_through_modal_and_suspend_gaps(self):
        source = inspect.getsource(live.CoopMenuRuntime.maintain_party_edit_capture)
        self.assertIn("native_modal_active", source)
        self.assertIn("native_frontend_custom_suspended", source)

    def test_party_role_change_discards_old_ui_but_keeps_manual_capture(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_frontend_custom_suspended = True
        shell.native_frontend_suspended_since = 10.0
        shell.native_frontend_observed_open = True
        shell.native_frontend_close_candidate_since = 0.0
        shell.native_frontend_suspension_source = "p1"
        shell.native_frontend_suspended_battle_info = 0x500000
        shell.native_frontend_suspended_role_generation = 4
        shell.native_party_menu_active = True
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell._clear_native_party_menu_request = mock.Mock()
        shell.party_edit_capture = SimpleNamespace(cancel=mock.Mock())
        shell.native_dialog_bridge = SimpleNamespace(set_party_menu_blocked=mock.Mock())
        shell.native_wait_mode_bridge = SimpleNamespace(maintain=mock.Mock())

        shell.abort_native_frontend_suspension("role_generation")

        shell.hard_close_p1_menu.assert_called_once_with()
        shell.hard_close_p2_menu.assert_called_once_with()
        shell.hard_close_p3_menu.assert_called_once_with()
        shell.party_edit_capture.cancel.assert_not_called()
        self.assertFalse(shell.native_frontend_custom_suspended)

    def test_native_party_edges_are_observed_before_native_ui_early_returns(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        observer = source.index('self.bindings.edge(player, "native_party_menu")')
        self.assertLess(observer, source.index("if native_pause_open:"))
        self.assertLess(observer, source.index("self._route_native_frontend_input("))

    def test_p1_competing_tree_reuses_broad_wait_presentation_sample(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        self.assertIn(
            "wait_presentation_owned = (\n"
            "            self.native_wait_mode_bridge.owns_native_presentation()",
            source,
        )
        self.assertIn(
            "p1_binding_enabled and p1_native_command_open",
            source,
        )
        self.assertNotIn("owns_native_lifecycle()", source)

    def test_owned_wait_with_rewritten_issuer_never_becomes_external_menu(self):
        identity = session_state.SessionIdentity(0, 2, 3)
        empty = session_state.EMPTY_PARTY_ID
        raw = session_state.SessionSnapshot(
            identity=identity,
            battle_info=0x500000,
            current_layout=(0, 2, 3, empty, empty, empty, empty, empty, empty),
            ui_layout=(0, 2, 3, empty, empty, empty, empty, empty, empty),
            leader_party_member_id=0,
            current_location=7,
            ui_location=7,
            user_control_enabled=True,
            native_menu_open=True,
            native_modal_active=False,
            town=False,
            loading=False,
            cutscene=False,
            p1_gameplay_ready=True,
            gameplay_ready=True,
            p1_is_leader=True,
            p2_is_leader=False,
            p1_menu_allowed=False,
            p2_menu_allowed=False,
            movement_allowed=False,
            camera_allowed=True,
            reason="native fullscreen menu",
        )
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.session_snapshot = raw
        shell.runtime_catalog_identity = identity
        shell.runtime_catalog_p1_owner = 0
        shell.session = SimpleNamespace(snapshot=mock.Mock(return_value=raw))
        shell.process = mock.Mock()
        shell.materialized_party_count = 3
        shell.active_party = [(0, "Vaan", 0x100001, 0xABC000)]
        shell.pending_party_identity = None
        shell.pending_party_identity_since = 0.0
        shell.party_identity_save_retry_at = 0.0
        shell.native_wait_mode_bridge = SimpleNamespace(
            owns_native_presentation=mock.Mock(return_value=True),
        )
        shell.role_generation = 4
        shell.native_frontend_custom_suspended = False
        shell.native_frontend_observed_open = False
        shell.native_frontend_close_candidate_since = 0.0
        shell.native_party_menu_request_pending = False
        shell.suspend_custom_menus_for_native_frontend = mock.Mock()

        with (
            mock.patch.object(
                live.legacy.coop,
                "read_native_leader_role_transfer",
                return_value=None,
            ),
            mock.patch.object(
                live,
                "transient_native_leader_handoff_active",
                return_value=False,
            ),
            mock.patch.object(live, "controlled_actor_alive", return_value=True),
            mock.patch.object(
                session_state,
                "replacement_identity_for_live_party",
                return_value=None,
            ) as replacement,
        ):
            effective = shell.refresh_session_state()

        replacement_snapshot = replacement.call_args.args[0]
        self.assertFalse(replacement_snapshot.native_menu_open)
        self.assertEqual(replacement_snapshot.reason, "custom Wait command menu")
        self.assertFalse(effective.native_menu_open)
        self.assertEqual(effective.reason, "custom Wait command menu")
        self.assertTrue(effective.p1_menu_allowed)
        shell.native_wait_mode_bridge.owns_native_presentation.assert_called_once_with()

        shell.maintain_native_frontend_suspension(effective, now=10.0)
        shell.suspend_custom_menus_for_native_frontend.assert_not_called()

    def test_native_ui_suppression_reads_inverted_steam_render_bit(self):
        reads = []

        class FakeProcess:
            def __init__(self, flags):
                self.flags = flags

            def read(self, address, size):
                reads.append((address, size))
                return struct.pack("<H", self.flags)

        self.assertEqual(live.NATIVE_RENDERING_FLAGS, 0x022C7F90)
        self.assertFalse(live.native_ui_render_suppressed(FakeProcess(0x00FF)))
        self.assertTrue(live.native_ui_render_suppressed(FakeProcess(0x00F7)))
        self.assertEqual(
            reads,
            [
                (live.NATIVE_RENDERING_FLAGS, 2),
                (live.NATIVE_RENDERING_FLAGS, 2),
            ],
        )

        failing = SimpleNamespace(read=mock.Mock(side_effect=OSError("transition")))
        self.assertFalse(live.native_ui_render_suppressed(failing))

    def test_custom_ui_gate_covers_special_cutscene_and_native_frontends(self):
        ordinary = self.ui_snapshot()
        self.assertFalse(live.custom_ui_suppression_required(ordinary))
        self.assertTrue(
            live.custom_ui_suppression_required(ordinary, native_ui_suppressed=True)
        )
        self.assertFalse(
            live.custom_ui_suppression_required(
                ordinary,
                native_ui_suppressed=True,
                owned_wait_presentation=True,
            )
        )
        # Wait ownership only explains the render-bit pulse. It must never
        # punch through an actual scene, Pause screen or Mist presentation.
        self.assertTrue(
            live.custom_ui_suppression_required(
                self.ui_snapshot(cutscene=True),
                native_ui_suppressed=True,
                owned_wait_presentation=True,
            )
        )
        self.assertTrue(
            live.custom_ui_suppression_required(
                ordinary,
                native_ui_suppressed=True,
                native_pause_open=True,
                owned_wait_presentation=True,
            )
        )
        self.assertTrue(
            live.custom_ui_suppression_required(ordinary, native_pause_open=True)
        )
        self.assertTrue(
            live.custom_ui_suppression_required(ordinary, mist_presentation_active=True)
        )
        self.assertTrue(
            live.custom_ui_suppression_required(self.ui_snapshot(cutscene=True))
        )
        # Party/Licenses opened with the native Y button publishes this state.
        self.assertTrue(
            live.custom_ui_suppression_required(self.ui_snapshot(native_menu_open=True))
        )
        self.assertTrue(
            live.custom_ui_suppression_required(
                self.ui_snapshot(native_modal_active=True)
            )
        )

    def test_native_mist_action_is_an_independent_suppression_fallback(self):
        process = MemoryProcess()
        actor = 0x12000
        party = [("Fran", 0x100002, actor)]
        process.put(
            actor + live.P2_ACTION_CURRENT_OFFSET,
            struct.pack("<H", 0xD3),
        )
        action_ids = live.native_party_action_ids(process, party)
        self.assertTrue(action_ids & live.MIST_PRESENTATION_ACTION_IDS)
        process.put(
            actor + live.P2_ACTION_CURRENT_OFFSET,
            struct.pack("<H", 0x96),
        )
        action_ids = live.native_party_action_ids(process, party)
        self.assertFalse(action_ids & live.MIST_PRESENTATION_ACTION_IDS)

    def test_quickening_detector_excludes_esper_presentations(self):
        process = MemoryProcess()
        actor = 0x12000
        party = [("Fran", 0x100002, actor)]
        process.put(
            actor + live.P2_ACTION_CURRENT_OFFSET,
            struct.pack("<H", 0xE1),
        )
        action_ids = live.native_party_action_ids(process, party)
        self.assertTrue(action_ids & live.QUICKENING_PRESENTATION_ACTION_IDS)
        process.put(
            actor + live.P2_ACTION_CURRENT_OFFSET,
            struct.pack("<H", 0xE2),
        )
        action_ids = live.native_party_action_ids(process, party)
        self.assertFalse(action_ids & live.QUICKENING_PRESENTATION_ACTION_IDS)
        self.assertTrue(action_ids & live.MIST_PRESENTATION_ACTION_IDS)

    def test_actor_has_libra_reads_temporary_and_permanent_effect_words(self):
        self.assertEqual(live.PERMANENT_STATUS_EFFECT_IMMUNITIES_OFFSET, 0x38)
        self.assertEqual(live.PERMANENT_STATUS_EFFECTS_OFFSET, 0x3C)
        self.assertEqual(live.TEMPORARY_STATUS_EFFECTS_OFFSET, 0x64)
        actor = 0x12000
        battle_keep = 0x15000
        for offset in (
            live.TEMPORARY_STATUS_EFFECTS_OFFSET,
            live.PERMANENT_STATUS_EFFECTS_OFFSET,
        ):
            with self.subTest(offset=offset):
                process = MemoryProcess()
                process.put(actor + 0x698, struct.pack("<Q", battle_keep))
                process.put(
                    battle_keep + offset,
                    struct.pack("<I", live.LIBRA_STATUS_MASK),
                )
                shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
                shell.process = process
                self.assertTrue(shell.actor_has_libra(actor))

        process = MemoryProcess()
        process.put(actor + 0x698, struct.pack("<Q", battle_keep))
        shell.process = process
        self.assertFalse(shell.actor_has_libra(actor))

    def test_actor_has_libra_ignores_permanent_effect_immunities(self):
        process = MemoryProcess()
        actor = 0x12000
        battle_keep = 0x15000
        process.put(actor + 0x698, struct.pack("<Q", battle_keep))
        process.put(
            battle_keep + live.PERMANENT_STATUS_EFFECT_IMMUNITIES_OFFSET,
            struct.pack("<I", live.LIBRA_STATUS_MASK),
        )
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = process

        self.assertFalse(shell.actor_has_libra(actor))
        self.assertFalse(shell.actor_has_anti_libra(actor))

    def test_actor_has_anti_libra_reads_both_native_augment_tables(self):
        self.assertEqual(live.TEMPORARY_AUGMENTS_OFFSET, 0x68)
        self.assertEqual(live.PERMANENT_AUGMENTS_OFFSET, 0x78)
        self.assertEqual(live.ANTI_LIBRA_AUGMENT_BYTE_INDEX, 5)
        self.assertEqual(live.ANTI_LIBRA_AUGMENT_MASK, 0x02)
        actor = 0x12000
        battle_keep = 0x15000
        for offset in (
            live.TEMPORARY_AUGMENTS_OFFSET,
            live.PERMANENT_AUGMENTS_OFFSET,
        ):
            with self.subTest(offset=offset):
                process = MemoryProcess()
                process.put(actor + 0x698, struct.pack("<Q", battle_keep))
                process.put(
                    battle_keep + offset + live.ANTI_LIBRA_AUGMENT_BYTE_INDEX,
                    bytes((live.ANTI_LIBRA_AUGMENT_MASK,)),
                )
                shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
                shell.process = process
                self.assertTrue(shell.actor_has_anti_libra(actor))

        process = MemoryProcess()
        process.put(actor + 0x698, struct.pack("<Q", battle_keep))
        shell.process = process
        self.assertFalse(shell.actor_has_anti_libra(actor))

    def test_flying_label_requires_both_native_class_flags(self):
        actor = 0x12000
        class_record = 0x18000
        for has_info, movement, expected in (
            (0x80, 0x10, True),
            (0x00, 0x10, False),
            (0x80, 0x20, False),
        ):
            with self.subTest(
                has_info=has_info,
                movement=movement,
            ):
                process = MemoryProcess()
                process.put(
                    actor + live.ACTOR_CLASS_RECORD_OFFSET,
                    struct.pack("<Q", class_record),
                )
                process.put(
                    class_record + live.CLASS_HAS_FLYING_INFO_OFFSET,
                    bytes((has_info,)),
                )
                process.put(
                    class_record + live.CLASS_MOVEMENT_FLAGS_OFFSET,
                    bytes((movement,)),
                )
                shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
                shell.process = process
                self.assertEqual(shell.actor_has_flying_info(actor), expected)

    def test_temporary_effect_libra_reveals_enemy_target_stats(self):
        process = MemoryProcess()
        party_actor = 0x12000
        party_keep = 0x15000
        foe_actor = 0x22000
        foe_keep = 0x25000
        foe_handle = 0x200027
        process.put(party_actor + 0x698, struct.pack("<Q", party_keep))
        process.put(
            party_keep + live.TEMPORARY_STATUS_EFFECTS_OFFSET,
            struct.pack("<I", live.LIBRA_STATUS_MASK),
        )
        process.put(foe_actor + 0x698, struct.pack("<Q", foe_keep))
        process.put(foe_actor + 0xD0, struct.pack("<3f", 1.0, 2.0, 3.0))
        process.put(foe_keep + 0x1C2, b"\x1c")
        process.put(foe_keep + 0x24, struct.pack("<I", 4090))
        process.put(foe_keep + 0x48, struct.pack("<I", 2000))
        process.put(foe_keep + 0x40, b"\x08")
        process.put(
            foe_keep + live.PERMANENT_STATUS_EFFECTS_OFFSET,
            struct.pack("<I", 1 << 8),
        )

        model = live.P2MenuModel(0x100008, live.make_demo_catalog())
        model.set_candidates(
            (live.TargetCandidate(foe_handle, "Dive Talon A", live.TargetGroup.FOES),)
        )
        model.open()
        model.confirm()

        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = process
        shell.model = model
        shell.active_party = [(0, "Vaan", 0x100001, party_actor)]
        shell._libra_status_seen = True
        shell.live_actor_for_handle = mock.Mock(return_value=foe_actor)

        target = shell.selected_live_target(owner_actor=party_actor)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertTrue(target.reveal_stats)
        self.assertTrue(target.viewer_has_libra)
        self.assertTrue(target.status_mask & (1 << 8))
        self.assertEqual(target.level, 28)
        self.assertEqual((target.current_hp, target.max_hp), (2000, 4090))

    def test_anti_libra_hides_enemy_stats_but_marks_the_target(self):
        process = MemoryProcess()
        party_actor = 0x12000
        party_keep = 0x15000
        foe_actor = 0x22000
        foe_keep = 0x25000
        foe_handle = 0x200027
        process.put(party_actor + 0x698, struct.pack("<Q", party_keep))
        process.put(
            party_keep + live.TEMPORARY_STATUS_EFFECTS_OFFSET,
            struct.pack("<I", live.LIBRA_STATUS_MASK),
        )
        process.put(foe_actor + 0x698, struct.pack("<Q", foe_keep))
        process.put(foe_actor + 0xD0, struct.pack("<3f", 1.0, 2.0, 3.0))
        process.put(foe_keep + 0x1C2, b"\x1c")
        process.put(foe_keep + 0x24, struct.pack("<I", 4090))
        process.put(foe_keep + 0x48, struct.pack("<I", 2000))
        process.put(
            foe_keep
            + live.PERMANENT_AUGMENTS_OFFSET
            + live.ANTI_LIBRA_AUGMENT_BYTE_INDEX,
            bytes((live.ANTI_LIBRA_AUGMENT_MASK,)),
        )

        model = live.P2MenuModel(0x100008, live.make_demo_catalog())
        model.set_candidates(
            (live.TargetCandidate(foe_handle, "Boss A", live.TargetGroup.FOES),)
        )
        model.open()
        model.confirm()
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = process
        shell.model = model
        shell.active_party = [(0, "Vaan", 0x100001, party_actor)]
        shell.live_actor_for_handle = mock.Mock(return_value=foe_actor)

        target = shell.selected_live_target(owner_actor=party_actor)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertFalse(target.reveal_stats)
        self.assertTrue(target.anti_libra)
        self.assertTrue(target.viewer_has_libra)

    def test_immunity_word_neither_reveals_stats_nor_adds_libra_badges(self):
        process = MemoryProcess()
        party_actor = 0x12000
        party_keep = 0x15000
        foe_actor = 0x22000
        foe_keep = 0x25000
        foe_handle = 0x200027
        for actor, battle_keep in (
            (party_actor, party_keep),
            (foe_actor, foe_keep),
        ):
            process.put(actor + 0x698, struct.pack("<Q", battle_keep))
            process.put(
                battle_keep + live.PERMANENT_STATUS_EFFECT_IMMUNITIES_OFFSET,
                struct.pack("<I", live.LIBRA_STATUS_MASK),
            )
        process.put(foe_actor + 0xD0, struct.pack("<3f", 1.0, 2.0, 3.0))
        process.put(foe_keep + 0x1C2, b"\x1c")
        process.put(foe_keep + 0x24, struct.pack("<I", 4090))
        process.put(foe_keep + 0x48, struct.pack("<I", 2000))

        model = live.P2MenuModel(0x100008, live.make_demo_catalog())
        model.set_candidates(
            (live.TargetCandidate(foe_handle, "Dive Talon A", live.TargetGroup.FOES),)
        )
        model.open()
        model.confirm()
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = process
        shell.model = model
        shell.active_party = [(0, "Vaan", 0x100001, party_actor)]
        shell._libra_status_seen = True
        shell.live_actor_for_handle = mock.Mock(return_value=foe_actor)

        target = shell.selected_live_target(owner_actor=party_actor)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertFalse(target.reveal_stats)
        self.assertFalse(target.viewer_has_libra)
        self.assertFalse(target.anti_libra)
        self.assertFalse(target.status_mask & live.LIBRA_STATUS_MASK)

    def test_presentation_suppression_hard_closes_all_three_menus(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.party = [object(), object(), object()]
        shell.last_session_gate_signature = None
        shell.native_frontend_custom_suspended = False
        shell.native_dialog_bridge = SimpleNamespace(
            set_p1_replacement=mock.Mock(),
            set_custom_directional_owner=mock.Mock(),
            set_p1_menu_owns_lb=mock.Mock(),
        )
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        identity = session_state.SessionIdentity(0, 2, 3)
        snapshot = SimpleNamespace(
            identity=identity,
            p1_menu_allowed=True,
            p2_menu_allowed=True,
            bridge_ownership_allowed=True,
            loading=False,
            cutscene=False,
            town=False,
            native_menu_open=False,
            native_modal_active=False,
            leader_party_member_id=0,
            camera_allowed=True,
            reason="field",
        )

        shell.apply_session_gates(snapshot, presentation_suppressed=True)
        # An unchanged gate signature takes the early return. Directional
        # ownership must still be dropped on every suppressed poll, while the
        # independent X/A/B mask survives the one-tick Wait close race.
        shell.apply_session_gates(snapshot, presentation_suppressed=True)

        self.assertTrue(shell.custom_ui_suppressed)
        shell.native_dialog_bridge.set_p1_replacement.assert_not_called()
        self.assertEqual(
            shell.native_dialog_bridge.set_custom_directional_owner.call_args_list,
            [mock.call(False), mock.call(False)],
        )
        shell.hard_close_p1_menu.assert_called_once_with()
        shell.hard_close_p2_menu.assert_called_once_with()
        shell.hard_close_p3_menu.assert_called_once_with()

    def test_presentation_suppression_is_published_to_the_renderer(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.party = [object(), object(), object()]
        shell.last_session_gate_signature = None
        shell.native_frontend_custom_suspended = False
        shell.native_dialog_bridge = SimpleNamespace(
            set_p1_replacement=mock.Mock(),
            set_custom_directional_owner=mock.Mock(),
            set_p1_menu_owns_lb=mock.Mock(),
        )
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.base = 0x100000
        shell.process = SimpleNamespace(write=mock.Mock())
        identity = session_state.SessionIdentity(0, 2, 3)
        snapshot = SimpleNamespace(
            identity=identity,
            p1_menu_allowed=True,
            p2_menu_allowed=True,
            bridge_ownership_allowed=True,
            loading=False,
            cutscene=False,
            town=False,
            native_menu_open=False,
            native_modal_active=False,
            leader_party_member_id=0,
            camera_allowed=True,
            reason="field",
        )

        shell.apply_session_gates(snapshot, presentation_suppressed=True)

        shell.process.write.assert_any_call(
            shell.base + live.RENDER_PRESENTATION_SUPPRESSED_OFFSET,
            struct.pack("<I", 1),
        )

    def test_run_uses_one_visibility_gate_for_every_custom_overlay(self):
        source = inspect.getsource(live.CoopMenuRuntime.update_frame)
        self.assertIn(
            "presentation_visible = bool(\n"
            "            overlay_visible\n"
            "            and self.coop_runtime_active\n"
            "            and not self.custom_ui_suppressed\n"
            "            and not self.native_frontend_custom_suspended",
            source,
        )
        for call in (
            "self.paint_p1_custom_menu(p1_visual_visible)",
            "self.paint_p1_target_info(p1_visual_visible)",
            "self.paint_world_target(p2_visual_visible)",
            "self.paint_p3_target_info(p3_visual_visible)",
            "self.paint_p2_interaction_prompt(p2_interaction_visible)",
            "self.paint_p3_interaction_prompt(p3_interaction_visible)",
            "self.paint(p2_visual_visible)",
            "self.paint_p3(p3_visual_visible)",
            "self.publish_p1_help_badge(p1_visual_visible)",
        ):
            self.assertIn(call, source)

    def test_hidden_panels_keep_the_publisher_heartbeat_alive(self):
        source = inspect.getsource(live.CoopMenuRuntime.run)
        heartbeat = source.index("self._maintain_panel_transport_heartbeat()")
        frame = source.index("self._update_frame_resilient(foreground)")
        self.assertLess(heartbeat, frame)
        heartbeat_source = inspect.getsource(
            live.CoopMenuRuntime._maintain_panel_transport_heartbeat
        )
        self.assertIn("transport.touch()", heartbeat_source)

    def test_input_releases_custom_ownership_while_ui_is_suppressed(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        route = inspect.getsource(live.CoopMenuRuntime._route_native_frontend_input)
        self.assertIn("and not self.custom_ui_suppressed", source)
        self.assertIn("self.custom_ui_suppressed", route)
        gate = source.index("self._route_native_frontend_input(")
        self.assertLess(gate, source.index("self._world_interactions()"))
        self.assertLess(gate, source.index("self._handle_p2_battle_input("))

    def test_town_is_forwarded_to_the_p1_native_command_mask_gate(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        mask_start = source.index("p1_mask_requested = p1_command_mask_requested(")
        mask_end = source.index("custom_directional_lifecycle_owned", mask_start)
        self.assertIn("town=snapshot.town", source[mask_start:mask_end])

    def test_suppression_bypasses_interaction_prompt_grace_immediately(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.transition_kind = None
        shell.foes = []
        shell.interaction_prompt_panel = mock.Mock()
        cache = live.InteractionPromptState()
        cache.key = (2, 0x1234, "Treasure", "A", False)
        cache.image = object()
        cache.last_point = (500.0, 300.0)
        shell.interaction_prompts = {2: cache}

        shell.paint_coop_interaction_prompt(2, False)

        shell.interaction_prompt_panel.hide.assert_called_once_with()
        self.assertIsNone(cache.key)
        self.assertIsNone(cache.image)
        self.assertIsNone(cache.last_point)

    @mock.patch.object(
        live.world_overlay,
        "project_actor_point",
        return_value=(500.0, 300.0),
    )
    @mock.patch.object(
        live.world_overlay,
        "is_probable_minimap_view",
        return_value=False,
    )
    @mock.patch.object(
        live.legacy,
        "decode_ff12_text",
        return_value="Treasure",
    )
    def test_follower_prompt_stays_visible_for_60_blocked_frames(
        self, _decode, _minimap, _project
    ):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        actor = 0x73000000
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.transition_kind = None
        shell.foes = []
        shell.party = [
            ("Vaan", 0x100001, 0x72000000),
            ("Fran", 0x100002, actor),
        ]
        shell.native_dialog_bridge = SimpleNamespace(
            modal_active=mock.Mock(return_value=False)
        )
        shell.interaction_prompt_panel = mock.Mock()
        shell.interaction_prompts = {2: live.InteractionPromptState()}
        shell.private_p2_interaction = mock.Mock(
            return_value=(2, 0x00100015, 0x12345678, 0)
        )
        shell.p2_world_interaction_allowed = mock.Mock(return_value=False)
        shell.process = SimpleNamespace(
            read=lambda _address, size: (
                struct.pack("<3f", 1.0, 2.0, 3.0)
                if size == 12
                else struct.pack("<16f", *([0.0] * 16))
            )
        )
        shell.bindings = SimpleNamespace(
            action_prompt_source=mock.Mock(return_value=("controller", "A"))
        )
        shell.pending_native_sounds = []
        shell.render_interaction_prompt = mock.Mock(return_value=object())
        shell._draw_interaction_prompt = mock.Mock()

        for _ in range(60):
            shell.paint_coop_interaction_prompt(2, True)

        self.assertEqual(shell.private_p2_interaction.call_count, 60)
        self.assertEqual(shell._draw_interaction_prompt.call_count, 60)
        shell.interaction_prompt_panel.hide.assert_not_called()
        shell.p2_world_interaction_allowed.assert_not_called()

    @mock.patch.object(
        live.world_overlay,
        "project_actor_point",
        return_value=(500.0, 300.0),
    )
    @mock.patch.object(
        live.world_overlay,
        "is_probable_minimap_view",
        return_value=False,
    )
    @mock.patch.object(
        live.native_interaction,
        "resolve_handle",
        return_value=0x74000000,
    )
    @mock.patch.object(
        live,
        "native_interaction_object_label",
        side_effect=("", "Jinn"),
    )
    def test_descriptor_only_npc_retries_name_after_text_table_catches_up(
        self, label, _resolve, _minimap, _project
    ):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.transition_kind = None
        shell.foes = []
        shell.party = [
            ("Vaan", 0x100001, 0x72000000),
            ("Fran", 0x100002, 0x73000000),
        ]
        shell.native_dialog_bridge = SimpleNamespace(
            modal_active=mock.Mock(return_value=False)
        )
        shell.interaction_prompt_panel = mock.Mock()
        shell.interaction_prompts = {2: live.InteractionPromptState()}
        shell.private_p2_interaction = mock.Mock(
            return_value=(2, 0x00100019, 0, -1)
        )
        shell.process = SimpleNamespace(
            read=lambda _address, size: (
                struct.pack("<3f", 1.0, 2.0, 3.0)
                if size == 12
                else struct.pack("<16f", *([0.0] * 16))
            )
        )
        shell.bindings = SimpleNamespace(
            action_prompt_source=mock.Mock(return_value=("controller", "A"))
        )
        shell.render_interaction_prompt = mock.Mock(return_value=object())
        shell._draw_interaction_prompt = mock.Mock()
        shell.queue_native_sound = mock.Mock()
        shell.leader_allows_interaction_cached = mock.Mock(return_value=True)

        shell.paint_coop_interaction_prompt(2, True)
        self.assertEqual(shell.interaction_prompts[2].label, "Interact")
        shell.paint_coop_interaction_prompt(2, True)

        self.assertEqual(label.call_count, 2)
        self.assertEqual(shell.interaction_prompts[2].label, "Jinn")
        self.assertEqual(
            shell.render_interaction_prompt.call_args_list[-1].args[1],
            "Jinn",
        )

    def test_follower_animation_gate_is_lazy_and_only_runs_on_confirm(self):
        painter = inspect.getsource(live.CoopMenuRuntime.paint_coop_interaction_prompt)
        p2_input = inspect.getsource(live.CoopMenuRuntime._handle_p2_battle_input)
        p3_input = inspect.getsource(live.CoopMenuRuntime._handle_p3_battle_input)

        # The painter now asks whether the leader would allow the press, so it
        # can fall back to the proximity marker instead of offering a button
        # that is refused. It must still not call the uncached check per frame:
        # that is what this guard is really protecting.
        self.assertNotIn("follower_world_interaction_allowed()", painter)
        self.assertIn("leader_allows_interaction_cached", painter)
        self.assertEqual(p2_input.count("p2_world_interaction_allowed()"), 1)
        self.assertEqual(p3_input.count("p3_world_interaction_allowed()"), 1)
        self.assertLess(
            p3_input.index("if not controls.confirm:"),
            p3_input.index("p3_world_interaction_allowed()"),
        )
        p2_gate = p2_input.index("p2_world_interaction_allowed()")
        self.assertGreater(p2_input.rfind("if not controls.confirm:", 0, p2_gate), -1)

    def test_both_followers_can_acknowledge_a_native_chest_modal(self):
        route = inspect.getsource(live.CoopMenuRuntime._route_native_frontend_input)
        self.assertIn("or snapshot.native_modal_active", route)
        modal_start = route.index("if modal_active:")
        modal_route = route[modal_start:]

        self.assertIn("follower_button is not None", modal_route)
        self.assertIn(
            "self.native_dialog_bridge.pulse(button)",
            modal_route,
        )

    def test_p2_and_p3_keep_independent_native_indicator_retries(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.pending_combat_indicators = {}
        shell.live_actor_for_handle = mock.Mock(return_value=0x1000)

        with mock.patch.object(live.time, "monotonic", return_value=10.0):
            shell.schedule_native_combat_indicator(0x21, 0x31)
            shell.schedule_native_combat_indicator(0x22, 0x32)

        self.assertEqual(set(shell.pending_combat_indicators), {0x21, 0x22})
        self.assertEqual(
            shell.pending_combat_indicators[0x21].target,
            0x31,
        )
        self.assertEqual(
            shell.pending_combat_indicators[0x22].target,
            0x32,
        )

    def test_run_observes_native_actions_for_both_followers(self):
        run_source = inspect.getsource(live.CoopMenuRuntime.update_frame)
        self.assertIn("self.observe_p2_native_action()", run_source)
        self.assertIn("self.observe_p3_native_action()", run_source)

    def test_followers_can_navigate_non_party_native_menus(self):
        route = inspect.getsource(live.CoopMenuRuntime._route_native_frontend_input)
        menu_start = route.index("if snapshot.native_menu_open:")
        menu_route = route[menu_start:]

        self.assertIn("follower_button is not None", menu_route)
        self.assertIn("NATIVE_FOLLOWER_MENU_INPUT", menu_route)

    def test_disabling_coop_restores_native_command_menu_for_followers(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_dialog_bridge = SimpleNamespace(
            set_party_menu_blocked=mock.Mock(),
            set_p1_replacement=mock.Mock(),
            set_custom_directional_owner=mock.Mock(),
            set_p1_menu_owns_lb=mock.Mock(),
            pulse=mock.Mock(),
        )
        shell.native_wait_mode_bridge = SimpleNamespace(maintain=mock.Mock())
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.LIST)
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.bindings = SimpleNamespace(
            repeat=lambda *_args: False,
            edge=lambda player, control: (player, control) == ("p2", "confirm"),
            native_action_edge=lambda player, control, _circle: (
                player,
                control,
            )
            == ("p2", "confirm"),
        )
        snapshot = SimpleNamespace(native_menu_open=False, native_modal_active=False)

        shell._route_coop_disabled_input(
            foreground=True,
            circle_decides=False,
            snapshot=snapshot,
            p2_character_up=False,
            p2_character_down=False,
            p3_character_up=False,
            p3_character_down=False,
            native_party_menu_edges={"p1": False, "p2": False, "p3": False},
        )

        shell.hard_close_p1_menu.assert_called_once_with()
        shell.native_dialog_bridge.set_party_menu_blocked.assert_called_once_with(False)
        shell.native_dialog_bridge.set_p1_replacement.assert_called_once_with(False)
        shell.native_dialog_bridge.set_custom_directional_owner.assert_called_once_with(
            False
        )
        shell.native_wait_mode_bridge.maintain.assert_called_once_with(False)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_COMMAND
        )

    def test_disabled_coop_routes_followers_inside_the_native_menu(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_dialog_bridge = SimpleNamespace(
            set_party_menu_blocked=mock.Mock(),
            set_p1_replacement=mock.Mock(),
            set_custom_directional_owner=mock.Mock(),
            set_p1_menu_owns_lb=mock.Mock(),
            pulse=mock.Mock(),
        )
        shell.native_wait_mode_bridge = SimpleNamespace(maintain=mock.Mock())
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.bindings = SimpleNamespace(
            repeat=lambda *_args: False,
            edge=lambda *_args: False,
            native_action_edge=lambda *_args: False,
        )
        snapshot = SimpleNamespace(native_menu_open=True, native_modal_active=False)

        shell._route_coop_disabled_input(
            foreground=True,
            circle_decides=False,
            snapshot=snapshot,
            p2_character_up=False,
            p2_character_down=False,
            p3_character_up=False,
            p3_character_down=True,
            native_party_menu_edges={"p1": False, "p2": False, "p3": False},
        )

        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_DOWN
        )

    def test_p3_inherits_the_complete_p2_session_gate(self):
        identity = session_state.SessionIdentity(0, 4, 1)
        allowed = SimpleNamespace(
            p2_menu_allowed=True,
            identity=identity,
        )
        cutscene = SimpleNamespace(
            p2_menu_allowed=False,
            identity=identity,
        )
        self.assertTrue(live.p3_session_menu_allowed(allowed, 3))
        self.assertFalse(live.p3_session_menu_allowed(cutscene, 3))
        self.assertFalse(live.p3_session_menu_allowed(allowed, 2))

    def test_p3_menu_does_not_depend_on_an_occupied_p2_seat(self):
        snapshot = self.party_menu_snapshot(
            p2_menu_allowed=False,
            identity=session_state.SessionIdentity(0, None, 1, 3),
            chocobo_mode_active=False,
        )

        self.assertTrue(live.p3_session_menu_allowed(snapshot, 3))
        self.assertFalse(
            live.native_party_menu_request_allowed(snapshot, "p2", 3)
        )
        self.assertTrue(
            live.native_party_menu_request_allowed(snapshot, "p3", 3)
        )

    def test_p3_visuals_use_the_same_session_gate_as_input(self):
        dispatch = inspect.getsource(live.CoopMenuRuntime.dispatch_p3)
        run = inspect.getsource(live.CoopMenuRuntime.update_frame)
        self.assertIn("p3_session_menu_allowed(", dispatch)
        self.assertIn("p3_session_menu_allowed(snapshot, len(self.party))", run)

    def test_dead_leader_gate_waits_for_a_living_selected_replacement(self):
        gate = live.DeadLeaderMenuGate()
        self.assertTrue(
            gate.update(
                owner_alive=False,
                owner_member_id=0,
                native_leader_member_id=0,
                native_leader_alive=False,
                user_control_enabled=True,
            )
        )
        # Torn actor/leader samples and a selected actor under ucoff do not
        # release custom menus early.
        self.assertTrue(
            gate.update(
                owner_alive=None,
                owner_member_id=0,
                native_leader_member_id=None,
                native_leader_alive=None,
                user_control_enabled=False,
            )
        )
        self.assertTrue(
            gate.update(
                owner_alive=False,
                owner_member_id=0,
                native_leader_member_id=2,
                native_leader_alive=True,
                user_control_enabled=False,
            )
        )
        self.assertFalse(
            gate.update(
                owner_alive=False,
                owner_member_id=0,
                native_leader_member_id=2,
                native_leader_alive=True,
                user_control_enabled=True,
            )
        )

    def test_dead_leader_gate_also_accepts_a_revived_original(self):
        gate = live.DeadLeaderMenuGate(True, 0)
        self.assertFalse(
            gate.update(
                owner_alive=True,
                owner_member_id=0,
                native_leader_member_id=0,
                native_leader_alive=True,
                user_control_enabled=True,
            )
        )

    def test_native_party_recovery_requires_every_active_actor_to_be_dead(self):
        self.assertTrue(live.native_party_recovery_required((False,)))
        self.assertTrue(live.native_party_recovery_required((False, False, False)))
        self.assertFalse(live.native_party_recovery_required(()))
        self.assertFalse(live.native_party_recovery_required((False, True)))
        self.assertFalse(live.native_party_recovery_required((False, None)))

    def test_dead_party_recovery_passthrough_survives_native_role_promotion(self):
        required = live.native_party_recovery_passthrough_required
        self.assertTrue(
            required(
                (False, False),
                latched=False,
                native_menu_observed=False,
                native_menu_open=False,
                native_frontend_suspended=False,
                suspension_source="",
            )
        )
        self.assertTrue(
            required(
                (True, False),
                latched=True,
                native_menu_observed=True,
                native_menu_open=True,
                native_frontend_suspended=True,
                suspension_source="dead_party_recovery",
            )
        )
        self.assertTrue(
            required(
                (True, False),
                latched=True,
                native_menu_observed=True,
                native_menu_open=False,
                native_frontend_suspended=True,
                suspension_source="dead_party_recovery",
            )
        )
        self.assertFalse(
            required(
                (True, False),
                latched=True,
                native_menu_observed=True,
                native_menu_open=False,
                native_frontend_suspended=False,
                suspension_source="",
            )
        )

    def test_dead_party_native_frontend_ignores_role_generation_change(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_frontend_custom_suspended = True
        shell.native_frontend_suspended_battle_info = 0x500000
        shell.native_frontend_suspended_role_generation = 4
        shell.native_frontend_suspension_source = "dead_party_recovery"
        shell.native_frontend_close_candidate_since = 0.0
        shell.native_frontend_observed_open = True
        shell.native_frontend_open_candidate_since = None
        shell.native_party_menu_request_pending = False
        shell.native_party_menu_active = True
        shell.unexpected_native_menu_cancel_latched = False
        shell.role_generation = 5
        shell.abort_native_frontend_suspension = mock.Mock()

        shell.maintain_native_frontend_suspension(
            self.ui_snapshot(native_menu_open=True),
            now=10.0,
        )

        shell.abort_native_frontend_suspension.assert_not_called()
        self.assertEqual(shell.native_frontend_suspended_role_generation, 5)

    def test_dead_party_recovery_releases_native_input_before_frontend_routing(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        recovery = source.index("if dead_party_recovery:")
        quarantine = source.index("if unproven_native_menu:")
        self.assertLess(recovery, quarantine)
        recovery_source = source[recovery:quarantine]
        for field in (
            "p1_replacement=False",
            "non_p1_command_blocked=False",
            "party_menu_blocked=False",
            "leader_replacement=False",
        ):
            self.assertIn(field, recovery_source)
        self.assertIn("return", recovery_source)

    def test_dead_leader_gate_hides_custom_world_interaction_prompts(self):
        run = inspect.getsource(live.CoopMenuRuntime.update_frame)
        interaction_gate = run[
            run.index("interaction_visible = bool(") : run.index(
                "if self.native_frontend_custom_suspended:"
            )
        ]
        self.assertIn(
            "and not self.dead_leader_menu_gate.pending",
            interaction_gate,
        )
        self.assertIn(
            'and self.ko_promoted_follower_role != "p2"',
            interaction_gate,
        )
        self.assertIn(
            'and self.ko_promoted_follower_role != "p3"',
            interaction_gate,
        )

    def test_dead_leader_latch_blocks_followers_and_effective_wait(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        run = inspect.getsource(live.CoopMenuRuntime.update_frame)
        for binding_gate in ("p2_binding_enabled", "p3_binding_enabled"):
            start = source.index(f"{binding_gate} = bool(")
            end = source.index(")", start)
            self.assertIn("not leader_selection_pending", source[start:end])
        self.assertIn("and not self.dead_leader_menu_gate.pending", run)

    def test_native_party_follower_navigation_maps_one_logical_button(self):
        self.assertEqual(
            live.native_party_follower_button(p2_down=True),
            ("p2", live.NATIVE_DIALOG_DOWN),
        )
        self.assertEqual(
            live.native_party_follower_button(p3_confirm=True),
            ("p3", live.NATIVE_DIALOG_CONFIRM),
        )
        self.assertEqual(
            live.native_party_follower_button(p2_up=True, p3_cancel=True),
            ("p3", live.NATIVE_DIALOG_CANCEL),
        )
        self.assertIsNone(live.native_party_follower_button(p2_up=True, p2_down=True))
        self.assertEqual(
            live.native_party_follower_button(p2_party_menu=True),
            ("p2", live.NATIVE_DIALOG_PARTY_MENU),
        )
        self.assertEqual(
            live.native_party_follower_button(p3_previous=True),
            ("p3", live.NATIVE_DIALOG_LB),
        )
        self.assertEqual(
            live.native_party_follower_button(p2_next=True),
            ("p2", live.NATIVE_DIALOG_RB),
        )

    def test_transport_reserves_five_independent_p3_panels(self):
        self.assertEqual(d3d_panel_transport.PANEL_COUNT, 17)
        self.assertEqual(
            (
                d3d_panel_transport.PANEL_SLOTS["menu_p3"],
                d3d_panel_transport.PANEL_SLOTS["help_p3"],
                d3d_panel_transport.PANEL_SLOTS["target_info_p3"],
                d3d_panel_transport.PANEL_SLOTS["interaction_prompt_p3"],
                d3d_panel_transport.PANEL_SLOTS["party_target_cursor_p3"],
            ),
            (9, 10, 11, 12, 15),
        )

    def test_the_third_player_owns_its_own_world_interaction_prompt(self):
        # A shared prompt panel cannot show two followers at once: whichever
        # painted last would erase the other's sprite every frame.
        source = inspect.getsource(live.CoopMenuRuntime._initialize)
        self.assertIn("self.p3_interaction_prompt_panel = legacy.LayeredPanel(", source)
        self.assertIn('"interaction_prompt_p3"', source)
        self.assertIn("self.p3_interaction_prompt_panel,", source)
        painter = inspect.getsource(live.CoopMenuRuntime.paint_coop_interaction_prompt)
        self.assertIn("self.p3_interaction_prompt_panel", painter)
        self.assertIn("self.interaction_prompt_panel", painter)
        run = inspect.getsource(live.CoopMenuRuntime.update_frame)
        self.assertIn("self.paint_p3_interaction_prompt(", run)

    def test_follower_interaction_prompt_state_is_really_independent(self):
        p2 = live.InteractionPromptState()
        p3 = live.InteractionPromptState()
        self.assertIsNot(p2, p3)
        p2.key = ("p2",)
        self.assertIsNone(p3.key)

    def test_native_interaction_marker_keeps_its_cached_authored_size(self):
        first = live.native_interaction_prompt_sprite(False)
        second = live.native_interaction_prompt_sprite(False)
        self.assertIs(first, second)
        self.assertEqual(first.size, (35, 63))
        self.assertEqual(
            live.native_interaction_prompt_sprite(True).size,
            (94, 94),
        )

    def test_interaction_prompt_rejects_one_frame_projection_spikes(self):
        state = live.InteractionPromptState()
        state.last_point = (900.0, 400.0)
        self.assertEqual(
            state.stabilize_point((1400.0, 800.0)),
            state.last_point,
        )
        # Returning immediately to the real point clears the provisional jump.
        self.assertEqual(
            state.stabilize_point((904.0, 403.0)),
            (904.0, 403.0),
        )
        self.assertEqual(state.pending_point_count, 0)

    def test_interaction_prompt_accepts_a_confirmed_camera_snap(self):
        state = live.InteractionPromptState()
        state.last_point = (900.0, 400.0)
        self.assertEqual(
            state.stabilize_point((1200.0, 600.0)),
            state.last_point,
        )
        self.assertEqual(
            state.stabilize_point((1204.0, 602.0)),
            (1204.0, 602.0),
        )

    def test_interaction_diagnosis_is_not_shipped(self):
        source = inspect.getsource(live)
        self.assertNotIn("P3_INTERACTION_DIAGNOSIS", source)
        self.assertNotIn("def _p3dbg", source)
        self.assertLessEqual(
            live.INTERACTION_PROMPT_INNER_HOLD_SECONDS,
            live.INTERACTION_PROMPT_GRACE_SECONDS,
        )

    def test_live_frontend_has_an_independent_model_controller_and_catalog(self):
        source = inspect.getsource(live.CoopMenuRuntime._initialize)
        self.assertIn("self.p3_model = P2MenuModel", source)
        self.assertIn("self.p3_controller = HeadlessFrontendController", source)
        self.assertIn("self.p3_runtime_catalog = BattlePackRuntimeCatalog", source)
        self.assertIn('LayeredPanel(self.pid, "menu_p3")', source)

    def test_p3_input_uses_only_the_configured_third_player_bindings(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        handler = inspect.getsource(live.CoopMenuRuntime._handle_p3_battle_input)
        self.assertIn('"p3", "menu_up", accelerated=True', source)
        self.assertIn(
            'self.bindings.native_action_edge(\n            "p3",\n            "confirm"',
            source,
        )
        self.assertIn("excluded_keyboard_vks=(0x01,)", source)
        self.assertIn("self.dispatch_p3", handler)

    def test_native_modal_input_runs_before_custom_presentation_suppression(self):
        source = inspect.getsource(live.CoopMenuRuntime._route_native_frontend_input)
        modal = source.index("if modal_active:")
        suppression = source.index("self.custom_ui_suppressed", modal)
        self.assertLess(modal, suppression)
        self.assertIn("NATIVE_FOLLOWER_MODAL_INPUT", source[modal:suppression])

    def test_dialog_confirm_wins_over_the_generic_native_menu_route(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.native_party_menu_active = False
        shell.native_frontend_custom_suspended = False
        shell.native_frontend_observed_open = False
        shell.native_frontend_close_candidate_since = 0.0
        shell.custom_ui_suppressed = False
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.bindings = SimpleNamespace(repeat=lambda *_args: False)
        shell.native_dialog_bridge = SimpleNamespace(pulse=mock.Mock())
        snapshot = SimpleNamespace(
            native_menu_open=True,
            native_modal_active=True,
            user_control_enabled=True,
            loading=False,
            cutscene=False,
            town=False,
        )

        consumed = shell._route_native_frontend_input(
            foreground=True,
            snapshot=snapshot,
            p1_native_command_open=True,
            now=10.0,
            p1=live.PlayerMenuInput(),
            p2=live.PlayerMenuInput(confirm_edge=True),
            p3=live.PlayerMenuInput(),
        )

        self.assertTrue(consumed)
        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_CONFIRM
        )

    def test_follower_dpad_opens_and_cycles_character_selector(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        handlers = inspect.getsource(
            live.CoopMenuRuntime._handle_p2_battle_input
        ) + inspect.getsource(live.CoopMenuRuntime._handle_p3_battle_input)
        for player in ("p2", "p3"):
            self.assertIn(
                f'"{player}", "menu_up", accelerated=True',
                source,
            )
            self.assertIn(
                f'"{player}", "menu_down", accelerated=True',
                source,
            )
            self.assertIn(f"{player}_character_direction_ready", source)
        self.assertIn("self.open_p2_character_selector()", handlers)
        self.assertIn("self.open_p3_character_selector()", handlers)
        self.assertEqual(live.CHARACTER_SELECTOR_EXIT_DELAY, 0.30)
        self.assertLessEqual(live.MENU_REOPEN_QUIET_SECONDS, 0.10)

    def test_triangle_fast_scroll_is_list_only_and_consumes_party_menu_edge(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        self.assertIn('model.mode is MenuMode.LIST', source)
        self.assertIn('self.bindings.down(player, "native_party_menu")', source)
        self.assertIn('native_party_menu_edges[player] = False', source)

    def test_wait_mode_counts_the_third_menu_as_a_command_menu(self):
        source = inspect.getsource(live.CoopMenuRuntime.update_frame)
        self.assertIn(
            "or self.p3_model.mode is not MenuMode.CLOSED",
            source,
        )
        self.assertIn("self.native_wait_mode_bridge.maintain", source)

    def test_source_entrypoint_runs_only_after_all_runtime_helpers_are_defined(self):
        source = inspect.getsource(live)
        helper = source.index("def summon_dismiss_owner(")
        entrypoint = source.index('if __name__ == "__main__":')
        self.assertLess(helper, entrypoint)

    def test_p3_target_visuals_are_character_themed_and_native_first(self):
        sync_source = inspect.getsource(live.CoopMenuRuntime.sync_world_target)
        info_source = inspect.getsource(live.CoopMenuRuntime.paint_p3_target_info)
        self.assertIn("self.native_p3_target_circle.sync", sync_source)
        self.assertIn("self.p3_theme_name", sync_source)
        self.assertIn("theme_name=self.p3_theme_name", info_source)

    def test_three_player_target_circles_do_not_use_the_d3d_fallback(self):
        source = inspect.getsource(live.CoopMenuRuntime.sync_world_target)
        self.assertIn("self.native_p1_target_circle.sync", source)
        self.assertIn("self.native_target_circle.sync", source)
        self.assertIn("self.native_p3_target_circle.sync", source)
        self.assertNotIn("INPROCESS_RING_", source)

    def test_native_circle_capacity_patch_is_owned_and_restored(self):
        init_source = inspect.getsource(live.CoopMenuRuntime._initialize)
        run_source = inspect.getsource(live.CoopMenuRuntime.run)
        cleanup_source = inspect.getsource(live.CoopMenuRuntime._cleanup_after_run)
        self.assertIn("NativeTargetCircleCapacity", init_source)
        self.assertIn(
            "self.native_target_circle_capacity.maintain()",
            init_source,
        )
        self.assertIn(
            'release("native_target_circle_capacity", "restore")',
            cleanup_source,
        )
        self.assertIn("self._cleanup_after_run()", run_source)

    @staticmethod
    def lane_shell(p1_mode, p2_mode, p3_mode):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.p1_model = SimpleNamespace(mode=p1_mode)
        shell.model = SimpleNamespace(mode=p2_mode)
        shell.p3_model = SimpleNamespace(mode=p3_mode)
        shell.p1_help_fade_opacity = 0.0
        shell.p2_help_fade_opacity = 0.0
        shell.p3_help_fade_opacity = 0.0
        shell.p1_target_info_handle = 0
        shell.p1_target_info_exit_started = 0.0
        shell.p2_target_info_presented = False
        shell.target_visual_exit_started = 0.0
        shell.p3_target_info_handle = 0
        shell.p3_target_info_exit_started = 0.0
        shell.p1_help_layout_y = float(live.P1_HELP_Y)
        shell.p2_help_layout_y = float(live.P1_HELP_Y)
        shell.p3_help_layout_y = float(live.P1_HELP_Y)
        shell.p1_target_info_layout_x = float(live.P1_TARGET_INFO_X)
        shell.p2_target_info_layout_x = float(live.P1_TARGET_INFO_X)
        shell.p3_target_info_layout_x = float(live.P1_TARGET_INFO_X)
        shell.p1_target_info_layout_y_offset = 0.0
        shell.p2_target_info_layout_y_offset = 0.0
        shell.p3_target_info_layout_y_offset = 0.0
        shell.info_lane_layout_initialized = False
        shell.info_lane_layout_last_tick = 0.0
        shell.info_help_active_last = (False, False, False)
        shell.info_target_active_last = (False, False, False)
        shell.info_help_target_suppressed = (False, False, False)
        shell.upper_info_layout_offset_y = -float(live.UPPER_INFO_IDLE_LIFT_PX)
        shell.combat_log_on_screen = False
        shell.combat_log_gate_hidden = False
        shell.two_line_message_until = 0.0
        return shell

    def test_mixed_info_lanes_put_p3_help_directly_below_p1(self):
        shell = self.lane_shell(
            live.MenuMode.LIST,
            live.MenuMode.TARGET,
            live.MenuMode.LIST,
        )
        shell.p2_target_info_presented = True
        shell.update_player_info_lanes()
        pitch = live.P2_HELP_COMPACT_Y - live.P1_HELP_Y
        top = (
            live.P2_HELP_TARGET_CLEAR_Y
            + live.INFO_LANE_TOP_PADDING_PX
            - live.UPPER_INFO_IDLE_LIFT_PX
        )
        self.assertEqual(shell.p1_help_layout_y, top)
        self.assertEqual(shell.p3_help_layout_y, top + pitch)
        self.assertEqual(shell.p2_target_info_layout_x, live.P1_TARGET_INFO_X)
        self.assertEqual(shell.p2_target_info_layout_y_offset, 0.0)

    def test_three_target_cards_fill_adjacent_full_size_slots(self):
        shell = self.lane_shell(
            live.MenuMode.TARGET,
            live.MenuMode.TARGET,
            live.MenuMode.TARGET,
        )
        shell.p1_target_info_handle = 0x10000F
        shell.p2_target_info_presented = True
        shell.p3_target_info_handle = 0x200011
        shell.update_player_info_lanes()
        # Three adjacent lanes, evenly spaced by whatever the card currently
        # measures. This used to compare against 390 and 718, which were the
        # positions of a full-size card and went wrong the moment it shrank.
        pitch = live.world_overlay.panel_pitch()
        self.assertEqual(
            (
                shell.p1_target_info_layout_x,
                shell.p2_target_info_layout_x,
                shell.p3_target_info_layout_x,
            ),
            (
                live.P1_TARGET_INFO_X,
                live.P1_TARGET_INFO_X + pitch,
                live.P1_TARGET_INFO_X + 2 * pitch,
            ),
        )
        self.assertEqual(shell.p3_target_info_layout_y_offset, 0.0)

    def test_no_available_targets_does_not_push_other_help_lanes_down(self):
        shell = self.lane_shell(
            live.MenuMode.LIST,
            live.MenuMode.LIST,
            live.MenuMode.TARGET,
        )
        # P3 is targeting, but its target handle/card never materialized.
        shell.update_player_info_lanes()
        pitch = live.P2_HELP_COMPACT_Y - live.P1_HELP_Y
        top = (
            live.P1_HELP_Y
            + live.INFO_LANE_TOP_PADDING_PX
            - live.UPPER_INFO_IDLE_LIFT_PX
        )
        self.assertEqual(shell.p1_help_layout_y, top)
        self.assertEqual(shell.p2_help_layout_y, top + pitch)

    def test_battle_info_waits_for_its_target_tail_then_uses_final_log_lane(self):
        shell = self.lane_shell(
            live.MenuMode.LIST,
            live.MenuMode.CLOSED,
            live.MenuMode.CLOSED,
        )
        shell.p1_target_info_handle = 0x10000F
        shell.combat_log_on_screen = False
        shell.combat_log_gate_hidden = True

        shell.update_player_info_lanes()

        self.assertTrue(shell.info_help_target_suppressed[0])
        self.assertFalse(shell.info_help_active_last[0])

        shell.p1_target_info_handle = 0
        shell.info_lane_layout_last_tick = 0.0
        shell.update_player_info_lanes()

        self.assertTrue(shell.info_help_target_suppressed[0])
        self.assertFalse(shell.info_help_active_last[0])

        shell.combat_log_gate_hidden = False
        shell.combat_log_on_screen = True
        shell.info_lane_layout_last_tick = 0.0
        shell.update_player_info_lanes()
        expected = (
            live.P1_HELP_Y
            + live.INFO_LANE_TOP_PADDING_PX
            + live.COMBAT_LOG_CLEARANCE_PX
            + live.COMBAT_LOG_ACTIVE_INFO_NUDGE_PX
        )

        self.assertFalse(shell.info_help_target_suppressed[0])
        self.assertTrue(shell.info_help_active_last[0])
        self.assertEqual(shell.p1_help_layout_y, expected)

    def test_selecting_the_current_p3_swaps_followers_without_erasing_p3(self):
        source = inspect.getsource(live.CoopMenuRuntime.select_coop_character)
        self.assertIn("if p3_member_id == member_id:", source)
        self.assertIn(
            "p3_member_id = p2_member_id",
            source,
        )
        self.assertIn(
            "replacement_identity = session_state.SessionIdentity(\n"
            "            p1_reserved_member_id,\n"
            "            p2_member_id,\n"
            "            p3_member_id,",
            source,
        )

    def test_p3_character_selector_survives_forced_catalog_refresh(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.p3_character_selector_active = mock.Mock(return_value=True)
        shell.p3_runtime_catalog = mock.Mock()

        shell.refresh_p3_runtime_catalog(force=True)

        shell.p3_runtime_catalog.read_state.assert_not_called()

    def test_runtime_catalog_cycle_reads_one_shared_snapshot(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.party = [
            ("Vaan", 0x100000, 0xA000),
            ("Basch", 0x100003, 0xB000),
            ("Penelo", 0x100005, 0xC000),
        ]
        shell.process = mock.Mock()
        shell.runtime_catalog = mock.Mock()
        shared = mock.sentinel.shared_catalog
        shell.runtime_catalog.read_shared_state.return_value = shared
        shell.last_runtime_catalog_shared_refresh = 0.0
        shell.refresh_runtime_catalog = mock.Mock()
        shell.refresh_p1_runtime_catalog = mock.Mock()
        shell.refresh_p3_runtime_catalog = mock.Mock()
        snapshot = self.party_menu_snapshot()

        with (
            mock.patch.object(live.time, "monotonic", return_value=10.0),
            mock.patch.object(live, "p3_session_menu_allowed", return_value=True),
        ):
            shell.refresh_runtime_catalogs(snapshot)

        shell.runtime_catalog.read_shared_state.assert_called_once_with(shell.process)
        expected = dict(
            force=False,
            shared_state=shared,
            now=10.0,
            summon_active=False,
        )
        shell.refresh_runtime_catalog.assert_called_once_with(**expected)
        shell.refresh_p1_runtime_catalog.assert_called_once_with(**expected)
        shell.refresh_p3_runtime_catalog.assert_called_once_with(**expected)

        with (
            mock.patch.object(live.time, "monotonic", return_value=10.1),
            mock.patch.object(live, "p3_session_menu_allowed", return_value=True),
        ):
            shell.refresh_runtime_catalogs(snapshot)

        shell.runtime_catalog.read_shared_state.assert_called_once()

    def test_item_rasters_warm_one_player_per_idle_step(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.menu_raster_warm_pending = {1, 2, 3}
        shell.menu_raster_warm_after = 0.0
        shell.menu_raster_warm_last = 0.0
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        presenters = [mock.Mock() for _ in range(3)]
        for presenter in presenters:
            presenter.prewarm_page.return_value = True
        shell.p1_controller = SimpleNamespace(presenter=presenters[0])
        shell.controller = SimpleNamespace(presenter=presenters[1])
        shell.p3_controller = SimpleNamespace(presenter=presenters[2])
        shell.p1_transition_kind = None
        shell.transition_kind = None
        shell.p3_transition_kind = None
        shell.custom_ui_suppressed = False
        shell.native_frontend_custom_suspended = False
        shell.dead_leader_menu_gate = SimpleNamespace(pending=False)
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 3, 5)
        )
        shell.party = [("Vaan", 1, 2), ("Basch", 3, 4), ("Penelo", 5, 6)]
        shell.three_player_layout = mock.Mock(return_value=True)
        snapshot = SimpleNamespace(
            gameplay_ready=True,
            loading=False,
            cutscene=False,
            native_menu_open=False,
            native_modal_active=False,
        )

        shell.maintain_menu_raster_warmup(snapshot, now=1.0)
        shell.maintain_menu_raster_warmup(snapshot, now=1.005)

        presenters[0].prewarm_page.assert_called_once_with(
            "items",
            player=1,
            compose_layers=False,
        )
        presenters[1].prewarm_page.assert_not_called()
        presenters[2].prewarm_page.assert_not_called()
        self.assertEqual(shell.menu_raster_warm_pending, {2, 3})

        shell.maintain_menu_raster_warmup(snapshot, now=1.02)
        presenters[1].prewarm_page.assert_called_once_with(
            "items",
            player=2,
            compose_layers=False,
        )
        presenters[2].prewarm_page.assert_not_called()

        shell.maintain_menu_raster_warmup(snapshot, now=1.04)
        presenters[2].prewarm_page.assert_called_once_with(
            "items",
            player=3,
            compose_layers=False,
        )

    def test_item_raster_warmup_waits_while_any_menu_is_open(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.menu_raster_warm_pending = {1}
        shell.menu_raster_warm_after = 0.0
        shell.menu_raster_warm_last = 0.0
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.model = SimpleNamespace(mode=live.MenuMode.LIST)
        shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        presenter = mock.Mock()
        shell.p1_controller = SimpleNamespace(presenter=presenter)
        shell.controller = SimpleNamespace(presenter=mock.Mock())
        shell.p3_controller = SimpleNamespace(presenter=mock.Mock())
        shell.p1_transition_kind = None
        shell.transition_kind = None
        shell.p3_transition_kind = None
        shell.custom_ui_suppressed = False
        shell.native_frontend_custom_suspended = False
        shell.dead_leader_menu_gate = SimpleNamespace(pending=False)
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 3, 5)
        )
        shell.party = [("Vaan", 1, 2), ("Basch", 3, 4), ("Penelo", 5, 6)]
        snapshot = SimpleNamespace(
            gameplay_ready=True,
            loading=False,
            cutscene=False,
            native_menu_open=False,
            native_modal_active=False,
        )

        shell.maintain_menu_raster_warmup(snapshot, now=1.0)

        presenter.prewarm_page.assert_not_called()
        self.assertEqual(shell.menu_raster_warm_pending, {1})

    def test_p3_character_selector_confirm_reaches_its_submitter(self):
        model = live.P2MenuModel(0x100003, live.make_demo_catalog())
        model.stack.clear()
        model.pending_action = live.P2_CHANGE_CHARACTER_ACTION
        model.target_group = live.TargetGroup.PARTY
        model.target_index = 0
        model.mode = live.MenuMode.TARGET
        model.set_candidates(
            (live.TargetCandidate(0x100006, "Penelo", live.TargetGroup.PARTY),)
        )
        submitter = mock.Mock(return_value=1)
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.p3_model = model
        shell.p3_controller = live.HeadlessFrontendController(
            model,
            target_provider=lambda _model: (),
            submitter=submitter,
        )
        shell.p3_runtime_catalog = mock.Mock()
        shell.role_generation = 4
        shell.party = [
            ("Vaan", 0x100001, 0xA000),
            ("Fran", 0x100003, 0xB000),
            ("Balthier", 0x100004, 0xC000),
        ]
        shell.session_snapshot = self.party_menu_snapshot(p2_menu_allowed=True)
        shell.capture_p3_for_close = mock.Mock()
        shell.clear_p3_target_visual_immediately = mock.Mock()
        shell.begin_p3_closing = mock.Mock()
        shell.pending_native_sounds = []

        shell.dispatch_p3(live.InputCommand.CONFIRM)

        submitter.assert_called_once()
        request = submitter.call_args.args[0]
        self.assertEqual(request.action_id, live.P2_CHANGE_CHARACTER_ACTION_ID)
        self.assertEqual(request.target, 0x100006)

    def test_simultaneous_character_selectors_keep_their_explicit_owner(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.process = mock.Mock()
        shell.model = SimpleNamespace(issuer=0x100003)
        shell.p3_model = SimpleNamespace(issuer=0x100004)
        shell.party = [
            ("P1", 0x100001, 0xA000),
            ("P2", 0x100003, 0xB000),
            ("P3", 0x100004, 0xC000),
        ]
        shell.select_coop_character = mock.Mock(return_value=1)
        p2_request = SimpleNamespace(
            issuer=shell.model.issuer,
            target=0x100005,
            action_id=live.P2_CHANGE_CHARACTER_ACTION_ID,
        )
        p3_request = SimpleNamespace(
            issuer=shell.p3_model.issuer,
            target=0x100001,
            action_id=live.P2_CHANGE_CHARACTER_ACTION_ID,
        )

        with mock.patch.object(
            live, "leader_control_availability", return_value=True
        ):
            self.assertEqual(shell.submit_p2_request(p2_request), 1)
            self.assertEqual(shell.submit_p3_request(p3_request), 1)

        self.assertEqual(
            shell.select_coop_character.call_args_list,
            [mock.call(2, 0x100005), mock.call(3, 0x100001)],
        )

    def test_character_selector_rejects_a_stale_or_wrong_role_issuer(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.p1_model = SimpleNamespace(issuer=0x100001)
        shell.model = SimpleNamespace(issuer=0x100003)
        shell.p3_model = SimpleNamespace(issuer=0x100004)
        shell.select_coop_character = mock.Mock(return_value=1)
        stale_request = SimpleNamespace(
            issuer=0x100099,
            target=0x100005,
            action_id=live.P2_CHANGE_CHARACTER_ACTION_ID,
        )

        self.assertEqual(shell.submit_p2_request(stale_request), 0)
        self.assertEqual(shell.submit_p3_request(stale_request), 0)
        self.assertEqual(shell.submit_p1_request(stale_request), 0)
        shell.select_coop_character.assert_not_called()

    def test_external_identity_reload_is_a_catalog_generation_barrier(self):
        old_identity = session_state.SessionIdentity(0, 5, 1)
        new_identity = session_state.SessionIdentity(0, 2, 3)
        snapshot = SimpleNamespace(
            identity=new_identity,
            leader_party_member_id=0,
            current_layout=(0, 2, 3, session_state.EMPTY_PARTY_ID),
            ui_layout=(0, 2, 3, session_state.EMPTY_PARTY_ID),
        )
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        # refresh_actors has already published the externally reloaded identity;
        # only the separate catalog owner can still expose the change here.
        shell.session_snapshot = snapshot
        shell.runtime_catalog_identity = old_identity
        shell.session = SimpleNamespace(snapshot=mock.Mock(return_value=snapshot))
        shell.process = mock.Mock()
        shell.materialized_party_count = 3
        shell.pending_party_identity = None
        shell.pending_party_identity_since = 0.0
        shell.party_identity_save_retry_at = 0.0
        shell.native_wait_mode_bridge = SimpleNamespace(
            owns_native_presentation=mock.Mock(return_value=False),
        )
        shell.role_generation = 11
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.install_runtime_catalog_identity = mock.Mock()
        shell.last_actor_refresh = 9.0

        with (
            mock.patch.object(
                live.legacy.coop,
                "read_native_leader_role_transfer",
                return_value=None,
            ),
            mock.patch.object(
                live,
                "transient_native_leader_handoff_active",
                return_value=False,
            ),
            mock.patch.object(
                session_state,
                "replacement_identity_for_live_party",
                return_value=None,
            ),
            mock.patch.object(
                session_state,
                "mask_owned_command_shadow",
                side_effect=lambda value, **_kwargs: value,
            ),
        ):
            result = shell.refresh_session_state()

        self.assertIs(result, snapshot)
        self.assertEqual(shell.role_generation, 12)
        shell.hard_close_p1_menu.assert_called_once_with()
        shell.hard_close_p2_menu.assert_called_once_with()
        shell.hard_close_p3_menu.assert_called_once_with()
        shell.install_runtime_catalog_identity.assert_called_once_with(
            new_identity,
            p1_owner_member_id=0,
        )
        self.assertEqual(shell.last_actor_refresh, 0.0)

    def test_dead_p1_promotion_never_persists_the_followers_as_new_roles(self):
        identity = session_state.SessionIdentity(4, 2, 1)
        snapshot = SimpleNamespace(
            identity=identity,
            leader_party_member_id=2,
            current_layout=(4, 2, 1, session_state.EMPTY_PARTY_ID),
            ui_layout=(4, 2, 1, session_state.EMPTY_PARTY_ID),
        )
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.session_snapshot = snapshot
        shell.runtime_catalog_identity = identity
        shell.session = SimpleNamespace(snapshot=mock.Mock(return_value=snapshot))
        shell.process = mock.Mock()
        shell.materialized_party_count = 3
        shell.active_party = [(4, "Basch", 0x100004, 0xABC000)]
        shell.pending_party_identity = None
        shell.pending_party_identity_since = 0.0
        shell.party_identity_save_retry_at = 0.0
        shell.native_wait_mode_bridge = SimpleNamespace(
            owns_native_presentation=mock.Mock(return_value=False),
        )
        shell.role_generation = 3

        with (
            mock.patch.object(
                live.legacy.coop,
                "read_native_leader_role_transfer",
                return_value=None,
            ),
            mock.patch.object(
                live,
                "transient_native_leader_handoff_active",
                return_value=False,
            ),
            mock.patch.object(
                live,
                "leader_control_availability",
                return_value=False,
            ),
            mock.patch.object(
                session_state,
                "replacement_identity_for_live_party",
            ) as replacement,
            mock.patch.object(
                session_state,
                "mask_owned_command_shadow",
                side_effect=lambda value, **_kwargs: value,
            ),
        ):
            result = shell.refresh_session_state()

        self.assertIs(result, snapshot)
        replacement.assert_not_called()
        self.assertIsNone(shell.pending_party_identity)

    def test_guest_owner_change_is_a_p1_catalog_generation_barrier(self):
        identity = session_state.SessionIdentity(0, 2, 1)
        snapshot = SimpleNamespace(
            identity=identity,
            p1_owner_party_member_id=13,
            leader_party_member_id=13,
            current_layout=(0, 2, 1, 13),
            ui_layout=(0, 2, 1, 13),
        )
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.session_snapshot = snapshot
        shell.runtime_catalog_identity = identity
        shell.runtime_catalog_p1_owner = 0
        shell.session = SimpleNamespace(snapshot=mock.Mock(return_value=snapshot))
        shell.process = mock.Mock()
        shell.active_party = []
        shell.materialized_party_count = 4
        shell.pending_party_identity = None
        shell.pending_party_identity_since = 0.0
        shell.party_identity_save_retry_at = 0.0
        shell.native_wait_mode_bridge = SimpleNamespace(
            owns_native_presentation=mock.Mock(return_value=False),
        )
        shell.role_generation = 4
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.install_runtime_catalog_identity = mock.Mock()
        shell.last_actor_refresh = 9.0

        with (
            mock.patch.object(
                live.legacy.coop,
                "read_native_leader_role_transfer",
                return_value=None,
            ),
            mock.patch.object(
                live,
                "transient_native_leader_handoff_active",
                return_value=False,
            ),
            mock.patch.object(
                session_state,
                "replacement_identity_for_live_party",
                return_value=None,
            ),
            mock.patch.object(
                session_state,
                "mask_owned_command_shadow",
                side_effect=lambda value, **_kwargs: value,
            ),
        ):
            result = shell.refresh_session_state()

        self.assertIs(result, snapshot)
        self.assertEqual(shell.role_generation, 5)
        shell.install_runtime_catalog_identity.assert_called_once_with(
            identity,
            p1_owner_member_id=13,
        )
        self.assertEqual(shell.last_actor_refresh, 0.0)

    def test_p3_role_swap_is_a_generation_barrier_for_p2_confirm(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        old_identity = session_state.SessionIdentity(0, 5, 3)
        selected_actor = 0xABC000
        selected_handle = 0x100006
        battle_keep = 0xDEF000
        owner_actor = 0xABD000
        owner_handle = 0x100003
        owner_battle_keep = 0xDEE000
        shell.session_snapshot = SimpleNamespace(
            identity=old_identity,
            p1_owner_party_member_id=0,
        )
        shell.active_party = [
            (5, "Penelo", selected_handle, selected_actor),
            (3, "Balthier", owner_handle, owner_actor),
        ]
        shell.process = mock.Mock()
        shell.process.u64.side_effect = lambda address: {
            selected_actor + 0x698: battle_keep,
            owner_actor + 0x698: owner_battle_keep,
            battle_keep + 0x64: 0,
            owner_battle_keep + 0x64: 0,
            selected_actor: 0,
            owner_actor: 0,
        }.get(address, 0)
        shell.process.u32.side_effect = lambda address: {
            battle_keep + 0x48: 100,
            owner_battle_keep + 0x48: 100,
        }.get(address, 0)
        shell.session = SimpleNamespace(
            path="session.json",
            reload=mock.Mock(),
        )
        shell.action_catalog = live.NativeActionCatalog()
        shell.hard_close_p1_menu = mock.Mock()
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        shell.role_generation = 7
        shell.last_actor_refresh = 1.0
        shell.pending_combat_indicators = {
            1: live.PendingCombatIndicator(
                source=1,
                target=2,
                origin="menu",
                node=3,
                next_try=4.0,
                attempt_started=5.0,
                deadline=6.0,
                attempts=7,
            )
        }
        shell.observed_native_actions = {2: (1, 2, 3, 4)}
        shell.observed_native_action_states = {2: 2}
        p2_confirm = mock.Mock()

        with mock.patch.object(session_state, "save_identity") as save:
            input_generation = shell.role_generation
            self.assertEqual(
                shell.select_coop_character(3, selected_handle),
                1,
            )
            if shell.role_generation == input_generation:
                p2_confirm()

        replacement = session_state.SessionIdentity(0, 3, 5)
        save.assert_called_once_with(replacement, "session.json")
        shell.session.reload.assert_called_once_with(force=True)
        shell.hard_close_p1_menu.assert_called_once_with()
        shell.hard_close_p2_menu.assert_called_once_with()
        shell.hard_close_p3_menu.assert_called_once_with()
        self.assertEqual(shell.role_generation, input_generation + 1)
        p2_confirm.assert_not_called()
        self.assertEqual(shell.p1_runtime_catalog.party_member_id, 0)
        self.assertEqual(shell.runtime_catalog.party_member_id, 3)
        self.assertEqual(shell.p3_runtime_catalog.party_member_id, 5)
        self.assertEqual(shell.pending_combat_indicators, {})
        self.assertEqual(shell.observed_native_actions, {})
        self.assertEqual(shell.observed_native_action_states, {})

    def test_input_checks_role_generation_before_later_player_dispatch(self):
        source = inspect.getsource(live.CoopMenuRuntime.input)
        p3_handler = inspect.getsource(live.CoopMenuRuntime._handle_p3_battle_input)
        p3_dispatch = p3_handler.index("self.dispatch_p3(InputCommand.CONFIRM)")
        generation_check = p3_handler.index(
            "if self.role_generation != input_role_generation:", p3_dispatch
        )
        self.assertLess(p3_dispatch, generation_check)
        self.assertLess(
            source.index("self._handle_p3_battle_input("),
            source.index("self._handle_p2_battle_input("),
        )

    def test_stable_p1_is_an_exchange_candidate_but_guests_never_are(self):
        for p1_member_id in session_state.MAIN_PARTY_MEMBER_IDS:
            for member_id in range(15):
                with self.subTest(
                    p1_member_id=p1_member_id,
                    member_id=member_id,
                ):
                    self.assertEqual(
                        live.coop_character_role_candidate(
                            member_id,
                            p1_member_id,
                            p1_member_id,
                        ),
                        member_id in session_state.MAIN_PARTY_MEMBER_IDS,
                    )

    def test_transient_p1_owners_are_not_exchange_candidates(self):
        for member_id in range(15):
            with self.subTest(member_id=member_id):
                self.assertEqual(
                    live.coop_character_role_candidate(member_id, 3, 0),
                    member_id in session_state.MAIN_PARTY_MEMBER_IDS
                    and member_id not in (0, 3),
                )

    def test_p2_selecting_p1_routes_one_atomic_owner_exchange(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        p1_actor = 0xABC000
        p2_actor = 0xABD000
        p1_keep = 0xDEF000
        p2_keep = 0xDEE000
        p1_handle = 0x100000
        p2_handle = 0x100005
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 5, 1),
            p1_owner_party_member_id=0,
            p1_reserved_party_member_id=0,
        )
        shell.active_party = [
            (0, "Vaan", p1_handle, p1_actor),
            (5, "Penelo", p2_handle, p2_actor),
        ]
        shell.process = mock.Mock()
        shell.process.u64.side_effect = lambda address: {
            p1_actor + 0x698: p1_keep,
            p2_actor + 0x698: p2_keep,
            p1_actor: 0,
            p2_actor: 0,
        }.get(address, 0)
        shell.process.u32.side_effect = lambda address: {
            p1_keep + 0x48: 100,
            p2_keep + 0x48: 100,
        }.get(address, 0)
        shell.select_p1_leader = mock.Mock(return_value=1)

        with mock.patch.object(session_state, "save_identity") as save:
            self.assertEqual(shell.select_coop_character(2, p1_handle), 1)

        shell.select_p1_leader.assert_called_once_with(p2_handle)
        save.assert_not_called()

    def test_p3_selecting_p1_routes_one_atomic_owner_exchange(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        p1_actor = 0xABC000
        p3_actor = 0xABE000
        p1_keep = 0xDEF000
        p3_keep = 0xDED000
        p1_handle = 0x100000
        p3_handle = 0x100001
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 5, 1),
            p1_owner_party_member_id=0,
            p1_reserved_party_member_id=0,
        )
        shell.active_party = [
            (0, "Vaan", p1_handle, p1_actor),
            (1, "Ashe", p3_handle, p3_actor),
        ]
        shell.process = mock.Mock()
        shell.process.u64.side_effect = lambda address: {
            p1_actor + 0x698: p1_keep,
            p3_actor + 0x698: p3_keep,
            p1_actor: 0,
            p3_actor: 0,
        }.get(address, 0)
        shell.process.u32.side_effect = lambda address: {
            p1_keep + 0x48: 100,
            p3_keep + 0x48: 100,
        }.get(address, 0)
        shell.select_p1_leader = mock.Mock(return_value=1)

        with mock.patch.object(session_state, "save_identity") as save:
            self.assertEqual(shell.select_coop_character(3, p1_handle), 1)

        shell.select_p1_leader.assert_called_once_with(p3_handle)
        save.assert_not_called()

    def test_character_commit_rejects_guest_before_actor_or_config_access(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 5, 1),
            p1_owner_party_member_id=0,
        )
        shell.active_party = [(11, "Vossler", 0x1234, 0xABC000)]
        shell.process = mock.Mock()
        shell.session = SimpleNamespace(path="must-not-be-used")

        with mock.patch.object(session_state, "save_identity") as save:
            self.assertEqual(
                shell.select_coop_character(2, 0x1234),
                0,
            )

        save.assert_not_called()
        shell.process.u64.assert_not_called()

    def test_character_commit_rejects_a_dead_follower_owner(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        owner_actor = 0xABC000
        target_actor = 0xABD000
        owner_keep = 0xDEF000
        target_keep = 0xDEE000
        shell.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 5, 3),
            p1_owner_party_member_id=0,
        )
        shell.active_party = [
            (3, "Balthier", 0x100003, owner_actor),
            (5, "Penelo", 0x100005, target_actor),
        ]
        shell.process = mock.Mock()
        shell.process.u64.side_effect = lambda address: {
            owner_actor + 0x698: owner_keep,
            target_actor + 0x698: target_keep,
            owner_keep + 0x64: 0,
            target_keep + 0x64: 0,
        }.get(address, 0)
        shell.process.u32.side_effect = lambda address: {
            owner_keep + 0x48: 0,
            target_keep + 0x48: 100,
        }.get(address, 0)
        shell.session = SimpleNamespace(path="must-not-be-used")

        with mock.patch.object(session_state, "save_identity") as save:
            self.assertEqual(
                shell.select_coop_character(3, 0x100005),
                0,
            )

        save.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
