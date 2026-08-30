import unittest
from unittest import mock
import struct

import start_headless_coop as launcher


BASES = (0x30000000, 0x31000000, 0x32000000)
PUBLICATIONS = {
    launcher.runtime_paths.CAVE_BASE_ENV: BASES[0],
    launcher.runtime_paths.DUAL_CAVE_BASE_ENV: BASES[1],
    launcher.runtime_paths.DAMAGE_CAVE_BASE_ENV: BASES[2],
}


class MarkerProcess:
    def __init__(self, marker_payloads=None):
        if marker_payloads is None:
            marker_payloads = (b"\x00" * launcher.GLOBAL_INSTALL_MARKER_STRUCT.size,) * 3
        self.memory = {
            base + launcher.GLOBAL_INSTALL_MARKER_OFFSET: bytes(payload)
            for base, payload in zip(BASES, marker_payloads, strict=True)
        }
        self.memory.update(
            {
                base + launcher.coop.RUNTIME_INSTALL_MARKER_OFFSET: b"\x00"
                * launcher.coop.RUNTIME_INSTALL_MARKER_STRUCT.size
                for base in BASES
            }
        )
        self.writes = []
        self.patches = []
        self.ensure_calls = []
        self.closed = False

    def read(self, address, size):
        allocation_layout = (
            (BASES[0], launcher.coop.CAVE_ALLOCATION_SIZE, launcher.coop.CAVE_MAGIC),
            (
                BASES[1],
                launcher.coop.DUAL_INTERACTION_ALLOCATION_SIZE,
                launcher.coop.DUAL_INTERACTION_MAGIC,
            ),
            (
                BASES[2],
                launcher.coop.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
                launcher.coop.DAMAGE_NUMBER_THEME_MAGIC,
            ),
        )
        for base, allocation_size, magic in allocation_layout:
            if address == base and size == allocation_size:
                image = bytearray(allocation_size)
                image[: len(magic)] = magic
                for resident_address, resident in self.memory.items():
                    offset = resident_address - base
                    if 0 <= offset and offset + len(resident) <= allocation_size:
                        image[offset : offset + len(resident)] = resident
                return bytes(image)
        payload = self.memory.get(address)
        if payload is None or len(payload) < size:
            raise OSError(f"unexpected read at {address:#x} size={size:#x}")
        return payload[:size]

    def write(self, address, payload):
        payload = bytes(payload)
        self.writes.append((address, payload))
        self.memory[address] = payload

    def patch_code(self, address, payload):
        self.patches.append((address, bytes(payload)))

    def ensure_cave_allocation(self, **_kwargs):
        self.ensure_calls.append("primary")
        raise AssertionError("read-only preflight allocated primary")

    def ensure_dual_interaction_allocation(self, **_kwargs):
        self.ensure_calls.append("dual")
        raise AssertionError("read-only preflight allocated dual")

    def ensure_damage_number_theme_allocation(self, **_kwargs):
        self.ensure_calls.append("damage")
        raise AssertionError("read-only preflight allocated damage")

    def close(self):
        self.closed = True


class GlobalLauncherPreflightTests(unittest.TestCase):
    def assert_no_remote_mutation(self, process):
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        self.assertEqual(process.ensure_calls, [])

    def test_global_marker_starts_after_local_triple_marker(self):
        local_end = (
            launcher.coop.RUNTIME_INSTALL_MARKER_OFFSET
            + launcher.coop.RUNTIME_INSTALL_MARKER_STRUCT.size
        )
        self.assertEqual(launcher.GLOBAL_INSTALL_MARKER_OFFSET, 0x60)
        self.assertGreaterEqual(launcher.GLOBAL_INSTALL_MARKER_OFFSET, local_end)
        self.assertLessEqual(
            launcher.GLOBAL_INSTALL_MARKER_OFFSET
            + launcher.GLOBAL_INSTALL_MARKER_STRUCT.size,
            0x100,
        )

    def test_build_108_atomic_transition_layout_revision_is_published(self):
        self.assertEqual(
            launcher.GLOBAL_INSTALL_LAYOUT_ID.hex(),
            "f27d69812c074b92a74aa1f05867202c",
        )

    def test_no_allocations_classifies_fresh_without_side_effects(self):
        process = MarkerProcess()
        with (
            mock.patch.object(launcher, "_runtime_cave_publications", return_value={}),
            mock.patch.object(launcher, "ensure_runtime_cave_bootstrap") as ensure,
            mock.patch.object(launcher, "inject_render_dll") as inject,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
            mock.patch.object(launcher.coop, "inspect_native_install") as inspect,
        ):
            result = launcher.classify_global_install(
                process,
                upstream_camera_v2=True,
            )

        self.assertEqual(result.mode, "fresh")
        self.assertEqual(result.bases, (0, 0, 0))
        ensure.assert_not_called()
        inject.assert_not_called()
        popen.assert_not_called()
        inspect.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_partial_allocation_set_aborts_before_every_mutation(self):
        process = MarkerProcess()
        partial = {launcher.runtime_paths.CAVE_BASE_ENV: BASES[0]}
        with (
            mock.patch.object(
                launcher,
                "_runtime_cave_publications",
                return_value=partial,
            ),
            mock.patch.object(launcher, "ensure_runtime_cave_bootstrap") as ensure,
            mock.patch.object(launcher, "inject_render_dll") as inject,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(RuntimeError, "partial global"):
                launcher.classify_global_install(
                    process,
                    upstream_camera_v2=True,
                )

        ensure.assert_not_called()
        inject.assert_not_called()
        popen.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_pristine_preallocated_headers_classify_fresh_read_only(self):
        process = MarkerProcess()
        with (
            mock.patch.object(
                launcher,
                "_runtime_cave_publications",
                return_value=PUBLICATIONS,
            ),
            mock.patch.object(launcher.coop, "inspect_native_install") as inspect,
        ):
            result = launcher.classify_global_install(
                process,
                upstream_camera_v2=True,
            )

        self.assertEqual(result.mode, "fresh")
        self.assertEqual(result.bases, BASES)
        inspect.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_zero_global_headers_reject_an_existing_local_install(self):
        process = MarkerProcess()
        process.memory[
            BASES[1] + launcher.coop.RUNTIME_INSTALL_MARKER_OFFSET
        ] = b"L" * launcher.coop.RUNTIME_INSTALL_MARKER_STRUCT.size
        with (
            mock.patch.object(
                launcher,
                "_runtime_cave_publications",
                return_value=PUBLICATIONS,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "marker is absent"):
                launcher.classify_global_install(
                    process,
                    upstream_camera_v2=True,
                )

        self.assert_no_remote_mutation(process)

    def test_zero_markers_reject_old_partial_helper_bytes(self):
        process = MarkerProcess()
        process.memory[BASES[0] + 0xEE00] = b"\x90\x90"
        with mock.patch.object(
            launcher,
            "_runtime_cave_publications",
            return_value=PUBLICATIONS,
        ):
            with self.assertRaisesRegex(RuntimeError, "not pristine"):
                launcher.classify_global_install(
                    process,
                    upstream_camera_v2=True,
                )

        self.assert_no_remote_mutation(process)

    def test_prepared_transaction_aborts_without_repair_or_spawn(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        prepared = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_PREPARED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((prepared, prepared, prepared))
        with (
            mock.patch.object(
                launcher,
                "_runtime_cave_publications",
                return_value=PUBLICATIONS,
            ),
            mock.patch.object(launcher, "inject_render_dll") as inject,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(RuntimeError, "PREPARED"):
                launcher.classify_global_install(
                    process,
                    upstream_camera_v2=True,
                )

        inject.assert_not_called()
        popen.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_mixed_triple_commit_aborts_without_repair(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        empty = b"\x00" * launcher.GLOBAL_INSTALL_MARKER_STRUCT.size
        process = MarkerProcess((committed, committed, empty))
        with mock.patch.object(
            launcher,
            "_runtime_cave_publications",
            return_value=PUBLICATIONS,
        ):
            with self.assertRaisesRegex(RuntimeError, "triple commit is partial"):
                launcher.classify_global_install(
                    process,
                    upstream_camera_v2=True,
                )

        self.assert_no_remote_mutation(process)

    def test_same_but_wrong_layout_aborts_without_mutation(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        wrong = launcher.GLOBAL_INSTALL_MARKER_STRUCT.pack(
            launcher.GLOBAL_INSTALL_MARKER_MAGIC,
            launcher.GLOBAL_INSTALL_MARKER_VERSION,
            launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits,
            launcher.GLOBAL_INSTALL_COMPONENT_MASK,
            bytes(reversed(launcher.GLOBAL_INSTALL_LAYOUT_ID)),
            launcher._global_install_build_id(feature_bits),
        )
        process = MarkerProcess((wrong, wrong, wrong))
        with mock.patch.object(
            launcher,
            "_runtime_cave_publications",
            return_value=PUBLICATIONS,
        ):
            with self.assertRaisesRegex(RuntimeError, "layout/build differs"):
                launcher.classify_global_install(
                    process,
                    upstream_camera_v2=True,
                )

        self.assert_no_remote_mutation(process)

    def test_exact_commit_classifies_adopt_without_initialization(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((committed, committed, committed))
        with (
            mock.patch.object(
                launcher,
                "_runtime_cave_publications",
                return_value=PUBLICATIONS,
            ),
            mock.patch.object(launcher, "ensure_runtime_cave_bootstrap") as ensure,
            mock.patch.object(launcher, "inject_render_dll") as inject,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            result = launcher.classify_global_install(
                process,
                upstream_camera_v2=True,
            )

        self.assertEqual(result.mode, "adopt")
        self.assertEqual(result.bases, BASES)
        ensure.assert_not_called()
        inject.assert_not_called()
        popen.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_adopt_revalidates_layout_presence_and_all_heartbeats(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        preflight = launcher.GlobalInstallPreflight("adopt", BASES, feature_bits)
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((committed,) * 3)
        with (
            mock.patch.object(
                launcher.coop,
                "inspect_native_install",
                return_value=("reuse", BASES, ((1, 2, b"code"),)),
            ) as inspect,
            mock.patch.object(
                launcher,
                "_renderer_module_present",
                return_value=(True, "renderer-hash.dll"),
            ) as renderer,
            mock.patch.object(
                launcher,
                "expected_movement_hook_patch",
                return_value=b"hook",
            ),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "movement-live"),
            ) as movement,
            mock.patch.object(
                launcher,
                "menu_publisher_liveness_snapshot",
                return_value=(True, "menu-live"),
            ) as menu,
            mock.patch.object(launcher, "wait_renderer_ready") as renderer_wait,
            mock.patch.object(launcher, "wait_panel_transport_ready") as panel_wait,
        ):
            launcher.validate_global_adoption(process, 77, preflight)

        inspect.assert_called_once_with(process, upstream_camera_v2=True)
        renderer.assert_called_once_with(77)
        movement.assert_called_once_with(process, BASES[0], b"hook")
        menu.assert_called_once_with(77)
        renderer_wait.assert_called_once_with(
            process,
            BASES[0],
            launcher.RING_READY_MAGIC,
        )
        panel_wait.assert_called_once_with(77)
        self.assert_no_remote_mutation(process)

    def test_adopt_missing_renderer_aborts_before_heartbeat_waits(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        preflight = launcher.GlobalInstallPreflight("adopt", BASES, feature_bits)
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((committed,) * 3)
        with (
            mock.patch.object(
                launcher.coop,
                "inspect_native_install",
                return_value=("reuse", BASES, (object(),)),
            ),
            mock.patch.object(
                launcher,
                "_renderer_module_present",
                return_value=(False, "ausente"),
            ),
            mock.patch.object(launcher, "wait_renderer_ready") as renderer_wait,
            mock.patch.object(launcher, "wait_panel_transport_ready") as panel_wait,
        ):
            with self.assertRaisesRegex(RuntimeError, "renderer is not loaded"):
                launcher.validate_global_adoption(process, 77, preflight)

        renderer_wait.assert_not_called()
        panel_wait.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_adopt_rejects_commit_change_during_heartbeat_validation(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        preflight = launcher.GlobalInstallPreflight("adopt", BASES, feature_bits)
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((committed,) * 3)

        def change_secondary_marker(_pid):
            process.memory[
                BASES[1] + launcher.GLOBAL_INSTALL_MARKER_OFFSET
            ] = b"\x00" * launcher.GLOBAL_INSTALL_MARKER_STRUCT.size

        with (
            mock.patch.object(launcher, "_validate_global_native_layout"),
            mock.patch.object(
                launcher,
                "_renderer_module_present",
                return_value=(True, "renderer.dll"),
            ),
            mock.patch.object(
                launcher,
                "expected_movement_hook_patch",
                return_value=b"hook",
            ),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "live"),
            ),
            mock.patch.object(
                launcher,
                "menu_publisher_liveness_snapshot",
                return_value=(True, "live"),
            ),
            mock.patch.object(launcher, "wait_renderer_ready"),
            mock.patch.object(
                launcher,
                "wait_panel_transport_ready",
                side_effect=change_secondary_marker,
            ),
            self.assertRaisesRegex(RuntimeError, "commit changed"),
        ):
            launcher.validate_global_adoption(process, 77, preflight)

        self.assert_no_remote_mutation(process)

    def test_adopt_validates_signed_damage_meter_proxy_without_detaching_it(self):
        import damage_meter

        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        preflight = launcher.GlobalInstallPreflight("adopt", BASES, feature_bits)
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((committed,) * 3)
        site = launcher.coop.DAMAGE_NUMBER_SOURCE_CALL_SITE
        proxy_base = BASES[2] + launcher.coop.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
        proxy_entry = proxy_base + launcher.coop.DAMAGE_METER_PROXY_CODE_OFFSET
        hook_base = 0x0000012345000000
        hook_wrapper = hook_base + 0x100
        previous_target = (
            BASES[2] + launcher.coop.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
        )
        proxy_call = b"\xe8" + launcher.coop.rel32(site, 5, proxy_entry)
        underlying_call = b"\xe8" + launcher.coop.rel32(
            site,
            5,
            previous_target,
        )
        wrapper = damage_meter.build_wrapper(
            hook_wrapper,
            hook_base,
            previous_target,
        )
        process.memory[site] = proxy_call
        process.memory[proxy_base] = damage_meter.build_proxy_image(
            hook_base,
            previous_target,
            hook_wrapper,
        )
        process.memory[hook_base] = damage_meter.build_hook_header(
            previous_target,
            site,
            wrapper,
            sequence=123,
        )
        process.memory[hook_wrapper] = wrapper

        def inspect(read_view, *, upstream_camera_v2):
            self.assertTrue(upstream_camera_v2)
            self.assertIsNot(read_view, process)
            self.assertEqual(read_view.read(site, 5), underlying_call)
            return "reuse", BASES, ((1, site, underlying_call),)

        with (
            mock.patch.object(
                launcher.coop,
                "inspect_native_install",
                side_effect=inspect,
            ),
            mock.patch.object(
                launcher,
                "_renderer_module_present",
                return_value=(True, "renderer.dll"),
            ),
            mock.patch.object(
                launcher,
                "expected_movement_hook_patch",
                return_value=b"hook",
            ),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "live"),
            ),
            mock.patch.object(
                launcher,
                "menu_publisher_liveness_snapshot",
                return_value=(True, "live"),
            ),
            mock.patch.object(launcher, "wait_renderer_ready"),
            mock.patch.object(launcher, "wait_panel_transport_ready"),
        ):
            launcher.validate_global_adoption(process, 77, preflight)

        self.assertEqual(process.memory[site], proxy_call)
        self.assert_no_remote_mutation(process)

    def test_adopt_rejects_poisoned_damage_meter_proxy_without_detaching_it(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        preflight = launcher.GlobalInstallPreflight("adopt", BASES, feature_bits)
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((committed,) * 3)
        site = launcher.coop.DAMAGE_NUMBER_SOURCE_CALL_SITE
        proxy_base = BASES[2] + launcher.coop.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
        proxy_entry = proxy_base + launcher.coop.DAMAGE_METER_PROXY_CODE_OFFSET
        proxy_call = b"\xe8" + launcher.coop.rel32(site, 5, proxy_entry)
        process.memory[site] = proxy_call
        process.memory[
            proxy_base + launcher.coop.DAMAGE_METER_PROXY_ALLOCATION_OFFSET
        ] = struct.pack("<Q", 0xDEAD0000)
        process.memory[
            proxy_base + launcher.coop.DAMAGE_METER_PROXY_OLD_TARGET_OFFSET
        ] = struct.pack("<Q", BASES[2] + 0xDEAD)

        with (
            mock.patch.object(launcher.coop, "inspect_native_install") as inspect,
            self.assertRaisesRegex(RuntimeError, "invalid damage-meter proxy"),
        ):
            launcher.validate_global_adoption(process, 77, preflight)

        inspect.assert_not_called()
        self.assertEqual(process.memory[site], proxy_call)
        self.assert_no_remote_mutation(process)

    def test_prepare_and_commit_have_primary_last_commit_point(self):
        process = MarkerProcess()
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )

        launcher.publish_global_install_prepared(
            process,
            BASES,
            feature_bits=feature_bits,
        )
        prepared = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_PREPARED,
            feature_bits=feature_bits,
        )
        self.assertEqual(
            process.writes,
            [
                (base + launcher.GLOBAL_INSTALL_MARKER_OFFSET, prepared)
                for base in BASES
            ],
        )

        process.writes.clear()
        launcher.commit_global_install(
            process,
            BASES,
            feature_bits=feature_bits,
        )
        committed = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_COMMITTED,
            feature_bits=feature_bits,
        )
        self.assertEqual(
            process.writes,
            [
                (BASES[1] + launcher.GLOBAL_INSTALL_MARKER_OFFSET, committed),
                (BASES[2] + launcher.GLOBAL_INSTALL_MARKER_OFFSET, committed),
                (BASES[0] + launcher.GLOBAL_INSTALL_MARKER_OFFSET, committed),
            ],
        )
        self.assertEqual(process.patches, [])

    def test_prepare_refuses_a_changed_header_before_first_write(self):
        poisoned = b"X" * launcher.GLOBAL_INSTALL_MARKER_STRUCT.size
        process = MarkerProcess((poisoned, poisoned, poisoned))
        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
            launcher.publish_global_install_prepared(
                process,
                BASES,
                feature_bits=0,
            )
        self.assert_no_remote_mutation(process)

    def test_finalize_does_not_commit_when_native_layout_validation_fails(self):
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        prepared = launcher.global_install_marker(
            state=launcher.GLOBAL_INSTALL_PREPARED,
            feature_bits=feature_bits,
        )
        process = MarkerProcess((prepared,) * 3)
        with (
            mock.patch.object(
                launcher,
                "_validate_global_native_layout",
                side_effect=RuntimeError("native mismatch"),
            ) as validate,
            mock.patch.object(launcher, "commit_global_install") as commit,
            self.assertRaisesRegex(RuntimeError, "native mismatch"),
        ):
            launcher.finalize_global_install(
                process,
                BASES,
                feature_bits=feature_bits,
            )

        validate.assert_called_once_with(
            process,
            BASES,
            feature_bits=feature_bits,
        )
        commit.assert_not_called()
        self.assert_no_remote_mutation(process)

    def test_prepared_callback_runs_before_identity_and_renderer(self):
        events = []
        process = mock.Mock()
        process.ensure_cave_allocation.side_effect = (
            lambda: events.append("primary") or BASES[0]
        )
        process.ensure_dual_interaction_allocation.side_effect = (
            lambda: events.append("dual") or BASES[1]
        )
        process.ensure_damage_number_theme_allocation.side_effect = (
            lambda: events.append("damage") or BASES[2]
        )
        identity = mock.Mock(p3_party_member_id=None)
        identity.name.side_effect = lambda role: role
        with (
            mock.patch.object(
                launcher,
                "ensure_runtime_cave_bootstrap",
                side_effect=lambda *_args: events.append("bootstrap"),
            ),
            mock.patch.object(
                launcher,
                "wait_game_startup_ready",
                side_effect=lambda *_args: events.append("startup"),
            ),
            mock.patch.object(
                launcher,
                "seed_authored_session_identity",
                side_effect=lambda: events.append("identity") or identity,
            ),
            mock.patch.object(
                launcher,
                "inject_render_dll",
                side_effect=lambda _pid: events.append("renderer"),
            ),
        ):
            launcher.initialize_runtime_memory_and_renderer(
                process,
                77,
                before_renderer=lambda bases: events.append(("prepared", bases)),
            )

        self.assertEqual(
            events,
            [
                "bootstrap",
                "startup",
                "primary",
                "dual",
                "damage",
                ("prepared", BASES),
                "identity",
                "renderer",
            ],
        )

    def test_main_adopt_path_hands_committed_runtime_to_reconnector(self):
        process = MarkerProcess()
        feature_bits = launcher.global_install_feature_bits(
            upstream_camera_v2=True
        )
        preflight = launcher.GlobalInstallPreflight("adopt", BASES, feature_bits)
        with (
            mock.patch.object(launcher.coop, "find_pid", return_value=77),
            mock.patch.object(launcher, "acquire_mutex", return_value=123),
            mock.patch.object(launcher.coop, "RemoteProcess", return_value=process),
            mock.patch.object(
                launcher,
                "classify_global_install",
                return_value=preflight,
            ),
            mock.patch.object(
                launcher,
                "supervise_committed_runtime",
                return_value=0,
            ) as reconnect,
            mock.patch.object(launcher, "reset_session_logs") as reset_logs,
            mock.patch.object(launcher.runtime_status, "clear_status") as clear,
            mock.patch.object(launcher.runtime_status, "write_status") as status,
            mock.patch.object(launcher, "inject_render_dll") as inject,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
            mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        ):
            result = launcher.main()

        self.assertEqual(result, 0)
        reconnect.assert_called_once_with(
            process,
            77,
            preflight,
            launcher.LOG_ROOT / "77",
        )
        reset_logs.assert_not_called()
        clear.assert_not_called()
        status.assert_not_called()
        inject.assert_not_called()
        popen.assert_not_called()
        self.assert_no_remote_mutation(process)
        self.assertTrue(process.closed)
        close_handle.assert_called_once_with(123)

    def test_main_preflight_abort_has_no_cleanup_mutation_or_popen(self):
        process = MarkerProcess()
        with (
            mock.patch.object(launcher.coop, "find_pid", return_value=77),
            mock.patch.object(launcher, "acquire_mutex", return_value=123),
            mock.patch.object(launcher.coop, "RemoteProcess", return_value=process),
            mock.patch.object(
                launcher,
                "classify_global_install",
                side_effect=RuntimeError("partial global runtime"),
            ),
            mock.patch.object(launcher, "reset_session_logs") as reset_logs,
            mock.patch.object(launcher.runtime_status, "clear_status") as clear,
            mock.patch.object(launcher.runtime_status, "write_status") as status,
            mock.patch.object(launcher, "inject_render_dll") as inject,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
            mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        ):
            with self.assertRaisesRegex(RuntimeError, "partial global"):
                launcher.main()

        reset_logs.assert_not_called()
        clear.assert_not_called()
        status.assert_not_called()
        inject.assert_not_called()
        popen.assert_not_called()
        self.assert_no_remote_mutation(process)
        self.assertTrue(process.closed)
        close_handle.assert_called_once_with(123)

    def test_main_commits_only_after_complete_runtime_validation(self):
        import inspect

        source = inspect.getsource(launcher.main)
        self.assertLess(
            source.index("classify_global_install("),
            source.index("initialize_runtime_memory_and_renderer("),
        )
        self.assertLess(
            source.index("wait_runtime_ready("),
            source.index("finalize_global_install("),
        )


if __name__ == "__main__":
    unittest.main()
