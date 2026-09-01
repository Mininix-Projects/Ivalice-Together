import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import controller_bindings as controls
import coop_launcher
import launcher_core
import pygame


def test_launcher_binding_grid_uses_the_shared_four_slot_contract():
    card = inspect.getsource(coop_launcher.LauncherApp.draw_binding_card)
    page = inspect.getsource(coop_launcher.LauncherApp.draw_controls)
    self_limit = "controls.MAX_BINDINGS_PER_ACTION"
    assert self_limit in card
    assert self_limit in page
    assert "SLOTS" in page


def test_launcher_does_not_advertise_the_removed_change_character_route():
    assert "change_character" not in coop_launcher.ACTION_LABELS
    for player in ("p2", "p3"):
        assert "change_character" not in {
            action for action, _label in controls.PLAYER_ACTIONS[player]
        }


def pad_state(buttons=0, *, left_x=0, left_y=0, lt=0, rt=0):
    state = controls.XINPUT_STATE()
    state.Gamepad.wButtons = buttons
    state.Gamepad.sThumbLX = left_x
    state.Gamepad.sThumbLY = left_y
    state.Gamepad.bLeftTrigger = lt
    state.Gamepad.bRightTrigger = rt
    return state


def sdl_state(guid="sdl-guid", name="SDL Controller", **active_controls):
    strengths = {control: 0.0 for control in controls.GAMEPAD_CONTROLS}
    strengths.update({control: float(value) for control, value in active_controls.items()})
    return controls.SDLControllerState(
        controls.sdl_assignment(guid, 0, name),
        strengths,
    )


class LauncherConfigTests(unittest.TestCase):
    def test_launcher_opens_the_game_executable_from_its_own_root(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(launcher_core, "process_ids", return_value=()),
        ):
            root = Path(directory)
            supervisor = launcher_core.CoopLaunchSupervisor(root)

        self.assertEqual(
            supervisor.game_executable,
            (root / "x64" / launcher_core.GAME_EXECUTABLE).resolve(),
        )

    def test_runtime_storage_probe_leaves_no_file_behind(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "coop"
            launcher_core.ensure_runtime_storage(directory)
            self.assertTrue(directory.is_dir())
            self.assertEqual(tuple(directory.iterdir()), ())

    def test_public_coop_overrides_are_limited_to_runtime_assets(self):
        self.assertEqual(
            launcher_core.GAME_OVERRIDE_PATHS,
            (
                Path("gamedata/d3d11/artdata/menu/targetline_p_clut_2.dds.phyre"),
                Path("gamedata/d3d11/artdata/menu/targetline_p_clut_6.dds.phyre"),
                *launcher_core.DAMAGE_NUMBER_OVERRIDE_PATHS,
            ),
        )

    def test_default_language_is_english(self):
        self.assertEqual(
            launcher_core.normalize_launcher_config(None)["language"],
            "en",
        )
        self.assertEqual(
            launcher_core.normalize_launcher_config({})["language"],
            "en",
        )

    def test_legacy_config_gains_english_without_losing_preferences(self):
        legacy = launcher_core.normalize_launcher_config(
            {
                "version": 1,
                "menu_sounds": False,
                "sound_volume": 0.34,
                "minimize_when_ready": False,
            }
        )
        self.assertEqual(legacy["version"], 6)
        self.assertEqual(legacy["language"], "en")
        self.assertFalse(legacy["menu_sounds"])
        self.assertEqual(
            legacy["battle_menu_sounds"],
            {"p1": True, "p2": True, "p3": True},
        )
        self.assertEqual(legacy["sound_volume"], 0.34)
        self.assertFalse(legacy["minimize_when_ready"])
        self.assertFalse(legacy["damage_meter_enabled"])
        self.assertEqual(legacy["damage_meter_mode"], "area")
        self.assertFalse(legacy["damage_meter_effective"])
        self.assertEqual(legacy["damage_meter_session_id"], 1)

    def test_only_supported_languages_are_accepted(self):
        self.assertEqual(
            launcher_core.normalize_launcher_config({"language": "pt"})["language"],
            "pt",
        )
        for invalid in (
            "PT",
            "pt-BR",
            "english",
            "",
            None,
            1,
            True,
            [],
            {},
        ):
            with self.subTest(language=invalid):
                self.assertEqual(
                    launcher_core.normalize_launcher_config({"language": invalid})[
                        "language"
                    ],
                    "en",
                )

    def test_damage_session_id_is_bounded_and_migrates_safely(self):
        self.assertEqual(
            launcher_core.normalize_launcher_config(
                {"damage_meter_session_id": "12"}
            )["damage_meter_session_id"],
            12,
        )
        for invalid in (None, "", "unknown", -9, 0):
            with self.subTest(value=invalid):
                self.assertEqual(
                    launcher_core.normalize_launcher_config(
                        {"damage_meter_session_id": invalid}
                    )["damage_meter_session_id"],
                    1,
                )

    def test_portuguese_round_trip_preserves_existing_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher_config.json"
            saved = launcher_core.save_launcher_config(
                {
                    "version": 1,
                    "language": "pt",
                    "menu_sounds": False,
                    "sound_volume": 0.41,
                    "minimize_when_ready": False,
                    "damage_meter_enabled": True,
                    "damage_meter_mode": "indefinite",
                    "damage_meter_effective": True,
                },
                path,
            )
            self.assertEqual(
                saved,
                {
                    "version": 6,
                    "language": "pt",
                    "menu_sounds": False,
                    "battle_menu_sounds": {
                        "p1": True,
                        "p2": True,
                        "p3": True,
                    },
                    "sound_volume": 0.41,
                    "minimize_when_ready": False,
                    "damage_meter_enabled": True,
                    "damage_meter_mode": "indefinite",
                    "damage_meter_effective": True,
                    "damage_meter_session_id": 1,
                },
            )
            self.assertEqual(launcher_core.load_launcher_config(path), saved)

    def test_launcher_config_is_clamped_and_saved_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher_config.json"
            saved = launcher_core.save_launcher_config(
                {
                    "menu_sounds": 0,
                    "sound_volume": 5,
                    "minimize_when_ready": "yes",
                },
                path,
            )
            self.assertFalse(saved["menu_sounds"])
            self.assertEqual(saved["sound_volume"], 1.0)
            self.assertTrue(saved["minimize_when_ready"])
            self.assertEqual(launcher_core.load_launcher_config(path), saved)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_damage_meter_mode_is_validated(self):
        enabled = launcher_core.normalize_launcher_config(
            {
                "damage_meter_enabled": 1,
                "damage_meter_mode": "INDEFINITE",
            }
        )
        self.assertTrue(enabled["damage_meter_enabled"])
        self.assertEqual(enabled["damage_meter_mode"], "indefinite")
        self.assertFalse(enabled["damage_meter_effective"])
        invalid = launcher_core.normalize_launcher_config(
            {"damage_meter_mode": "forever"}
        )
        self.assertEqual(invalid["damage_meter_mode"], "area")

        effective = launcher_core.normalize_launcher_config(
            {"damage_meter_effective": 1}
        )
        self.assertTrue(effective["damage_meter_effective"])

    def test_battle_menu_sound_preferences_are_independent(self):
        config = launcher_core.normalize_launcher_config(
            {
                "battle_menu_sounds": {
                    "p1": False,
                    "p2": True,
                    "p3": False,
                }
            }
        )
        self.assertEqual(
            config["battle_menu_sounds"],
            {"p1": False, "p2": True, "p3": False},
        )

    def test_source_portrait_resolver_rejects_path_traversal(self):
        resolved = launcher_core.bundled_portrait("../0-Vaan.png")
        self.assertEqual(resolved.name, "0-Vaan.png")
        self.assertEqual(resolved.parent.name, "portraits")
        self.assertTrue(resolved.is_file())


class LauncherSupervisorStatusTests(unittest.TestCase):
    def test_supervisor_has_no_direct_game_start_path(self):
        self.assertFalse(hasattr(launcher_core.CoopLaunchSupervisor, "start"))
        self.assertFalse(hasattr(launcher_core.CoopLaunchSupervisor, "_worker"))

    def test_mutex_without_status_is_preparing_never_ready(self):
        supervisor = launcher_core.CoopLaunchSupervisor.__new__(
            launcher_core.CoopLaunchSupervisor
        )
        supervisor.runtime_directory = Path("runtime")
        with mock.patch.object(
            launcher_core.runtime_status,
            "read_status",
            return_value=None,
        ):
            state, message = supervisor._attached_state(1234)
        self.assertEqual(state, "preparing")
        self.assertIn("Confirmando", message)

    def test_session_active_covers_monitor_and_attached_runtime(self):
        supervisor = launcher_core.CoopLaunchSupervisor.__new__(
            launcher_core.CoopLaunchSupervisor
        )
        supervisor.thread = None
        supervisor.attached_pid = None
        self.assertFalse(supervisor.session_active)

        supervisor.thread = mock.Mock()
        supervisor.thread.is_alive.return_value = True
        self.assertTrue(supervisor.session_active)

        supervisor.thread.is_alive.return_value = False
        supervisor.attached_pid = 1234
        self.assertTrue(supervisor.session_active)

    def test_configurator_attaches_to_runtime_started_outside_launcher(self):
        supervisor = launcher_core.CoopLaunchSupervisor.__new__(
            launcher_core.CoopLaunchSupervisor
        )
        supervisor.game_executable = Path("C:/Game/x64/FFXII_TZA.exe")
        supervisor.thread = None
        supervisor.attached_pid = None
        supervisor._lock = __import__("threading").Lock()
        supervisor._begin_monitoring = mock.Mock()

        with (
            mock.patch.object(launcher_core, "process_ids", return_value=(4242,)),
            mock.patch.object(
                launcher_core,
                "active_runtime_pid",
                return_value=4242,
            ),
        ):
            attached = supervisor.attach_if_running()

        self.assertTrue(attached)
        supervisor._begin_monitoring.assert_called_once_with(
            4242,
            publish_initial=True,
        )

    def test_configurator_does_not_start_a_second_runtime_monitor(self):
        supervisor = launcher_core.CoopLaunchSupervisor.__new__(
            launcher_core.CoopLaunchSupervisor
        )
        supervisor.thread = mock.Mock()
        supervisor.thread.is_alive.return_value = True
        supervisor.attached_pid = 4242
        supervisor._lock = __import__("threading").Lock()
        supervisor._begin_monitoring = mock.Mock()

        with mock.patch.object(launcher_core, "active_runtime_pid") as active:
            attached = supervisor.attach_if_running()

        self.assertFalse(attached)
        active.assert_not_called()
        supervisor._begin_monitoring.assert_not_called()


class WindowIconLifecycleTests(unittest.TestCase):
    def test_identity_and_icon_surround_native_window_creation(self):
        calls = []

        class StopAfterNativeIcon(RuntimeError):
            pass

        def record(name, result=None):
            def callback(*_args, **_kwargs):
                calls.append(name)
                return result

            return callback

        def stop_after_native_icon(*_args, **_kwargs):
            calls.append("native_icon")
            raise StopAfterNativeIcon

        with (
            mock.patch.object(
                coop_launcher.LauncherApp,
                "_enable_windows_dpi_awareness",
                side_effect=record("dpi"),
            ),
            mock.patch.object(
                coop_launcher.LauncherApp,
                "_set_windows_identity",
                side_effect=record("identity"),
            ),
            mock.patch.object(
                coop_launcher.LauncherApp,
                "_set_icon",
                side_effect=record("pygame_icon"),
            ),
            mock.patch.object(
                coop_launcher.LauncherApp,
                "_set_native_window_icons",
                side_effect=stop_after_native_icon,
            ),
            mock.patch.object(
                coop_launcher.pygame,
                "init",
                side_effect=record("pygame_init"),
            ),
            mock.patch.object(
                coop_launcher.pygame.joystick,
                "get_init",
                return_value=True,
            ),
            mock.patch.object(
                coop_launcher.pygame,
                "Surface",
                return_value=object(),
            ),
            mock.patch.object(
                coop_launcher.pygame.display,
                "set_mode",
                side_effect=record("set_mode", object()),
            ),
            mock.patch.object(coop_launcher.pygame.display, "set_caption"),
        ):
            with self.assertRaises(StopAfterNativeIcon):
                coop_launcher.LauncherApp(preview=False)

        self.assertEqual(
            calls,
            [
                "dpi",
                "identity",
                "pygame_init",
                "pygame_icon",
                "set_mode",
                "native_icon",
            ],
        )


class ControlEditorTests(unittest.TestCase):
    def make_editor(self, directory):
        return launcher_core.ControlEditor(Path(directory) / "controls.json")

    def test_keyboard_is_rejected_for_a_player_without_desktop_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.begin_capture("p1", "confirm", 0, [None] * 4)
            result = editor.feed_keyboard_vk(ord("Q"))
            self.assertEqual(result.kind, "invalid")
            self.assertIn("pertencem a P2", result.message)
            self.assertIsNotNone(editor.capture_target)

    def test_keyboard_capture_is_allowed_for_each_desktop_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            for player in controls.PLAYER_IDS:
                with self.subTest(player=player):
                    editor.assign_desktop(player)
                    editor.begin_capture(player, "confirm", 0, [None] * 4)
                    result = editor.feed_keyboard_vk(ord(player[-1]))
                    self.assertEqual(result.kind, "bound")
                    self.assertTrue(editor.owns_desktop(player))

    def test_desktop_transfer_preserves_controllers_and_inactive_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.config["devices"].update({"p1": 0, "p2": 1, "p3": 2})
            devices_before = dict(editor.config["devices"])
            p2_bindings_before = json.loads(
                json.dumps(editor.config["players"]["p2"])
            )

            previous = editor.assign_desktop("p3")

            self.assertEqual(previous, "p2")
            self.assertEqual(editor.desktop_owner(), "p3")
            self.assertEqual(editor.config["devices"], devices_before)
            self.assertEqual(editor.config["players"]["p2"], p2_bindings_before)
            self.assertTrue(
                any(
                    binding.get("device") == "keyboard"
                    for bindings in editor.config["players"]["p2"].values()
                    for binding in bindings
                )
            )

    def test_legacy_normalization_is_reported_and_persisted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controls.json"
            legacy = controls.default_config()
            legacy["version"] = 8
            legacy.pop("desktop")
            path.write_text(json.dumps(legacy), encoding="utf-8")
            editor = launcher_core.ControlEditor(path)
            report = editor.normalization_report
            app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
            app.editor = editor
            app.show_status = mock.Mock()
            app.audio = mock.Mock()

            with mock.patch.object(
                controls,
                "save_config",
                wraps=controls.save_config,
            ) as save:
                self.assertTrue(app.persist_control_normalization(report))
                editor.reload()

            save.assert_called_once_with(mock.ANY, path)
            self.assertFalse(editor.dirty)
            self.assertFalse(editor.normalization_report.needs_persist)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["version"],
                controls.CONFIG_VERSION,
            )

    def test_future_control_config_is_read_only_and_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controls.json"
            future = controls.default_config()
            future["version"] = controls.CONFIG_VERSION + 1
            future["future_field"] = {"must": "survive"}
            payload = (json.dumps(future, indent=1) + "\n").encode("utf-8")
            path.write_bytes(payload)
            editor = launcher_core.ControlEditor(path)
            in_memory = json.loads(json.dumps(editor.config))

            self.assertTrue(editor.read_only)
            self.assertFalse(editor.dirty)
            with self.assertRaises(controls.UnsupportedControllerConfigVersion):
                editor.assign_desktop("p3")
            with self.assertRaises(controls.UnsupportedControllerConfigVersion):
                editor.restore_defaults()
            with self.assertRaises(controls.UnsupportedControllerConfigVersion):
                editor.save()

            self.assertEqual(editor.config, in_memory)
            self.assertEqual(path.read_bytes(), payload)

    def test_unassigned_player_auto_claims_pad_during_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.assign_controller("p2", None)
            editor.begin_capture("p2", "confirm", 0, [None] * 4)
            result = editor.poll_xinput(
                [None, None, pad_state(controls.XINPUT_BUTTONS["A"]), None]
            )
            self.assertEqual(result.kind, "bound")
            self.assertEqual(editor.assigned("p2"), 2)
            self.assertEqual(
                editor.bindings("p2", "confirm")[0],
                controls.pad_binding(2, "A"),
            )

    def test_assigned_player_rejects_input_from_wrong_pad(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.assign_controller("p2", 1)
            self.assertEqual(editor.assigned("p2"), 1)
            editor.begin_capture("p2", "confirm", 0, [None] * 4)
            result = editor.poll_xinput(
                [pad_state(controls.XINPUT_BUTTONS["A"]), None, None, None]
            )
            self.assertEqual(result.kind, "wrong_pad")
            self.assertIsNotNone(editor.capture_target)

    def test_held_input_requires_release_and_repress(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            held = [None, pad_state(controls.XINPUT_BUTTONS["X"]), None, None]
            editor.begin_capture("p2", "flee", 0, held)
            self.assertIsNone(editor.poll_xinput(held))
            self.assertIsNone(editor.poll_xinput([None] * 4))
            result = editor.poll_xinput(held)
            self.assertEqual(result.kind, "bound")

    def test_start_assigns_controller_and_atomically_swaps_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.assign_controller("p2", 1)
            editor.begin_assignment("p2", [None] * 4)
            result = editor.poll_xinput(
                [pad_state(controls.XINPUT_BUTTONS["START"]), None, None, None]
            )
            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p2"), 0)
            self.assertEqual(editor.assigned("p1"), 1)
            self.assertTrue(
                all(
                    binding["index"] == 1
                    for action, _label in controls.PLAYER_ACTIONS["p1"]
                    for binding in editor.bindings("p1", action)
                    if binding.get("device") == "xinput"
                )
            )

    def test_assigning_remote_pad_to_p1_moves_local_pad_to_previous_role(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.assign_controller("p2", 1)
            editor.begin_assignment("p1", [None] * 4)
            result = editor.poll_xinput(
                [None, pad_state(controls.XINPUT_BUTTONS["START"]), None, None]
            )

            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p1"), 1)
            self.assertEqual(editor.assigned("p2"), 0)
            for player, index in (("p1", 1), ("p2", 0)):
                self.assertTrue(
                    all(
                        binding["index"] == index
                        for action, _label in controls.PLAYER_ACTIONS[player]
                        for binding in editor.bindings(player, action)
                        if binding.get("device") == "xinput"
                    )
                )

    def test_start_assignment_restores_missing_normal_pad_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.config["players"]["p2"]["move_up"] = [controls.key_binding(ord("W"))]
            editor.config["players"]["p2"]["confirm"] = [controls.key_binding(0x20)]

            editor.begin_assignment("p2", [None] * 4)
            result = editor.poll_xinput(
                [None, None, None, pad_state(controls.XINPUT_BUTTONS["START"])]
            )

            self.assertEqual(result.kind, "assigned")
            self.assertIn(
                controls.pad_binding(3, "LS_UP"),
                editor.bindings("p2", "move_up"),
            )
            self.assertIn(
                controls.pad_binding(3, "A"),
                editor.bindings("p2", "confirm"),
            )

    def test_assignment_preserves_a_custom_pad_control(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.config["players"]["p2"]["confirm"] = [controls.pad_binding(1, "X")]

            editor.assign_controller("p2", 3)

            self.assertEqual(
                editor.bindings("p2", "confirm"),
                [controls.pad_binding(3, "X")],
            )

    def test_assignment_ignores_every_controller_input_except_start(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.begin_assignment("p2", [None] * 4)
            noise = (
                controls.XINPUT_BUTTONS["A"]
                | controls.XINPUT_BUTTONS["B"]
                | controls.XINPUT_BUTTONS["DPAD_UP"]
                | controls.XINPUT_BUTTONS["LB"]
            )
            result = editor.poll_xinput(
                [pad_state(noise, left_x=32767, rt=255), None, None, None]
            )
            self.assertIsNone(result)
            self.assertEqual(editor.assign_target, "p2")

    def test_assignment_merges_simultaneous_xinput_and_sdl_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            dualsense = sdl_state(
                "dualsense-guid",
                "DualSense Wireless Controller",
                START=1.0,
            )
            editor.begin_assignment("p2", [None] * 4)

            result = editor.poll_assignment(
                [pad_state(controls.XINPUT_BUTTONS["START"]), None, None, None],
                [dualsense],
                now=10.0,
            )

            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p2"), 0)
            self.assertIsNone(editor.assigned("p1"))
            self.assertTrue(
                controls.device_assignments_overlap(
                    editor.config,
                    0,
                    dualsense.assignment,
                )
            )

    def test_assignment_merges_ds4windows_physical_and_virtual_mirrors(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            physical = sdl_state(
                "physical-ds4",
                "Wireless Controller",
                START=1.0,
            )
            virtual = sdl_state(
                "virtual-xbox",
                "Controller (XBOX 360 For Windows)",
                START=1.0,
            )
            editor.begin_assignment("p2", [None] * 4)

            result = editor.poll_assignment(
                [pad_state(controls.XINPUT_BUTTONS["START"]), None, None, None],
                [physical, virtual],
                now=10.0,
            )

            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p2"), 0)
            self.assertTrue(
                controls.device_assignments_overlap(
                    editor.config,
                    physical.assignment,
                    virtual.assignment,
                )
            )
            self.assertEqual(
                len(controls.aliased_sdl_assignments(editor.config, 0)),
                2,
            )

    def test_assignment_correlates_backend_edges_from_adjacent_poll_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            dualsense = sdl_state("dualsense-guid", "DualSense", START=1.0)
            editor.begin_assignment("p2", [None] * 4)

            self.assertIsNone(
                editor.poll_assignment(
                    [None, pad_state(controls.XINPUT_BUTTONS["START"]), None, None],
                    [],
                    now=20.0,
                )
            )
            result = editor.poll_assignment(
                [None] * 4,
                [dualsense],
                now=20.1,
            )

            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p2"), 1)
            self.assertTrue(
                controls.device_assignments_overlap(
                    editor.config,
                    1,
                    dualsense.assignment,
                )
            )

    def test_assignment_correlates_a_delayed_sdl_counterpart(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            dualsense = sdl_state("dualsense-guid", "DualSense", START=1.0)
            editor.begin_assignment("p2", [None] * 4)

            self.assertIsNone(
                editor.poll_assignment(
                    [None, pad_state(controls.XINPUT_BUTTONS["START"]), None, None],
                    [],
                    now=25.0,
                )
            )
            result = editor.poll_assignment(
                [None] * 4,
                [dualsense],
                now=25.5,
            )

            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p2"), 1)
            self.assertTrue(
                controls.device_assignments_overlap(
                    editor.config,
                    1,
                    dualsense.assignment,
                )
            )

    def test_xinput_only_assignment_waits_for_alias_window_then_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.begin_assignment("p2", [None] * 4)
            held = [None, pad_state(controls.XINPUT_BUTTONS["START"]), None, None]

            self.assertIsNone(editor.poll_assignment(held, [], now=30.0))
            result = editor.poll_assignment(held, [], now=30.8)

            self.assertEqual(result.kind, "assigned")
            self.assertEqual(editor.assigned("p2"), 1)

    def test_alias_counterpart_cannot_be_assigned_to_a_second_player(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            dualsense = controls.sdl_assignment("dualsense-guid", 0, "DualSense")
            controls.register_device_alias(editor.config, 0, dualsense)

            displaced = editor.assign_controller("p2", dualsense)

            self.assertEqual(displaced, "p1")
            self.assertIsNone(editor.assigned("p1"))
            self.assertEqual(editor.assigned("p2"), dualsense)

    def test_discovered_alias_repairs_a_legacy_cross_backend_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            dualsense = controls.sdl_assignment("dualsense-guid", 0, "DualSense")
            editor.config["devices"].update({"p1": 0, "p2": dualsense})
            controls.register_device_alias(editor.config, 0, dualsense)

            displaced = editor.assign_controller("p2", 0)

            self.assertEqual(displaced, "p1")
            self.assertIsNone(editor.assigned("p1"))
            self.assertEqual(editor.assigned("p2"), 0)
            editor.save()
            self.assertIsNone(editor.assigned("p1"))
            self.assertEqual(editor.assigned("p2"), 0)

    def test_binding_deduplicates_same_action(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.begin_capture("p2", "confirm", 1, [None] * 4)
            result = editor.feed_keyboard_vk(0x20)
            self.assertEqual(result.kind, "bound")
            self.assertEqual(
                editor.bindings("p2", "confirm"),
                [
                    controls.key_binding(0x20),
                    controls.key_binding(0x01),
                    controls.pad_binding(1, "A"),
                ],
            )

    def test_fourth_binding_slot_is_editable_persisted_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            editor = self.make_editor(directory)
            editor.config["players"]["p2"]["battle_menu"] = [
                controls.key_binding(vk) for vk in (ord("F"), ord("G"), ord("H"))
            ]

            editor.begin_capture("p2", "battle_menu", 3, [None] * 4)
            result = editor.feed_keyboard_vk(ord("J"))

            self.assertEqual(result.kind, "bound")
            self.assertEqual(
                editor.bindings("p2", "battle_menu"),
                [
                    controls.key_binding(vk)
                    for vk in (ord("F"), ord("G"), ord("H"), ord("J"))
                ],
            )
            editor.save()
            self.assertEqual(len(editor.bindings("p2", "battle_menu")), 4)
            with self.assertRaisesRegex(ValueError, "0..3"):
                editor.begin_capture(
                    "p2",
                    "battle_menu",
                    controls.MAX_BINDINGS_PER_ACTION,
                    [None] * 4,
                )


class LauncherGamepadInputTests(unittest.TestCase):
    @staticmethod
    def make_app():
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.last_pad_poll = -1.0
        app.states = [None] * 4
        app.editor = mock.Mock(capture_target=None, assign_target=None)
        app.show_status = mock.Mock()
        app.audio = mock.Mock()
        app.control_player = "p1"
        app.selected_key = "home:start_all"
        app.reset_pointer = mock.Mock()
        return app

    def test_xinput_name_comes_from_its_captured_alias_not_list_position(self):
        app = self.make_app()
        config = controls.default_config()
        expected = controls.sdl_assignment("wanted", 0, "Expected Pad", "uid-a")
        controls.register_device_alias(config, 2, expected)
        app.editor.config = config
        app.sdl_states = (
            sdl_state("unrelated", "Wrong Positional Name"),
            controls.SDLControllerState(expected, {}),
        )

        self.assertEqual(app.controller_name(2), "Expected Pad")

    def test_launcher_lists_only_runtime_supported_sdl_controllers(self):
        app = self.make_app()
        app.sdl_states = (sdl_state("supported", "Supported Pad"),)

        app.refresh_joysticks()

        self.assertEqual(app.joystick_names, ("Supported Pad",))
        self.assertEqual(app.joysticks, [])

    def test_all_controller_input_is_ignored_outside_assignment(self):
        app = self.make_app()
        noise = 0
        for name in (
            "START",
            "DPAD_UP",
            "DPAD_DOWN",
            "DPAD_LEFT",
            "DPAD_RIGHT",
            "A",
            "B",
            "LB",
            "RB",
        ):
            noise |= controls.XINPUT_BUTTONS[name]
        states = [
            pad_state(noise, left_x=32767, left_y=-32768, lt=255, rt=255),
            None,
            None,
            None,
        ]
        with mock.patch.object(
            coop_launcher.controls,
            "read_xinput_states",
            return_value=states,
        ):
            app.poll_pad(1.0)
        app.editor.poll_xinput.assert_not_called()
        self.assertFalse(hasattr(app, "start_coop"))

    def test_controller_is_captured_during_binding_capture(self):
        app = self.make_app()
        app.editor.capture_target = ("p2", "confirm", 0)
        states = [
            pad_state(
                controls.XINPUT_BUTTONS["START"] | controls.XINPUT_BUTTONS["A"],
                left_x=32767,
                rt=255,
            ),
            None,
            None,
            None,
        ]
        with mock.patch.object(
            coop_launcher.controls,
            "read_xinput_states",
            return_value=states,
        ):
            app.poll_pad(1.0)
        app.editor.poll_xinput.assert_called_once()
        self.assertFalse(hasattr(app, "start_coop"))

    def test_controller_is_captured_while_game_is_open(self):
        app = self.make_app()
        app.game_open = True
        app.controller_discovery_suspended = False
        app.editor.capture_target = ("p2", "confirm", 0)
        states = [
            pad_state(controls.XINPUT_BUTTONS["A"]),
            None,
            None,
            None,
        ]
        with mock.patch.object(
            coop_launcher.controls,
            "read_xinput_states",
            return_value=states,
        ):
            app.poll_pad(1.0)

        app.editor.poll_xinput.assert_called_once()

    def test_assignment_modal_delegates_start_without_launching_coop(self):
        app = self.make_app()
        app.editor.assign_target = "p2"
        start = controls.XINPUT_BUTTONS["START"]

        def finish_assignment(_states, _sdl_states, *, now):
            app.editor.assign_target = None
            return launcher_core.CaptureResult("assigned", "assigned")

        app.editor.poll_assignment.side_effect = finish_assignment
        with (
            mock.patch.object(
                coop_launcher.controls,
                "read_xinput_states",
                return_value=[pad_state(start), None, None, None],
            ),
            mock.patch.object(
                coop_launcher.controls,
                "read_sdl_controller_states",
                return_value=(),
            ),
        ):
            app.poll_pad(1.0)
        app.editor.poll_assignment.assert_called_once()
        app.editor.save.assert_called_once_with()
        self.assertFalse(hasattr(app, "start_coop"))
        app.reset_pointer.assert_called_once()

    def test_successful_assignment_is_saved_for_live_runtime_reload(self):
        app = self.make_app()
        app.editor.assign_target = "p1"

        def finish_assignment(_states, _sdl_states, *, now):
            app.editor.assign_target = None
            return launcher_core.CaptureResult("assigned", "assigned")

        app.editor.poll_assignment.side_effect = finish_assignment
        with (
            mock.patch.object(
                coop_launcher.controls,
                "read_xinput_states",
                return_value=[
                    None,
                    pad_state(controls.XINPUT_BUTTONS["START"]),
                    None,
                    None,
                ],
            ),
            mock.patch.object(
                coop_launcher.controls,
                "read_sdl_controller_states",
                return_value=(),
            ),
        ):
            self.assertTrue(app.poll_pad(1.0))

        app.editor.save.assert_called_once_with()
        app.show_status.assert_called_once_with("assigned Aplicado ao co-op.", "ok")


class LauncherKeyboardAndMouseInputTests(unittest.TestCase):
    @staticmethod
    def make_app():
        app = LauncherGamepadInputTests.make_app()
        app.navigate = mock.Mock()
        app.set_control_player = mock.Mock()
        app.page = "controls"
        return app

    def test_keyboard_never_operates_the_launcher_outside_capture(self):
        app = self.make_app()
        keys = (
            pygame.K_UP,
            pygame.K_w,
            pygame.K_LEFT,
            pygame.K_a,
            pygame.K_RETURN,
            pygame.K_SPACE,
            pygame.K_DELETE,
            pygame.K_ESCAPE,
            pygame.K_TAB,
        )
        for key in keys:
            app.handle_key(mock.Mock(key=key, unicode=""))
        app.editor.feed_keyboard_vk.assert_not_called()
        app.navigate.assert_not_called()
        app.set_control_player.assert_not_called()

    def test_keyboard_is_data_only_in_mouse_opened_binding_capture(self):
        app = self.make_app()
        app.editor.capture_target = ("p2", "confirm", 0)

        def finish_binding(_vk):
            app.editor.capture_target = None
            return launcher_core.CaptureResult("bound", "bound")

        app.editor.feed_keyboard_vk.side_effect = finish_binding
        app.handle_key(mock.Mock(key=pygame.K_q, unicode="q"))
        app.editor.feed_keyboard_vk.assert_called_once_with(ord("Q"))
        app.reset_pointer.assert_called_once()
        app.navigate.assert_not_called()

    def test_keyboard_is_ignored_while_controller_assignment_waits_for_start(self):
        app = self.make_app()
        app.editor.assign_target = "p2"
        app.handle_key(mock.Mock(key=pygame.K_RETURN, unicode="\r"))
        app.editor.feed_keyboard_vk.assert_not_called()

    def test_mouse_click_still_activates_the_hovered_item(self):
        app = self.make_app()
        action = mock.Mock()
        app.logical_mouse = mock.Mock(return_value=(20, 20))
        app.items = [
            coop_launcher.Item(
                "mouse:item",
                pygame.Rect(0, 0, 80, 80),
                action,
            )
        ]
        app.handle_mouse(
            mock.Mock(
                type=pygame.MOUSEBUTTONDOWN,
                pos=(20, 20),
                button=1,
            )
        )
        action.assert_called_once_with()


class LauncherMouseOnlyAdjustmentTests(unittest.TestCase):
    @staticmethod
    def _drawing_stub():
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.canvas = pygame.Surface(coop_launcher.LOGICAL_SIZE, pygame.SRCALPHA)
        app.text_commands = []
        app.items = []
        app.selected_key = ""
        app.draw_header = mock.Mock()
        app.draw_panel = mock.Mock()
        app.draw_text = mock.Mock()
        app.draw_badge = mock.Mock()
        app.damage_reset_confirm_until = 0.0
        app.fonts = mock.Mock()
        app.fit_text = mock.Mock(side_effect=lambda _font, text, _width: text)
        return app

    @staticmethod
    def _click(app, item):
        app.logical_mouse = mock.Mock(return_value=item.rect.center)
        app.handle_mouse(
            mock.Mock(
                type=pygame.MOUSEBUTTONDOWN,
                pos=item.rect.center,
                button=1,
            )
        )

    def test_settings_exposes_separate_mouse_minus_and_plus_actions(self):
        app = self._drawing_stub()
        app.config = {"sound_volume": 0.5}
        app.draw_language_row = mock.Mock()
        app.draw_switch_row = mock.Mock()
        app.adjust_volume = mock.Mock()
        app.draw_settings()

        down = next(item for item in app.items if item.key == "settings:volume:down")
        up = next(item for item in app.items if item.key == "settings:volume:up")
        volume_row = pygame.Rect(
            coop_launcher.CONTENT_X,
            322,
            coop_launcher.CONTENT_W,
            78,
        )
        self.assertTrue(volume_row.contains(down.rect))
        self.assertTrue(volume_row.contains(up.rect))
        self.assertFalse(down.rect.colliderect(up.rect))

        self._click(app, down)
        app.adjust_volume.assert_called_once_with(-1)
        app.adjust_volume.reset_mock()
        self._click(app, up)
        app.adjust_volume.assert_called_once_with(1)

    def test_settings_exposes_one_battle_sound_switch_per_player(self):
        app = self._drawing_stub()
        app.config = {
            "sound_volume": 0.5,
            "battle_menu_sounds": {"p1": True, "p2": False, "p3": True},
        }
        app.draw_language_row = mock.Mock()
        app.draw_switch_row = mock.Mock()
        app.toggle_battle_menu_sound = mock.Mock()
        app.draw_settings()

        switches = [
            next(
                item
                for item in app.items
                if item.key == f"settings:battle_sound:{player}"
            )
            for player in ("p1", "p2", "p3")
        ]
        for item in switches:
            self._click(app, item)
        self.assertEqual(
            app.toggle_battle_menu_sound.call_args_list,
            [mock.call("p1"), mock.call("p2"), mock.call("p3")],
        )

    def test_controller_row_mouse_can_open_assignment_or_choose_none(self):
        app = self._drawing_stub()
        app.control_player = "p2"
        app.editor = mock.Mock(dirty=False)
        app.editor.assigned.return_value = 1
        app.editor.desktop_owner.return_value = "p2"
        app.states = [None] * 4
        app.draw_control_tab = mock.Mock()
        app.draw_binding_card = mock.Mock()
        app.assignment_label = mock.Mock(return_value="Controller · XINPUT 2")
        app.begin_assignment = mock.Mock()
        app.clear_controller_assignment = mock.Mock()
        app.assign_desktop_to_current = mock.Mock()
        app.clear_desktop_assignment = mock.Mock()
        app.save_controls = mock.Mock()
        app.restore_controls = mock.Mock()
        app.draw_controls()

        assign = next(item for item in app.items if item.key == "controls:assign")
        unassign = next(item for item in app.items if item.key == "controls:unassign")
        desktop = next(item for item in app.items if item.key == "controls:desktop")
        desktop_clear = next(
            item for item in app.items if item.key == "controls:desktop_clear"
        )
        self.assertFalse(assign.rect.colliderect(unassign.rect))
        self.assertFalse(desktop.rect.colliderect(desktop_clear.rect))
        self.assertFalse(assign.rect.colliderect(desktop.rect))

        self._click(app, unassign)
        app.clear_controller_assignment.assert_called_once_with()
        app.begin_assignment.assert_not_called()

        app.logical_mouse = mock.Mock(
            return_value=(assign.rect.x + 30, assign.rect.centery)
        )
        app.handle_mouse(
            mock.Mock(
                type=pygame.MOUSEBUTTONDOWN,
                pos=(assign.rect.x + 30, assign.rect.centery),
                button=1,
            )
        )
        app.begin_assignment.assert_called_once_with()

        self._click(app, desktop)
        app.assign_desktop_to_current.assert_called_once_with()

    def test_non_owner_tab_does_not_offer_a_desktop_remove_action(self):
        app = self._drawing_stub()
        app.control_player = "p3"
        app.editor = mock.Mock(dirty=False)
        app.editor.assigned.return_value = 2
        app.editor.desktop_owner.return_value = "p2"
        app.states = [None] * 4
        app.draw_control_tab = mock.Mock()
        app.draw_binding_card = mock.Mock()
        app.assignment_label = mock.Mock(return_value="Controller · XINPUT 3")
        app.begin_assignment = mock.Mock()
        app.clear_controller_assignment = mock.Mock()
        app.assign_desktop_to_current = mock.Mock()
        app.clear_desktop_assignment = mock.Mock()
        app.save_controls = mock.Mock()
        app.restore_controls = mock.Mock()

        app.draw_controls()

        self.assertFalse(any(item.key == "controls:desktop_clear" for item in app.items))
        desktop_copy = [call.args[2] for call in app.draw_text.call_args_list]
        self.assertIn("2 · TECLADO + MOUSE", desktop_copy)
        self.assertIn("DONO ÚNICO: P2", desktop_copy)
        self.assertIn("MOVER TRANSFERE AMBOS", desktop_copy)
        self.assertTrue(any(call.args[1] == "—" for call in app.draw_badge.call_args_list))

    def test_future_control_config_attempt_is_blocked_with_clear_status(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        report = controls.ConfigNormalizationReport(
            source_version=controls.CONFIG_VERSION + 1,
            migrated_desktop=False,
            kept_desktop_owner="p2",
            removed_desktop_owners=(),
        )
        app.editor = mock.Mock(read_only=True, normalization_report=report)
        app.show_status = mock.Mock()
        app.audio = mock.Mock()

        self.assertFalse(app.control_config_editable())

        message = app.control_future_version_message(report)
        self.assertIn(f"versão {controls.CONFIG_VERSION + 1}", message)
        self.assertIn("preservado sem alterações", message)
        app.show_status.assert_called_once_with(message, "warning", 12.0)
        app.audio.play.assert_called_once_with("error")

    def test_desktop_transfer_is_rolled_back_when_atomic_save_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controls.json"
            controls.save_config(controls.default_config(), path)
            app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
            app.editor = launcher_core.ControlEditor(path)
            app.control_player = "p3"
            app.show_status = mock.Mock()
            app.audio = mock.Mock()

            with mock.patch.object(
                app.editor,
                "save",
                side_effect=OSError("disk full"),
            ):
                app.assign_desktop_to_current()

            self.assertEqual(app.editor.desktop_owner(), "p2")
            self.assertEqual(controls.desktop_owner(controls.load_config(path)), "p2")
            app.show_status.assert_called_once_with(
                "Não foi possível salvar: disk full",
                "error",
            )
            app.audio.play.assert_called_once_with("error")

    def test_choose_none_clears_the_current_player_assignment(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.control_player = "p2"
        app.editor = mock.Mock()
        app.show_status = mock.Mock()
        app.audio = mock.Mock()
        app.clear_controller_assignment()
        app.editor.assign_controller.assert_called_once_with("p2", None)
        app.editor.save.assert_called_once_with()
        app.show_status.assert_called_once()
        app.audio.play.assert_called_once_with("cancel")

    def test_damage_session_reset_requires_confirmation_and_applies_live(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.config = {"damage_meter_session_id": 9}
        app.damage_reset_confirm_until = 0.0
        app.show_status = mock.Mock()
        app.audio = mock.Mock()
        with (
            mock.patch.object(coop_launcher.time, "monotonic", side_effect=(10.0, 11.0)),
            mock.patch.object(
                coop_launcher.core,
                "save_launcher_config",
                side_effect=launcher_core.normalize_launcher_config,
            ) as save,
        ):
            app.reset_damage_meter_session()
            self.assertEqual(app.config["damage_meter_session_id"], 9)
            app.reset_damage_meter_session()

        self.assertEqual(app.config["damage_meter_session_id"], 10)
        save.assert_called_once()
        self.assertEqual(
            app.audio.play.call_args_list,
            [mock.call("open"), mock.call("confirm")],
        )

    def test_theme_card_opens_an_explicit_palette_without_cycling(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.player_theme_draft = {"p1": "blue", "p2": "gold", "p3": "pink"}
        app.character_theme_draft = {5: "gold"}
        app.theme_picker_target = None
        app.theme_picker_return_key = ""
        app.selected_key = "colors:character:5"
        app.theme_dirty = False
        app.reset_pointer = mock.Mock()
        app.audio = mock.Mock()
        app.save_themes = mock.Mock(return_value=True)

        app.open_theme_picker(5)

        self.assertEqual(app.theme_picker_target, 5)
        self.assertEqual(app.selected_key, "colors:theme:gold")
        self.assertEqual(app.character_theme_draft[5], "gold")
        app.select_theme("emerald")
        self.assertEqual(app.character_theme_draft[5], "emerald")
        self.assertIsNone(app.theme_picker_target)
        self.assertEqual(app.selected_key, "colors:character:5")
        self.assertTrue(app.theme_dirty)
        app.save_themes.assert_called_once_with()
        self.assertEqual(
            app.audio.play.call_args_list,
            [mock.call("open"), mock.call("confirm")],
        )

    def test_theme_picker_lists_every_palette_as_a_separate_action(self):
        app = self._drawing_stub()
        app.capture_shade = pygame.Surface(coop_launcher.LOGICAL_SIZE, pygame.SRCALPHA)
        app.player_theme_draft = {"p1": "blue", "p2": "gold", "p3": "pink"}
        app.character_theme_draft = {5: "gold"}
        app.theme_picker_target = 5
        app.theme_picker_return_key = "colors:character:5"
        app.selected_key = "colors:theme:gold"
        app.select_theme = mock.Mock()
        app.draw_aa_box = mock.Mock()

        app.draw_theme_picker()

        choices = [
            item
            for item in app.items
            if item.key.startswith("colors:theme:")
            and item.key != "colors:theme:cancel"
        ]
        self.assertEqual(
            {item.key.removeprefix("colors:theme:") for item in choices},
            set(coop_launcher.menu_themes.PRESETS),
        )
        emerald = next(item for item in choices if item.key == "colors:theme:emerald")
        emerald.action()
        app.select_theme.assert_called_once_with("emerald")


class LauncherInstanceTests(unittest.TestCase):
    def test_duplicate_launcher_closes_the_new_mutex_handle(self):
        kernel32 = mock.Mock()
        kernel32.CreateMutexW.return_value = 123
        with (
            mock.patch.object(launcher_core.os, "name", "nt"),
            mock.patch.object(launcher_core.ct, "WinDLL", return_value=kernel32),
            mock.patch.object(launcher_core.ct, "set_last_error"),
            mock.patch.object(
                launcher_core.ct,
                "get_last_error",
                return_value=launcher_core.ERROR_ALREADY_EXISTS,
            ),
        ):
            self.assertIsNone(launcher_core.acquire_launcher_instance())
        kernel32.CloseHandle.assert_called_once_with(123)

    def test_first_launcher_keeps_the_mutex_until_release(self):
        kernel32 = mock.Mock()
        kernel32.CreateMutexW.return_value = 456
        with (
            mock.patch.object(launcher_core.os, "name", "nt"),
            mock.patch.object(launcher_core.ct, "WinDLL", return_value=kernel32),
            mock.patch.object(launcher_core.ct, "set_last_error"),
            mock.patch.object(launcher_core.ct, "get_last_error", return_value=0),
        ):
            handle = launcher_core.acquire_launcher_instance()
            launcher_core.release_launcher_instance(handle)
        self.assertEqual(handle, 456)
        kernel32.CloseHandle.assert_called_once_with(456)

    def test_runtime_mutex_observation_proves_only_exact_absence(self):
        for error, expected in (
            (launcher_core.ERROR_FILE_NOT_FOUND, False),
            (5, True),
            (0, True),
        ):
            with self.subTest(error=error):
                kernel32 = mock.Mock()
                kernel32.OpenMutexW.return_value = 0
                with (
                    mock.patch.object(launcher_core.os, "name", "nt"),
                    mock.patch.object(
                        launcher_core.ct,
                        "WinDLL",
                        return_value=kernel32,
                    ),
                    mock.patch.object(
                        launcher_core.ct,
                        "set_last_error",
                    ) as clear_error,
                    mock.patch.object(
                        launcher_core.ct,
                        "get_last_error",
                        return_value=error,
                    ),
                ):
                    self.assertEqual(
                        launcher_core.runtime_mutex_active(4242),
                        expected,
                    )
                clear_error.assert_called_once_with(0)

    def test_runtime_mutex_observation_closes_a_live_probe_handle(self):
        kernel32 = mock.Mock()
        kernel32.OpenMutexW.return_value = 789
        with (
            mock.patch.object(launcher_core.os, "name", "nt"),
            mock.patch.object(launcher_core.ct, "WinDLL", return_value=kernel32),
            mock.patch.object(launcher_core.ct, "set_last_error"),
        ):
            self.assertTrue(launcher_core.runtime_mutex_active(4242))

        kernel32.CloseHandle.assert_called_once_with(789)


class LauncherIdleLifecycleTests(unittest.TestCase):
    def make_app(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.last_process_poll = 0.0
        app.game_open = True
        app.in_tray = True
        app.taskbar_minimized = False
        app.page = "home"
        app.window_focused = True
        app.last_controller_state_poll = 0.0
        app.restore_from_tray = mock.Mock()
        app.restore_from_taskbar = mock.Mock()
        app.hide_to_tray = mock.Mock()
        app.minimize_to_taskbar = mock.Mock()
        app.launcher = mock.Mock()
        app.audio = mock.Mock()
        app.config = {"minimize_when_ready": True}
        app.preview = False
        return app

    def test_game_close_restores_the_launcher_from_tray(self):
        app = self.make_app()
        app.launcher.drain.return_value = []
        with mock.patch.object(coop_launcher.core, "process_ids", return_value=()):
            self.assertTrue(app.update_launcher())
        app.restore_from_tray.assert_called_once_with()
        self.assertFalse(app.game_open)

    def test_game_close_restores_a_taskbar_minimized_launcher(self):
        app = self.make_app()
        app.in_tray = False
        app.taskbar_minimized = True
        app.launcher.drain.return_value = []
        with mock.patch.object(coop_launcher.core, "process_ids", return_value=()):
            self.assertTrue(app.update_launcher())
        app.restore_from_taskbar.assert_called_once_with()
        app.restore_from_tray.assert_not_called()
        self.assertFalse(app.game_open)

    def test_game_open_keeps_visible_controls_discovery_active(self):
        app = self.make_app()
        app.game_open = False
        app.in_tray = False
        app.page = "controls"
        app.window_focused = True
        app.last_controller_state_poll = time.monotonic()
        app.suspend_controller_discovery = mock.Mock(return_value=True)
        app.resume_controller_discovery = mock.Mock(return_value=False)
        app.launcher.attach_if_running.return_value = False
        app.launcher.drain.return_value = []

        with mock.patch.object(coop_launcher.core, "process_ids", return_value=(321,)):
            self.assertTrue(app.update_launcher())

        app.suspend_controller_discovery.assert_not_called()
        self.assertTrue(app.game_open)

    def test_suspending_discovery_closes_and_forgets_sdl_handles(self):
        app = self.make_app()
        app.controller_discovery_suspended = False
        app.sdl_states = (sdl_state(),)
        app.joysticks = [object()]
        app.joystick_names = ("Bluetooth pad",)

        with mock.patch.object(controls, "release_sdl_controllers") as release:
            self.assertTrue(app.suspend_controller_discovery())

        release.assert_called_once_with()
        self.assertTrue(app.controller_discovery_suspended)
        self.assertEqual(app.sdl_states, ())
        self.assertEqual(app.joysticks, [])
        self.assertEqual(app.joystick_names, ())

    def test_visible_configurator_reacquires_devices_after_game_closes(self):
        app = self.make_app()
        app.game_open = False
        app.in_tray = False
        app.page = "controls"
        app.controller_discovery_suspended = True
        app.refresh_controller_states = mock.Mock(return_value=True)

        self.assertTrue(app.resume_controller_discovery())

        self.assertFalse(app.controller_discovery_suspended)
        app.refresh_controller_states.assert_called_once_with()

    def test_visible_configurator_can_discover_devices_while_game_is_open(self):
        app = self.make_app()
        app.game_open = True
        app.in_tray = False
        app.page = "controls"
        app.window_focused = True
        app.controller_discovery_suspended = True
        app.refresh_controller_states = mock.Mock(return_value=True)

        self.assertTrue(app.resume_controller_discovery())

        self.assertFalse(app.controller_discovery_suspended)
        app.refresh_controller_states.assert_called_once_with()

    def test_transient_sdl_disconnect_does_not_escape_the_event_loop(self):
        app = self.make_app()
        app.suspend_controller_discovery = mock.Mock()

        with mock.patch.object(pygame.event, "get", side_effect=SystemError):
            self.assertEqual(app.read_window_events(10.0), ())

        app.suspend_controller_discovery.assert_called_once_with()
        self.assertEqual(app.controller_recovery_at, 10.75)

    def test_ready_event_minimizes_the_launcher_to_the_taskbar(self):
        app = self.make_app()
        app.in_tray = False
        app.last_process_poll = time.monotonic()
        app.launcher.drain.return_value = [launcher_core.LaunchEvent("ready", "ready")]
        self.assertTrue(app.update_launcher())
        app.minimize_to_taskbar.assert_called_once_with()
        app.hide_to_tray.assert_not_called()

    def test_runtime_error_restores_a_taskbar_minimized_launcher(self):
        app = self.make_app()
        app.in_tray = False
        app.taskbar_minimized = True
        app.last_process_poll = time.monotonic()
        app.launcher.drain.return_value = [launcher_core.LaunchEvent("error", "failed")]

        self.assertTrue(app.update_launcher())

        app.audio.play.assert_called_once_with("error")
        app.restore_from_taskbar.assert_called_once_with()
        app.restore_from_tray.assert_not_called()

    def test_taskbar_minimize_keeps_the_window_out_of_tray(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.in_tray = False
        app.taskbar_minimized = False
        app.window_focused = True
        app.tray = mock.Mock()
        app.native_window_handle = mock.Mock(return_value=321)
        app.suspend_controller_discovery = mock.Mock()
        user32 = mock.Mock()
        windows = SimpleNamespace(user32=user32)

        with mock.patch.object(
            coop_launcher,
            "ctypes",
            SimpleNamespace(windll=windows),
        ):
            app.minimize_to_taskbar()

        user32.ShowWindowAsync.assert_called_once_with(321, 6)
        app.tray.show.assert_not_called()
        app.tray.hide.assert_not_called()
        self.assertFalse(app.in_tray)
        self.assertTrue(app.taskbar_minimized)
        self.assertFalse(app.window_focused)
        app.suspend_controller_discovery.assert_called_once_with()

    def test_close_stays_in_tray_while_runtime_uses_the_bundle(self):
        app = self.make_app()
        app.running = True
        app.launcher.session_active = True
        app.request_close()
        app.hide_to_tray.assert_called_once_with()
        self.assertTrue(app.running)

    def test_close_exits_after_the_runtime_ends(self):
        app = self.make_app()
        app.running = True
        app.launcher.session_active = False
        app.request_close()
        app.hide_to_tray.assert_not_called()
        self.assertFalse(app.running)

    def test_explicit_tray_exit_closes_even_during_an_active_session(self):
        app = self.make_app()
        app.running = True
        app.launcher.session_active = True
        app.exit_completely()
        self.assertFalse(app.running)


class LauncherLanguageUiTests(unittest.TestCase):
    def test_language_switch_saves_only_language_and_updates_live_ui(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.config = {
            "version": 2,
            "language": "en",
            "menu_sounds": False,
            "sound_volume": 0.41,
            "minimize_when_ready": False,
        }
        app.audio = mock.Mock()
        app.selected_key = "settings:language:en"
        app.reset_pointer = mock.Mock()
        app.show_status = mock.Mock()

        with mock.patch.object(
            coop_launcher.core,
            "save_launcher_config",
            side_effect=launcher_core.normalize_launcher_config,
        ) as save:
            app.set_language("pt")

        save.assert_called_once()
        self.assertEqual(app.language, "pt")
        self.assertEqual(app.localize("AJUSTES"), "AJUSTES")
        self.assertEqual(app.selected_key, "settings:language:pt")
        self.assertFalse(app.config["menu_sounds"])
        self.assertEqual(app.config["sound_volume"], 0.41)
        self.assertFalse(app.config["minimize_when_ready"])
        self.assertIs(app.audio.config, app.config)


class KeyTranslationTests(unittest.TestCase):
    def test_letters_digits_arrows_function_and_keypad_use_win32_vk(self):
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_a), ord("A"))
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_7), ord("7"))
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_LEFT), 0x25)
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_F12), 0x7B)
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_KP4), 0x64)
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_RSHIFT), 0x10)

    def test_escape_is_a_valid_binding_not_an_implicit_capture_cancel(self):
        self.assertEqual(coop_launcher.pygame_key_to_vk(pygame.K_ESCAPE), 0x1B)


if __name__ == "__main__":
    unittest.main()
