import os
import unittest
from pathlib import Path
from unittest import mock

import runtime_shutdown


class RuntimeShutdownTests(unittest.TestCase):
    def setUp(self):
        self.previous_last_check = runtime_shutdown._last_check
        self.previous_requested = runtime_shutdown._requested
        runtime_shutdown._last_check = 0.0
        runtime_shutdown._requested = False

    def tearDown(self):
        runtime_shutdown._last_check = self.previous_last_check
        runtime_shutdown._requested = self.previous_requested

    def test_missing_marker_keeps_helper_running(self):
        with (
            mock.patch.dict(
                os.environ,
                {runtime_shutdown.SHUTDOWN_FILE_ENV: "missing.shutdown"},
                clear=False,
            ),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            self.assertFalse(runtime_shutdown.requested(force=True))

    def test_transient_marker_io_failure_is_retried_without_stopping_helper(self):
        with (
            mock.patch.dict(
                os.environ,
                {runtime_shutdown.SHUTDOWN_FILE_ENV: "session.shutdown"},
                clear=False,
            ),
            mock.patch.object(
                Path,
                "is_file",
                side_effect=(OSError("sharing violation"), True),
            ) as is_file,
        ):
            self.assertFalse(runtime_shutdown.requested(force=True))
            self.assertTrue(runtime_shutdown.requested(force=True))
        self.assertEqual(is_file.call_count, 2)

    def test_latched_request_survives_later_filesystem_failure(self):
        with (
            mock.patch.dict(
                os.environ,
                {runtime_shutdown.SHUTDOWN_FILE_ENV: "session.shutdown"},
                clear=False,
            ),
            mock.patch.object(Path, "is_file", return_value=True) as is_file,
        ):
            self.assertTrue(runtime_shutdown.requested(force=True))
            self.assertTrue(runtime_shutdown.requested(force=True))
        is_file.assert_called_once()


if __name__ == "__main__":
    unittest.main()
