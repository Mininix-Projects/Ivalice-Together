import struct
import time
import unittest
from types import SimpleNamespace

import session_state
from headless_menu_model import MenuMode
from headless_menu_shell import (
    NATIVE_DIALOG_CANCEL,
    NATIVE_DIALOG_COMMAND,
    NATIVE_DIALOG_CONFIRM,
    NATIVE_DIALOG_PARTY_MENU,
    NATIVE_DIALOG_PAUSE,
    NATIVE_DIALOG_CAVE_OFFSET,
    NATIVE_DIALOG_STATE_OFFSET,
    NATIVE_DIALOG_P1_SLOT_OFFSET,
    NATIVE_DECIDE_BUTTON_CIRCLE,
    NATIVE_DECIDE_BUTTON_CROSS,
    NATIVE_DECIDE_BUTTON_OFFSET,
    NATIVE_GAME_SETTINGS_GLOBAL,
    NATIVE_GET_BUTTON_ORIGINAL,
    NATIVE_INPUT_COMMAND_MASK,
    NATIVE_INPUT_CONFIRM_MASK,
    NATIVE_INPUT_CUSTOM_DIRECTION_MASK,
    NATIVE_INPUT_LB_MASK,
    NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
    NATIVE_INPUT_PARTY_MASK,
    NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
    NATIVE_INPUT_POLICY_SLOT_SIZE,
    NATIVE_INPUT_POLICY_SUPPRESS_WHEEL,
    P2_CONTROLLED_ACTOR_OFFSET,
    QUICKENING_BLOCK_COMMAND,
    QUICKENING_BLOCK_CONFIRM,
    DirectionalReleaseGuard,
    NativeDialogInputBridge,
    NativePauseVisibilityGate,
    native_input_policy,
    custom_directional_bridge_enabled,
    custom_directional_control_down,
    custom_directional_input_owned,
    p1_command_mask_enabled,
    p1_command_mask_requested,
    p1_frontend_close_handoff_active,
    p1_menu_input_owned,
    p1_menu_owns_lb,
    p1_native_party_request_blocked,
    p1_pause_suspension_required,
    native_decide_button,
    native_pause_request_allowed,
)


class FakeProcess:
    def __init__(self):
        self.writes = []

    def write(self, address, data):
        self.writes.append((address, bytes(data)))


class NativeDialogBridgeStateTests(unittest.TestCase):
    def test_p1_native_slot_is_published_independently(self):
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)

        bridge.set_p1_controller(3)

        self.assertEqual(
            process.writes[0],
            (
                bridge.state + NATIVE_DIALOG_P1_SLOT_OFFSET,
                struct.pack("<I", 3),
            ),
        )
        policy_address, packed_policy = process.writes[1]
        self.assertEqual(
            policy_address,
            bridge.input_state + NATIVE_INPUT_POLICY_SLOT_SIZE,
        )
        self.assertEqual(struct.unpack("<4I", packed_policy)[2], 3)
        self.assertEqual(
            process.writes[2],
            (
                bridge.input_state + NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
                struct.pack("<I", 3),
            ),
        )

    def test_button_and_axis_caves_do_not_assume_xinput_slot_zero(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        fixed_slot_zero = b"\x83\xb9\x58\x04\x00\x00\x00"

        button_cave = bridge._build_cave()
        axis_cave = bridge._build_axis_cave()
        for cave in (button_cave, axis_cave):
            self.assertNotIn(fixed_slot_zero, cave)
        self.assertIn(b"\x39\x81\x58\x04\x00\x00", button_cave)
        self.assertIn(b"\x44\x39\x99\x58\x04\x00\x00", axis_cave)

    def test_button_cave_stays_before_its_state_block(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        self.assertLessEqual(
            len(bridge._build_cave()),
            NATIVE_DIALOG_STATE_OFFSET - NATIVE_DIALOG_CAVE_OFFSET,
        )

    def test_native_decide_button_reads_the_live_game_setting(self):
        class SettingsProcess:
            def u64(self, address):
                self.assert_address = address
                return 0x500000

            def u32(self, address):
                self.value_address = address
                return NATIVE_DECIDE_BUTTON_CIRCLE

        process = SettingsProcess()
        self.assertEqual(
            native_decide_button(process),
            NATIVE_DECIDE_BUTTON_CIRCLE,
        )
        self.assertEqual(process.assert_address, NATIVE_GAME_SETTINGS_GLOBAL)
        self.assertEqual(
            process.value_address,
            0x500000 + NATIVE_DECIDE_BUTTON_OFFSET,
        )

    def test_native_decide_button_keeps_cross_on_invalid_memory(self):
        class MissingSettingsProcess:
            def u64(self, _address):
                return 0

        self.assertEqual(
            native_decide_button(MissingSettingsProcess()),
            NATIVE_DECIDE_BUTTON_CROSS,
        )

    def test_party_menu_mailbox_uses_the_proven_native_y_logical_id(self):
        self.assertEqual(NATIVE_DIALOG_PARTY_MENU, 0x0E)
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)

        bridge.pulse(NATIVE_DIALOG_PARTY_MENU, duration=0.0)

        self.assertEqual(
            process.writes,
            [(bridge.state, struct.pack("<I", 0x0E) + b"\x01")],
        )

    def test_pause_mailbox_uses_the_native_start_logical_id(self):
        self.assertEqual(NATIVE_DIALOG_PAUSE, 0x14)
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)

        bridge.pulse(NATIVE_DIALOG_PAUSE, duration=0.0)

        self.assertEqual(
            process.writes,
            [(bridge.state, struct.pack("<I", 0x14) + b"\x01")],
        )

    def test_pause_request_is_available_in_field_and_town_control_states(self):
        state = dict(
            p1_gameplay_ready=True,
            user_control_enabled=True,
            loading=False,
            cutscene=False,
            native_menu_open=False,
            native_modal_active=False,
        )
        self.assertTrue(native_pause_request_allowed(SimpleNamespace(**state)))
        state["cutscene"] = True
        self.assertFalse(native_pause_request_allowed(SimpleNamespace(**state)))

    def test_directional_guard_waits_for_release_even_after_a_long_hold(self):
        guard = DirectionalReleaseGuard(delay_seconds=0.30)
        self.assertTrue(
            guard.update(
                lifecycle_owned=True,
                directional_down=True,
                now=1.0,
            )
        )
        self.assertTrue(
            guard.update(
                lifecycle_owned=False,
                directional_down=True,
                now=10.0,
            )
        )
        self.assertTrue(
            guard.update(
                lifecycle_owned=False,
                directional_down=False,
                now=10.0,
            )
        )
        self.assertFalse(
            guard.update(
                lifecycle_owned=False,
                directional_down=False,
                now=10.31,
            )
        )

    def test_direction_observer_includes_p3_targets_and_raw_p1_dpad(self):
        class FakeBindings:
            def __init__(self, action_down=None, control_down=None):
                self.action_down = action_down
                self.control_down = control_down

            def down(self, player, action):
                return (player, action) == self.action_down

            def xinput_control_down(self, player, control):
                return (player, control) == self.control_down

        self.assertTrue(
            custom_directional_control_down(
                FakeBindings(action_down=("p3", "target_previous"))
            )
        )
        self.assertTrue(
            custom_directional_control_down(
                FakeBindings(control_down=("p1", "DPAD_LEFT"))
            )
        )
        self.assertFalse(custom_directional_control_down(FakeBindings()))

    def test_p2_target_screen_owns_the_global_native_directions(self):
        self.assertTrue(
            custom_directional_input_owned(
                MenuMode.CLOSED,
                MenuMode.TARGET,
                MenuMode.CLOSED,
            )
        )

    def test_p3_menu_owns_the_global_native_directions(self):
        self.assertTrue(
            custom_directional_input_owned(
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                MenuMode.LIST,
            )
        )

    def test_all_closed_menus_leave_the_native_leader_selector_available(self):
        self.assertFalse(
            custom_directional_input_owned(
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                MenuMode.CLOSED,
            )
        )

    def test_close_transition_retains_directional_ownership(self):
        self.assertTrue(
            custom_directional_input_owned(
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                p2_transition_kind="closing",
            )
        )

    def test_post_close_release_guard_retains_directional_ownership(self):
        self.assertTrue(
            custom_directional_input_owned(
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                p3_release_guard_active=True,
            )
        )

    def test_directional_owner_writes_the_native_hook_state_byte(self):
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)
        bridge.set_custom_directional_owner(True)
        _address, packed_policy = process.writes[0]
        p1_mask, other_mask, _slot, flags = struct.unpack("<4I", packed_policy)
        self.assertEqual(
            p1_mask & NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
            NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
        )
        self.assertFalse(p1_mask & NATIVE_INPUT_LB_MASK)
        self.assertEqual(other_mask & NATIVE_INPUT_CUSTOM_DIRECTION_MASK, NATIVE_INPUT_CUSTOM_DIRECTION_MASK)
        self.assertTrue(flags & NATIVE_INPUT_POLICY_SUPPRESS_WHEEL)
        self.assertEqual(
            process.writes[-1][0],
            bridge.input_state + NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
        )

    def test_directional_owner_is_independent_of_the_p1_f6_route(self):
        self.assertTrue(custom_directional_bridge_enabled(True, False, True))
        self.assertFalse(custom_directional_bridge_enabled(False, False, True))
        self.assertFalse(custom_directional_bridge_enabled(True, True, True))

    def test_only_the_p1_menu_owns_the_native_lb_button(self):
        self.assertTrue(p1_menu_input_owned(MenuMode.LIST, None))
        self.assertTrue(p1_menu_input_owned(MenuMode.CLOSED, "opening"))
        self.assertFalse(p1_menu_input_owned(MenuMode.CLOSED, None))

        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)
        bridge.set_p1_menu_owns_lb(True)
        p1_mask, other_mask, _slot, _flags = struct.unpack(
            "<4I", process.writes[0][1]
        )
        self.assertTrue(p1_mask & NATIVE_INPUT_LB_MASK)
        self.assertFalse(other_mask & NATIVE_INPUT_LB_MASK)

    def test_p1_leader_selector_releases_only_p1_lb_for_native_turbo(self):
        self.assertFalse(
            p1_menu_owns_lb(
                MenuMode.TARGET,
                directional_owner=True,
                leader_selector_active=True,
            )
        )
        policy = native_input_policy(
            p1_replacement=False,
            non_p1_command_blocked=False,
            custom_directional_owner=True,
            p1_menu_owns_lb=False,
            party_menu_blocked=False,
            quickening_block_mask=0,
            leader_replacement=False,
            capture_desktop_events=False,
            p1_native_slot=0,
        )
        self.assertFalse(policy.p1_semantic_mask & NATIVE_INPUT_LB_MASK)
        self.assertEqual(
            policy.p1_semantic_mask & NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
            NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
        )
        self.assertTrue(policy.other_semantic_mask & NATIVE_INPUT_LB_MASK)

    def test_p1_battle_menu_wins_over_a_same_frame_party_request(self):
        self.assertTrue(p1_native_party_request_blocked(MenuMode.CLOSED, None, True))
        self.assertTrue(p1_native_party_request_blocked(MenuMode.LIST, None, False))
        self.assertFalse(p1_native_party_request_blocked(MenuMode.CLOSED, None, False))

    def test_button_cave_masks_all_custom_navigation_before_consulting_f6(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        cave = bridge._build_cave()
        policy = native_input_policy(
            p1_replacement=False,
            non_p1_command_blocked=False,
            custom_directional_owner=True,
            p1_menu_owns_lb=False,
            party_menu_blocked=False,
            quickening_block_mask=0,
            leader_replacement=False,
            capture_desktop_events=False,
            p1_native_slot=0,
        )
        self.assertEqual(
            policy.p1_semantic_mask & NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
            NATIVE_INPUT_P1_CUSTOM_DIRECTION_MASK,
        )
        self.assertEqual(
            policy.other_semantic_mask & NATIVE_INPUT_CUSTOM_DIRECTION_MASK,
            NATIVE_INPUT_CUSTOM_DIRECTION_MASK,
        )
        # P1 LB has an independent owner so the Leader selector can preserve
        # native Turbo. Followers still own LB with the remaining navigation.
        self.assertFalse(policy.p1_semantic_mask & NATIVE_INPUT_LB_MASK)
        self.assertTrue(policy.other_semantic_mask & NATIVE_INPUT_LB_MASK)
        self.assertFalse(policy.p1_semantic_mask & NATIVE_INPUT_CONFIRM_MASK)
        self.assertIn(b"\x41\x0f\xa3\xc0", cave)
        self.assertIn(NATIVE_GET_BUTTON_ORIGINAL, cave)

    def test_pause_visibility_survives_pointer_dropout_and_focus_resume(self):
        gate = NativePauseVisibilityGate(
            pointer_dropout_seconds=0.30,
            focus_resume_seconds=0.50,
        )
        self.assertTrue(gate.update(True, foreground=True, now=1.0))
        self.assertTrue(gate.update(False, foreground=True, now=1.20))
        self.assertTrue(gate.update(False, foreground=False, now=4.0))
        self.assertTrue(gate.update(False, foreground=True, now=8.0))
        self.assertTrue(gate.update(False, foreground=True, now=8.49))
        self.assertFalse(gate.update(False, foreground=True, now=8.51))

    def test_accepted_pause_edge_is_guarded_until_native_object_appears(self):
        gate = NativePauseVisibilityGate(
            pointer_dropout_seconds=0.30,
            focus_resume_seconds=0.50,
        )
        gate.arm(now=2.0, foreground=True)
        self.assertTrue(gate.update(False, foreground=True, now=2.20))
        self.assertFalse(gate.update(False, foreground=True, now=2.31))

    def test_f6_masks_stock_x_a_and_b_command_buttons(self):
        policy = native_input_policy(
            p1_replacement=True,
            non_p1_command_blocked=False,
            custom_directional_owner=False,
            p1_menu_owns_lb=False,
            party_menu_blocked=False,
            quickening_block_mask=0,
            leader_replacement=False,
            capture_desktop_events=False,
            p1_native_slot=0,
        )
        expected = sum(
            1 << (button - NATIVE_DIALOG_COMMAND)
            for button in (
                NATIVE_DIALOG_COMMAND,
                NATIVE_DIALOG_CONFIRM,
                NATIVE_DIALOG_CANCEL,
            )
        )
        self.assertEqual(policy.p1_semantic_mask & expected, expected)

    def test_non_p1_command_path_blocks_mouse_without_masking_other_actions(self):
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)
        bridge.set_non_p1_command_blocked(True)
        p1_mask, other_mask, _slot, _flags = struct.unpack(
            "<4I", process.writes[0][1]
        )
        self.assertFalse(p1_mask & NATIVE_INPUT_COMMAND_MASK)
        self.assertTrue(other_mask & NATIVE_INPUT_COMMAND_MASK)
        self.assertEqual(other_mask & ~NATIVE_INPUT_COMMAND_MASK, 0)

        cave = bridge._build_cave()
        self.assertIn(
            b"\x83\xfa" + bytes((NATIVE_DIALOG_COMMAND,)),
            cave,
        )
        self.assertIn(b"\x41\xb8\x02\x00\x00\x00", cave)

    def test_custom_menu_blocks_physical_y_in_its_independent_state_byte(self):
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)

        bridge.set_party_menu_blocked(True)

        p1_mask, other_mask, _slot, _flags = struct.unpack(
            "<4I", process.writes[0][1]
        )
        self.assertTrue(p1_mask & NATIVE_INPUT_PARTY_MASK)
        self.assertTrue(other_mask & NATIVE_INPUT_PARTY_MASK)
        cave = bridge._build_cave()
        # Mailbox matching precedes the physical-Y block, so a deferred
        # synthetic pulse can cross the immutable policy mask after Wait closes.
        self.assertLess(
            cave.index(b"\x3b\x10"),
            cave.index(b"\x41\x0f\xa3\xc0"),
        )

    def test_quickening_filter_has_independent_state_and_keeps_pulses_first(self):
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)
        mask = QUICKENING_BLOCK_COMMAND | QUICKENING_BLOCK_CONFIRM

        bridge.set_quickening_block_mask(mask)

        p1_mask, other_mask, _slot, _flags = struct.unpack(
            "<4I", process.writes[0][1]
        )
        expected = NATIVE_INPUT_COMMAND_MASK | NATIVE_INPUT_CONFIRM_MASK
        self.assertEqual(p1_mask & expected, expected)
        self.assertEqual(other_mask & expected, expected)
        cave = bridge._build_cave()
        self.assertLess(cave.index(b"\x3b\x10"), cave.index(b"\x41\x0f\xa3\xc0"))

    def test_button_policy_has_no_pause_or_field_control_bypass(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        cave = bridge._build_cave()
        self.assertNotIn(b"\x48\xa1" + struct.pack("<Q", 0x0209AD28), cave)
        self.assertNotIn(
            b"\x48\xa1" + struct.pack("<Q", session_state.USER_CONTROL_GLOBAL),
            cave,
        )

    def test_pulse_release_does_not_clear_persistent_f6_ownership(self):
        process = FakeProcess()
        bridge = NativeDialogInputBridge(process, 0x500000)
        bridge.release_at = time.perf_counter() - 1.0
        bridge.maintain()
        self.assertEqual(process.writes, [(bridge.state + 4, b"\x00")])
        self.assertEqual(bridge.release_at, 0.0)

    def test_f6_mask_survives_loading_and_cutscene_style_snapshots(self):
        self.assertTrue(
            p1_command_mask_enabled(
                True,
                native_menu_open=False,
                native_command_open=False,
            )
        )

    def test_ucon_never_bypasses_button_or_axis_policy(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        user_control_load = b"\x48\xa1" + struct.pack(
            "<Q", session_state.USER_CONTROL_GLOBAL
        )
        enabled_test = b"\x80\x78" + bytes(
            (session_state.USER_CONTROL_ENABLED_OFFSET, 0x00)
        )

        button_cave = bridge._build_cave()
        self.assertNotIn(user_control_load, button_cave)
        self.assertNotIn(enabled_test, button_cave)
        axis_cave = bridge._build_axis_cave()
        self.assertNotIn(user_control_load, axis_cave)
        self.assertNotIn(enabled_test, axis_cave)
        self.assertIn(
            b"\x48\xa1" + struct.pack("<Q", 0x0209AD28),
            axis_cave,
        )

    def test_game_thread_handoff_keeps_mask_armed_while_ucoff_snapshot_lags(self):
        self.assertTrue(
            p1_command_mask_requested(
                True,
                user_control_enabled=False,
                native_command_open=False,
                close_guard_active=False,
                native_pause_open=False,
                native_modal_active=False,
                game_thread_handoff=True,
            )
        )

    def test_game_thread_handoff_never_outranks_proven_native_owner(self):
        common = dict(
            replacement_enabled=True,
            user_control_enabled=False,
            native_command_open=False,
            close_guard_active=False,
            native_pause_open=False,
            native_modal_active=False,
            game_thread_handoff=True,
        )
        for owner in ("native_pause_open", "native_modal_active"):
            values = dict(common)
            values[owner] = True
            with self.subTest(owner=owner):
                self.assertFalse(p1_command_mask_requested(**values))

    def test_deferred_confirm_is_limited_to_coherent_post_frontend_close(self):
        common = dict(
            suspended=True,
            observed_open=True,
            close_candidate_since=10.0,
            native_menu_open=False,
            native_modal_active=False,
            user_control_enabled=True,
            loading=False,
            cutscene=False,
            town=False,
        )
        self.assertTrue(p1_frontend_close_handoff_active(**common))
        for blocked in (
            "native_menu_open",
            "native_modal_active",
            "loading",
            "cutscene",
            "town",
        ):
            values = dict(common)
            values[blocked] = True
            with self.subTest(blocked=blocked):
                self.assertFalse(p1_frontend_close_handoff_active(**values))
        for missing in (
            "suspended",
            "observed_open",
            "user_control_enabled",
        ):
            values = dict(common)
            values[missing] = False
            with self.subTest(missing=missing):
                self.assertFalse(p1_frontend_close_handoff_active(**values))
        values = dict(common)
        values["close_candidate_since"] = 0.0
        self.assertFalse(p1_frontend_close_handoff_active(**values))

    def test_f6_mask_releases_only_for_real_fullscreen_menu(self):
        self.assertFalse(
            p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=False,
            )
        )
        self.assertTrue(
            p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=True,
            )
        )

    def test_post_submit_guard_never_releases_mask_to_native_menu(self):
        self.assertTrue(
            p1_command_mask_enabled(
                True,
                native_menu_open=True,
                native_command_open=False,
                close_guard_active=True,
            )
        )

    def test_field_f6_mask_does_not_depend_on_presentation_visibility(self):
        self.assertTrue(
            p1_command_mask_requested(
                True,
                user_control_enabled=True,
                native_command_open=False,
                close_guard_active=False,
                native_pause_open=False,
                native_modal_active=False,
            )
        )

    def test_town_releases_p1_command_mask_to_native_world_interactions(self):
        self.assertFalse(
            p1_command_mask_requested(
                True,
                user_control_enabled=True,
                native_command_open=False,
                close_guard_active=False,
                native_pause_open=False,
                native_modal_active=False,
                town=True,
                game_thread_handoff=True,
            )
        )

    def test_chocobo_releases_p1_confirm_cancel_and_command_buttons(self):
        self.assertFalse(
            p1_command_mask_requested(
                True,
                user_control_enabled=True,
                native_command_open=False,
                close_guard_active=True,
                native_pause_open=False,
                native_modal_active=False,
                chocobo_mode_active=True,
                game_thread_handoff=True,
            )
        )

    def test_post_close_or_competing_tree_retains_mask_without_ucon(self):
        common = dict(
            replacement_enabled=True,
            user_control_enabled=False,
            native_pause_open=False,
            native_modal_active=False,
        )
        self.assertTrue(
            p1_command_mask_requested(
                **common,
                native_command_open=False,
                close_guard_active=True,
            )
        )
        self.assertTrue(
            p1_command_mask_requested(
                **common,
                native_command_open=True,
                close_guard_active=False,
            )
        )

    def test_proven_native_owners_release_physical_command_buttons(self):
        common = dict(
            replacement_enabled=True,
            user_control_enabled=True,
            native_command_open=True,
            close_guard_active=True,
            native_pause_open=False,
            native_modal_active=False,
        )
        for owner in ("native_pause_open", "native_modal_active"):
            values = dict(common)
            values[owner] = True
            with self.subTest(owner=owner):
                self.assertFalse(p1_command_mask_requested(**values))

    def test_disabled_f6_never_masks_native_buttons(self):
        self.assertFalse(
            p1_command_mask_enabled(
                False,
                native_menu_open=False,
                native_command_open=True,
            )
        )

    def test_real_pause_temporarily_suspends_f6(self):
        self.assertTrue(
            p1_pause_suspension_required(
                native_menu_open=False,
                native_command_open=False,
                native_pause_open=True,
            )
        )
        self.assertFalse(
            p1_pause_suspension_required(
                native_menu_open=False,
                native_command_open=False,
                native_pause_open=False,
            )
        )

    def test_competing_field_command_is_not_treated_as_pause(self):
        self.assertFalse(
            p1_pause_suspension_required(
                native_menu_open=True,
                native_command_open=True,
            )
        )

    def test_transient_menu_global_without_pause_object_is_not_pause(self):
        self.assertFalse(
            p1_pause_suspension_required(
                native_menu_open=True,
                native_command_open=False,
                native_pause_open=False,
            )
        )

    def test_transient_missing_p2_actor_cannot_bypass_f6_button_mask(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        mailbox = struct.pack("<Q", bridge.base + P2_CONTROLLED_ACTOR_OFFSET)
        self.assertNotIn(mailbox, bridge._build_cave())

    def test_transient_missing_p2_actor_cannot_bypass_f6_axis_mask(self):
        bridge = NativeDialogInputBridge(FakeProcess(), 0x500000)
        mailbox = struct.pack("<Q", bridge.base + P2_CONTROLLED_ACTOR_OFFSET)
        self.assertNotIn(mailbox, bridge._build_axis_cave())


if __name__ == "__main__":
    unittest.main()
