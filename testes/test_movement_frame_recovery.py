import ast
import inspect
import struct
import textwrap
import unittest
from unittest import mock

import coop_runtime as coop


class MemoryProcess:
    def __init__(self, *, fail_write=None):
        self.handle = 0x1234
        self.fail_write = fail_write
        self.memory = {}
        self.writes = []
        self.patch_calls = []

    def read(self, address, size):
        return bytes(self.memory.get(int(address) + index, 0) for index in range(size))

    def write(self, address, payload):
        address = int(address)
        payload = bytes(payload)
        if address == self.fail_write:
            raise OSError(299, f"injected write failure at {address:#x}")
        self.writes.append((address, payload))
        for index, value in enumerate(payload):
            self.memory[address + index] = value

    def patch_code(self, address, payload):
        self.patch_calls.append((int(address), bytes(payload)))
        raise AssertionError("frame recovery must never patch executable code")


class MovementFrameRecoveryTests(unittest.TestCase):
    def test_process_liveness_uses_the_existing_read_only_handle(self):
        process = MemoryProcess()

        def exit_code(value):
            def query(handle, output):
                self.assertEqual(handle, process.handle)
                output._obj.value = value
                return True

            return query

        with mock.patch.object(
            coop.kernel32,
            "GetExitCodeProcess",
            side_effect=exit_code(coop.STILL_ACTIVE),
        ):
            self.assertTrue(coop.movement_runtime_process_alive(process))
        with mock.patch.object(
            coop.kernel32,
            "GetExitCodeProcess",
            side_effect=exit_code(0),
        ):
            self.assertFalse(coop.movement_runtime_process_alive(process))

    def test_failed_lifetime_query_does_not_turn_one_frame_fault_into_exit(self):
        process = MemoryProcess()
        with mock.patch.object(
            coop.kernel32,
            "GetExitCodeProcess",
            return_value=False,
        ):
            self.assertTrue(coop.movement_runtime_process_alive(process))

    def test_transient_fault_quarantines_and_retries_with_capped_backoff(self):
        process = MemoryProcess()
        sleeps = []
        times = iter((10.0, 10.1, 10.2))
        recovery = coop.MovementFrameIoRecovery(
            clock=lambda: next(times),
            sleeper=sleeps.append,
        )

        with (
            mock.patch.object(coop.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(
                coop, "movement_runtime_process_alive", return_value=True
            ),
            mock.patch.object(
                coop, "validate_movement_frame_recovery_topology"
            ) as validate,
            mock.patch.object(
                coop,
                "quarantine_movement_frame_publications",
                return_value=(),
            ) as quarantine,
            mock.patch("builtins.print"),
        ):
            self.assertTrue(recovery.recover(process, OSError(299, "loading")))
            self.assertTrue(recovery.recover(process, struct.error("party rebuild")))
            recovery.recovered()

        self.assertEqual(sleeps, [coop.POLL_SECONDS, coop.POLL_SECONDS * 2])
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(quarantine.call_count, 2)
        self.assertEqual(recovery.attempts, 0)
        self.assertIsNone(recovery.started_at)

    def test_dead_process_stops_retry_without_touching_publications(self):
        process = MemoryProcess()
        sleeps = []
        recovery = coop.MovementFrameIoRecovery(sleeper=sleeps.append)

        with (
            mock.patch.object(coop.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(
                coop, "movement_runtime_process_alive", return_value=False
            ),
            mock.patch.object(
                coop, "validate_movement_frame_recovery_topology"
            ) as validate,
            mock.patch.object(
                coop, "quarantine_movement_frame_publications"
            ) as quarantine,
        ):
            self.assertFalse(recovery.recover(process, OSError(6, "game exited")))

        validate.assert_not_called()
        quarantine.assert_not_called()
        self.assertEqual(sleeps, [])

    def test_structural_topology_error_is_not_converted_into_a_retry(self):
        process = MemoryProcess()
        recovery = coop.MovementFrameIoRecovery(sleeper=lambda _delay: None)

        with (
            mock.patch.object(coop.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(
                coop, "movement_runtime_process_alive", return_value=True
            ),
            mock.patch.object(
                coop,
                "validate_movement_frame_recovery_topology",
                side_effect=RuntimeError("foreign root"),
            ),
            mock.patch.object(
                coop, "quarantine_movement_frame_publications"
            ) as quarantine,
        ):
            with self.assertRaisesRegex(RuntimeError, "foreign root"):
                recovery.recover(process, OSError(299, "actor disappeared"))

        quarantine.assert_not_called()

    def test_unreadable_topology_is_retryable_and_defers_all_writes(self):
        process = MemoryProcess()
        sleeps = []
        recovery = coop.MovementFrameIoRecovery(
            clock=lambda: 20.0,
            sleeper=sleeps.append,
        )

        with (
            mock.patch.object(coop.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(
                coop, "movement_runtime_process_alive", return_value=True
            ),
            mock.patch.object(
                coop,
                "validate_movement_frame_recovery_topology",
                side_effect=OSError(299, "partial copy during loading"),
            ),
            mock.patch.object(
                coop, "quarantine_movement_frame_publications"
            ) as quarantine,
            mock.patch("builtins.print") as report,
        ):
            self.assertTrue(recovery.recover(process, OSError(299, "actor moved")))

        quarantine.assert_not_called()
        self.assertEqual(process.writes, [])
        self.assertEqual(sleeps, [coop.POLL_SECONDS])
        self.assertIn("topology=unavailable:OSError", report.call_args.args[0])

    def test_struct_decode_failure_in_topology_probe_is_also_retryable(self):
        process = MemoryProcess()
        recovery = coop.MovementFrameIoRecovery(sleeper=lambda _delay: None)

        with (
            mock.patch.object(coop.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(
                coop, "movement_runtime_process_alive", return_value=True
            ),
            mock.patch.object(
                coop,
                "validate_movement_frame_recovery_topology",
                side_effect=struct.error("torn topology sample"),
            ),
            mock.patch.object(
                coop, "quarantine_movement_frame_publications"
            ) as quarantine,
            mock.patch("builtins.print"),
        ):
            self.assertTrue(
                recovery.recover(process, struct.error("torn actor sample"))
            )

        quarantine.assert_not_called()

    def test_quarantine_isolates_write_failures_and_never_patches_code(self):
        failed_address = coop.COMMAND_BLOCK + coop.CAMERA_ACTIVE_OFFSET
        process = MemoryProcess(fail_write=failed_address)

        failures = coop.quarantine_movement_frame_publications(process)

        self.assertIn("camera-active", failures)
        self.assertIn(
            (coop.COMMAND_BLOCK + coop.MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 0)),
            process.writes,
        )
        self.assertIn(
            (
                coop.COMMAND_BLOCK + coop.INTERACTION_ROUTING_ACTIVE_OFFSET,
                struct.pack("<I", 0),
            ),
            process.writes,
        )
        self.assertTrue(
            any(address == coop.P2_MOVEMENT_MAILBOX for address, _ in process.writes)
        )
        self.assertTrue(
            any(address == coop.P3_MOVEMENT_MAILBOX for address, _ in process.writes)
        )
        self.assertEqual(process.patch_calls, [])

    def test_main_frame_barrier_continues_only_after_recovery(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(coop.main)))
        loop = next(node for node in ast.walk(tree) if isinstance(node, ast.While))
        self.assertIsInstance(loop.body[0], ast.Try)
        frame_try = loop.body[0]
        handler = next(
            item
            for item in frame_try.handlers
            if isinstance(item.type, ast.Tuple)
            and {ast.unparse(value) for value in item.type.elts}
            == {"OSError", "struct.error"}
        )
        handler_source = ast.unparse(ast.Module(body=handler.body, type_ignores=[]))
        self.assertIn("if not frame_io_recovery.recover(process, error):\n    break", handler_source)
        self.assertIn("world_epochs.observe(None, valid=False)", handler_source)
        self.assertIn("actor_by_handle.clear()", handler_source)
        self.assertIn("continue", handler_source)
        self.assertTrue(
            any(
                isinstance(node, ast.Expr)
                and ast.unparse(node.value) == "frame_io_recovery.recovered()"
                for node in frame_try.orelse
            )
        )

    def test_shutdown_isolates_every_cleanup_and_closes_owned_handles(self):
        process = mock.Mock()
        writes = []
        failed_address = coop.COMMAND_BLOCK + coop.MOVEMENT_ACTIVE_OFFSET

        def write(address, payload):
            writes.append((int(address), bytes(payload)))
            if int(address) == failed_address:
                raise OSError(299, "injected movement-master teardown")

        process.write.side_effect = write
        ko_ownership = mock.Mock()
        ko_ownership.shutdown.side_effect = RuntimeError("injected KO cleanup")
        area_handoff = mock.Mock()
        bindings = mock.Mock()
        bindings._controller_channel.close.side_effect = OSError(
            "injected channel close"
        )

        with (
            mock.patch.object(
                coop,
                "disarm_second_player_command",
                side_effect=OSError("injected p2 mailbox"),
            ),
            mock.patch.object(coop, "disarm_third_player_command"),
            mock.patch.object(coop, "clear_p1_berserk_command"),
            mock.patch.object(coop, "clear_third_player_command"),
            mock.patch.object(coop, "publish_camera_collision_formation"),
            mock.patch.object(coop, "publish_camera_collision_focus_escape"),
            mock.patch.object(
                coop,
                "native_leader_role_transfer_recovery_required",
                return_value=False,
            ),
            mock.patch("builtins.print"),
        ):
            coop.shutdown_runtime(
                process,
                ko_leader_ownership=ko_ownership,
                area_handoff=area_handoff,
                camera_tuning=mock.Mock(),
                bindings=bindings,
            )

        attempted_addresses = {address for address, _payload in writes}
        self.assertIn(failed_address, attempted_addresses)
        self.assertIn(
            coop.COMMAND_BLOCK + coop.INTERACTION_ROUTING_ACTIVE_OFFSET,
            attempted_addresses,
        )
        self.assertIn(
            coop.third_player_block() + coop.P3_MOVEMENT_ACTIVE_OFFSET,
            attempted_addresses,
        )
        self.assertIn(
            coop.COMMAND_BLOCK + coop.P2_INTERACTION_ENTRY_OFFSET,
            attempted_addresses,
        )
        area_handoff.shutdown.assert_called_once_with(process)
        bindings._controller_channel.close.assert_called_once_with()
        process.close.assert_called_once_with()
        process.patch_code.assert_not_called()

    def test_remote_process_open_failure_releases_movement_lease(self):
        lease = mock.Mock()
        with (
            mock.patch.object(coop, "find_pid", return_value=1234),
            mock.patch.object(coop.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                coop,
                "RemoteProcess",
                side_effect=OSError("OpenProcess failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "OpenProcess"):
                coop.main()

        lease.close.assert_called_once_with()

    def test_service_initialization_failure_closes_process_then_lease(self):
        process = mock.Mock()
        lease = mock.Mock()
        events = []
        def fail_process_close():
            events.append("process")
            raise OSError("injected process close")

        def fail_lease_close():
            events.append("lease")
            raise RuntimeError("injected lease close")

        process.close.side_effect = fail_process_close
        lease.close.side_effect = fail_lease_close
        with (
            mock.patch.object(coop, "find_pid", return_value=1234),
            mock.patch.object(coop.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(coop, "RemoteProcess", return_value=process),
            mock.patch.object(
                coop,
                "_initialize_movement_services",
                side_effect=RuntimeError("service init failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "service init"):
                coop.main()

        self.assertEqual(events, ["process", "lease"])


if __name__ == "__main__":
    unittest.main()
