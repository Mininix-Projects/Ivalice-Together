import json
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import controller_bindings as controls


class ConfirmReopenGuardTests(unittest.TestCase):
    def test_held_confirm_never_reopens_after_timestamp_alone(self):
        guard = controls.ConfirmReopenGuard(0.20)
        guard.arm(10.0)
        guard.update(confirm_down=True, now=10.5)
        self.assertFalse(guard.can_open(12.0))

    def test_release_starts_fresh_two_tenths_quiet_window(self):
        guard = controls.ConfirmReopenGuard(0.20)
        guard.arm(10.0)
        guard.update(confirm_down=False, now=10.4)
        self.assertFalse(guard.can_open(10.599))
        self.assertTrue(guard.can_open(10.6001))

    def test_rearming_never_shortens_an_existing_block(self):
        guard = controls.ConfirmReopenGuard(0.20)
        guard.blocked_until = 20.0
        guard.arm(10.0)
        guard.update(confirm_down=False, now=10.1)
        self.assertEqual(guard.blocked_until, 20.0)

    def test_native_leader_confirm_arm_route_is_not_exposed(self):
        self.assertFalse(hasattr(controls, "native_leader_menu_just_closed"))


class ThreePlayerControllerConfigTests(unittest.TestCase):
    def test_default_has_one_explicit_desktop_owner(self):
        config = controls.default_config()

        self.assertEqual(config["version"], controls.CONFIG_VERSION)
        self.assertEqual(
            config["desktop"],
            {"p1": False, "p2": True, "p3": False},
        )
        self.assertEqual(controls.desktop_owner(config), "p2")

    def test_v8_desktop_migration_preserves_devices_aliases_and_bindings(self):
        legacy = controls.default_config()
        legacy["version"] = 8
        legacy.pop("desktop")
        sdl = controls.sdl_assignment("pad-guid", 0, "Pad", "pad-uid")
        legacy["device_aliases"] = [{"xinput": 1, "sdl": sdl}]
        legacy["devices"] = {"p1": 0, "p2": 1, "p3": 2}
        legacy["players"]["p3"]["confirm"] = [
            controls.key_binding(ord("U")),
            controls.pad_binding(2, "X"),
        ]

        normalized, report = controls.normalize_config_with_report(legacy)

        self.assertEqual(controls.desktop_owner(normalized), "p2")
        self.assertEqual(normalized["devices"], legacy["devices"])
        self.assertEqual(normalized["device_aliases"], legacy["device_aliases"])
        self.assertEqual(
            normalized["players"]["p3"]["confirm"],
            legacy["players"]["p3"]["confirm"],
        )
        self.assertTrue(report.migrated_desktop)
        self.assertTrue(report.needs_persist)
        self.assertEqual(report.source_version, 8)
        self.assertEqual(report.kept_desktop_owner, "p2")

    def test_duplicate_desktop_owners_are_repaired_and_reported_once(self):
        raw = controls.default_config()
        raw["desktop"] = {"p1": True, "p2": True, "p3": True}

        normalized, report = controls.normalize_config_with_report(raw)

        self.assertEqual(
            normalized["desktop"],
            {"p1": True, "p2": False, "p3": False},
        )
        self.assertEqual(report.kept_desktop_owner, "p1")
        self.assertEqual(report.removed_desktop_owners, ("p2", "p3"))
        self.assertTrue(report.needs_persist)

        again, second_report = controls.normalize_config_with_report(normalized)
        self.assertEqual(again, normalized)
        self.assertFalse(second_report.needs_persist)
        self.assertEqual(second_report.removed_desktop_owners, ())

    def test_legacy_file_without_keyboard_bindings_migrates_to_no_owner(self):
        normalized, report = controls.normalize_config_with_report(
            {
                "version": 8,
                "devices": {"p1": 0, "p2": 1, "p3": 2},
                "players": {},
            }
        )

        self.assertIsNone(controls.desktop_owner(normalized))
        self.assertEqual(
            normalized["desktop"],
            {"p1": False, "p2": False, "p3": False},
        )
        self.assertTrue(report.needs_persist)

    def test_p1_keyboard_binding_is_preserved_when_p1_owns_desktop(self):
        raw = controls.default_config()
        controls.set_desktop_owner(raw, "p1")
        raw["players"]["p1"]["confirm"] = [controls.key_binding(ord("Q"))]

        normalized = controls.normalize_config(raw)

        self.assertEqual(
            normalized["players"]["p1"]["confirm"],
            [controls.key_binding(ord("Q"))],
        )

    def test_two_identical_sdl_models_with_distinct_uids_remain_assignable(self):
        first = controls.sdl_assignment("same-guid", 0, "DualSense", "first")
        second = controls.sdl_assignment("same-guid", 1, "DualSense", "second")
        raw = controls.default_config()
        raw["devices"] = {"p1": first, "p2": second, "p3": None}

        normalized = controls.normalize_config(raw)

        self.assertEqual(normalized["devices"]["p1"], first)
        self.assertEqual(normalized["devices"]["p2"], second)

    def test_distinct_sdl_uids_survive_a_reused_legacy_ordinal(self):
        first = controls.sdl_assignment("same-guid", 0, "DualSense", "first")
        second = controls.sdl_assignment("same-guid", 0, "DualSense", "second")
        raw = controls.default_config()
        raw["devices"] = {"p1": first, "p2": second, "p3": None}

        normalized = controls.normalize_config(raw)

        self.assertFalse(controls.sdl_assignments_match(first, second))
        self.assertFalse(controls.device_assignments_overlap(raw, first, second))
        self.assertEqual(normalized["devices"]["p1"], first)
        self.assertEqual(normalized["devices"]["p2"], second)

    def test_legacy_sdl_identity_still_matches_its_one_time_uid_upgrade(self):
        legacy = controls.sdl_assignment("same-guid", 0, "DualSense")
        modern = controls.sdl_assignment("same-guid", 0, "DualSense", "stable")

        self.assertTrue(controls.sdl_assignments_match(legacy, modern))
        self.assertTrue(
            controls.device_assignments_overlap(
                controls.default_config(),
                legacy,
                modern,
            )
        )

    def test_aliases_keep_distinct_modern_uids_that_reused_one_ordinal(self):
        first = controls.sdl_assignment("same-guid", 0, "DualSense", "first")
        second = controls.sdl_assignment("same-guid", 0, "DualSense", "second")
        aliases = controls.normalize_device_aliases(
            [
                {"xinput": 1, "sdl": first},
                {"xinput": 2, "sdl": second},
            ]
        )

        self.assertEqual(len(aliases), 2)
        config = controls.default_config()
        config["device_aliases"] = aliases
        self.assertFalse(controls.device_assignments_overlap(config, 1, 2))

    def test_future_config_is_reported_incompatible_and_never_marked_for_migration(self):
        raw = controls.default_config()
        raw["version"] = controls.CONFIG_VERSION + 1
        raw["future_field"] = {"preserve": True}

        normalized, report = controls.normalize_config_with_report(raw)

        self.assertTrue(report.incompatible_future)
        self.assertFalse(report.needs_persist)
        self.assertFalse(report.migrated_desktop)
        self.assertEqual(normalized["version"], controls.CONFIG_VERSION + 1)

    def test_loaded_future_config_cannot_be_saved_over_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controls.json"
            future = controls.default_config()
            future["version"] = controls.CONFIG_VERSION + 1
            future["future_field"] = {"must": "survive"}
            payload = (json.dumps(future, indent=1) + "\n").encode("utf-8")
            path.write_bytes(payload)

            loaded = controls.load_config(path)

            self.assertEqual(loaded["version"], controls.CONFIG_VERSION + 1)
            with self.assertRaises(controls.UnsupportedControllerConfigVersion):
                controls.save_config(loaded, path)
            self.assertEqual(path.read_bytes(), payload)

    def test_cross_backend_alias_has_only_one_player_owner(self):
        dualsense = controls.sdl_assignment("dualsense-guid", 0, "DualSense")
        normalized = controls.normalize_config(
            {
                "version": controls.CONFIG_VERSION,
                "device_aliases": [{"xinput": 0, "sdl": dualsense}],
                "devices": {"p1": 0, "p2": dualsense, "p3": None},
                "players": {},
            }
        )

        self.assertEqual(normalized["devices"]["p1"], 0)
        self.assertIsNone(normalized["devices"]["p2"])
        self.assertTrue(
            controls.device_assignments_overlap(normalized, 0, dualsense)
        )

    def test_sdl_owner_does_not_silently_move_alias_to_another_xinput_slot(self):
        dualsense = controls.sdl_assignment("dualsense-guid", 0, "DualSense")
        normalized = controls.normalize_config(
            {
                "version": controls.CONFIG_VERSION,
                "device_aliases": [{"xinput": 0, "sdl": dualsense}],
                "devices": {"p1": dualsense, "p2": 0, "p3": None},
                "players": {},
            }
        )

        self.assertEqual(normalized["devices"]["p1"], dualsense)
        self.assertIsNone(normalized["devices"]["p2"])

    def test_controller_aliases_allow_multiple_sdl_mirrors_for_one_xinput(self):
        first = controls.sdl_assignment("first-guid", 0, "First")
        second = controls.sdl_assignment("second-guid", 0, "Second")
        normalized = controls.normalize_config(
            {
                "device_aliases": [
                    {"xinput": 1, "sdl": first},
                    {"xinput": 1, "sdl": second},
                    {"xinput": 2, "sdl": first},
                    {"xinput": 9, "sdl": second},
                ]
            }
        )

        self.assertEqual(
            normalized["device_aliases"],
            [
                {"xinput": 1, "sdl": first},
                {"xinput": 1, "sdl": second},
            ],
        )

    def test_alias_group_treats_physical_and_virtual_sdl_as_one_pad(self):
        config = controls.default_config()
        physical = controls.sdl_assignment("ds4-guid", 0, "Wireless Controller")
        virtual = controls.sdl_assignment("xbox-guid", 0, "Virtual Xbox 360")

        controls.register_device_alias_group(config, 1, (physical, virtual))

        self.assertTrue(controls.device_assignments_overlap(config, 1, physical))
        self.assertTrue(controls.device_assignments_overlap(config, 1, virtual))
        self.assertTrue(
            controls.device_assignments_overlap(config, physical, virtual)
        )

    def test_registering_alias_replaces_stale_backend_links(self):
        config = controls.default_config()
        old = controls.sdl_assignment("old-guid", 0, "Old")
        current = controls.sdl_assignment("current-guid", 0, "Current")
        controls.register_device_alias(config, 2, old)
        controls.register_device_alias(config, 2, current)

        self.assertEqual(
            config["device_aliases"],
            [{"xinput": 2, "sdl": current}],
        )

    def test_persistent_sdl_uid_survives_ordinal_changes(self):
        configured = controls.sdl_assignment(
            "same-guid", 1, "Virtual Pad", "stable-device"
        )
        live = controls.SDLControllerState(
            controls.sdl_assignment(
                "same-guid", 0, "Virtual Pad", "stable-device"
            ),
            {},
        )

        self.assertIs(controls.resolve_sdl_state((live,), configured), live)

    def test_legacy_sdl_identity_is_upgraded_when_its_match_is_unambiguous(self):
        legacy = controls.sdl_assignment("same-guid", 0, "Virtual Pad")
        modern = controls.sdl_assignment(
            "same-guid", 0, "Virtual Pad", "stable-device"
        )
        config = controls.default_config()
        config["devices"]["p2"] = legacy
        config["device_aliases"] = [{"xinput": 1, "sdl": legacy}]

        changed = controls.upgrade_sdl_identities(
            config,
            (controls.SDLControllerState(modern, {}),),
        )

        self.assertTrue(changed)
        self.assertEqual(config["devices"]["p2"], modern)
        self.assertEqual(config["device_aliases"], [{"xinput": 1, "sdl": modern}])

    def test_legacy_sdl_identity_is_not_upgraded_from_an_ambiguous_ordinal(self):
        legacy = controls.sdl_assignment("same-guid", 0, "Legacy pad")
        first = controls.sdl_assignment(
            "same-guid", 0, "First physical pad", "first-device"
        )
        second = controls.sdl_assignment(
            "same-guid", 0, "Second physical pad", "second-device"
        )
        config = controls.default_config()
        config["devices"]["p2"] = legacy
        config["device_aliases"] = [{"xinput": 1, "sdl": legacy}]

        changed = controls.upgrade_sdl_identities(
            config,
            (
                controls.SDLControllerState(first, {}),
                controls.SDLControllerState(second, {}),
            ),
        )

        self.assertFalse(changed)
        self.assertEqual(config["devices"]["p2"], legacy)
        self.assertEqual(config["device_aliases"], [{"xinput": 1, "sdl": legacy}])

    def test_persistent_sdl_uid_never_steals_an_identical_controller(self):
        configured = controls.sdl_assignment(
            "same-guid", 0, "Virtual Pad", "first-device"
        )
        other = controls.SDLControllerState(
            controls.sdl_assignment(
                "same-guid", 0, "Virtual Pad", "second-device"
            ),
            {},
        )

        self.assertIsNone(controls.resolve_sdl_state((other,), configured))

    def test_sdl_backend_reserves_ordinals_for_reconnecting_identical_pads(self):
        backend = controls._SDLControllerBackend()
        backend._ordinal_by_identity = {
            ("same-guid", "first"): 0,
            ("same-guid", "second"): 1,
        }
        backend._controllers = {
            42: (
                object(),
                controls.sdl_assignment("same-guid", 1, "Pad", "second"),
            )
        }

        self.assertEqual(backend._allocate_ordinal("same-guid", "first"), 0)
        self.assertEqual(backend._allocate_ordinal("same-guid", "third"), 2)

    def test_xinput_assignment_falls_back_to_its_sdl_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller_config.json"
            config = controls.default_config()
            sdl = controls.sdl_assignment("dual-guid", 0, "DualSense", "dual-uid")
            controls.register_device_alias(config, 1, sdl)
            config["devices"]["p2"] = 1
            controls.save_config(config, path)
            poller = controls.BindingPoller(path)
            poller._enabled = True
            poller._states = [None] * 4
            poller._sdl_states = (
                controls.SDLControllerState(sdl, {"A": 1.0}),
            )

            self.assertEqual(poller.effective_gamepad_assignment("p2"), sdl)
            self.assertTrue(poller.xinput_control_down("p2", "A"))

    def test_xinput_assignment_uses_any_connected_sdl_mirror(self):
        config = controls.default_config()
        physical = controls.sdl_assignment(
            "ds4-guid", 0, "Wireless Controller", "physical"
        )
        virtual = controls.sdl_assignment(
            "xbox-guid", 0, "Virtual Xbox 360", "virtual"
        )
        controls.register_device_alias_group(config, 1, (physical, virtual))
        config["devices"]["p2"] = 1
        live_virtual = controls.SDLControllerState(virtual, {"A": 1.0})

        self.assertEqual(
            controls.effective_gamepad_assignment(
                config,
                "p2",
                [None] * 4,
                (live_virtual,),
            ),
            virtual,
        )

    def test_sdl_assignment_falls_back_to_its_xinput_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller_config.json"
            config = controls.default_config()
            sdl = controls.sdl_assignment("dual-guid", 0, "DualSense", "dual-uid")
            controls.register_device_alias(config, 2, sdl)
            config["devices"]["p2"] = sdl
            controls.save_config(config, path)
            poller = controls.BindingPoller(path)
            xinput = controls.XINPUT_STATE()
            xinput.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]
            poller._enabled = True
            poller._states = [None, None, xinput, None]
            poller._sdl_states = ()

            self.assertEqual(poller.effective_gamepad_assignment("p2"), 2)
            self.assertTrue(poller.xinput_control_down("p2", "A"))

    def test_p1_never_adopts_a_different_unowned_xinput_slot(self):
        config = controls.default_config()
        live = controls.XINPUT_STATE()

        self.assertIsNone(
            controls.effective_gamepad_assignment(
                config,
                "p1",
                [None, None, live, None],
                (),
            )
        )
        self.assertEqual(
            controls.effective_gamepad_assignment(
                config,
                "p1",
                [live, None, None, None],
                (),
            ),
            0,
        )

    def test_direct_sdl_assignment_uses_a_live_sibling_alias(self):
        config = controls.default_config()
        preferred = controls.sdl_assignment(
            "preferred-guid", 0, "Preferred", "preferred-uid"
        )
        sibling = controls.sdl_assignment(
            "sibling-guid", 0, "Virtual mirror", "sibling-uid"
        )
        controls.register_device_alias_group(config, 1, (preferred, sibling))
        config["devices"]["p2"] = preferred
        live_sibling = controls.SDLControllerState(sibling, {"A": 1.0})

        self.assertEqual(
            controls.effective_gamepad_assignment(
                config,
                "p2",
                [None] * 4,
                (live_sibling,),
            ),
            sibling,
        )

    def test_live_reload_absorbs_a_held_button_until_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller_config.json"
            config = controls.default_config()
            config["devices"]["p2"] = 1
            controls.save_config(config, path)
            poller = controls.BindingPoller(path)
            held = controls.XINPUT_STATE()
            held.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]

            poller._reload_history_pending = True
            with mock.patch.object(
                controls,
                "read_xinput_states",
                return_value=[None, held, None, None],
            ):
                poller.begin_frame(True, keyboard_enabled=False)
            self.assertFalse(poller.edge("p2", "confirm"))

            with mock.patch.object(
                controls,
                "read_xinput_states",
                return_value=[None, controls.XINPUT_STATE(), None, None],
            ):
                poller.begin_frame(True, keyboard_enabled=False)
            self.assertFalse(poller.edge("p2", "confirm"))

            with mock.patch.object(
                controls,
                "read_xinput_states",
                return_value=[None, held, None, None],
            ):
                poller.begin_frame(True, keyboard_enabled=False)
            self.assertTrue(poller.edge("p2", "confirm"))

    def test_transient_reload_read_failure_retains_all_player_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller_config.json"
            initial = controls.default_config()
            initial["devices"].update({"p1": 0, "p2": 1, "p3": 2})
            controls.save_config(initial, path)
            poller = controls.BindingPoller(path)
            known_good = poller.config

            changed = controls.normalize_config(initial)
            changed["devices"]["p3"] = None
            controls.save_config(changed, path)
            poller._mtime_ns = -1
            poller._next_reload_check = 0.0

            with mock.patch.object(
                Path,
                "read_text",
                side_effect=PermissionError("scanner lock during replace"),
            ):
                poller._reload_if_changed(10.0)

            self.assertIs(poller.config, known_good)
            self.assertEqual(poller._mtime_ns, -1)
            self.assertEqual(poller.assigned_gamepad("p2"), 1)
            self.assertEqual(poller.assigned_gamepad("p3"), 2)

            poller._reload_if_changed(10.25)
            self.assertEqual(poller.assigned_gamepad("p2"), 1)
            self.assertIsNone(poller.assigned_gamepad("p3"))
            self.assertNotEqual(poller._mtime_ns, -1)

    def test_transient_startup_read_failure_retries_the_same_config_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller_config.json"
            configured = controls.default_config()
            configured["devices"].update({"p1": 0, "p2": 1, "p3": 2})
            controls.save_config(configured, path)

            real_read_text = Path.read_text
            attempts = {"count": 0}

            def fail_once(candidate, *args, **kwargs):
                if candidate == path and attempts["count"] == 0:
                    attempts["count"] += 1
                    raise PermissionError("startup scanner lock")
                return real_read_text(candidate, *args, **kwargs)

            with mock.patch.object(Path, "read_text", new=fail_once):
                poller = controls.BindingPoller(path)

            self.assertEqual(poller._mtime_ns, -1)
            self.assertIsNone(poller.assigned_gamepad("p3"))

            poller._reload_if_changed(10.0)

            self.assertEqual(poller.assigned_gamepad("p2"), 1)
            self.assertEqual(poller.assigned_gamepad("p3"), 2)
            self.assertNotEqual(poller._mtime_ns, -1)

    def test_xinput_target_prompts_follow_the_configured_actions(self):
        poller = controls.BindingPoller(Path("missing-controller-config.json"))
        poller.config["devices"].update({"p2": 1, "p3": 2})
        poller._active_device.update({"p2": "xinput", "p3": "xinput"})
        self.assertEqual(poller.target_prompt_pair("p2"), ("LB", "RB"))
        self.assertEqual(poller.target_prompt_pair("p3"), ("LB", "RB"))

        poller.config["players"]["p2"]["target_previous"] = [
            controls.pad_binding(1, "X")
        ]
        poller.config["players"]["p2"]["target_next"] = [controls.pad_binding(1, "Y")]
        self.assertEqual(poller.target_prompt_pair("p2"), ("X", "Y"))

    def test_legacy_two_player_file_keeps_p3_unassigned(self):
        normalized = controls.normalize_config(
            {
                "version": 2,
                "devices": {"p1": 0, "p2": 1},
                "players": {},
            }
        )
        self.assertIsNone(normalized["devices"]["p3"])

    def test_explicit_null_still_unassigns_p3(self):
        normalized = controls.normalize_config(
            {
                "version": 2,
                "devices": {"p1": 0, "p2": 1, "p3": None},
                "players": {},
            }
        )
        self.assertIsNone(normalized["devices"]["p3"])

    def test_migrated_p3_default_never_steals_an_old_explicit_pad(self):
        normalized = controls.normalize_config(
            {
                "version": 2,
                "devices": {"p1": 2, "p2": 1},
                "players": {},
            }
        )
        self.assertEqual(
            normalized["devices"],
            {"p1": 2, "p2": 1, "p3": None},
        )

    def test_legacy_shared_pads_are_migrated_to_unique_owners(self):
        normalized = controls.normalize_config(
            {
                "version": 2,
                "allow_shared_controller": False,
                "devices": {"p1": 2, "p2": 2, "p3": 2},
                "players": {},
            }
        )
        self.assertEqual(normalized["devices"], {"p1": 2, "p2": None, "p3": None})
        self.assertNotIn("allow_shared_controller", normalized)

    def test_legacy_target_toggle_is_split_between_previous_and_next(self):
        legacy = controls.default_config()
        legacy["version"] = 2
        legacy["players"]["p2"]["target_toggle"] = [
            controls.key_binding(ord("Q")),
            controls.key_binding(ord("E")),
            controls.pad_binding(1, "RB"),
        ]
        legacy["players"]["p2"].pop("target_previous")
        legacy["players"]["p2"].pop("target_next")

        normalized = controls.normalize_config(legacy)

        self.assertEqual(
            normalized["players"]["p2"]["target_previous"],
            [controls.key_binding(ord("Q")), controls.pad_binding(1, "LB")],
        )
        self.assertEqual(
            normalized["players"]["p2"]["target_next"],
            [controls.key_binding(ord("E")), controls.pad_binding(1, "RB")],
        )


def test_defaults_cover_every_runtime_action():
    config = controls.default_config()
    for player, actions in controls.PLAYER_ACTIONS.items():
        assert set(config["players"][player]) == {action for action, _label in actions}


def test_packaged_controller_defaults_match_the_current_generated_profile():
    development_root = Path(__file__).resolve().parents[1]
    packaged = (
        development_root / "source" / "controller_config.json",
    )
    available = tuple(path for path in packaged if path.is_file())
    if not available:
        raise unittest.SkipTest("generated release configuration is unavailable")
    expected = controls.default_config()
    for path in available:
        assert json.loads(path.read_text(encoding="utf-8")) == expected


def test_follower_keyboard_defaults_match_the_pc_layout_without_role_drift():
    config = controls.default_config()
    expected = {
        "move_up": [ord("W")],
        "move_down": [ord("S")],
        "move_left": [ord("A")],
        "move_right": [ord("D")],
        "run_toggle": [0xA0],
        "flee": [0xA2],
        "battle_menu": [ord("F")],
        "menu_up": [ord("I"), 0x68],
        "menu_down": [ord("K"), 0x62],
        "menu_left": [ord("J"), 0x64, ord("Q")],
        "menu_right": [ord("L"), 0x66, ord("E")],
        "target_previous": [ord("1")],
        "target_next": [ord("3")],
        "quickening": [ord("F")],
        "confirm": [0x20, 0x0D, 0x01],
        "cancel": [ord("C"), 0x08, 0x02],
        "native_party_menu": [ord("R")],
        "native_pause": [0x1B],
    }

    for action, expected_vks in expected.items():
        p2_vks = [
            binding["vk"]
            for binding in config["players"]["p2"][action]
            if binding.get("device") == "keyboard"
        ]
        p3_vks = [
            binding["vk"]
            for binding in config["players"]["p3"][action]
            if binding.get("device") == "keyboard"
        ]
        assert p2_vks == expected_vks
        assert p3_vks == expected_vks

    # Z remains FF12's Display Map. I/K already open and cycle the co-op
    # character selector, so no extra keyboard hotkey is needed here.
    follower_vks = {
        binding["vk"]
        for player in ("p2", "p3")
        for bindings in config["players"][player].values()
        for binding in bindings
        if binding.get("device") == "keyboard"
    }
    assert ord("Z") not in follower_vks

    for player in ("p2", "p3"):
        assert "change_character" not in config["players"][player]
        index = 1 if player == "p2" else 2
        assert config["players"][player]["battle_menu"] == [
            controls.key_binding(ord("F")),
            controls.pad_binding(index, "X"),
        ]
        assert config["players"][player]["native_pause"] == [
            controls.key_binding(0x1B),
            controls.pad_binding(index, "START"),
        ]

    assert "change_character" not in controls.FOLLOWER_KEYBOARD_DEFAULTS
    assert "change_character" not in {
        action for action, _label in controls.PLAYER_ACTIONS["p2"]
    }
    assert "change_character" not in {
        action for action, _label in controls.PLAYER_ACTIONS["p3"]
    }

    assert controls.VK_NAMES[0x01] == "Mouse Left"
    assert controls.VK_NAMES[0x02] == "Mouse Right"
    assert controls.VK_NAMES[0xA0] == "Left Shift"
    assert controls.VK_NAMES[0xA2] == "Left Ctrl"


def test_four_binding_limit_preserves_full_pc_aliases_and_truncates_a_fifth():
    config = controls.default_config()
    for player, index in (("p2", 1), ("p3", 2)):
        assert config["players"][player]["menu_left"] == [
            controls.key_binding(ord("J")),
            controls.key_binding(0x64),
            controls.key_binding(ord("Q")),
            controls.pad_binding(index, "DPAD_LEFT"),
        ]
        assert controls.normalize_config(config)["players"][player][
            "menu_left"
        ] == config["players"][player]["menu_left"]

    config["players"]["p2"]["battle_menu"] = [
        controls.key_binding(vk) for vk in (1, 2, 3, 4, 5)
    ]
    normalized = controls.normalize_config(config)
    assert normalized["players"]["p2"]["battle_menu"] == [
        controls.key_binding(vk) for vk in (1, 2, 3, 4)
    ]


def test_follower_mod_toggles_use_f6_f7_and_leave_f3_free_for_game_speed():
    config = controls.default_config()
    assert config["players"]["p2"]["toggle_control"] == [controls.key_binding(0x75)]
    assert config["players"]["p3"]["toggle_control"] == [controls.key_binding(0x76)]
    follower_vks = {
        binding["vk"]
        for player in ("p2", "p3")
        for bindings in config["players"][player].values()
        for binding in bindings
        if binding.get("device") == "keyboard"
    }
    assert 0x72 not in follower_vks  # F3: native quadruple game speed
    assert 0x73 not in follower_vks  # F4: no stale P3 toggle


def _legacy_v9_stock_follower_config():
    config = controls.default_config()
    config["version"] = 9
    for player, index in (("p2", 1), ("p3", 2)):
        config["players"][player]["battle_menu"] = [
            controls.key_binding(ord("F"))
        ]
        config["players"][player]["native_pause"] = [
            controls.key_binding(0x1B)
        ]
        config["players"][player]["change_character"] = [
            controls.pad_binding(index, "BACK")
        ]
    return config


def test_v9_stock_follower_actions_migrate_to_build106_gamepad_aliases():
    normalized, report = controls.normalize_config_with_report(
        _legacy_v9_stock_follower_config()
    )

    assert normalized["version"] == controls.CONFIG_VERSION == 10
    assert report.source_version == 9
    assert report.needs_persist
    for player, index in (("p2", 1), ("p3", 2)):
        assert normalized["players"][player]["battle_menu"] == [
            controls.key_binding(ord("F")),
            controls.pad_binding(index, "X"),
        ]
        assert normalized["players"][player]["native_pause"] == [
            controls.key_binding(0x1B),
            controls.pad_binding(index, "START"),
        ]
        assert "change_character" not in normalized["players"][player]


def test_v9_custom_follower_battle_and_pause_bindings_remain_exact():
    config = _legacy_v9_stock_follower_config()
    custom = {
        "p2": {
            "battle_menu": [
                controls.key_binding(ord("F")),
                controls.pad_binding(1, "A"),
            ],
            "native_pause": [
                controls.key_binding(0x1B),
                controls.key_binding(ord("P")),
            ],
        },
        "p3": {
            "battle_menu": [controls.key_binding(ord("G"))],
            "native_pause": [controls.pad_binding(2, "BACK")],
        },
    }
    for player, actions in custom.items():
        config["players"][player].update(actions)

    normalized = controls.normalize_config(config)

    for player, actions in custom.items():
        for action, bindings in actions.items():
            assert normalized["players"][player][action] == bindings


def test_v9_stock_migration_is_idempotent_after_schema_10_persist():
    migrated = controls.normalize_config(_legacy_v9_stock_follower_config())

    normalized_again, report = controls.normalize_config_with_report(migrated)

    assert normalized_again == migrated
    assert report.source_version == controls.CONFIG_VERSION == 10
    assert not report.needs_persist


def test_follower_keyboard_lists_are_independent_and_gamepads_keep_role_indices():
    config = controls.default_config()
    p2 = config["players"]["p2"]
    p3 = config["players"]["p3"]

    assert p2["move_up"] is not p3["move_up"]
    assert p2["move_up"][0] is not p3["move_up"][0]
    p2["move_up"][0]["vk"] = ord("T")
    assert p3["move_up"][0] == controls.key_binding(ord("W"))
    assert controls.default_config()["players"]["p2"]["move_up"][0] == (
        controls.key_binding(ord("W"))
    )

    for action, control in controls.FOLLOWER_GAMEPAD_DEFAULTS.items():
        for player, index in (("p2", 1), ("p3", 2)):
            matches = [
                binding
                for binding in config["players"][player][action]
                if binding.get("device") == "xinput"
            ]
            assert matches == [controls.pad_binding(index, control)]
    assert p2["quickening"][-1] == controls.pad_binding(1, "X")
    assert p3["quickening"][-1] == controls.pad_binding(2, "A")
    assert p2["battle_menu"][-1] == controls.pad_binding(1, "X")
    assert p3["battle_menu"][-1] == controls.pad_binding(2, "X")
    assert p2["native_pause"][-1] == controls.pad_binding(1, "START")
    assert p3["native_pause"][-1] == controls.pad_binding(2, "START")


def test_follower_keyboard_profile_keeps_desktop_ownership_exclusive():
    config = controls.default_config()
    assert config["desktop"] == {"p1": False, "p2": True, "p3": False}
    assert controls.desktop_owner(config) == "p2"
    assert controls.player_owns_desktop(config, "p2")
    assert not controls.player_owns_desktop(config, "p3")


def test_native_party_menu_defaults_to_r_and_the_unique_xinput_y_for_followers():
    config = controls.default_config()
    for index, player in enumerate(("p1", "p2", "p3")):
        expected = [controls.pad_binding(index, "Y")]
        if player != "p1":
            expected.insert(0, controls.key_binding(ord("R")))
        assert config["players"][player]["native_party_menu"] == expected
        xinput_y_owners = [
            action
            for action, bindings in config["players"][player].items()
            if any(
                binding.get("device") == "xinput" and binding.get("control") == "Y"
                for binding in bindings
            )
        ]
        assert xinput_y_owners == ["native_party_menu"]

    assert config["players"]["p1"]["native_party_menu"] == [
        controls.pad_binding(0, "Y")
    ]


def test_old_config_missing_native_party_action_inherits_current_defaults():
    old = controls.default_config()
    for player in ("p1", "p2", "p3"):
        old["players"][player].pop("native_party_menu")
    normalized = controls.normalize_config(old)
    for index, player in enumerate(("p1", "p2", "p3")):
        expected = [controls.pad_binding(index, "Y")]
        if player != "p1":
            expected.insert(0, controls.key_binding(ord("R")))
        assert normalized["players"][player]["native_party_menu"] == expected


def test_p1_pause_defaults_to_start_and_migrates_old_configs():
    config = controls.default_config()
    assert config["players"]["p1"]["native_pause"] == [
        controls.pad_binding(0, "START")
    ]

    config["players"]["p1"].pop("native_pause")
    normalized = controls.normalize_config(config)
    assert normalized["players"]["p1"]["native_pause"] == [
        controls.pad_binding(0, "START")
    ]


def test_normalize_keeps_valid_controls_on_the_assigned_pad():
    raw = controls.default_config()
    raw["devices"]["p2"] = 1
    raw["players"]["p2"]["confirm"] = [
        controls.key_binding(0x20),
        controls.pad_binding(2, "A"),
        {"device": "xinput", "index": 9, "control": "A"},
        {"device": "keyboard", "vk": 999},
    ]
    normalized = controls.normalize_config(raw)
    assert normalized["players"]["p2"]["confirm"] == [
        controls.key_binding(0x20),
        controls.pad_binding(1, "A"),
    ]


def test_save_and_load_are_round_trip(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["players"]["p1"]["confirm"] = [controls.pad_binding(3, "X")]
    controls.save_config(config, path)
    assert controls.load_config(path) == controls.normalize_config(config)
    assert not path.with_suffix(".json.tmp").exists()


def test_load_report_becomes_clean_after_migration_is_saved(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    legacy = controls.default_config()
    legacy["version"] = 8
    legacy.pop("desktop")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    migrated, report = controls.load_config_with_report(path)
    assert report.needs_persist
    assert controls.desktop_owner(migrated) == "p2"

    controls.save_config(migrated, path)
    reloaded, clean_report = controls.load_config_with_report(path)
    assert reloaded == migrated
    assert not clean_report.needs_persist


def test_xinput_analog_direction_has_deadzone_and_full_scale():
    state = controls.XINPUT_STATE()
    state.Gamepad.sThumbLX = 32767
    state.Gamepad.sThumbLY = -32768
    assert controls.xinput_control_strength(state, "LS_RIGHT") == 1.0
    assert controls.xinput_control_strength(state, "LS_DOWN") == 1.0
    assert controls.xinput_control_strength(state, "LS_LEFT") == 0.0
    state.Gamepad.sThumbLX = 1000
    assert controls.xinput_control_strength(state, "LS_RIGHT") == 0.0


def test_xinput_full_diagonal_preserves_run_magnitude():
    state = controls.XINPUT_STATE()
    diagonal = round(32767 / math.sqrt(2.0))
    state.Gamepad.sThumbLX = diagonal
    state.Gamepad.sThumbLY = diagonal

    right = controls.xinput_control_strength(state, "LS_RIGHT")
    forward = controls.xinput_control_strength(state, "LS_UP")

    assert math.hypot(forward, right) > 0.99


def test_sdl_full_diagonal_preserves_run_magnitude():
    diagonal = 1.0 / math.sqrt(2.0)
    backend = controls._SDLControllerBackend()
    normalized = backend._normalized_strengths(
        {
            "LS_UP": diagonal,
            "LS_DOWN": 0.0,
            "LS_LEFT": 0.0,
            "LS_RIGHT": diagonal,
            "RS_UP": 0.0,
            "RS_DOWN": 0.0,
            "RS_LEFT": 0.0,
            "RS_RIGHT": 0.0,
        }
    )

    assert math.hypot(normalized["LS_UP"], normalized["LS_RIGHT"]) > 0.99


def test_sdl_refresh_skips_one_broken_virtual_device(monkeypatch):
    class FakeJoystick:
        def get_guid(self):
            return "healthy-guid"

    class FakeController:
        id = 101
        name = "Healthy Controller"

        def as_joystick(self):
            return FakeJoystick()

        def attached(self):
            return True

        def quit(self):
            return None

    class FakeSdlControllerModule:
        @staticmethod
        def get_init():
            return True

        @staticmethod
        def get_count():
            return 2

        @staticmethod
        def is_controller(_index):
            return True

        @staticmethod
        def Controller(index):
            if index == 0:
                raise RuntimeError("stale DS4Windows device")
            return FakeController()

    monkeypatch.setattr(controls, "sdl_controller", FakeSdlControllerModule)
    monkeypatch.setattr(
        controls,
        "_sdl_device_metadata",
        lambda index: (100 + index, f"device-{index}"),
    )
    backend = controls._SDLControllerBackend()

    backend._refresh()

    assert tuple(backend._controllers) == (101,)
    assert backend._controllers[101][1]["name"] == "Healthy Controller"


def test_sdl_refresh_uses_the_opened_handle_instance_after_enumeration_reorders(
    monkeypatch,
):
    class FakeJoystick:
        def get_guid(self):
            return "actual-guid"

    class FakeController:
        id = 202
        name = "Actual Controller"

        def as_joystick(self):
            return FakeJoystick()

        def attached(self):
            return True

        def quit(self):
            return None

    class FakeSdlControllerModule:
        @staticmethod
        def get_init():
            return True

        @staticmethod
        def get_count():
            return 1

        @staticmethod
        def is_controller(_index):
            return True

        @staticmethod
        def Controller(_index):
            return FakeController()

    monkeypatch.setattr(controls, "sdl_controller", FakeSdlControllerModule)
    monkeypatch.setattr(
        controls,
        "_sdl_device_metadata",
        lambda _index: (101, "uid-from-the-stale-index"),
    )
    backend = controls._SDLControllerBackend()

    assert backend._refresh()

    assert tuple(backend._controllers) == (202,)
    assignment = backend._controllers[202][1]
    assert assignment["guid"] == "actual-guid"
    assert "uid" not in assignment


def test_sdl_refresh_keeps_an_attached_handle_during_a_coherent_list_omission(
    monkeypatch,
):
    existing = mock.Mock(name="existing")
    existing.attached.return_value = True
    assignment = controls.sdl_assignment(
        "healthy-guid", 0, "Healthy", "healthy-uid"
    )

    class FakeSdlControllerModule:
        @staticmethod
        def get_init():
            return True

        @staticmethod
        def get_count():
            return 0

    backend = controls._SDLControllerBackend()
    backend._controllers = {7: (existing, assignment)}
    monkeypatch.setattr(controls, "sdl_controller", FakeSdlControllerModule)

    assert backend._refresh()
    assert tuple(backend._controllers) == (7,)
    existing.quit.assert_not_called()


def test_sdl_read_quarantines_only_the_broken_live_controller(monkeypatch):
    healthy = mock.Mock(name="healthy")
    healthy.attached.return_value = True
    broken = mock.Mock(name="broken")
    broken.attached.return_value = True
    healthy_assignment = controls.sdl_assignment(
        "healthy-guid",
        0,
        "Healthy",
        "healthy-uid",
    )
    broken_assignment = controls.sdl_assignment(
        "broken-guid",
        0,
        "Broken",
        "broken-uid",
    )
    backend = controls._SDLControllerBackend()
    backend._controllers = {
        1: (broken, broken_assignment),
        2: (healthy, healthy_assignment),
    }
    strengths = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    strengths["A"] = 1.0
    monkeypatch.setattr(backend, "_refresh", mock.Mock())
    monkeypatch.setattr(controls.sdl_controller, "update", mock.Mock())
    monkeypatch.setattr(
        backend,
        "_native_strengths",
        mock.Mock(
            side_effect=lambda controller: (
                strengths
                if controller is healthy
                else (_ for _ in ()).throw(OSError("stale Bluetooth handle"))
            )
        ),
    )

    states = backend.read()

    assert [state.assignment for state in states] == [healthy_assignment]
    assert states[0].strengths["A"] == 1.0
    assert tuple(backend._controllers) == (2,)
    broken.quit.assert_called_once_with()
    healthy.quit.assert_not_called()


def test_sdl_global_pump_failure_retains_only_a_bounded_coherent_sample(
    monkeypatch,
):
    controller = mock.Mock(name="healthy")
    controller.attached.return_value = True
    assignment = controls.sdl_assignment(
        "healthy-guid",
        0,
        "Healthy",
        "healthy-uid",
    )
    backend = controls._SDLControllerBackend()
    backend._controllers = {1: (controller, assignment)}
    strengths = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    strengths["LS_UP"] = 1.0
    monkeypatch.setattr(backend, "_refresh", mock.Mock())
    monkeypatch.setattr(
        controls.sdl_controller,
        "update",
        mock.Mock(
            side_effect=[
                None,
                OSError("transient SDL pump"),
                OSError("persistent SDL pump"),
            ]
        ),
    )
    monkeypatch.setattr(
        backend,
        "_native_strengths",
        mock.Mock(return_value=strengths),
    )
    monkeypatch.setattr(
        controls.time,
        "monotonic",
        mock.Mock(
            side_effect=[
                100.0,
                100.0 + controls.SDL_PUMP_FAILURE_GRACE_SECONDS / 2.0,
                100.0 + controls.SDL_PUMP_FAILURE_GRACE_SECONDS * 2.0,
            ]
        ),
    )

    coherent = backend.read()
    transient = backend.read()
    expired = backend.read()

    assert coherent and coherent[0].strengths["LS_UP"] == 1.0
    assert transient == coherent
    assert expired == ()
    assert tuple(backend._controllers) == (1,)
    controller.quit.assert_not_called()


def test_sdl_global_pump_grace_starts_when_failure_is_observed(monkeypatch):
    controller = mock.Mock(name="healthy")
    controller.attached.return_value = True
    assignment = controls.sdl_assignment(
        "healthy-guid",
        0,
        "Healthy",
        "healthy-uid",
    )
    backend = controls._SDLControllerBackend()
    backend._controllers = {1: (controller, assignment)}
    strengths = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    strengths["LS_UP"] = 1.0
    monkeypatch.setattr(backend, "_refresh", mock.Mock(return_value=True))
    monkeypatch.setattr(
        controls.sdl_controller,
        "update",
        mock.Mock(side_effect=[None, OSError("late transient pump")]),
    )
    monkeypatch.setattr(backend, "_native_strengths", mock.Mock(return_value=strengths))
    monkeypatch.setattr(
        controls.time,
        "monotonic",
        mock.Mock(side_effect=[100.0, 500.0]),
    )

    coherent = backend.read()
    first_failure = backend.read()

    assert first_failure == coherent
    assert backend._pump_failure_started_at == 500.0


def test_sdl_partial_enumeration_failure_keeps_existing_healthy_handles(
    monkeypatch,
):
    existing = mock.Mock(name="existing")
    existing.attached.return_value = True
    assignment = controls.sdl_assignment(
        "healthy-guid",
        0,
        "Healthy",
        "healthy-uid",
    )

    class FakeSdlControllerModule:
        @staticmethod
        def get_init():
            return True

        @staticmethod
        def get_count():
            return 2

        @staticmethod
        def is_controller(_index):
            return True

        @staticmethod
        def Controller(_index):
            raise OSError("one broken enumeration entry")

    backend = controls._SDLControllerBackend()
    backend._controllers = {7: (existing, assignment)}
    monkeypatch.setattr(controls, "sdl_controller", FakeSdlControllerModule)
    monkeypatch.setattr(controls, "_sdl_device_metadata", lambda _index: (None, ""))

    assert backend._refresh()
    assert tuple(backend._controllers) == (7,)
    existing.quit.assert_not_called()


def test_xinput_transient_query_gap_has_no_zero_frame_or_recovery_edge(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    held = controls.XINPUT_STATE()
    held.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]
    neutral = controls.XINPUT_STATE()

    with (
        mock.patch.object(
            controls,
            "read_xinput_states",
            side_effect=(
                [None, held, None, None],
                [None] * 4,
                [None, held, None, None],
                [None, neutral, None, None],
                [None, held, None, None],
            ),
        ),
        mock.patch.object(
            controls.time,
            "monotonic",
            side_effect=(10.0, 10.05, 10.08, 10.09, 10.10),
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.edge("p2", "confirm")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.down("p2", "confirm")
        assert not poller.edge("p2", "confirm")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.down("p2", "confirm")
        assert not poller.edge("p2", "confirm")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.edge("p2", "confirm")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.edge("p2", "confirm")


def test_xinput_expired_gap_absorbs_held_toggle_on_recovery(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    config["players"]["p2"]["toggle_control"] = [controls.pad_binding(1, "A")]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    neutral = controls.XINPUT_STATE()
    held = controls.XINPUT_STATE()
    held.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]

    with (
        mock.patch.object(
            controls,
            "read_xinput_states",
            side_effect=(
                [None, neutral, None, None],
                [None] * 4,
                [None] * 4,
                [None, held, None, None],
                [None, neutral, None, None],
                [None, held, None, None],
            ),
        ),
        mock.patch.object(
            controls.time,
            "monotonic",
            side_effect=(20.0, 20.01, 20.12, 20.13, 20.14, 20.15),
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=False)
        poller.begin_frame(True, keyboard_enabled=False)
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.down("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.down("p2", "toggle_control")
        assert not poller.edge("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.edge("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.edge("p2", "toggle_control")


def test_first_xinput_hotplug_absorbs_a_held_toggle_until_repress(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    config["players"]["p2"]["toggle_control"] = [controls.pad_binding(1, "A")]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    neutral = controls.XINPUT_STATE()
    held = controls.XINPUT_STATE()
    held.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]

    with mock.patch.object(
        controls,
        "read_xinput_states",
        side_effect=(
            [None] * 4,
            [None, held, None, None],
            [None, neutral, None, None],
            [None, held, None, None],
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=False)
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.down("p2", "toggle_control")
        assert not poller.edge("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.edge("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.edge("p2", "toggle_control")


def test_sdl_hot_unplug_replug_absorbs_held_toggle(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    assignment = controls.sdl_assignment(
        "03000000deadbeef",
        0,
        "Bluetooth pad",
        "stable-pad-uid",
    )
    config["devices"]["p2"] = assignment
    config["players"]["p2"]["toggle_control"] = [controls.pad_binding(1, "A")]
    controls.save_config(config, path)
    neutral = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    held = dict(neutral)
    held["A"] = 1.0

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value="direct",
        ),
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
        mock.patch.object(
            controls,
            "read_sdl_controller_states",
            side_effect=(
                (controls.SDLControllerState(assignment, neutral, generation=3),),
                (),
                (controls.SDLControllerState(assignment, held, generation=4),),
                (controls.SDLControllerState(assignment, neutral, generation=4),),
                (controls.SDLControllerState(assignment, held, generation=4),),
            ),
        ),
    ):
        poller = controls.BindingPoller(path)
        poller.begin_frame(True, keyboard_enabled=False)
        poller.begin_frame(True, keyboard_enabled=False)
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.down("p2", "toggle_control")
        assert not poller.edge("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.edge("p2", "toggle_control")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.edge("p2", "toggle_control")


def test_sdl_translation_populates_the_complete_xinput_gamepad_layout():
    strengths = {control: 1.0 for control in controls.XINPUT_BUTTONS}
    strengths.update(
        {
            "LT": 0.25,
            "RT": 0.75,
            "LS_LEFT": 1.0,
            "LS_DOWN": 0.5,
            "RS_RIGHT": 0.5,
            "RS_UP": 1.0,
        }
    )

    payload = controls.pack_xinput_gamepad_from_strengths(strengths)

    assert len(payload) == 12
    buttons, left_trigger, right_trigger, lx, ly, rx, ry = struct.unpack(
        "<HBBhhhh", payload
    )
    assert buttons == sum(controls.XINPUT_BUTTONS.values())
    assert (left_trigger, right_trigger) == (64, 191)
    assert (lx, ly, rx, ry) == (-32768, -16384, 16384, 32767)


def test_configured_sdl_p1_stays_as_an_idle_virtual_pad_while_unavailable(tmp_path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p1"] = controls.sdl_assignment("dual-sense", 0, "DualSense")
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    poller._enabled = True
    poller._sdl_states = ()

    assert poller.sdl_xinput_gamepad("p1") == b"\x00" * 12
    assert poller.sdl_xinput_gamepad("p2") is None


def test_assigned_pad_control_can_be_observed_without_action_edge(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    controls.save_config(controls.default_config(), path)
    poller = controls.BindingPoller(path)
    state = controls.XINPUT_STATE()
    state.Gamepad.wButtons = controls.XINPUT_BUTTONS["DPAD_UP"]
    poller._enabled = True
    poller._states = [state, None, None, None]
    assert poller.xinput_control_down("p1", "DPAD_UP")
    assert not poller.xinput_control_down("p1", "DPAD_DOWN")


def test_every_player_follows_the_native_decide_button(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"].update({"p1": 0, "p2": 1, "p3": 2})
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    states = [controls.XINPUT_STATE() for _player in ("p1", "p2", "p3")]
    for state in states:
        state.Gamepad.wButtons = controls.XINPUT_BUTTONS["B"]
    poller._enabled = True
    poller._states = [*states, None]

    for player in ("p1", "p2", "p3"):
        assert poller.native_action_down(player, "confirm", circle_decides=True)
        assert not poller.native_action_down(player, "cancel", circle_decides=True)
        assert not poller.native_action_down(player, "confirm", circle_decides=False)


def test_native_decide_setting_swaps_p2_confirm_and_cancel(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    state = controls.XINPUT_STATE()
    state.Gamepad.wButtons = controls.XINPUT_BUTTONS["B"]
    poller._enabled = True
    poller._states = [None, state, None, None]

    assert poller.native_action_down("p2", "confirm", circle_decides=True)
    assert not poller.native_action_down("p2", "cancel", circle_decides=True)
    assert poller.native_action_edge("p2", "confirm", circle_decides=True)
    assert not poller.native_action_edge("p2", "confirm", circle_decides=True)


def test_live_reload_absorbs_a_held_swapped_confirm_until_release(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    held = controls.XINPUT_STATE()
    held.Gamepad.wButtons = controls.XINPUT_BUTTONS["B"]

    poller._reload_history_pending = True
    with mock.patch.object(
        controls,
        "read_xinput_states",
        return_value=[None, held, None, None],
    ):
        poller.begin_frame(True, keyboard_enabled=False)
    assert not poller.native_action_edge("p2", "confirm", circle_decides=True)

    with mock.patch.object(
        controls,
        "read_xinput_states",
        return_value=[None, controls.XINPUT_STATE(), None, None],
    ):
        poller.begin_frame(True, keyboard_enabled=False)
    assert not poller.native_action_edge("p2", "confirm", circle_decides=True)

    with mock.patch.object(
        controls,
        "read_xinput_states",
        return_value=[None, held, None, None],
    ):
        poller.begin_frame(True, keyboard_enabled=False)
    assert poller.native_action_edge("p2", "confirm", circle_decides=True)


def test_native_decide_setting_does_not_swap_keyboard_bindings(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = None
    config["players"]["p2"]["confirm"] = [controls.key_binding(ord("V"))]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    poller._enabled = True
    poller._keyboard_enabled = True

    with mock.patch.object(
        controls._user32,
        "GetAsyncKeyState",
        side_effect=lambda vk: 0x8000 if vk == ord("V") else 0,
    ):
        assert poller.native_action_down("p2", "confirm", circle_decides=True)


def test_native_action_edge_can_exclude_left_mouse_keyboard_vk(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    controls.set_desktop_owner(config, "p2")
    config["players"]["p2"]["confirm"] = [controls.key_binding(0x01)]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    poller._enabled = True
    poller._keyboard_enabled = True

    with mock.patch.object(
        controls._user32,
        "GetAsyncKeyState",
        side_effect=lambda vk: 0x8000 if vk == 0x01 else 0,
    ):
        assert not poller.native_action_edge(
            "p2", "confirm", False, excluded_keyboard_vks={0x01}
        )
        assert poller.native_action_edge("p2", "confirm", False)


def test_native_action_edge_filter_has_independent_cache_and_history(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    controls.set_desktop_owner(config, "p2")
    config["devices"]["p2"] = 1
    config["players"]["p2"]["confirm"] = [
        controls.key_binding(0x01),
        controls.pad_binding(1, "A"),
    ]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    state = controls.XINPUT_STATE()
    poller._enabled = True
    poller._keyboard_enabled = True
    poller._states = [None, state, None, None]

    with mock.patch.object(
        controls._user32,
        "GetAsyncKeyState",
        side_effect=lambda vk: 0x8000 if vk == 0x01 else 0,
    ):
        assert poller.native_action_edge("p2", "confirm", False)
        assert not poller.native_action_edge(
            "p2", "confirm", False, excluded_keyboard_vks=(0x01,)
        )

        state.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]
        poller._event_strength_cache.clear()
        assert poller.native_action_edge(
            "p2", "confirm", False, excluded_keyboard_vks=(0x01,)
        )
        assert not poller.native_action_edge("p2", "confirm", False)


def test_menu_hold_repeat_starts_after_one_quarter_second():
    assert controls.INITIAL_REPEAT_DELAY == 0.250


def test_physical_dpad_edge_has_independent_history(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    state = controls.XINPUT_STATE()
    state.Gamepad.wButtons = controls.XINPUT_BUTTONS["DPAD_UP"]
    poller._enabled = True
    poller._states = [None, state, None, None]

    assert poller.xinput_control_edge("p2", "DPAD_UP")
    assert not poller.xinput_control_edge("p2", "DPAD_UP")
    # Reading the mapped menu action uses a separate history key.
    assert poller.edge("p2", "menu_up")


def test_each_follower_reads_dpad_down_from_its_assigned_controller(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"].update({"p2": 1, "p3": 2})
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    p2 = controls.XINPUT_STATE()
    p3 = controls.XINPUT_STATE()
    p2.Gamepad.wButtons = controls.XINPUT_BUTTONS["DPAD_DOWN"]
    p3.Gamepad.wButtons = controls.XINPUT_BUTTONS["DPAD_DOWN"]
    poller._enabled = True
    poller._states = [None, p2, p3, None]

    assert poller.repeat("p2", "menu_down")
    assert poller.repeat("p3", "menu_down")
    assert not poller.repeat("p2", "menu_up")
    assert not poller.repeat("p3", "menu_up")


def test_background_frame_reads_pad_but_never_desktop_keyboard(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    p2 = controls.XINPUT_STATE()
    p2.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]

    with (
        mock.patch.object(
            controls, "read_xinput_states", return_value=[None, p2, None, None]
        ),
        mock.patch.object(controls._user32, "GetAsyncKeyState", return_value=0x8000),
    ):
        poller.begin_frame(True, keyboard_enabled=False)

        assert poller.down("p2", "confirm")
        assert not poller.down("p2", "toggle_control")


def test_foreground_frame_keeps_the_existing_keyboard_default(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    controls.save_config(controls.default_config(), path)
    poller = controls.BindingPoller(path)

    with (
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
        mock.patch.object(controls._user32, "GetAsyncKeyState", return_value=0x8000),
    ):
        poller.begin_frame(True)

        assert poller.down("p2", "toggle_control")


def test_shared_keyboard_key_is_visible_only_to_the_desktop_owner(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["players"]["p2"]["flee"] = [controls.key_binding(ord("X"))]
    config["players"]["p3"]["flee"] = [controls.key_binding(ord("X"))]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)

    with mock.patch.object(
        controls._user32,
        "GetAsyncKeyState",
        side_effect=lambda vk: 0x8000 if vk == ord("X") else 0,
    ):
        poller._enabled = True
        poller._keyboard_enabled = True
        assert poller.down("p2", "flee")
        assert not poller.down("p3", "flee")
        assert poller.desktop_input_enabled("p2")
        assert not poller.desktop_input_enabled("p3")


def test_live_desktop_transfer_absorbs_a_held_key_until_repress(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["players"]["p2"]["flee"] = [controls.key_binding(ord("X"))]
    config["players"]["p3"]["flee"] = [controls.key_binding(ord("X"))]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    held = {"value": False}

    with (
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
        mock.patch.object(
            controls._user32,
            "GetAsyncKeyState",
            side_effect=lambda vk: (
                0x8000 if held["value"] and vk == ord("X") else 0
            ),
        ),
    ):
        # The first coherent sample seeds history.  Establish a real press
        # before exercising the live ownership handoff.
        poller.begin_frame(True)
        assert not poller.edge("p2", "flee")
        held["value"] = True
        poller.begin_frame(True)
        assert poller.edge("p2", "flee")
        assert not poller.edge("p3", "flee")
        poller._active_device["p2"] = "keyboard"

        controls.set_desktop_owner(config, "p3")
        controls.save_config(config, path)
        poller._mtime_ns = -1
        poller._next_reload_check = 0.0
        poller.begin_frame(True)

        assert poller.desktop_owner() == "p3"
        assert not poller.down("p2", "flee")
        assert not poller.edge("p3", "flee")
        assert poller.active_device("p2") != "keyboard"

        held["value"] = False
        poller.begin_frame(True)
        assert not poller.edge("p3", "flee")

        held["value"] = True
        poller.begin_frame(True)
        assert poller.edge("p3", "flee")


def test_foreground_resume_absorbs_held_desktop_key_until_repress(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["players"]["p2"]["flee"] = [controls.key_binding(ord("X"))]
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    held = {"value": False}

    with (
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
        mock.patch.object(
            controls._user32,
            "GetAsyncKeyState",
            side_effect=lambda vk: (
                0x8000 if held["value"] and vk == ord("X") else 0
            ),
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.edge("p2", "flee")

        poller.begin_frame(True, keyboard_enabled=False)
        held["value"] = True
        assert not poller.edge("p2", "flee")

        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.edge("p2", "flee")
        assert not poller.repeat("p2", "flee")
        assert not poller.native_action_edge("p2", "flee", False)

        held["value"] = False
        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.edge("p2", "flee")
        held["value"] = True
        poller.begin_frame(True, keyboard_enabled=True)
        assert poller.edge("p2", "flee")


def test_foreground_keyboard_reseed_does_not_consume_new_gamepad_edge(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    idle = controls.XINPUT_STATE()
    pressed = controls.XINPUT_STATE()
    pressed.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]
    sampled = {"state": idle}
    keyboard_held = {"value": False}

    with (
        mock.patch.object(
            controls,
            "read_xinput_states",
            side_effect=lambda: [None, sampled["state"], None, None],
        ),
        mock.patch.object(
            controls._user32,
            "GetAsyncKeyState",
            side_effect=lambda vk: (
                0x8000 if keyboard_held["value"] and vk == 0x20 else 0
            ),
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.edge("p2", "confirm")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.edge("p2", "confirm")

        keyboard_held["value"] = True
        sampled["state"] = pressed
        poller.begin_frame(True, keyboard_enabled=True)
        assert poller.edge("p2", "confirm")
        assert poller.down("p2", "confirm")


def test_foreground_keyboard_reseed_preserves_native_and_repeat_pad_edges(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    idle = controls.XINPUT_STATE()
    pressed = controls.XINPUT_STATE()
    pressed.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]
    sampled = {"state": idle}
    keyboard_held = {"value": False}

    with (
        mock.patch.object(
            controls,
            "read_xinput_states",
            side_effect=lambda: [None, sampled["state"], None, None],
        ),
        mock.patch.object(
            controls._user32,
            "GetAsyncKeyState",
            side_effect=lambda vk: (
                0x8000 if keyboard_held["value"] and vk == 0x20 else 0
            ),
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.native_action_edge("p2", "confirm", False)
        assert not poller.repeat("p2", "confirm")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.native_action_edge("p2", "confirm", False)
        assert not poller.repeat("p2", "confirm")

        keyboard_held["value"] = True
        sampled["state"] = pressed
        poller.begin_frame(True, keyboard_enabled=True)

        assert poller.native_action_edge("p2", "confirm", False)
        assert poller.repeat("p2", "confirm")


def test_foreground_keyboard_reseed_does_not_restart_held_pad_repeat_delay(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = 1
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    neutral = controls.XINPUT_STATE()
    pressed = controls.XINPUT_STATE()
    pressed.Gamepad.wButtons = controls.XINPUT_BUTTONS["A"]
    keyboard_held = {"value": False}
    pad_held = {"value": False}

    with (
        mock.patch.object(
            controls,
            "read_xinput_states",
            side_effect=lambda: [
                None,
                pressed if pad_held["value"] else neutral,
                None,
                None,
            ],
        ),
        mock.patch.object(
            controls._user32,
            "GetAsyncKeyState",
            side_effect=lambda vk: (
                0x8000 if keyboard_held["value"] and vk == 0x20 else 0
            ),
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.repeat("p2", "confirm")
        pad_held["value"] = True
        poller.begin_frame(True, keyboard_enabled=True)
        assert poller.repeat("p2", "confirm")
        poller._next_repeat[("p2", "confirm")] = 0.0

        poller.begin_frame(True, keyboard_enabled=False)
        keyboard_held["value"] = True
        poller.begin_frame(True, keyboard_enabled=True)

        assert poller.repeat("p2", "confirm")


def test_startup_absorbs_held_toggle_until_release_and_repress(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    controls.save_config(controls.default_config(), path)
    poller = controls.BindingPoller(path)
    held = {"value": True}

    with (
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
        mock.patch.object(
            controls._user32,
            "GetAsyncKeyState",
            side_effect=lambda vk: 0x8000 if held["value"] and vk == 0x75 else 0,
        ),
    ):
        poller.begin_frame(True, keyboard_enabled=True)
        assert poller.down("p2", "toggle_control")
        assert not poller.edge("p2", "toggle_control")

        held["value"] = False
        poller.begin_frame(True, keyboard_enabled=True)
        assert not poller.edge("p2", "toggle_control")

        held["value"] = True
        poller.begin_frame(True, keyboard_enabled=True)
        assert poller.edge("p2", "toggle_control")


def test_p1_native_left_stick_does_not_require_move_action_bindings(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    assert not any(action.startswith("move_") for action in config["players"]["p1"])
    controls.save_config(config, path)
    poller = controls.BindingPoller(path)
    state = controls.XINPUT_STATE()
    state.Gamepad.sThumbLX = 32767
    state.Gamepad.sThumbLY = -32768
    poller._enabled = True
    poller._states = [state, None, None, None]

    forward, right = poller.native_left_stick_axes("p1")

    assert forward == -1.0
    assert right == 1.0


def test_controller_broker_reader_retries_mapping_without_opening_sdl(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    assignment = controls.sdl_assignment(
        "03000000deadbeef",
        0,
        "Shared SDL pad",
        "stable-pad-uid",
    )
    config["devices"]["p2"] = assignment
    controls.save_config(config, path)
    idle = (0.0,) * len(controls.GAMEPAD_CONTROLS)
    active = list(idle)
    active[controls.GAMEPAD_CONTROLS.index("A")] = 1.0
    channel = mock.Mock()
    channel.open.return_value = False
    channel.read.side_effect = (
        None,
        (idle, tuple(active), idle),
        (idle, idle, idle),
        (idle, tuple(active), idle),
    )

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value=controls.controller_state_transport.ROLE_READER,
        ),
        mock.patch.object(
            controls.controller_state_transport,
            "ControllerStateChannel",
            return_value=channel,
        ),
        mock.patch.object(
            controls,
            "read_xinput_states",
            return_value=[None] * 4,
        ),
        mock.patch.object(
            controls,
            "read_sdl_controller_states",
            side_effect=AssertionError("reader must never open SDL directly"),
        ) as direct_sdl,
    ):
        poller = controls.BindingPoller(path)
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller._sdl_states == ()
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.xinput_control_down("p2", "A")
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)

    assert poller.xinput_control_down("p2", "A")
    assert poller.xinput_control_edge("p2", "A")
    assert channel.read.call_count == 4
    direct_sdl.assert_not_called()


class _BrokerSampleChannel:
    def __init__(self, samples):
        self.samples = iter(samples)
        self.sample_writer_id = None
        self.sample_connected_mask = 0
        self.sample_generations = (0, 0, 0)

    def open(self):
        return True

    def read(self):
        rows, writer_id, connected_mask, generations = next(self.samples)
        self.sample_writer_id = writer_id
        self.sample_connected_mask = connected_mask
        self.sample_generations = generations
        return rows


def _broker_rows(*, p2_control: str | None = None):
    idle = (0.0,) * len(controls.GAMEPAD_CONTROLS)
    p2 = list(idle)
    if p2_control is not None:
        p2[controls.GAMEPAD_CONTROLS.index(p2_control)] = 1.0
    return idle, tuple(p2), idle


def test_controller_broker_writer_restart_absorbs_an_inherited_button(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = controls.sdl_assignment(
        "03000000deadbeef",
        0,
        "Shared SDL pad",
        "stable-pad-uid",
    )
    controls.save_config(config, path)
    channel = _BrokerSampleChannel(
        (
            (_broker_rows(), 1001, 0b010, (0, 7, 0)),
            (_broker_rows(p2_control="A"), 2002, 0b010, (0, 1, 0)),
            (_broker_rows(), 2002, 0b010, (0, 1, 0)),
            (_broker_rows(p2_control="A"), 2002, 0b010, (0, 1, 0)),
        )
    )

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value=controls.controller_state_transport.ROLE_READER,
        ),
        mock.patch.object(
            controls.controller_state_transport,
            "ControllerStateChannel",
            return_value=channel,
        ),
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
        mock.patch.object(
            controls,
            "read_sdl_controller_states",
            side_effect=AssertionError("reader must never open SDL directly"),
        ),
    ):
        poller = controls.BindingPoller(path)
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.xinput_control_down("p2", "A")
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.xinput_control_edge("p2", "A")


def test_controller_broker_hotplug_generation_absorbs_held_reconnect(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = controls.sdl_assignment(
        "03000000deadbeef",
        0,
        "Shared SDL pad",
        "stable-pad-uid",
    )
    controls.save_config(config, path)
    channel = _BrokerSampleChannel(
        (
            (_broker_rows(), 1001, 0b010, (0, 7, 0)),
            (_broker_rows(p2_control="A"), 1001, 0b010, (0, 8, 0)),
            (_broker_rows(), 1001, 0b010, (0, 8, 0)),
            (_broker_rows(p2_control="A"), 1001, 0b010, (0, 8, 0)),
        )
    )

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value=controls.controller_state_transport.ROLE_READER,
        ),
        mock.patch.object(
            controls.controller_state_transport,
            "ControllerStateChannel",
            return_value=channel,
        ),
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
    ):
        poller = controls.BindingPoller(path)
        poller.begin_frame(True, keyboard_enabled=False)
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.xinput_control_edge("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.xinput_control_edge("p2", "A")


def test_controller_broker_connection_mask_fails_closed_until_pad_is_present(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    config["devices"]["p2"] = controls.sdl_assignment(
        "03000000deadbeef",
        0,
        "Shared SDL pad",
        "stable-pad-uid",
    )
    controls.save_config(config, path)
    channel = _BrokerSampleChannel(
        (
            (_broker_rows(p2_control="A"), 1001, 0, (0, 0, 0)),
            (_broker_rows(p2_control="A"), 1001, 0b010, (0, 1, 0)),
        )
    )

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value=controls.controller_state_transport.ROLE_READER,
        ),
        mock.patch.object(
            controls.controller_state_transport,
            "ControllerStateChannel",
            return_value=channel,
        ),
        mock.patch.object(controls, "read_xinput_states", return_value=[None] * 4),
    ):
        poller = controls.BindingPoller(path)
        poller.begin_frame(True, keyboard_enabled=False)
        assert not poller.xinput_control_down("p2", "A")
        poller.begin_frame(True, keyboard_enabled=False)
        assert poller.xinput_control_down("p2", "A")
        assert not poller.xinput_control_edge("p2", "A")


def test_controller_broker_writer_uses_a_live_secondary_sdl_alias(tmp_path: Path):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    primary = controls.sdl_assignment(
        "03000000primary",
        0,
        "Physical mirror",
        "physical-uid",
    )
    secondary = controls.sdl_assignment(
        "03000000secondary",
        0,
        "Virtual mirror",
        "virtual-uid",
    )
    config["devices"]["p2"] = 1
    controls.register_device_alias_group(config, 1, (primary, secondary))
    controls.save_config(config, path)
    channel = mock.Mock()
    channel.open.return_value = True
    channel.publish.return_value = True

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value=controls.controller_state_transport.ROLE_WRITER,
        ),
        mock.patch.object(
            controls.controller_state_transport,
            "ControllerStateChannel",
            return_value=channel,
        ),
    ):
        poller = controls.BindingPoller(path)
    strengths = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    strengths["A"] = 1.0
    poller._sdl_states = (
        controls.SDLControllerState(secondary, strengths, generation=37),
    )

    poller._publish_shared_sdl_states()

    rows = channel.publish.call_args.args[0]
    assert rows[1][controls.GAMEPAD_CONTROLS.index("A")] == 1.0
    assert channel.publish.call_args.kwargs["connected"] == [False, True, False]
    assert channel.publish.call_args.kwargs["generations"] == [0, 37, 0]


def test_controller_broker_writer_uses_a_sibling_of_a_direct_sdl_assignment(
    tmp_path: Path,
):
    path = tmp_path / "controller_config.json"
    config = controls.default_config()
    preferred = controls.sdl_assignment(
        "03000000preferred", 0, "Preferred mirror", "preferred-uid"
    )
    sibling = controls.sdl_assignment(
        "03000000sibling", 0, "Live mirror", "live-uid"
    )
    config["devices"]["p2"] = preferred
    controls.register_device_alias_group(config, 1, (preferred, sibling))
    controls.save_config(config, path)
    channel = mock.Mock()
    channel.open.return_value = True
    channel.publish.return_value = True

    with (
        mock.patch.object(
            controls.controller_state_transport,
            "configured_role",
            return_value=controls.controller_state_transport.ROLE_WRITER,
        ),
        mock.patch.object(
            controls.controller_state_transport,
            "ControllerStateChannel",
            return_value=channel,
        ),
    ):
        poller = controls.BindingPoller(path)
    strengths = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    strengths["A"] = 1.0
    poller._sdl_states = (
        controls.SDLControllerState(sibling, strengths, generation=41),
    )

    poller._publish_shared_sdl_states()

    rows = channel.publish.call_args.args[0]
    assert rows[1][controls.GAMEPAD_CONTROLS.index("A")] == 1.0
    assert channel.publish.call_args.kwargs["connected"] == [False, True, False]
    assert channel.publish.call_args.kwargs["generations"] == [0, 41, 0]
