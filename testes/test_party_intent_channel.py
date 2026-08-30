from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import party_intent_channel as channel

from party_intent_channel import (
    ExplicitPartyEditCapture,
    PartyIntentPublisher,
    normalize_active_members,
    read_party_intent,
)


class PartyIntentChannelTests(unittest.TestCase):
    def test_only_explicit_begin_can_produce_an_intent(self):
        capture = ExplicitPartyEditCapture(min_stable_seconds=0.0)
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 5, 1),
                now=1.0,
            )
        )

    def test_open_frontend_and_layout_churn_reset_the_debounce(self):
        capture = ExplicitPartyEditCapture(
            min_stable_polls=2,
            min_stable_seconds=0.20,
        )
        capture.begin("p2", battle_info=0x1234)
        self.assertIsNone(
            capture.observe(
                native_frontend_open=True,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 5, 1),
                now=1.0,
            )
        )
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 5, 1),
                now=1.1,
            )
        )
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 3, 1),
                now=1.2,
            )
        )
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 3, 1),
                now=1.3,
            )
        )
        self.assertEqual(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 3, 1),
                now=1.41,
            ),
            ((0, 3, 1), 0x1234, "p2"),
        )
        self.assertFalse(capture.armed)

    def test_a_new_battle_generation_cancels_instead_of_becoming_intent(self):
        capture = ExplicitPartyEditCapture(min_stable_seconds=0.0)
        capture.begin("p1", battle_info=0x1234)
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x5678,
                active_members=(0, 5, 1),
                now=1.0,
            )
        )
        self.assertFalse(capture.armed)

    def test_transient_missing_battle_generation_resets_but_does_not_cancel(self):
        capture = ExplicitPartyEditCapture(min_stable_seconds=0.0)
        capture.begin("p1", battle_info=0x1234)
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=False,
                battle_info=0,
                active_members=None,
                now=1.0,
            )
        )
        self.assertTrue(capture.armed)
        self.assertIsNone(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 5, 1),
                now=1.1,
            )
        )
        self.assertEqual(
            capture.observe(
                native_frontend_open=False,
                field_stable=True,
                battle_info=0x1234,
                active_members=(0, 5, 1),
                now=1.2,
            ),
            ((0, 5, 1), 0x1234, "p1"),
        )

    def test_atomic_record_is_pid_scoped_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            publisher = PartyIntentPublisher(4567, path)
            first = publisher.publish((0, 5, 1), battle_info=0xABCDEF, source="p3")
            loaded = read_party_intent(path, game_pid=4567)
            self.assertEqual(loaded, first)
            self.assertIsNone(read_party_intent(path, game_pid=9999))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_transient_publish_failure_retries_one_reserved_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = PartyIntentPublisher(
                4567,
                Path(directory) / "intent.json",
            )
            with (
                mock.patch.object(
                    channel.atomic_files,
                    "write_text",
                    side_effect=(OSError("locked"), None),
                ) as write,
                mock.patch.object(channel.time, "sleep") as sleep,
            ):
                record = publisher.publish(
                    (0, 5, 1),
                    battle_info=0xABCDEF,
                    source="p3",
                )

            self.assertEqual(record.epoch, 1)
            self.assertEqual(publisher.epoch, 1)
            self.assertEqual(write.call_count, 2)
            sleep.assert_called_once_with(channel.PUBLISH_RETRY_SECONDS)

    def test_failed_publish_never_reuses_an_ambiguous_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = PartyIntentPublisher(
                4567,
                Path(directory) / "intent.json",
            )
            with (
                mock.patch.object(
                    channel.atomic_files,
                    "write_text",
                    side_effect=OSError("locked"),
                ),
                mock.patch.object(channel.time, "sleep"),
            ):
                with self.assertRaises(OSError):
                    publisher.publish(
                        (0, 5, 1),
                        battle_info=0xABCDEF,
                        source="p3",
                    )
            self.assertEqual(publisher.epoch, 1)

    def test_committed_write_with_cleanup_error_is_accepted_by_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            publisher = PartyIntentPublisher(4567, path)

            def committed_then_failed(target, payload, *, encoding):
                Path(target).write_text(payload, encoding=encoding)
                raise OSError("temporary cleanup failed")

            with mock.patch.object(
                channel.atomic_files,
                "write_text",
                side_effect=committed_then_failed,
            ) as write:
                record = publisher.publish(
                    (0, 5, 1),
                    battle_info=0xABCDEF,
                    source="p3",
                )

            self.assertEqual(write.call_count, 1)
            self.assertEqual(read_party_intent(path, game_pid=4567), record)
            self.assertEqual(publisher.epoch, 1)

    def test_corrupt_or_guest_intent_is_not_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(read_party_intent(path, game_pid=1))
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "game_pid": 1,
                        "writer": "x",
                        "epoch": 1,
                        "published_unix_ns": 1,
                        "battle_info": 2,
                        "active_members": [0, 11],
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(read_party_intent(path, game_pid=1))

    def test_active_members_reject_duplicates_and_guests(self):
        self.assertEqual(normalize_active_members((0, 5, 1)), (0, 5, 1))
        for invalid in ((), (0, 0), (0, 6), (0, 1, 2, 3)):
            with self.assertRaises(ValueError):
                normalize_active_members(invalid)
        for inexact in ((0, 5.0), (0, True), ("0", 5)):
            with self.assertRaises(ValueError):
                normalize_active_members(inexact)


if __name__ == "__main__":
    unittest.main()
