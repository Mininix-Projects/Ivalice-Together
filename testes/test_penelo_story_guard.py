from penelo_story_guard import (
    ASHE_ID,
    BALTHIER_ID,
    EMPTY,
    PENELO_ID,
    REKS_ID,
    VAAN_ID,
    PeneloStoryGuard,
    layout_has_unique_members,
    normalize_coop_roster,
)
import struct
import threading
import unittest
from unittest import mock

import penelo_story_guard as guard
import pytest


class GuardStartupTests(unittest.TestCase):
    def test_transient_read_diagnostic_is_rate_limited_and_best_effort(self):
        with mock.patch("builtins.print") as output:
            deadline = guard.report_transient_read(
                OSError(299, "loading"),
                now=10.0,
                next_log_at=0.0,
            )
            same_deadline = guard.report_transient_read(
                struct.error("torn layout"),
                now=10.1,
                next_log_at=deadline,
            )

        self.assertEqual(deadline, 10.0 + guard.TRANSIENT_LOG_INTERVAL_SECONDS)
        self.assertEqual(same_deadline, deadline)
        output.assert_called_once()

    def test_unprintable_transient_error_cannot_stop_the_guard(self):
        class UnprintableError(OSError):
            def __str__(self):
                raise RuntimeError("broken diagnostic")

        with mock.patch("builtins.print", side_effect=OSError("broken pipe")):
            deadline = guard.report_transient_read(
                UnprintableError(),
                now=7.0,
                next_log_at=0.0,
            )

        self.assertEqual(deadline, 7.0 + guard.TRANSIENT_LOG_INTERVAL_SECONDS)

    def test_remote_process_open_failure_releases_penelo_lease(self):
        lease = mock.Mock()
        with (
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                guard.coop,
                "RemoteProcess",
                side_effect=OSError("OpenProcess failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "OpenProcess"):
                guard.main()

        lease.close.assert_called_once_with()

    def test_guard_waits_without_installing_hooks_when_start_all_is_disabled(self):
        process = mock.Mock()
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", ()),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(guard.runtime_shutdown, "requested", return_value=True),
            mock.patch.object(guard, "PeneloStoryGuard") as story_guard,
        ):
            self.assertEqual(guard.main(), 0)

        story_guard.assert_not_called()
        process.close.assert_called_once_with()

    def test_empty_roster_main_survives_transient_read_while_handle_is_alive(self):
        process = mock.Mock()
        process.read.side_effect = OSError("transient image read")
        lease = mock.Mock()
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", ()),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(
                guard.coop,
                "movement_runtime_process_alive",
                return_value=True,
            ) as alive,
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                guard.runtime_shutdown,
                "requested",
                side_effect=(False, True),
            ),
            mock.patch.object(guard.time, "sleep"),
        ):
            self.assertEqual(guard.main(), 0)

        alive.assert_called_once_with(process)
        process.read.assert_not_called()
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_empty_roster_main_exits_when_process_handle_is_signalled(self):
        process = mock.Mock()
        process.read.side_effect = OSError("dead image read")
        lease = mock.Mock()
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", ()),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(
                guard.coop,
                "movement_runtime_process_alive",
                return_value=False,
            ) as alive,
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(guard.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(guard.time, "sleep") as sleep,
        ):
            self.assertEqual(guard.main(), 0)

        alive.assert_called_once_with(process)
        sleep.assert_not_called()
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_active_guard_main_survives_transient_read_while_handle_is_alive(self):
        process = mock.Mock()
        process.read.side_effect = OSError("transient image read")
        lease = mock.Mock()
        story_guard = mock.Mock()
        story_guard.tick.side_effect = OSError("loading changed battle info")
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", (PENELO_ID,)),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(guard, "PeneloStoryGuard", return_value=story_guard),
            mock.patch.object(
                guard.coop,
                "movement_runtime_process_alive",
                return_value=True,
            ) as alive,
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                guard.runtime_shutdown,
                "requested",
                side_effect=(False, True),
            ),
            mock.patch.object(guard.time, "sleep"),
        ):
            self.assertIsNone(guard.main())

        self.assertEqual(alive.call_args_list, [mock.call(process), mock.call(process)])
        process.read.assert_not_called()
        story_guard.tick.assert_called_once_with()
        story_guard.close.assert_called_once_with()
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_guard_construction_failure_still_closes_process_and_lease(self):
        process = mock.Mock()
        lease = mock.Mock()
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", (PENELO_ID,)),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(
                guard,
                "PeneloStoryGuard",
                side_effect=RuntimeError("foreign party hook"),
            ),
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
        ):
            with self.assertRaisesRegex(RuntimeError, "foreign party hook"):
                guard.main()

        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_structural_tick_error_is_not_hidden_by_frame_recovery(self):
        process = mock.Mock()
        lease = mock.Mock()
        story_guard = mock.Mock()
        story_guard.tick.side_effect = RuntimeError("party ABI changed")
        story_guard.close.side_effect = RuntimeError("guard cleanup failed")
        process.close.side_effect = OSError("process close failed")
        lease.close.side_effect = RuntimeError("lease close failed")
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", (PENELO_ID,)),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(guard, "PeneloStoryGuard", return_value=story_guard),
            mock.patch.object(
                guard.coop,
                "movement_runtime_process_alive",
                return_value=True,
            ),
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(guard.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(guard.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "party ABI"):
                guard.main()

        sleep.assert_not_called()
        story_guard.close.assert_called_once_with()
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_active_guard_main_exits_when_process_handle_is_signalled(self):
        process = mock.Mock()
        process.read.side_effect = OSError("dead image read")
        lease = mock.Mock()
        story_guard = mock.Mock()
        with (
            mock.patch.object(guard, "PROTECTED_MEMBER_IDS", (PENELO_ID,)),
            mock.patch.object(guard.coop, "find_pid", return_value=1234),
            mock.patch.object(guard.coop, "RemoteProcess", return_value=process),
            mock.patch.object(guard, "PeneloStoryGuard", return_value=story_guard),
            mock.patch.object(
                guard.coop,
                "movement_runtime_process_alive",
                return_value=False,
            ) as alive,
            mock.patch.object(guard.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(guard.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(guard.time, "sleep") as sleep,
        ):
            self.assertIsNone(guard.main())

        alive.assert_called_once_with(process)
        process.read.assert_not_called()
        sleep.assert_not_called()
        story_guard.tick.assert_not_called()
        story_guard.close.assert_called_once_with()
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_guard_uses_the_pre_reserved_rel32_workspace(self):
        process = mock.Mock()
        process.read.side_effect = (
            b"\x48\x89\x5c\x24\x08",
            b"\x48\x83\xec\x28",
        )
        damage_base = 0x30000000
        process.ensure_damage_number_theme_allocation.return_value = damage_base
        workspace = damage_base + guard.coop.PARTY_GUARD_WORKSPACE_OFFSET
        caller = mock.Mock(workspace=workspace)

        with (
            mock.patch.object(guard, "RemoteCaller", return_value=caller) as factory,
            mock.patch.object(guard, "PeneloRemovalFilter"),
            mock.patch.object(
                guard.story_removal_journal, "StoryRemovalJournal"
            ) as journal,
            mock.patch.object(
                guard.party_intent_channel, "read_party_intent", return_value=None
            ),
        ):
            instance = guard.PeneloStoryGuard(process, game_pid=1234)

        factory.assert_called_once_with(process, workspace)
        journal.assert_called_once_with(process, workspace)
        self.assertIs(instance.caller, caller)
        instance.removal_filter.set_enabled.assert_called_once_with(False)

    def test_post_hook_initialization_failure_closes_every_owned_component(self):
        process = mock.Mock()
        process.read.side_effect = (
            b"\x48\x89\x5c\x24\x08",
            b"\x48\x83\xec\x28",
        )
        process.ensure_damage_number_theme_allocation.return_value = 0x30000000
        caller = mock.Mock(workspace=0x30001000)
        removal_filter = mock.Mock()
        journal = mock.Mock()
        with (
            mock.patch.object(guard, "RemoteCaller", return_value=caller),
            mock.patch.object(
                guard,
                "PeneloRemovalFilter",
                return_value=removal_filter,
            ),
            mock.patch.object(
                guard.story_removal_journal,
                "StoryRemovalJournal",
                return_value=journal,
            ),
            mock.patch.object(
                guard.party_intent_channel,
                "read_party_intent",
                side_effect=OSError("initial intent unavailable"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "initial intent"):
                guard.PeneloStoryGuard(process, game_pid=1234)

        caller.close.assert_called_once_with()
        removal_filter.restore.assert_called_once_with()
        journal.restore.assert_called_once_with()

    def test_guard_close_waits_caller_before_disarming_static_consumers(self):
        instance = guard.PeneloStoryGuard.__new__(guard.PeneloStoryGuard)
        events = []
        instance.caller = mock.Mock()
        instance.removal_filter = mock.Mock()
        instance.story_removal_journal = mock.Mock()
        instance.caller.close.side_effect = lambda: events.append("caller")
        instance.removal_filter.restore.side_effect = lambda: events.append("filter")
        instance.story_removal_journal.restore.side_effect = lambda: events.append(
            "journal"
        )

        instance.close()

        self.assertEqual(events, ["caller", "filter", "journal"])

    def test_guard_close_attempts_every_component_after_cleanup_failures(self):
        instance = guard.PeneloStoryGuard.__new__(guard.PeneloStoryGuard)
        instance.caller = mock.Mock()
        instance.removal_filter = mock.Mock()
        instance.story_removal_journal = mock.Mock()
        instance.caller.close.side_effect = RuntimeError("caller cleanup")
        instance.removal_filter.restore.side_effect = OSError("filter cleanup")

        with mock.patch("builtins.print"):
            instance.close()

        instance.caller.close.assert_called_once_with()
        instance.removal_filter.restore.assert_called_once_with()
        instance.story_removal_journal.restore.assert_called_once_with()


class RemoteCallerLifetimeTests(unittest.TestCase):
    class SlotProcess:
        def __init__(self):
            self.handle = 0x1234
            self.memory = {}
            self.writes = []
            self.fail_claim_timing = None

        def put(self, address, payload):
            for offset, value in enumerate(bytes(payload)):
                self.memory[address + offset] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + offset, 0) for offset in range(size))

        def write(self, address, payload):
            payload = bytes(payload)
            if (
                self.fail_claim_timing == "before"
                and payload == guard.REMOTE_CALL_CLAIM_MAGIC
            ):
                raise OSError("injected claim failure")
            self.writes.append((address, payload))
            self.put(address, payload)
            if (
                self.fail_claim_timing == "after"
                and payload == guard.REMOTE_CALL_CLAIM_MAGIC
            ):
                raise OSError("injected claim failure")

    def make_caller(self):
        process = mock.Mock(handle=0x1234)
        memory = {}

        def read(address, size):
            return bytes(memory.get(address + offset, 0) for offset in range(size))

        def write(address, payload):
            for offset, value in enumerate(bytes(payload)):
                memory[address + offset] = value

        process.read.side_effect = read
        process.write.side_effect = write
        process.u64.return_value = 0xABCDEF
        caller = guard.RemoteCaller(process, 0x30000000)
        process.read.reset_mock()
        process.write.reset_mock()
        return caller, process

    def test_timeout_is_only_a_poll_slice_until_the_thread_signals(self):
        caller, process = self.make_caller()
        waits = (
            guard.WAIT_TIMEOUT,
            guard.WAIT_TIMEOUT,
            guard.WAIT_TIMEOUT,
            guard.WAIT_TIMEOUT,
            guard.WAIT_OBJECT_0,
        )
        with (
            mock.patch.object(guard.kernel32, "CreateRemoteThread", return_value=77),
            mock.patch.object(
                guard.kernel32,
                "WaitForSingleObject",
                side_effect=waits,
            ) as wait,
            mock.patch.object(guard.kernel32, "CloseHandle") as close,
        ):
            self.assertEqual(caller.call(0x3172E0, 5, 6), 0xABCDEF)

        self.assertEqual(
            wait.call_args_list,
            [
                mock.call(77, guard.REMOTE_CALL_WAIT_SLICE_MS),
                mock.call(process.handle, 0),
                mock.call(77, guard.REMOTE_CALL_WAIT_SLICE_MS),
                mock.call(process.handle, 0),
                mock.call(77, guard.REMOTE_CALL_WAIT_SLICE_MS),
            ],
        )
        close.assert_called_once_with(77)
        process.u64.assert_called_once_with(0x30000100)

    def test_game_exit_ends_the_wait_without_reading_remote_workspace(self):
        caller, process = self.make_caller()
        with (
            mock.patch.object(guard.kernel32, "CreateRemoteThread", return_value=88),
            mock.patch.object(
                guard.kernel32,
                "WaitForSingleObject",
                side_effect=(guard.WAIT_TIMEOUT, guard.WAIT_OBJECT_0),
            ),
            mock.patch.object(guard.kernel32, "CloseHandle") as close,
        ):
            with self.assertRaisesRegex(RuntimeError, "game exited"):
                caller.call(0x3172E0)

        close.assert_called_once_with(88)
        process.u64.assert_not_called()

    def test_unprovable_thread_wait_quarantines_slot_without_hanging_or_reuse(self):
        caller, process = self.make_caller()
        claimed_slot = caller.call_workspace
        with (
            mock.patch.object(guard.kernel32, "CreateRemoteThread", return_value=89),
            mock.patch.object(
                guard.kernel32,
                "WaitForSingleObject",
                side_effect=(guard.WAIT_FAILED, guard.WAIT_TIMEOUT),
            ),
            mock.patch.object(guard.kernel32, "CloseHandle") as close,
        ):
            with self.assertRaisesRegex(RuntimeError, "slot quarantined"):
                caller.call(0x3172E0)

        close.assert_called_once_with(89)
        process.u64.assert_not_called()
        self.assertEqual(caller.call_workspace, 0)
        self.assertEqual(caller.workspace, 0)

        process.write.reset_mock()
        caller.close()
        process.write.assert_not_called()
        self.assertGreater(claimed_slot, 0)

    def test_live_but_stalled_remote_thread_is_bounded_and_quarantined(self):
        caller, process = self.make_caller()
        with (
            mock.patch.object(guard.kernel32, "CreateRemoteThread", return_value=90),
            mock.patch.object(
                guard.kernel32,
                "WaitForSingleObject",
                side_effect=(guard.WAIT_TIMEOUT, guard.WAIT_TIMEOUT),
            ),
            mock.patch.object(
                guard.time,
                "monotonic",
                side_effect=(100.0, 100.0 + guard.REMOTE_CALL_MAX_WAIT_SECONDS),
            ),
            mock.patch.object(guard.kernel32, "CloseHandle") as close,
        ):
            with self.assertRaisesRegex(RuntimeError, "slot quarantined"):
                caller.call(0x3172E0)

        close.assert_called_once_with(90)
        process.u64.assert_not_called()
        self.assertEqual(caller.call_workspace, 0)
        self.assertEqual(caller.workspace, 0)

    def test_closed_caller_never_reuses_its_workspace(self):
        caller, process = self.make_caller()
        caller.close()
        process.write.reset_mock()

        with self.assertRaisesRegex(RuntimeError, "remote caller is closed"):
            caller.call(0x3172E0)

        process.write.assert_not_called()

    def test_close_waits_until_an_inflight_remote_thread_is_signalled(self):
        caller, _process = self.make_caller()
        remote_waiting = threading.Event()
        release_remote = threading.Event()
        call_result = []

        def wait(handle, _timeout):
            if handle == 77:
                remote_waiting.set()
                self.assertTrue(release_remote.wait(2.0))
                return guard.WAIT_OBJECT_0
            return guard.WAIT_TIMEOUT

        with (
            mock.patch.object(guard.kernel32, "CreateRemoteThread", return_value=77),
            mock.patch.object(guard.kernel32, "WaitForSingleObject", side_effect=wait),
            mock.patch.object(guard.kernel32, "CloseHandle"),
        ):
            worker = threading.Thread(
                target=lambda: call_result.append(caller.call(0x3172E0)),
            )
            worker.start()
            self.assertTrue(remote_waiting.wait(2.0))
            closer = threading.Thread(target=caller.close)
            closer.start()
            try:
                closer.join(0.05)
                self.assertTrue(closer.is_alive())
                self.assertNotEqual(caller.workspace, 0)
            finally:
                release_remote.set()
            worker.join(2.0)
            closer.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(call_result, [0xABCDEF])
        self.assertEqual(caller.workspace, 0)

    def test_python_interruption_is_deferred_until_remote_thread_completion(self):
        caller, process = self.make_caller()
        with (
            mock.patch.object(guard.kernel32, "CreateRemoteThread", return_value=91),
            mock.patch.object(
                guard.kernel32,
                "WaitForSingleObject",
                side_effect=(KeyboardInterrupt(), guard.WAIT_OBJECT_0),
            ) as wait,
            mock.patch.object(guard.kernel32, "CloseHandle") as close,
        ):
            with self.assertRaises(KeyboardInterrupt):
                caller.call(0x3172E0)

        self.assertEqual(wait.call_count, 2)
        close.assert_called_once_with(91)
        process.u64.assert_not_called()
        self.assertNotEqual(caller.workspace, 0)

    def test_call_stub_write_failures_never_start_a_thread_and_are_retryable(self):
        for fail_write, timing in (
            (1, "before"),
            (1, "after"),
            (2, "before"),
            (2, "after"),
        ):
            with self.subTest(fail_write=fail_write, timing=timing):
                caller, process = self.make_caller()
                base_write = process.write.side_effect
                writes = 0

                def failing_write(address, payload):
                    nonlocal writes
                    writes += 1
                    if writes == fail_write and timing == "before":
                        raise OSError("injected remote call write")
                    base_write(address, payload)
                    if writes == fail_write and timing == "after":
                        raise OSError("injected remote call write")

                process.write.side_effect = failing_write
                with mock.patch.object(
                    guard.kernel32,
                    "CreateRemoteThread",
                ) as create:
                    with self.assertRaisesRegex(OSError, "remote call write"):
                        caller.call(0x3172E0)
                create.assert_not_called()

                process.write.side_effect = base_write
                with (
                    mock.patch.object(
                        guard.kernel32,
                        "CreateRemoteThread",
                        return_value=77,
                    ),
                    mock.patch.object(
                        guard.kernel32,
                        "WaitForSingleObject",
                        return_value=guard.WAIT_OBJECT_0,
                    ),
                    mock.patch.object(guard.kernel32, "CloseHandle"),
                ):
                    self.assertEqual(caller.call(0x3172E0), 0xABCDEF)

    def test_hot_reattach_quarantines_a_prior_helpers_live_call_slot(self):
        process = self.SlotProcess()
        workspace = 0x30000000
        interrupted = guard.RemoteCaller(process, workspace)
        self.assertEqual(interrupted.call_workspace, workspace)

        attached = guard.RemoteCaller(process, workspace)

        self.assertEqual(
            attached.call_workspace,
            workspace + guard.REMOTE_CALL_REGION_OFFSETS[1],
        )
        self.assertEqual(
            process.read(
                interrupted.call_workspace + guard.REMOTE_CALL_CLAIM_OFFSET,
                len(guard.REMOTE_CALL_CLAIM_MAGIC),
            ),
            guard.REMOTE_CALL_CLAIM_MAGIC,
        )

    def test_clean_close_recycles_slot_only_after_lifetime_lock_is_owned(self):
        process = self.SlotProcess()
        workspace = 0x30000000
        first = guard.RemoteCaller(process, workspace)
        self.assertEqual(first.call_workspace, workspace)
        first.close()

        replacement = guard.RemoteCaller(process, workspace)

        self.assertEqual(replacement.call_workspace, workspace)

    def test_interrupted_claim_is_quarantined_on_the_next_attach(self):
        workspace = 0x30000000
        for timing, expected_offset in (
            ("before", guard.REMOTE_CALL_REGION_OFFSETS[0]),
            ("after", guard.REMOTE_CALL_REGION_OFFSETS[1]),
        ):
            with self.subTest(timing=timing):
                process = self.SlotProcess()
                process.fail_claim_timing = timing
                with self.assertRaisesRegex(OSError, "claim failure"):
                    guard.RemoteCaller(process, workspace)

                process.fail_claim_timing = None
                replacement = guard.RemoteCaller(process, workspace)
                self.assertEqual(
                    replacement.call_workspace,
                    workspace + expected_offset,
                )

    def test_all_quarantined_call_slots_fail_closed_without_mutation(self):
        process = self.SlotProcess()
        workspace = 0x30000000
        for offset in guard.REMOTE_CALL_REGION_OFFSETS:
            process.put(workspace + offset, b"\x7f")
        before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "all .* slots are quarantined"):
            guard.RemoteCaller(process, workspace)

        self.assertEqual(process.memory, before)
        self.assertEqual(process.writes, [])


def normalize_penelo(slots, *, prefer_active=True):
    return guard.coop_roster.normalize_live_party_layout(
        slots,
        (PENELO_ID,),
        empty=EMPTY,
        prefer_active=prefer_active,
    )


def layout_has_active_penelo(slots):
    return slots[1] == PENELO_ID and slots.count(PENELO_ID) == 1


def layout_has_single_penelo(slots):
    return slots.count(PENELO_ID) == 1


def build_legacy_penelo_remove_filter(cave, enabled_address):
    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", enabled_address)
    code += b"\x80\x38\x01\x75\x08"
    code += b"\x80\xf9\x05\x75\x03"
    code += b"\x31\xc0\xc3"
    code += guard.REMOVE_PARTY_MEMBER_ORIGINAL
    jump_site = cave + len(code)
    code += b"\xe9" + guard.coop.rel32(jump_site, 5, guard.REMOVE_PARTY_MEMBER + 7)
    return bytes(code)


class GuestSlotTests(unittest.TestCase):
    @staticmethod
    def signature(default, current=None, ui=None, menu=None):
        flags = tuple(0x1C for _member in guard.PROTECTED_MEMBER_IDS)
        current = default if current is None else current
        ui = current if ui is None else ui
        menu = default if menu is None else menu
        return flags, (default, current, ui, menu, default)

    def test_health_rejects_a_guest_in_a_selectable_live_slot(self):
        active = [VAAN_ID, *guard.PROTECTED_MEMBER_IDS]
        active = active[:3]
        good = tuple(active + [EMPTY] * 6)
        stray = list(good)
        stray[1] = 11
        displaced = active[1]
        stray[4] = displaced
        self.assertFalse(PeneloStoryGuard.healthy(self.signature(tuple(stray))))

    def test_every_story_guest_defers_full_actor_rebuild_after_party_edit(self):
        expected_guests = {
            6: "Reks",
            7: "Amalia/Ashe",
            8: "Basch (Prisoner)",
            9: "Basch (Fugitive)",
            10: "Lamont",
            11: "Vossler (Judge)",
            12: "Vossler (Knight)",
            13: "Larsa",
            14: "Reddas",
        }
        self.assertEqual(guard.STORY_GUEST_MEMBERS, expected_guests)

        for guest, name in expected_guests.items():
            with self.subTest(guest=guest, name=name):
                persistent = (
                    VAAN_ID,
                    PENELO_ID,
                    EMPTY,
                    guest,
                    *([EMPTY] * 5),
                )
                layouts = (
                    persistent,
                    persistent,
                    persistent,
                    persistent,
                    persistent,
                )

                self.assertEqual(
                    guard.coherent_story_guest_members(layouts),
                    (guest,),
                )
                self.assertEqual(
                    guard.guest_party_edit_cooldown_until(
                        2.0,
                        layouts,
                        manual_adopted=True,
                        now=10.0,
                    ),
                    10.0 + guard.GUEST_PARTY_EDIT_SETTLE_SECONDS,
                )
                self.assertEqual(
                    guard.guest_party_edit_cooldown_until(
                        2.0,
                        layouts,
                        manual_adopted=False,
                        now=10.0,
                    ),
                    2.0,
                )

    def test_non_story_temporary_actors_do_not_arm_guest_rebuild_settle(self):
        for temporary_actor in (26, 27, 39):
            with self.subTest(temporary_actor=temporary_actor):
                layout = (
                    VAAN_ID,
                    PENELO_ID,
                    EMPTY,
                    temporary_actor,
                    *([EMPTY] * 5),
                )
                layouts = (layout, layout, layout, layout, layout)
                self.assertEqual(guard.coherent_story_guest_members(layouts), ())
                self.assertEqual(
                    guard.guest_party_edit_cooldown_until(
                        2.0,
                        layouts,
                        manual_adopted=True,
                        now=10.0,
                    ),
                    2.0,
                )

    def test_torn_guest_generations_never_arm_party_rebuild_cooldown(self):
        default = (VAAN_ID, PENELO_ID, EMPTY, 11, *([EMPTY] * 5))
        menu = (VAAN_ID, PENELO_ID, EMPTY, 13, *([EMPTY] * 5))
        layouts = (default, default, default, menu, default)

        self.assertEqual(guard.coherent_story_guest_members(layouts), ())
        self.assertEqual(
            guard.guest_party_edit_cooldown_until(
                3.0,
                layouts,
                manual_adopted=True,
                now=10.0,
            ),
            3.0,
        )


def test_adds_penelo_after_vaan():
    result = normalize_penelo(
        (0, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
    )
    assert result == (0, 5, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
    assert layout_has_active_penelo(result)


def test_preserves_native_guest_slot_while_adding_penelo_to_trio():
    result = normalize_penelo((0, EMPTY, EMPTY, 8, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY))
    assert result[:4] == (0, 5, EMPTY, 8)


def test_removes_active_and_reserve_duplicates():
    result = normalize_penelo((0, 5, 5, EMPTY, 5, 4, 3, EMPTY, EMPTY))
    assert result == (0, 5, EMPTY, EMPTY, EMPTY, 4, 3, EMPTY, EMPTY)
    assert result.count(PENELO_ID) == 1


def test_story_repair_keeps_penelo_out_of_the_player_active_trio():
    result = normalize_penelo(
        (0, 3, 2, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY),
        prefer_active=False,
    )
    assert result[:4] == (0, 3, 2, EMPTY)
    assert result[4] == PENELO_ID
    assert layout_has_single_penelo(result)


def test_initial_coop_roster_adds_penelo_and_balthier_after_vaan():
    with mock.patch.object(guard, "PROTECTED_MEMBER_IDS", (PENELO_ID, BALTHIER_ID)):
        result = normalize_coop_roster(
            (VAAN_ID, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
        )
    assert result[:4] == (VAAN_ID, PENELO_ID, BALTHIER_ID, EMPTY)
    assert result.count(PENELO_ID) == 1
    assert result.count(BALTHIER_ID) == 1


def test_story_repair_preserves_existing_companion_and_restores_missing_one():
    with mock.patch.object(guard, "PROTECTED_MEMBER_IDS", (PENELO_ID, BALTHIER_ID)):
        result = normalize_coop_roster(
            (VAAN_ID, 3, 2, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY),
            prefer_active=False,
        )
    assert result[:4] == (VAAN_ID, BALTHIER_ID, 2, EMPTY)
    assert result[4] == PENELO_ID


def test_roster_normalizer_collapses_a_duplicated_guest():
    result = normalize_coop_roster(
        (VAAN_ID, 11, 2, EMPTY, 11, PENELO_ID, BALTHIER_ID, EMPTY, EMPTY),
        prefer_active=False,
    )
    assert result.count(11) == 1
    assert result[:4] == (VAAN_ID, EMPTY, 2, 11)
    assert layout_has_unique_members(result)


def test_legacy_remove_filter_has_a_disarmed_native_fallthrough_path():
    cave = 0x30000000
    state = 0x30000200
    code = guard.build_penelo_remove_filter(cave, state)

    assert code.startswith(b"\x48\xb8" + struct.pack("<Q", state))
    assert b"\x80\x38\x01" in code
    for member_id in guard.PROTECTED_MEMBER_IDS:
        assert b"\x80\xf9" + bytes((member_id,)) in code
    assert b"\x31\xc0\xc3" in code
    original_at = code.index(guard.REMOVE_PARTY_MEMBER_ORIGINAL)
    assert original_at > code.index(b"\x31\xc0\xc3")
    displacement = struct.unpack_from("<i", code, len(code) - 4)[0]
    assert cave + len(code) + displacement == guard.REMOVE_PARTY_MEMBER + 7


def test_remove_filter_encodes_all_five_members_with_correct_short_jumps():
    cave = 0x30000000
    state = 0x30000200
    members = (PENELO_ID, BALTHIER_ID, 2, 4, ASHE_ID)
    code = guard.build_penelo_remove_filter(cave, state, members)

    assert len(code) == 30 + 5 * len(members)
    assert guard.REMOVE_FILTER_CAVE_CAPACITY == 0x200
    assert len(code) <= guard.REMOVE_FILTER_CAVE_CAPACITY
    assert code[:15] == (
        b"\x48\xb8" + struct.pack("<Q", state) + b"\x80\x38\x01\x75\x1c"
    )

    swallow_at = code.index(b"\x31\xc0\xc3")
    original_at = code.index(guard.REMOVE_PARTY_MEMBER_ORIGINAL)
    state_jump = struct.unpack_from("<b", code, 14)[0]
    assert 15 + state_jump == original_at

    comparisons_at = 15
    for index, member_id in enumerate(members):
        comparison = comparisons_at + index * 5
        assert code[comparison : comparison + 3] == (b"\x80\xf9" + bytes((member_id,)))
        branch_opcode = code[comparison + 3]
        branch = struct.unpack_from("<b", code, comparison + 4)[0]
        branch_target = comparison + 5 + branch
        if index + 1 < len(members):
            assert branch_opcode == 0x74  # je swallow
            assert branch_target == swallow_at
        else:
            assert branch_opcode == 0x75  # jne native original
            assert branch_target == original_at

    displacement = struct.unpack_from("<i", code, len(code) - 4)[0]
    assert cave + len(code) + displacement == guard.REMOVE_PARTY_MEMBER + 7


def test_remove_filter_rejects_empty_too_many_and_non_byte_member_sets():
    cave = 0x30000000
    state = 0x30000200
    for members in (
        (),
        tuple(range(1, 7)),
        (-1,),
        (0x100,),
        (0,),
        (6,),
        (0xFF,),
        ("5",),
    ):
        with unittest.TestCase().assertRaises(ValueError):
            guard.build_penelo_remove_filter(cave, state, members)

    with mock.patch.object(guard, "REMOVE_FILTER_CAVE_CAPACITY", 54):
        with unittest.TestCase().assertRaises(RuntimeError):
            guard.build_penelo_remove_filter(
                cave,
                state,
                (PENELO_ID, BALTHIER_ID, 2, 4, ASHE_ID),
            )


def test_guard_never_arms_the_legacy_story_removal_block():
    class FakeProcess:
        def read(self, _address, size):
            return bytes((guard.JOINED_ONCE_FLAG,)) * size

    class FakeRemovalFilter:
        def __init__(self):
            self.states = []

        def set_enabled(self, enabled):
            self.states.append(enabled)

    instance = PeneloStoryGuard.__new__(PeneloStoryGuard)
    instance.process = FakeProcess()
    instance.removal_filter = FakeRemovalFilter()
    instance.story_removal_journal = mock.Mock()
    instance.story_removal_journal.snapshot.return_value = ()
    instance.armed = False
    instance.bad_signature = None
    instance.bad_since = 0.0
    instance.cooldown_until = 0.0
    instance.accepted_active_members = (VAAN_ID,)
    instance.pending_story_restore = set()
    instance.consume_manual_party_intent = lambda *_arguments: False
    instance.observe_unproven_active_change = lambda _layouts: None
    instance.healthy = lambda _signature: True
    good = (
        VAAN_ID,
        *guard.PROTECTED_MEMBER_IDS,
        *([EMPTY] * (8 - len(guard.PROTECTED_MEMBER_IDS))),
    )
    instance.battle_info = lambda: 0x500000
    instance.layout = lambda _base, _layout_id: good
    instance.controlled_party_is_vaan = lambda _base: True
    instance.signature = lambda _base: (
        tuple(0x1C for _member in guard.PROTECTED_MEMBER_IDS),
        (good,) * guard.PARTY_LAYOUT_COUNT,
    )

    instance.tick()

    assert instance.armed
    assert guard.BLOCK_NATIVE_STORY_REMOVALS is False
    assert instance.removal_filter.states == [False]


def test_fresh_reks_generation_resets_an_armed_previous_save():
    reks = (REKS_ID, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
    assert PeneloStoryGuard.is_early_reks_generation(0, reks)
    assert not PeneloStoryGuard.is_early_reks_generation(guard.JOINED_ONCE_FLAG, reks)
    assert not PeneloStoryGuard.is_early_reks_generation(
        0,
        (VAAN_ID, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY),
    )


def test_decodes_only_our_exact_resident_remove_filter():
    class FakeProcess:
        def __init__(self):
            self.memory = {}

        def write(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

    process = FakeProcess()
    workspace = 0x30000000
    cave = workspace + guard.REMOVE_FILTER_CAVE_OFFSET
    state = workspace + guard.REMOVE_FILTER_STATE_OFFSET
    code = guard.build_penelo_remove_filter(cave, state)
    process.write(cave, code)
    patch = b"\xe9" + guard.coop.rel32(guard.REMOVE_PARTY_MEMBER, 5, cave)
    patch += b"\x90" * (len(guard.REMOVE_PARTY_MEMBER_ORIGINAL) - len(patch))
    assert guard.decode_penelo_remove_filter(process, patch) == (cave, state)
    process.write(cave + 12, b"\xff")
    assert guard.decode_penelo_remove_filter(process, patch) is None

    wrong_state = state + 1
    process.write(
        cave,
        guard.build_penelo_remove_filter(
            cave,
            wrong_state,
            (PENELO_ID, BALTHIER_ID),
        ),
    )
    assert guard.decode_penelo_remove_filter(process, patch) is None


def test_decoder_adopts_exact_legacy_and_five_member_filters():
    class FakeProcess:
        def __init__(self):
            self.memory = {}

        def write(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

    process = FakeProcess()
    cave = 0x30000300
    state = 0x30000200
    patch = b"\xe9" + guard.coop.rel32(guard.REMOVE_PARTY_MEMBER, 5, cave)
    patch += b"\x90" * (len(guard.REMOVE_PARTY_MEMBER_ORIGINAL) - len(patch))
    member_sets = (
        (PENELO_ID,),
        (ASHE_ID, 2),
        (PENELO_ID, BALTHIER_ID, 2, 4, ASHE_ID),
    )
    for members in member_sets:
        with unittest.TestCase().subTest(members=members):
            code = guard.build_penelo_remove_filter(cave, state, members)
            process.write(cave, code)
            assert guard.decode_penelo_remove_filter(process, patch) == (
                cave,
                state,
            )

    legacy = build_legacy_penelo_remove_filter(cave, state)
    process.write(cave, legacy)
    assert guard.decode_penelo_remove_filter(process, patch) == (cave, state)


def test_removal_filter_rejects_live_layout_upgrade_without_hot_repair():
    class FakeProcess:
        def __init__(self):
            self.memory = {}
            self.patch_calls = []

        def write(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def patch_code(self, address, data):
            self.patch_calls.append((address, data))
            self.write(address, data)

    process = FakeProcess()
    old_workspace = 0x30000000
    old_state = old_workspace + guard.REMOVE_FILTER_STATE_OFFSET
    old_cave = old_workspace + guard.REMOVE_FILTER_CAVE_OFFSET
    old_code = guard.build_penelo_remove_filter(
        old_cave,
        old_state,
        (PENELO_ID, BALTHIER_ID),
    )
    old_patch = b"\xe9" + guard.coop.rel32(guard.REMOVE_PARTY_MEMBER, 5, old_cave)
    old_patch += b"\x90" * (len(guard.REMOVE_PARTY_MEMBER_ORIGINAL) - len(old_patch))
    process.write(old_cave, old_code)
    process.write(old_state, b"\x01")
    process.write(guard.REMOVE_PARTY_MEMBER, old_patch)
    original_memory = dict(process.memory)

    members = (PENELO_ID, BALTHIER_ID, 2, 4, ASHE_ID)
    with mock.patch.object(guard, "PROTECTED_MEMBER_IDS", members):
        with pytest.raises(RuntimeError, match="active Penelo removal filter cave"):
            guard.PeneloRemovalFilter(process, old_workspace)

    assert process.memory == original_memory
    assert process.patch_calls == []


def test_removal_filter_adopts_exact_live_layout_without_any_write():
    class FakeProcess:
        def __init__(self):
            self.memory = {}
            self.writes = []

        def write(self, address, data):
            payload = bytes(data)
            self.writes.append((address, payload))
            for index, value in enumerate(payload):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def patch_code(self, address, data):
            self.write(address, data)

    process = FakeProcess()
    workspace = 0x30000000
    state = workspace + guard.REMOVE_FILTER_STATE_OFFSET
    cave = workspace + guard.REMOVE_FILTER_CAVE_OFFSET
    code = guard.build_penelo_remove_filter(cave, state)
    patch = b"\xe9" + guard.coop.rel32(guard.REMOVE_PARTY_MEMBER, 5, cave)
    patch += b"\x90" * (len(guard.REMOVE_PARTY_MEMBER_ORIGINAL) - len(patch))
    process.write(cave, code)
    process.write(state, b"\x01")
    process.write(guard.REMOVE_PARTY_MEMBER, patch)
    process.writes.clear()

    removal_filter = guard.PeneloRemovalFilter(process, workspace)

    assert process.writes == []
    assert removal_filter.installed
    assert not removal_filter.owns_patch
    assert removal_filter.cave == cave
    assert removal_filter.state == state


def test_removal_filter_rejects_exact_code_outside_reserved_workspace_read_only():
    class FakeProcess:
        def __init__(self):
            self.memory = {}
            self.writes = []

        def put(self, address, data):
            for index, value in enumerate(bytes(data)):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def write(self, address, data):
            self.writes.append((address, bytes(data)))
            self.put(address, data)

        def patch_code(self, address, data):
            self.write(address, data)

    process = FakeProcess()
    resident_workspace = 0x30000000
    state = resident_workspace + guard.REMOVE_FILTER_STATE_OFFSET
    cave = resident_workspace + guard.REMOVE_FILTER_CAVE_OFFSET
    code = guard.build_penelo_remove_filter(cave, state)
    patch = b"\xe9" + guard.coop.rel32(guard.REMOVE_PARTY_MEMBER, 5, cave)
    patch += b"\x90" * (len(guard.REMOVE_PARTY_MEMBER_ORIGINAL) - len(patch))
    process.put(cave, code)
    process.put(state, b"\x01")
    process.put(guard.REMOVE_PARTY_MEMBER, patch)
    before = dict(process.memory)

    with pytest.raises(RuntimeError, match="reserved workspace"):
        guard.PeneloRemovalFilter(process, 0x31000000)

    assert process.memory == before
    assert process.writes == []


def test_removal_filter_owner_cleanup_disarms_without_unpatching():
    class FakeProcess:
        def __init__(self):
            self.memory = {}
            self.writes = []
            self.patch_calls = []

        def put(self, address, data):
            for index, value in enumerate(bytes(data)):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def write(self, address, data):
            payload = bytes(data)
            self.writes.append((address, payload))
            self.put(address, payload)

        def patch_code(self, address, data):
            payload = bytes(data)
            self.patch_calls.append((address, payload))
            self.put(address, payload)

    process = FakeProcess()
    process.put(guard.REMOVE_PARTY_MEMBER, guard.REMOVE_PARTY_MEMBER_ORIGINAL)
    workspace = 0x30000000
    removal_filter = guard.PeneloRemovalFilter(process, workspace)
    root = process.read(
        guard.REMOVE_PARTY_MEMBER,
        len(guard.REMOVE_PARTY_MEMBER_ORIGINAL),
    )
    process.writes.clear()
    process.patch_calls.clear()

    removal_filter.restore()

    assert process.read(guard.REMOVE_PARTY_MEMBER, len(root)) == root
    assert process.patch_calls == []
    assert process.writes == [(workspace + guard.REMOVE_FILTER_STATE_OFFSET, b"\x00")]


def test_removal_filter_stock_root_with_old_cave_aborts_read_only():
    class FakeProcess:
        def __init__(self):
            self.memory = {}
            self.writes = []
            self.patch_calls = []

        def put(self, address, data):
            for index, value in enumerate(bytes(data)):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def write(self, address, data):
            self.writes.append((address, bytes(data)))
            self.put(address, data)

        def patch_code(self, address, data):
            self.patch_calls.append((address, bytes(data)))
            self.put(address, data)

    process = FakeProcess()
    process.put(guard.REMOVE_PARTY_MEMBER, guard.REMOVE_PARTY_MEMBER_ORIGINAL)
    workspace = 0x30000000
    process.put(workspace + guard.REMOVE_FILTER_CAVE_OFFSET, b"old code")
    memory_before = dict(process.memory)

    with pytest.raises(RuntimeError, match="non-empty Penelo removal filter cave"):
        guard.PeneloRemovalFilter(process, workspace)

    assert process.memory == memory_before
    assert process.writes == []
    assert process.patch_calls == []


@pytest.mark.parametrize("operation", ("cave", "root"))
@pytest.mark.parametrize("timing", ("before", "after"))
def test_removal_filter_interrupted_transaction_is_safe_and_recoverable(
    operation, timing
):
    class FailingProcess:
        def __init__(self):
            self.memory = {}
            self.events = []
            self.failure = (operation, timing)

        def put(self, address, data):
            for index, value in enumerate(bytes(data)):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def mutate(self, kind, address, data):
            payload = bytes(data)
            self.events.append((kind, address, payload))
            selected = (
                kind == "write"
                and operation == "cave"
                and address == workspace + guard.REMOVE_FILTER_CAVE_OFFSET
            ) or (
                kind == "patch"
                and operation == "root"
                and address == guard.REMOVE_PARTY_MEMBER
            )
            if selected and self.failure == (operation, "before"):
                raise OSError("injected pre-mutation failure")
            self.put(address, payload)
            if selected and self.failure == (operation, "after"):
                raise OSError("injected post-mutation failure")

        def write(self, address, data):
            self.mutate("write", address, data)

        def patch_code(self, address, data):
            self.mutate("patch", address, data)

    workspace = 0x30000000
    process = FailingProcess()
    process.put(guard.REMOVE_PARTY_MEMBER, guard.REMOVE_PARTY_MEMBER_ORIGINAL)

    with pytest.raises(OSError, match="injected"):
        guard.PeneloRemovalFilter(process, workspace)

    cave = workspace + guard.REMOVE_FILTER_CAVE_OFFSET
    patch = b"\xe9" + guard.coop.rel32(guard.REMOVE_PARTY_MEMBER, 5, cave)
    patch += b"\x90" * (len(guard.REMOVE_PARTY_MEMBER_ORIGINAL) - len(patch))
    root = process.read(
        guard.REMOVE_PARTY_MEMBER,
        len(guard.REMOVE_PARTY_MEMBER_ORIGINAL),
    )
    assert root in (guard.REMOVE_PARTY_MEMBER_ORIGINAL, patch)

    process.failure = None
    process.events.clear()
    recovered = guard.PeneloRemovalFilter(process, workspace)
    assert recovered.installed
    assert process.read(guard.REMOVE_PARTY_MEMBER, len(patch)) == patch
    if root == patch:
        assert process.events == []


def test_removal_filter_root_change_before_commit_is_never_overwritten():
    class RacingProcess:
        def __init__(self):
            self.memory = {}
            self.root_reads = 0
            self.patch_calls = []

        def put(self, address, data):
            for index, value in enumerate(bytes(data)):
                self.memory[address + index] = value

        def read(self, address, size):
            if address == guard.REMOVE_PARTY_MEMBER:
                self.root_reads += 1
                if self.root_reads == 2:
                    self.put(address, b"\xcc" * len(guard.REMOVE_PARTY_MEMBER_ORIGINAL))
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def write(self, address, data):
            self.put(address, data)

        def patch_code(self, address, data):
            self.patch_calls.append((address, bytes(data)))
            self.put(address, data)

    process = RacingProcess()
    process.put(guard.REMOVE_PARTY_MEMBER, guard.REMOVE_PARTY_MEMBER_ORIGINAL)

    with pytest.raises(RuntimeError, match="root changed before commit"):
        guard.PeneloRemovalFilter(process, 0x30000000)

    assert process.patch_calls == []
    assert process.read(
        guard.REMOVE_PARTY_MEMBER,
        len(guard.REMOVE_PARTY_MEMBER_ORIGINAL),
    ) == b"\xcc" * len(guard.REMOVE_PARTY_MEMBER_ORIGINAL)


def test_removal_filter_rejects_foreign_state_tail_before_publishing_root():
    class FakeProcess:
        def __init__(self):
            self.memory = {}
            self.writes = []

        def put(self, address, data):
            for index, value in enumerate(bytes(data)):
                self.memory[address + index] = value

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def write(self, address, data):
            self.writes.append((address, bytes(data)))
            self.put(address, data)

        patch_code = write

    process = FakeProcess()
    workspace = 0x30000000
    process.put(guard.REMOVE_PARTY_MEMBER, guard.REMOVE_PARTY_MEMBER_ORIGINAL)
    process.put(workspace + guard.REMOVE_FILTER_STATE_OFFSET + 0xFF, b"\x7f")
    before = dict(process.memory)

    with pytest.raises(RuntimeError, match="non-empty.*state"):
        guard.PeneloRemovalFilter(process, workspace)

    assert process.memory == before
    assert process.writes == []


def test_fourth_active_storage_slot_is_never_repacked_as_overflow():
    result = normalize_penelo((0, 1, 2, 3, 4, EMPTY, EMPTY, EMPTY, EMPTY))
    assert result[:4] == (0, 1, 2, 3)
    assert result[4:] == (4, PENELO_ID, EMPTY, EMPTY, EMPTY)


def test_existing_penelo_is_idempotent():
    original = (0, 5, EMPTY, EMPTY, 4, 3, 2, EMPTY, EMPTY)
    assert normalize_penelo(original) == original


def test_health_requires_penelo_in_four_live_runtime_layouts():
    flags = tuple(0x1C for _member in guard.PROTECTED_MEMBER_IDS)
    active = tuple(dict.fromkeys((VAAN_ID, *guard.PROTECTED_MEMBER_IDS)))[:3]

    def layout(active_members, reserves=()):
        values = list(active_members) + [EMPTY] * (3 - len(active_members))
        values.append(EMPTY)
        values.extend(reserves)
        values.extend([EMPTY] * (9 - len(values)))
        return tuple(values)

    protected_reserves = tuple(
        member_id for member_id in guard.PROTECTED_MEMBER_IDS if member_id not in active
    )
    default_good = layout(active, protected_reserves)
    materialized_good = layout(active)
    unprotected = [
        member_id
        for member_id in range(6)
        if member_id not in guard.PROTECTED_MEMBER_IDS
    ][:3]
    active_without_protected = layout(unprotected)
    reserve = layout(unprotected, guard.PROTECTED_MEMBER_IDS)
    bad = (0, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
    good = (
        default_good,
        materialized_good,
        materialized_good,
        default_good,
        bad,
    )
    assert PeneloStoryGuard.healthy((flags, good))
    # The injected early Balthier actor can be fully materialized before the
    # stock 0x10 roster bit exists. Coherent layouts are the authority here.
    partial_flags = tuple(0x04 for _member in guard.PROTECTED_MEMBER_IDS)
    assert PeneloStoryGuard.healthy((partial_flags, good))
    # Native Current/UI materialize active units only and omit reserves.
    assert PeneloStoryGuard.healthy(
        (
            flags,
            (
                reserve,
                active_without_protected,
                active_without_protected,
                reserve,
                bad,
            ),
        )
    )
    assert not PeneloStoryGuard.healthy(
        (
            flags,
            (default_good, bad, materialized_good, default_good, bad),
        )
    )
    # Native layout 4 is a last/backup generation and cannot alone trigger a
    # full rebuild feedback loop.
    assert PeneloStoryGuard.healthy((flags, good))
    duplicated_guest = list(reserve)
    duplicated_guest[3] = 11
    duplicated_guest[-1] = 11
    duplicated_guest = tuple(duplicated_guest)
    assert not PeneloStoryGuard.healthy(
        (
            flags,
            (
                duplicated_guest,
                active_without_protected,
                active_without_protected,
                duplicated_guest,
                bad,
            ),
        )
    )


def test_town_leader_only_current_ui_is_not_repaired_as_missing_party():
    class FakeProcess:
        def __init__(self):
            self.memory = {}

        def put(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def put_u32(self, address, value):
            self.put(address, struct.pack("<I", value))

        def put_u64(self, address, value):
            self.put(address, struct.pack("<Q", value))

        def read(self, address, size):
            return bytes(self.memory[address + index] for index in range(size))

        def u32(self, address):
            return struct.unpack("<I", self.read(address, 4))[0]

        def u64(self, address):
            return struct.unpack("<Q", self.read(address, 8))[0]

    base = 0x500000
    location = 0x700000
    process = FakeProcess()
    process.put_u64(guard.LOCATION_SAVE_GLOBAL, location)
    process.put_u32(base + guard.PARTY_LEADER_ID_OFFSET, VAAN_ID)
    process.put_u32(location + guard.LOCATION_CURRENT_OFFSET, 227)
    process.put_u32(location + guard.LOCATION_UI_OFFSET, 227)
    process.put(
        location + guard.LOCATION_FLAGS_OFFSET,
        bytes((guard.LOCATION_TOWN_FLAG,)),
    )

    complete = [VAAN_ID, *guard.PROTECTED_MEMBER_IDS]
    complete.insert(3, EMPTY)  # preserve the native guest/story slot
    complete.extend([EMPTY] * (9 - len(complete)))
    complete = tuple(complete)
    leader_only = (VAAN_ID, *([EMPTY] * 8))
    layouts = (complete, leader_only, leader_only, complete, leader_only)

    instance = PeneloStoryGuard.__new__(PeneloStoryGuard)
    instance.process = process
    instance.repair_window_open = lambda _base, _layouts: True
    assert instance.town_materialization_is_validly_collapsed(base, layouts)

    damaged_menu = list(complete)
    damaged_menu[damaged_menu.index(guard.PROTECTED_MEMBER_IDS[-1])] = EMPTY
    damaged = list(layouts)
    damaged[guard.MENU_LAYOUT] = tuple(damaged_menu)
    assert not instance.town_materialization_is_validly_collapsed(
        base,
        tuple(damaged),
    )

    damaged_default = list(complete)
    damaged_default[damaged_default.index(guard.PROTECTED_MEMBER_IDS[-1])] = EMPTY
    damaged[guard.DEFAULT_LAYOUT] = tuple(damaged_default)
    assert not instance.town_materialization_is_validly_collapsed(
        base,
        tuple(damaged),
    )
    assert instance.town_materialization_is_validly_collapsed(
        base,
        tuple(damaged),
        require_complete_roster=False,
    )


def test_town_vaan_story_slot_is_valid_when_stored_leader_is_another_member():
    """Cities may force Vaan while retaining the field battle-leader ID."""

    class FakeProcess:
        def __init__(self):
            self.memory = {}

        def put(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def put_u32(self, address, value):
            self.put(address, struct.pack("<I", value))

        def put_u64(self, address, value):
            self.put(address, struct.pack("<Q", value))

        def read(self, address, size):
            return bytes(self.memory[address + index] for index in range(size))

        def u32(self, address):
            return struct.unpack("<I", self.read(address, 4))[0]

        def u64(self, address):
            return struct.unpack("<Q", self.read(address, 8))[0]

    base = 0x510000
    location = 0x710000
    process = FakeProcess()
    process.put_u64(guard.LOCATION_SAVE_GLOBAL, location)
    process.put_u32(location + guard.LOCATION_CURRENT_OFFSET, 227)
    process.put_u32(location + guard.LOCATION_UI_OFFSET, 227)
    process.put(
        location + guard.LOCATION_FLAGS_OFFSET,
        bytes((guard.LOCATION_TOWN_FLAG,)),
    )

    leader = guard.PROTECTED_MEMBER_IDS[0]
    companion = guard.PROTECTED_MEMBER_IDS[1]
    reserve = [
        VAAN_ID,
        *(
            member
            for member in guard.PROTECTED_MEMBER_IDS
            if member not in (leader, companion)
        ),
    ]
    complete = [leader, companion, EMPTY, EMPTY, *reserve]
    complete.extend([EMPTY] * (9 - len(complete)))
    complete = tuple(complete[:9])
    vaan_story_slot = (EMPTY, EMPTY, EMPTY, VAAN_ID, *([EMPTY] * 5))
    layouts = (
        complete,
        vaan_story_slot,
        vaan_story_slot,
        complete,
        vaan_story_slot,
    )
    process.put_u32(base + guard.PARTY_LEADER_ID_OFFSET, leader)

    instance = PeneloStoryGuard.__new__(PeneloStoryGuard)
    instance.process = process
    assert instance.town_materialization_is_validly_collapsed(base, layouts)

    # After the city takes ownership of Vaan, the game updates the leader before
    # changing the stored field formation. That combination is also valid.
    process.put_u32(base + guard.PARTY_LEADER_ID_OFFSET, VAAN_ID)
    assert instance.town_materialization_is_validly_collapsed(base, layouts)

    # A different reserve member cannot borrow this special-case acceptance.
    wrong_member = guard.PROTECTED_MEMBER_IDS[-1]
    wrong_story_slot = (EMPTY, EMPTY, EMPTY, wrong_member, *([EMPTY] * 5))
    wrong_layouts = (
        complete,
        wrong_story_slot,
        wrong_story_slot,
        complete,
        wrong_story_slot,
    )
    assert not instance.town_materialization_is_validly_collapsed(
        base,
        wrong_layouts,
    )


def test_vaan_story_slot_remains_valid_before_town_metadata_is_published():
    """Fran/Balthier's exit briefly omits the town bit for this exact state."""

    class FakeProcess:
        def __init__(self):
            self.memory = {}

        def put(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def put_u32(self, address, value):
            self.put(address, struct.pack("<I", value))

        def put_u64(self, address, value):
            self.put(address, struct.pack("<Q", value))

        def read(self, address, size):
            return bytes(self.memory.get(address + index, 0) for index in range(size))

        def u32(self, address):
            return struct.unpack("<I", self.read(address, 4))[0]

        def u64(self, address):
            return struct.unpack("<Q", self.read(address, 8))[0]

    base = 0x520000
    control = 0x620000
    location = 0x720000
    process = FakeProcess()
    process.put_u64(guard.USER_CONTROL_GLOBAL, control)
    process.put(control + guard.USER_CONTROL_ENABLED_OFFSET, b"\x01")
    process.put_u64(guard.LOCATION_SAVE_GLOBAL, location)
    process.put_u32(location + guard.LOCATION_CURRENT_OFFSET, 227)
    process.put_u32(location + guard.LOCATION_UI_OFFSET, 227)
    process.put(location + guard.LOCATION_FLAGS_OFFSET, b"\x00")

    leader = guard.PROTECTED_MEMBER_IDS[0]
    process.put_u32(base + guard.PARTY_LEADER_ID_OFFSET, leader)
    reserve = [
        VAAN_ID,
        *(member for member in guard.PROTECTED_MEMBER_IDS if member != leader),
    ]
    complete = [leader, EMPTY, EMPTY, EMPTY, *reserve]
    complete.extend([EMPTY] * (9 - len(complete)))
    complete = tuple(complete[:9])
    vaan_story_slot = (EMPTY, EMPTY, EMPTY, VAAN_ID, *([EMPTY] * 5))
    layouts = (
        complete,
        vaan_story_slot,
        vaan_story_slot,
        complete,
        vaan_story_slot,
    )

    instance = PeneloStoryGuard.__new__(PeneloStoryGuard)
    instance.process = process
    instance.repair_window_open = lambda _base, _layouts: True
    assert instance.town_materialization_is_validly_collapsed(base, layouts)

    # Only Vaan in the authored fourth story slot receives this fallback.
    wrong_slot = (VAAN_ID, *([EMPTY] * 8))
    wrong_layouts = (
        complete,
        wrong_slot,
        wrong_slot,
        complete,
        wrong_slot,
    )
    assert not instance.town_materialization_is_validly_collapsed(
        base,
        wrong_layouts,
    )


def test_repair_window_requires_control_and_coherent_location():
    class FakeProcess:
        def __init__(self):
            self.memory = {}

        def put(self, address, data):
            for index, value in enumerate(data):
                self.memory[address + index] = value

        def put_u32(self, address, value):
            self.put(address, struct.pack("<I", value))

        def put_u64(self, address, value):
            self.put(address, struct.pack("<Q", value))

        def read(self, address, size):
            return bytes(self.memory[address + i] for i in range(size))

        def u32(self, address):
            return struct.unpack("<I", self.read(address, 4))[0]

        def u64(self, address):
            return struct.unpack("<Q", self.read(address, 8))[0]

    import penelo_story_guard as guard_module

    process = FakeProcess()
    base = 0x500000
    control = 0x600000
    location = 0x700000
    process.put_u64(guard_module.USER_CONTROL_GLOBAL, control)
    process.put_u64(guard_module.LOCATION_SAVE_GLOBAL, location)
    process.put_u32(guard_module.NATIVE_MENU_STATE, 0)
    process.put(control + guard_module.USER_CONTROL_ENABLED_OFFSET, b"\x01")
    process.put_u32(location + guard_module.LOCATION_CURRENT_OFFSET, 227)
    process.put_u32(location + guard_module.LOCATION_UI_OFFSET, 227)
    layout = struct.pack("<9H", 0, *([EMPTY] * 8))
    for layout_id in range(5):
        process.put(
            base
            + guard_module.PARTY_LAYOUT_OFFSET
            + layout_id * guard_module.PARTY_LAYOUT_STRIDE,
            layout,
        )

    guard = PeneloStoryGuard.__new__(PeneloStoryGuard)
    guard.process = process
    guard.controlled_actor_valid = lambda: True
    assert guard.repair_window_open(base)

    # A repair decision must use the exact signature captured by tick.  Town
    # transitions can materialize Current/UI between that capture and this
    # check; mixing the two generations caused a periodic partyAllRead and a
    # one-frame idle reset on the running leader.
    stale = [(VAAN_ID, *([EMPTY] * 8)) for _layout_id in range(5)]
    stale[guard_module.CURRENT_LAYOUT] = (EMPTY,) * 9
    stale[guard_module.UI_LAYOUT] = (EMPTY,) * 9
    stale = tuple(stale)
    assert not guard.repair_window_open(base, stale)

    # Once armed, a stable non-Vaan trio remains a valid repair generation.
    non_vaan = struct.pack("<9H", 4, 1, 2, *([EMPTY] * 6))
    for layout_id in (
        guard_module.DEFAULT_LAYOUT,
        guard_module.CURRENT_LAYOUT,
        guard_module.UI_LAYOUT,
    ):
        process.put(
            base
            + guard_module.PARTY_LAYOUT_OFFSET
            + layout_id * guard_module.PARTY_LAYOUT_STRIDE,
            non_vaan,
        )
    assert guard.repair_window_open(base)

    # A torn Current/UI generation must not trigger a repair.
    process.put(
        base
        + guard_module.PARTY_LAYOUT_OFFSET
        + guard_module.CURRENT_LAYOUT * guard_module.PARTY_LAYOUT_STRIDE,
        struct.pack("<9H", 4, 1, *([EMPTY] * 7)),
    )
    assert not guard.repair_window_open(base)
    process.put(
        base
        + guard_module.PARTY_LAYOUT_OFFSET
        + guard_module.CURRENT_LAYOUT * guard_module.PARTY_LAYOUT_STRIDE,
        non_vaan,
    )
    process.put_u32(location + guard_module.LOCATION_UI_OFFSET, 228)
    assert not guard.repair_window_open(base)
    process.put_u32(location + guard_module.LOCATION_UI_OFFSET, 227)
    process.put(control + guard_module.USER_CONTROL_ENABLED_OFFSET, b"\x00")
    assert not guard.repair_window_open(base)


def test_repair_writes_only_complete_rosters_then_uses_native_refresh():
    base = 0x500000
    source = (
        VAAN_ID,
        ASHE_ID,
        2,
        11,
        4,
        EMPTY,
        EMPTY,
        EMPTY,
        EMPTY,
    )
    runtime = (VAAN_ID, ASHE_ID, 2, 11, *([EMPTY] * 5))
    layouts = [source, runtime, runtime, source, runtime]

    class FakeProcess:
        def __init__(self):
            self.layout_writes = []

        def write(self, address, data):
            layout_id = (
                address - base - guard.PARTY_LAYOUT_OFFSET
            ) // guard.PARTY_LAYOUT_STRIDE
            layouts[layout_id] = guard.unpack_layout(data)
            self.layout_writes.append(layout_id)

    class FakeCaller:
        def __init__(self):
            self.calls = []

        def call(self, function, *arguments):
            self.calls.append((function, arguments))
            return 0

    instance = PeneloStoryGuard.__new__(PeneloStoryGuard)
    instance.process = FakeProcess()
    instance.caller = FakeCaller()
    instance.layout = lambda _base, layout_id: layouts[layout_id]
    instance.town_materialization_is_validly_collapsed = lambda *_args, **_kwargs: False
    instance.accepted_active_members = None
    instance.bad_signature = object()
    instance.bad_since = 1.0
    instance.cooldown_until = 0.0
    protected_members = (PENELO_ID, BALTHIER_ID)
    flags = tuple(0x1C for _member in protected_members)

    with mock.patch.object(guard, "PROTECTED_MEMBER_IDS", protected_members):
        instance.repair(base, (flags, tuple(layouts)))

    assert set(instance.process.layout_writes) == {
        guard.DEFAULT_LAYOUT,
        guard.MENU_LAYOUT,
    }
    assert layouts[guard.CURRENT_LAYOUT] == runtime
    assert layouts[guard.UI_LAYOUT] == runtime
    assert layouts[guard.DEFAULT_LAYOUT][:4] == source[:4]
    assert layouts[guard.MENU_LAYOUT][:4] == source[:4]
    assert layouts[guard.DEFAULT_LAYOUT].count(ASHE_ID) == 1
    assert layouts[guard.DEFAULT_LAYOUT][3] == 11
    for member_id in protected_members:
        assert layouts[guard.DEFAULT_LAYOUT].count(member_id) == 1
    assert instance.caller.calls == [(guard.PARTY_ALL_READ, ())]


def test_town_roster_repair_does_not_rebuild_the_visible_actor():
    base = 0x500000
    source = (
        VAAN_ID,
        EMPTY,
        EMPTY,
        EMPTY,
        PENELO_ID,
        4,
        ASHE_ID,
        EMPTY,
        EMPTY,
    )
    vaan_only = (EMPTY, EMPTY, EMPTY, VAAN_ID, *([EMPTY] * 5))
    layouts = [source, vaan_only, vaan_only, source, vaan_only]

    class FakeProcess:
        def __init__(self):
            self.flag_writes = []

        def write(self, address, data):
            if address < base + guard.PARTY_LAYOUT_OFFSET:
                member_id = (
                    address - base - guard.PARTY_MEMBER_OFFSET
                ) // guard.PARTY_MEMBER_STRIDE
                self.flag_writes.append((member_id, data[0]))
                return
            layout_id = (
                address - base - guard.PARTY_LAYOUT_OFFSET
            ) // guard.PARTY_LAYOUT_STRIDE
            layouts[layout_id] = guard.unpack_layout(data)

    class FakeCaller:
        def __init__(self):
            self.calls = []

        def call(self, function, *arguments):
            self.calls.append((function, arguments))
            return 0

    instance = PeneloStoryGuard.__new__(PeneloStoryGuard)
    instance.process = FakeProcess()
    instance.caller = FakeCaller()
    instance.layout = lambda _base, layout_id: layouts[layout_id]
    instance.battle_info = lambda: base
    instance.town_materialization_is_validly_collapsed = lambda *_args, **_kwargs: True
    instance.accepted_active_members = None
    instance.bad_signature = object()
    instance.bad_since = 1.0
    instance.cooldown_until = 0.0
    protected_members = (BALTHIER_ID, 2, 4, ASHE_ID, PENELO_ID)
    flags = (0x0C, 0x0C, 0x1C, 0x18, 0x1C)

    with (
        mock.patch.object(guard, "PROTECTED_MEMBER_IDS", protected_members),
        mock.patch.object(guard.time, "sleep"),
    ):
        instance.repair(base, (flags, tuple(layouts)))

    assert instance.caller.calls == []
    assert instance.process.flag_writes == [
        (BALTHIER_ID, 0x1C),
        (2, 0x1C),
    ]
    assert all(
        function != guard.PARTY_ALL_READ for function, _args in instance.caller.calls
    )
    assert layouts[guard.CURRENT_LAYOUT] == vaan_only
    assert layouts[guard.UI_LAYOUT] == vaan_only
    for member_id in protected_members:
        assert layouts[guard.DEFAULT_LAYOUT].count(member_id) == 1
        assert layouts[guard.MENU_LAYOUT].count(member_id) == 1


class TownRosterRefreshRegressionTests(unittest.TestCase):
    def test_incomplete_town_roster_is_a_collapsed_actor_generation(self):
        test_town_leader_only_current_ui_is_not_repaired_as_missing_party()

    def test_town_repair_skips_visible_actor_rebuild(self):
        test_town_roster_repair_does_not_rebuild_the_visible_actor()

    def test_vaan_handoff_keeps_the_stored_coop_formation(self):
        test_town_vaan_story_slot_is_valid_when_stored_leader_is_another_member()
