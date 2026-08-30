import ctypes as ct
import unittest
from pathlib import Path
from unittest import mock

import game_process
import runtime_paths


class GameProcessSelectionTests(unittest.TestCase):
    def test_process_snapshot_retries_transient_bad_length(self):
        invalid = ct.c_void_p(-1).value
        kernel32 = mock.Mock()
        kernel32.CreateToolhelp32Snapshot.side_effect = [invalid, invalid, 0x1234]
        with (
            mock.patch.object(
                game_process.ct,
                "get_last_error",
                side_effect=[
                    game_process.ERROR_BAD_LENGTH,
                    game_process.ERROR_BAD_LENGTH,
                ],
            ),
            mock.patch.object(game_process.time, "sleep") as sleep,
        ):
            snapshot = game_process._create_process_snapshot(kernel32)

        self.assertEqual(snapshot, 0x1234)
        self.assertEqual(kernel32.CreateToolhelp32Snapshot.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_matching_processes_are_filtered_by_full_executable_path(self):
        expected = Path(r"D:\Detached FF12\x64\FFXII_TZA.exe")
        images = {
            100: expected,
            200: Path(r"D:\Steam\FFXII_TZA.exe"),
            300: Path(r"D:\DETACHED FF12\x64\ffxii_tza.EXE"),
        }
        with (
            mock.patch.object(
                game_process,
                "named_process_ids",
                return_value=(100, 200, 300),
            ),
            mock.patch.object(
                game_process,
                "process_image_path",
                side_effect=lambda pid: images[pid],
            ),
        ):
            result = game_process.matching_process_ids(expected)

        self.assertEqual(result, (100, 300))

    def test_preferred_launched_pid_wins_without_using_enumeration_order(self):
        self.assertEqual(
            game_process.choose_process_id((500, 400), preferred_pid=400),
            400,
        )

    def test_multiple_unrelated_candidates_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "multiple matching"):
            game_process.choose_process_id((400, 500))

    def test_configured_pid_is_validated_against_the_expected_image(self):
        expected = Path(r"D:\Detached FF12\x64\FFXII_TZA.exe")
        with mock.patch.object(
            game_process,
            "process_image_path",
            return_value=expected,
        ) as image_path:
            pid = game_process.configured_process_id(
                expected,
                environment={runtime_paths.GAME_PID_ENV: "4242"},
            )

        self.assertEqual(pid, 4242)
        image_path.assert_called_once_with(4242)

    def test_configured_pid_from_another_installation_is_rejected(self):
        expected = Path(r"D:\Detached FF12\x64\FFXII_TZA.exe")
        with mock.patch.object(
            game_process,
            "process_image_path",
            return_value=Path(r"D:\Steam FF12\x64\FFXII_TZA.exe"),
        ):
            with self.assertRaisesRegex(RuntimeError, "belongs to"):
                game_process.configured_process_id(
                    expected,
                    environment={runtime_paths.GAME_PID_ENV: "4242"},
                )

    def test_invalid_configured_pid_does_not_fall_back_to_another_game(self):
        expected = Path(r"D:\Detached FF12\x64\FFXII_TZA.exe")
        with (
            mock.patch.object(
                game_process,
                "validate_process_id",
                side_effect=RuntimeError("wrong process"),
            ),
            mock.patch.object(game_process, "matching_process_ids") as matching,
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong process"):
                game_process.find_process_id(
                    expected,
                    environment={runtime_paths.GAME_PID_ENV: "4242"},
                )

        matching.assert_not_called()


if __name__ == "__main__":
    unittest.main()
