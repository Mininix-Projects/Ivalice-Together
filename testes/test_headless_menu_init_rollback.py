import contextlib
import io
import struct
from types import SimpleNamespace
import unittest
from unittest import mock

import headless_menu_shell as live


class RecordingResource:
    def __init__(self, events, name, *, fail=False):
        self.events = events
        self.name = name
        self.fail = fail

    def _release(self, method):
        self.events.append((self.name, method))
        if self.fail:
            raise OSError(f"{self.name} cleanup failed")

    def restore(self):
        self._release("restore")

    def shutdown(self):
        self._release("shutdown")

    def close(self):
        self._release("close")


class RecordingProcess(RecordingResource):
    def write(self, address, payload):
        self.events.append((self.name, "write", address, payload))


class FailingAxisPatchProcess:
    def __init__(self):
        self.memory = {
            live.NATIVE_GET_BUTTON_SITE: live.NATIVE_GET_BUTTON_ORIGINAL,
            live.NATIVE_GET_AXIS_SITE: live.NATIVE_GET_AXIS_ORIGINAL,
            live.NATIVE_DESKTOP_BUTTON_SITE: live.NATIVE_DESKTOP_BUTTON_ORIGINAL,
            live.NATIVE_DESKTOP_WHEEL_SITE: live.NATIVE_DESKTOP_WHEEL_ORIGINAL,
            live.NATIVE_MOUSE_LOOK_SITE: live.NATIVE_MOUSE_LOOK_ORIGINAL,
        }

    def read(self, address, size):
        return self.memory.get(address, b"\x00" * size)[:size]

    def write(self, address, payload):
        self.memory[address] = bytes(payload)

    def patch_code(self, address, payload):
        if (
            address == live.NATIVE_GET_AXIS_SITE
            and payload != live.NATIVE_GET_AXIS_ORIGINAL
        ):
            raise OSError("simulated second-site failure")
        self.memory[address] = bytes(payload)


class HeadlessMenuInitializationRollbackTests(unittest.TestCase):
    def test_duplicate_mutex_precedes_every_publisher_and_remote_side_effect(self):
        events = []
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        fake_kernel32 = SimpleNamespace(
            CreateMutexW=lambda *_args: events.append(("mutex", "create")) or 0x1234,
            CloseHandle=lambda handle: events.append(("mutex", "close", handle)),
        )
        publisher = mock.Mock(
            side_effect=lambda *_args: events.append(("publisher", "create"))
        )
        capture = mock.Mock(
            side_effect=lambda *_args: events.append(("capture", "create"))
        )
        remote_process = mock.Mock(
            side_effect=lambda *_args: events.append(("process", "open"))
        )
        with (
            mock.patch.object(live, "kernel32", fake_kernel32),
            mock.patch.object(
                live.legacy.coop,
                "find_pid",
                side_effect=lambda: events.append(("pid", "found")) or 4242,
            ),
            mock.patch.object(
                live.ct,
                "set_last_error",
                side_effect=lambda value: events.append(("last_error", value)),
            ) as clear_error,
            mock.patch.object(
                live.ct,
                "get_last_error",
                return_value=live.ERROR_ALREADY_EXISTS,
            ),
            mock.patch.object(
                live.party_intent_channel,
                "PartyIntentPublisher",
                publisher,
            ),
            mock.patch.object(
                live.party_intent_channel,
                "ExplicitPartyEditCapture",
                capture,
            ),
            mock.patch.object(live.legacy.coop, "RemoteProcess", remote_process),
        ):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                shell._initialize()

        clear_error.assert_called_once_with(0)
        self.assertEqual(
            events,
            [
                ("pid", "found"),
                ("last_error", 0),
                ("mutex", "create"),
                ("mutex", "close", 0x1234),
            ],
        )
        publisher.assert_not_called()
        capture.assert_not_called()
        remote_process.assert_not_called()

    def test_dialog_bridge_preserves_published_root_after_init_failure(self):
        process = FailingAxisPatchProcess()
        bridge = live.NativeDialogInputBridge(process, 0x50000000)

        with self.assertRaisesRegex(RuntimeError, "restart the game"):
            bridge.install()

        expected_button_patch = (
            b"\xe9"
            + bridge._rel32(
                live.NATIVE_GET_BUTTON_SITE,
                5,
                bridge.cave,
            )
            + b"\x90"
        )
        self.assertEqual(
            process.read(
                live.NATIVE_GET_BUTTON_SITE,
                len(expected_button_patch),
            ),
            expected_button_patch,
        )
        self.assertEqual(
            process.read(
                live.NATIVE_GET_AXIS_SITE,
                len(live.NATIVE_GET_AXIS_ORIGINAL),
            ),
            live.NATIVE_GET_AXIS_ORIGINAL,
        )
        self.assertEqual(
            process.read(
                live.NATIVE_DESKTOP_BUTTON_SITE,
                len(live.NATIVE_DESKTOP_BUTTON_ORIGINAL),
            ),
            live.NATIVE_DESKTOP_BUTTON_ORIGINAL,
        )
        self.assertEqual(
            process.read(
                live.NATIVE_DESKTOP_WHEEL_SITE,
                len(live.NATIVE_DESKTOP_WHEEL_ORIGINAL),
            ),
            live.NATIVE_DESKTOP_WHEEL_ORIGINAL,
        )
        self.assertEqual(
            process.read(
                live.NATIVE_MOUSE_LOOK_SITE,
                len(live.NATIVE_MOUSE_LOOK_ORIGINAL),
            ),
            live.NATIVE_MOUSE_LOOK_ORIGINAL,
        )
        self.assertFalse(bridge.installed)

        with self.assertRaisesRegex(RuntimeError, "partial .* restart the game"):
            live.NativeDialogInputBridge(process, 0x50000000).install()

    def test_partial_initialization_rolls_back_and_preserves_original_error(self):
        events = []
        failure = RuntimeError("late initialization failure")
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)

        def fail_late(instance):
            instance.base = 0x50000000
            instance.native_sound_player = RecordingResource(events, "sound", fail=True)
            instance.native_interaction_router = RecordingResource(
                events, "interaction"
            )
            instance.native_dialog_bridge = RecordingResource(events, "dialog")
            instance.panel = RecordingResource(events, "panel")
            instance.process = RecordingProcess(events, "process")
            instance._initial_remote_theme_state = [(0x60001234, b"old-theme")]
            instance.session_mutex = 0x1234
            raise failure

        fake_kernel32 = SimpleNamespace(
            CloseHandle=lambda handle: events.append(("mutex", handle))
        )
        output = io.StringIO()
        with (
            mock.patch.object(live.CoopMenuRuntime, "_initialize", fail_late),
            mock.patch.object(live, "kernel32", fake_kernel32),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(RuntimeError) as caught:
                live.CoopMenuRuntime.__init__(shell)

        self.assertIs(caught.exception, failure)
        self.assertIn(("sound", "restore"), events)
        self.assertIn(("interaction", "shutdown"), events)
        self.assertIn(("dialog", "restore"), events)
        self.assertIn(("panel", "close"), events)
        self.assertIn(("process", "close"), events)
        self.assertIn(("mutex", 0x1234), events)
        self.assertIn(
            ("process", "write", 0x60001234, b"old-theme"),
            events,
        )
        self.assertIn("HEADLESS_MENU_INIT_ROLLBACK_WARNING", output.getvalue())
        self.assertIn(
            (
                "process",
                "write",
                0x50000000 + live.INPROCESS_RING_P1_HANDLE_OFFSET,
                b"\x00" * 8,
            ),
            events,
        )
        self.assertIn(
            (
                "process",
                "write",
                0x50000000 + live.INPROCESS_RING_P3_HANDLE_OFFSET,
                b"\x00" * 4,
            ),
            events,
        )

        before_repeat = list(events)
        shell._rollback_failed_initialization()
        self.assertEqual(events, before_repeat)

    def test_failure_before_first_remote_resource_is_also_safe(self):
        failure = KeyboardInterrupt()
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)

        def fail_immediately(_instance):
            raise failure

        with mock.patch.object(
            live.CoopMenuRuntime,
            "_initialize",
            fail_immediately,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                live.CoopMenuRuntime.__init__(shell)

        self.assertIs(caught.exception, failure)
        self.assertTrue(shell._initialization_rollback_complete)

    def test_failed_retry_keeps_an_adopted_dialog_bridge_fail_closed(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        bridge = SimpleNamespace(
            installed=True,
            owns_static=False,
            fail_closed_for_recovery=mock.Mock(),
            restore=mock.Mock(),
        )
        shell.native_dialog_bridge = bridge

        shell._rollback_failed_initialization()

        bridge.fail_closed_for_recovery.assert_called_once_with()
        bridge.restore.assert_not_called()

    def test_failed_fresh_install_restores_its_owned_dialog_bridge(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        bridge = SimpleNamespace(
            installed=True,
            owns_static=True,
            fail_closed_for_recovery=mock.Mock(),
            restore=mock.Mock(),
        )
        shell.native_dialog_bridge = bridge

        shell._rollback_failed_initialization()

        bridge.restore.assert_called_once_with()
        bridge.fail_closed_for_recovery.assert_not_called()


class HeadlessMenuRuntimeReliabilityTests(unittest.TestCase):
    @staticmethod
    def bare_shell():
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.running = True
        shell.frame_io_retry_started = None
        shell.frame_io_retry_signature = None
        return shell

    def test_one_transient_frame_io_failure_recovers_without_stopping_worker(self):
        shell = self.bare_shell()
        shell.update_frame = mock.Mock(
            side_effect=(OSError(299, "transient actor rebuild"), None)
        )
        output = io.StringIO()
        with (
            mock.patch.object(live.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(live.time, "monotonic", side_effect=(10.0, 10.25)),
            contextlib.redirect_stdout(output),
        ):
            shell._update_frame_resilient(True)
            shell._update_frame_resilient(True)

        self.assertTrue(shell.running)
        self.assertIsNone(shell.frame_io_retry_started)
        self.assertEqual(shell.update_frame.call_count, 2)
        self.assertIn("HEADLESS_MENU_FRAME_IO_RETRY", output.getvalue())
        self.assertIn("HEADLESS_MENU_FRAME_IO_RECOVERED", output.getvalue())

    def test_torn_struct_sample_is_quarantined_and_recovers_next_frame(self):
        shell = self.bare_shell()
        shell.update_frame = mock.Mock(
            side_effect=(struct.error("torn actor generation"), None)
        )
        output = io.StringIO()
        with (
            mock.patch.object(live.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(live.time, "monotonic", side_effect=(20.0, 20.25)),
            contextlib.redirect_stdout(output),
        ):
            shell._update_frame_resilient(True)
            shell._update_frame_resilient(True)

        self.assertTrue(shell.running)
        self.assertIsNone(shell.frame_io_retry_started)
        self.assertEqual(shell.update_frame.call_count, 2)
        self.assertIn("HEADLESS_MENU_FRAME_IO_RETRY", output.getvalue())
        self.assertIn("HEADLESS_MENU_FRAME_IO_RECOVERED", output.getvalue())

    def test_persistent_frame_io_failure_stays_quarantined_until_recovery(self):
        shell = self.bare_shell()
        failure = OSError(5, "persistent remote write failure")
        shell.update_frame = mock.Mock(side_effect=(failure, failure, None))
        output = io.StringIO()
        with (
            mock.patch.object(live.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=(
                    10.0,
                    10.0 + live.FRAME_IO_RETRY_WINDOW_SECONDS,
                    10.5 + live.FRAME_IO_RETRY_WINDOW_SECONDS,
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            shell._update_frame_resilient(True)
            shell._update_frame_resilient(True)
            shell._update_frame_resilient(True)

        self.assertTrue(shell.running)
        self.assertIsNone(shell.frame_io_retry_started)
        self.assertEqual(shell.update_frame.call_count, 3)
        self.assertIn("HEADLESS_MENU_FRAME_IO_STILL_RETRYING", output.getvalue())
        self.assertIn("HEADLESS_MENU_FRAME_IO_RECOVERED", output.getvalue())

    def test_structural_frame_failure_is_still_exposed_immediately(self):
        shell = self.bare_shell()
        failure = RuntimeError("incompatible native layout")
        shell.update_frame = mock.Mock(side_effect=failure)

        with self.assertRaises(RuntimeError) as caught:
            shell._update_frame_resilient(True)

        self.assertIs(caught.exception, failure)

    def test_shutdown_during_frame_io_failure_does_not_start_a_retry(self):
        shell = self.bare_shell()
        shell.update_frame = mock.Mock(side_effect=OSError(5, "runtime removed"))
        with mock.patch.object(
            live.runtime_shutdown,
            "requested",
            return_value=True,
        ) as requested:
            shell._update_frame_resilient(True)

        requested.assert_called_once_with(force=True)
        self.assertFalse(shell.running)
        self.assertIsNone(shell.frame_io_retry_started)

    def test_run_cleanup_continues_after_independent_restore_failure(self):
        events = []
        shell = self.bare_shell()
        shell.live_damage_meter = RecordingResource(
            events,
            "damage_meter",
            fail=True,
        )
        shell.native_combat_log_target_gate = None
        shell.native_dialog_bridge = RecordingResource(events, "dialog")
        shell.p3_interaction_prompt_panel = RecordingResource(events, "p3_prompt")
        shell.process = RecordingProcess(events, "process")
        shell.session_mutex = 0x1234
        shell.clear_world_target = lambda: events.append(("world_target", "clear"))
        shell.clear_d3d_target_rings = lambda: events.append(("rings", "clear"))
        shell.publish_p1_help_badge = lambda visible: events.append(
            ("help_badge", visible)
        )
        shell.cancel_p1_leader_handoff = lambda reason: events.append(
            ("leader_handoff", reason)
        )
        fake_kernel32 = SimpleNamespace(
            CloseHandle=lambda handle: events.append(("mutex", handle))
        )
        output = io.StringIO()
        with (
            mock.patch.object(live, "kernel32", fake_kernel32),
            contextlib.redirect_stdout(output),
        ):
            shell._cleanup_after_run()

        self.assertIn(("damage_meter", "close"), events)
        self.assertIn(("dialog", "restore"), events)
        self.assertIn(("p3_prompt", "close"), events)
        self.assertIn(("process", "close"), events)
        self.assertIn(("mutex", 0x1234), events)
        self.assertIn("HEADLESS_MENU_CLEANUP_WARNING", output.getvalue())

        before_repeat = list(events)
        shell._cleanup_after_run()
        self.assertEqual(events, before_repeat)

    def test_abnormal_cleanup_publishes_fail_closed_before_every_restore(self):
        events = []
        shell = self.bare_shell()

        def fail_closed_with_transient_first_failure():
            events.append(("dialog", "fail_closed"))
            if events.count(("dialog", "fail_closed")) == 1:
                raise OSError("transient first fail-closed publication")

        shell.native_dialog_bridge = SimpleNamespace(
            fail_closed_for_recovery=fail_closed_with_transient_first_failure,
            restore=lambda: events.append(("dialog", "restore")),
        )
        shell.live_damage_meter = RecordingResource(events, "damage_meter")
        shell.native_combat_log_target_gate = None
        shell.clear_world_target = lambda: events.append(("world_target", "clear"))
        shell.clear_d3d_target_rings = lambda: events.append(("rings", "clear"))
        shell.publish_p1_help_badge = lambda visible: events.append(
            ("help_badge", visible)
        )
        shell.cancel_p1_leader_handoff = lambda reason: events.append(
            ("leader_handoff", reason)
        )
        shell.session_mutex = None

        shell._cleanup_after_run(preserve_input_firewall=True)

        self.assertEqual(events[0], ("dialog", "fail_closed"))
        self.assertEqual(events.count(("dialog", "fail_closed")), 2)
        self.assertNotIn(("dialog", "restore"), events)

    def test_normal_cleanup_restores_dialog_bridge_only_at_teardown(self):
        events = []
        shell = self.bare_shell()
        shell.native_dialog_bridge = SimpleNamespace(
            fail_closed_for_recovery=lambda: events.append(
                ("dialog", "fail_closed")
            ),
            restore=lambda: events.append(("dialog", "restore")),
        )
        shell.live_damage_meter = RecordingResource(events, "damage_meter")
        shell.native_combat_log_target_gate = None
        shell.clear_world_target = lambda: events.append(("world_target", "clear"))
        shell.clear_d3d_target_rings = lambda: events.append(("rings", "clear"))
        shell.publish_p1_help_badge = lambda visible: events.append(
            ("help_badge", visible)
        )
        shell.cancel_p1_leader_handoff = lambda reason: events.append(
            ("leader_handoff", reason)
        )
        shell.session_mutex = None

        shell._cleanup_after_run()

        self.assertNotIn(("dialog", "fail_closed"), events)
        self.assertIn(("dialog", "restore"), events)
        self.assertGreater(
            events.index(("dialog", "restore")),
            events.index(("damage_meter", "close")),
        )


if __name__ == "__main__":
    unittest.main()
