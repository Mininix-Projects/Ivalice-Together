import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime_status
import start_headless_coop


class RuntimeStatusTests(unittest.TestCase):
    def test_status_round_trip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            expected = runtime_status.write_status(
                1234,
                "preparing",
                "Preparando",
                data_directory=data,
            )
            self.assertEqual(
                runtime_status.read_status(1234, data_directory=data),
                expected,
            )
            runtime_status.clear_status(1234, data_directory=data)
            self.assertIsNone(runtime_status.read_status(1234, data_directory=data))

    def test_invalid_or_foreign_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            path = runtime_status.status_path(77, data)
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"version": 1, "game_pid": 88, "state": "ready"}',
                encoding="utf-8",
            )
            self.assertIsNone(runtime_status.read_status(77, data_directory=data))

    def test_runtime_and_configurator_share_the_disposable_status_directory(self):
        with mock.patch.object(runtime_status, "write_status") as write_status:
            start_headless_coop.publish_runtime_status(
                77,
                "ready",
                "Ready",
                warning="status warning",
            )
        write_status.assert_called_once_with(
            77,
            "ready",
            "Ready",
            data_directory=start_headless_coop.RUNTIME_DIRECTORY,
        )

        with mock.patch.object(runtime_status, "clear_status") as clear_status:
            start_headless_coop.clear_runtime_status(77)
        clear_status.assert_called_once_with(
            77,
            data_directory=start_headless_coop.RUNTIME_DIRECTORY,
        )


if __name__ == "__main__":
    unittest.main()
