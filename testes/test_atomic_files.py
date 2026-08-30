import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atomic_files
import area_transition
import controller_bindings
import coop_roster
import launcher_core
import menu_themes
import party_intent_channel
import runtime_status
import session_state


class AtomicFilesTests(unittest.TestCase):
    def test_write_text_replaces_normally_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("old", encoding="utf-8")

            atomic_files.write_text(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_windows_cross_device_error_falls_back_to_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tmp"
            destination = root / "config.json"
            source.write_text("new", encoding="utf-8")
            destination.write_text("old", encoding="utf-8")
            error = OSError("different disk drive")
            error.winerror = atomic_files.WINDOWS_ERROR_NOT_SAME_DEVICE

            with mock.patch.object(atomic_files.os, "replace", side_effect=error):
                atomic_files.replace_file(source, destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), "new")
            self.assertFalse(source.exists())

    def test_posix_cross_device_error_uses_the_same_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tmp"
            destination = root / "config.json"
            source.write_text("new", encoding="utf-8")
            error = OSError(errno.EXDEV, "cross-device link")

            with mock.patch.object(atomic_files.os, "replace", side_effect=error):
                atomic_files.replace_file(source, destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), "new")
            self.assertFalse(source.exists())

    def test_unrelated_replace_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tmp"
            destination = root / "config.json"
            source.write_text("new", encoding="utf-8")
            error = PermissionError("denied")

            with (
                mock.patch.object(atomic_files.os, "replace", side_effect=error),
                self.assertRaises(PermissionError),
            ):
                atomic_files.replace_file(source, destination)

            self.assertTrue(source.exists())

    def test_write_text_retries_a_transient_replace_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            real_replace = atomic_files.replace_file
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("temporary scanner lock")
                return real_replace(source, destination)

            with mock.patch.object(
                atomic_files,
                "replace_file",
                side_effect=flaky_replace,
            ):
                atomic_files.write_text(path, "saved\n")

            self.assertEqual(attempts, 3)
            self.assertEqual(path.read_text(encoding="utf-8"), "saved\n")

    def test_every_user_config_writer_accepts_the_cross_device_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            error = OSError("different disk drive")
            error.winerror = atomic_files.WINDOWS_ERROR_NOT_SAME_DEVICE

            with mock.patch.object(atomic_files.os, "replace", side_effect=error):
                launcher_core.save_launcher_config({}, root / "launcher.json")
                controller_bindings.save_config(
                    controller_bindings.default_config(), root / "controllers.json"
                )
                coop_roster.save(coop_roster.default_roster(), root / "roster.json")
                menu_themes.save_selection(
                    menu_themes.default_selection(), root / "themes.json"
                )
                session_state.save_identity(
                    session_state.SessionIdentity(0, 5),
                    root / "session.json",
                )
                runtime_status.write_status(
                    42,
                    "preparing",
                    "testing",
                    data_directory=root,
                )
                party_intent_channel.PartyIntentPublisher(
                    42,
                    root / "party-intent.json",
                ).publish(
                    (0, 5),
                    battle_info=0x1234,
                    source="test",
                )
                area_transition.GatewayMemory(
                    storage_path=root / "gateways.json"
                ).observe(100, (1.0, 0.0, 2.0), 101, 3)

            for filename in (
                "launcher.json",
                "controllers.json",
                "roster.json",
                "themes.json",
                "session.json",
                "runtime_status/42.json",
                "party-intent.json",
                "gateways.json",
            ):
                self.assertTrue((root / filename).is_file())
            self.assertEqual(list(root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
