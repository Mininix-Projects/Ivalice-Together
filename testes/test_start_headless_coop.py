import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import start_headless_coop as launcher

SOURCE = Path(__file__).resolve().parents[1] / "source"


class FakeProcess:
    def __init__(self, current, ui, battle_info=0x70000000):
        self.battle_info = battle_info
        self.layouts = {1: current, 2: ui}

    def u64(self, address):
        self.assert_address = address
        return self.battle_info

    def read(self, address, size):
        self_id = (address - self.battle_info - 0x5A48) // 0x12
        values = tuple(self.layouts[self_id]) + (0xFFFF,) * 9
        return struct.pack("<9H", *values[:9])


class SessionLogTests(unittest.TestCase):
    def test_new_session_discards_only_old_log_files(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            (log_dir / "movement.out.log").write_text("old", encoding="utf-8")
            marker = log_dir / "party_active_intent.json"
            marker.write_text("{}", encoding="utf-8")

            launcher.reset_session_logs(log_dir)

            self.assertFalse((log_dir / "movement.out.log").exists())
            self.assertTrue(marker.exists())

    def test_unavailable_session_log_directory_does_not_block_startup(self):
        with mock.patch.object(
            Path,
            "mkdir",
            autospec=True,
            side_effect=OSError("diagnostic volume unavailable"),
        ):
            launcher.reset_session_logs(Path("unavailable") / "logs")

    def test_non_os_session_log_exception_does_not_block_startup(self):
        with mock.patch.object(
            Path,
            "mkdir",
            autospec=True,
            side_effect=RuntimeError("filesystem filter failed"),
        ):
            launcher.reset_session_logs(Path("unavailable") / "logs")


class SupervisorMutexTests(unittest.TestCase):
    def test_supervisor_mutex_clears_residual_last_error_before_create(self):
        fake_kernel32 = SimpleNamespace(
            CreateMutexW=mock.Mock(return_value=123),
            CloseHandle=mock.Mock(return_value=True),
        )
        with (
            mock.patch.object(launcher, "kernel32", fake_kernel32),
            mock.patch.object(launcher.ct, "set_last_error") as clear_error,
            mock.patch.object(launcher.ct, "get_last_error", return_value=0),
        ):
            handle = launcher.acquire_mutex(4242)

        self.assertEqual(handle, 123)
        clear_error.assert_called_once_with(0)
        fake_kernel32.CreateMutexW.assert_called_once_with(
            None,
            True,
            r"Local\FF12LocalCoopHeadlessLauncher_4242",
        )


class FakeMovementProcess:
    def __init__(
        self, base, patch, *, lifetime_ready, logical_active=0, owner_pid=4321
    ):
        self.base = base
        self.patch = patch
        if lifetime_ready:
            self.liveness = bytearray(
                launcher.coop.MOVEMENT_HELPER_LIVENESS_STRUCT.pack(
                    2,
                    launcher.coop.MOVEMENT_HELPER_LIVENESS_MAGIC,
                    launcher.coop.MOVEMENT_HELPER_LIVENESS_VERSION,
                    owner_pid,
                    max(1, int(time.monotonic() * 1000.0)),
                    0,
                    0,
                )
            )
        else:
            self.liveness = bytearray(launcher.coop.MOVEMENT_HELPER_LIVENESS_SIZE)
        self.logical_active = int(bool(logical_active))
        self.read_addresses = []
        self.write_addresses = []

    def read(self, address, size):
        self.read_addresses.append(address)
        if address == launcher.coop.MOVEMENT_HOOK_SITE:
            return self.patch[:size]
        if address == self.base + launcher.MOVEMENT_LIFETIME_READY_OFFSET:
            return bytes(self.liveness[:size])
        if address == self.base + 0x208:
            return struct.pack("<I", self.logical_active)
        raise OSError(f"unexpected read at {address:#x}")

    def write(self, address, data):
        if address != self.base + launcher.MOVEMENT_LIFETIME_READY_OFFSET:
            raise OSError(f"unexpected write at {address:#x}")
        self.write_addresses.append(address)
        if len(data) != launcher.coop.MOVEMENT_HELPER_LIVENESS_SIZE:
            raise OSError(f"unexpected liveness write size {len(data)}")
        self.liveness[:] = data


class FakeRendererProcess:
    def __init__(self, base, present_counts):
        self.base = base
        self.present_counts = iter(present_counts)
        self.last_present_count = 0

    def read(self, address, size):
        if address == self.base + launcher.RING_READY_OFFSET:
            return struct.pack("<I", launcher.RING_READY_MAGIC)
        if address == self.base + launcher.RING_PRESENT_COUNT_OFFSET:
            self.last_present_count = next(
                self.present_counts,
                self.last_present_count,
            )
            return struct.pack("<I", self.last_present_count)
        raise OSError(f"unexpected read at {address:#x}")


class RoleFenceProcess:
    def __init__(self, events=None):
        self.memory = {}
        self.events = events if events is not None else []
        self.landing_faults = set()

    def read(self, address, size):
        return bytes(
            self.memory.get(int(address) + index, 0)
            for index in range(int(size))
        )

    def write(self, address, payload):
        address = int(address)
        payload = bytes(payload)
        for index, value in enumerate(payload):
            self.memory[address + index] = value
        self.events.append(("write", address, payload))
        if address in self.landing_faults:
            self.landing_faults.remove(address)
            raise OSError("remote fence write landed before error")

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]


class FakeStream:
    def __init__(self, events=None, name="stream"):
        self.closed = False
        self.events = events
        self.name = name

    def close(self):
        if self.events is not None:
            self.events.append(f"close_{self.name}")
        self.closed = True


class FakeChild:
    def __init__(self, returncode=None, *, wait_timeouts=0, events=None, pid=4321):
        self.returncode = returncode
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []
        self.wait_timeouts = wait_timeouts
        self.events = events

    def poll(self):
        if self.events is not None:
            self.events.append("poll")
        return self.returncode

    def terminate(self):
        if self.events is not None:
            self.events.append("terminate")
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self):
        if self.events is not None:
            self.events.append("kill")
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        if self.events is not None:
            self.events.append("wait")
        self.wait_calls.append(timeout)
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            self.returncode = None
            raise subprocess.TimeoutExpired("fake-child", timeout)
        return self.returncode


def fake_child_record(*, returncode=None, wait_timeouts=1, events=None):
    return (
        FakeChild(
            returncode,
            wait_timeouts=wait_timeouts,
            events=events,
        ),
        FakeStream(events, "stdout"),
        FakeStream(events, "stderr"),
        Path("movement.out.log"),
        Path("movement.err.log"),
    )


def role_child_record(role, *, returncode=None, external=False):
    record = list(fake_child_record(returncode=returncode, wait_timeouts=0))
    record[3] = Path(f"{role}.out.log")
    record[4] = Path(f"{role}.err.log")
    if external:
        record[0]._ff12_external = True
    return tuple(record)


class HeadlessLauncherPartyTests(unittest.TestCase):
    def test_startup_snapshot_requires_responsive_window_and_d3d11(self):
        with (
            mock.patch.object(
                launcher,
                "game_window_state",
                return_value=(0x1234, 1920, 1080, True),
            ),
            mock.patch.object(
                launcher,
                "loaded_module_names",
                return_value=frozenset({"ffxii_tza.exe", "dxgi.dll", "d3d11.dll"}),
            ),
        ):
            ready, identity, detail = launcher.game_startup_snapshot(77)

        self.assertTrue(ready)
        self.assertEqual(identity, (0x1234, 1920, 1080))
        self.assertIn("d3d11=1", detail)

    def test_startup_snapshot_rejects_white_screen_initialization_window(self):
        with (
            mock.patch.object(
                launcher,
                "game_window_state",
                return_value=(0x1234, 1920, 1080, False),
            ),
            mock.patch.object(
                launcher,
                "loaded_module_names",
                return_value=frozenset({"dxgi.dll", "d3d11.dll"}),
            ),
        ):
            ready, _identity, _detail = launcher.game_startup_snapshot(77)

        self.assertFalse(ready)

    def test_cave_reservation_precedes_startup_barrier_and_renderer_injection(self):
        events = []
        process = mock.Mock()
        process.ensure_cave_allocation.side_effect = (
            lambda: events.append("allocate_primary") or 0x30000000
        )
        process.ensure_dual_interaction_allocation.side_effect = (
            lambda: events.append("allocate_dual") or 0x31000000
        )
        process.ensure_damage_number_theme_allocation.side_effect = (
            lambda: events.append("allocate_damage") or 0x32000000
        )
        identity = mock.Mock(p3_party_member_id=None)
        identity.name.side_effect = lambda role: role.upper()
        with (
            mock.patch.object(
                launcher,
                "wait_game_startup_ready",
                side_effect=lambda *_args: events.append("startup_ready"),
            ),
            mock.patch.object(
                launcher,
                "ensure_runtime_cave_bootstrap",
                side_effect=lambda _process, pid: events.append(("bootstrap", pid)),
            ),
            mock.patch.object(
                launcher,
                "seed_authored_session_identity",
                side_effect=lambda: events.append("publish_identity") or identity,
            ),
            mock.patch.object(
                launcher,
                "inject_render_dll",
                side_effect=lambda pid: events.append(("inject", pid)),
            ),
            mock.patch.dict(launcher.os.environ, {}, clear=False),
        ):
            result = launcher.initialize_runtime_memory_and_renderer(process, 77)

        self.assertEqual(
            result,
            (0x30000000, 0x31000000, 0x32000000, identity),
        )
        self.assertEqual(
            events,
            [
                ("bootstrap", 77),
                "startup_ready",
                "allocate_primary",
                "allocate_dual",
                "allocate_damage",
                "publish_identity",
                ("inject", 77),
            ],
        )

    def test_launcher_uses_current_in_process_panel_renderer(self):
        self.assertEqual(
            launcher.RENDER_DLL.name,
            "p2_menu_color_dx11_v85.dll",
        )
        self.assertTrue(launcher.RENDER_DLL.is_file())

    def test_launcher_embeds_the_module_loader_independent_cave_bootstrap(self):
        self.assertEqual(
            launcher.CAVE_BOOTSTRAP_DLL.name,
            "ivalice-together-autostart.dll",
        )
        self.assertTrue(launcher.CAVE_BOOTSTRAP_DLL.is_file())

    def test_cave_bootstrap_injects_only_when_a_reservation_is_missing(self):
        complete = {
            launcher.runtime_paths.CAVE_BASE_ENV: 0x10000000,
            launcher.runtime_paths.DUAL_CAVE_BASE_ENV: 0x10010000,
            launcher.runtime_paths.DAMAGE_CAVE_BASE_ENV: 0x10020000,
        }
        process = mock.Mock()
        with (
            mock.patch.object(
                launcher,
                "_runtime_cave_publications",
                side_effect=({}, complete),
            ),
            mock.patch.object(
                launcher,
                "materialize_runtime_dll",
                return_value=Path("C:/game/coop/runtime/bootstrap.dll"),
            ),
            mock.patch("inject_dll.inject") as inject,
            mock.patch.dict(launcher.os.environ, {}, clear=False),
        ):
            actual = launcher.ensure_runtime_cave_bootstrap(process, 77)
            published = {
                variable: launcher.os.environ[variable] for variable in complete
            }

        self.assertEqual(actual, complete)
        inject.assert_called_once_with(
            str(Path("C:/game/coop/runtime/bootstrap.dll")),
            pid=77,
        )
        for variable, address in complete.items():
            self.assertEqual(published[variable], f"{address:#x}")

    def test_renderer_is_materialized_outside_onefile_temp_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "_MEI250122" / "renderer.dll"
            destination_root = root / "game" / "coop" / "runtime"
            source.parent.mkdir()
            source.write_bytes(b"renderer-payload")

            first = launcher.materialize_render_dll(source, destination_root)
            second = launcher.materialize_render_dll(source, destination_root)
            self.assertEqual(first, second)
            self.assertEqual(first.parent, destination_root)
            self.assertIn("renderer-", first.name)
            self.assertNotIn("_MEI", str(first))
            self.assertEqual(first.read_bytes(), b"renderer-payload")

    def test_movement_retry_policy_is_one_retry_with_safe_windows(self):
        self.assertEqual(launcher.MOVEMENT_START_ATTEMPTS, 2)
        self.assertGreaterEqual(launcher.MOVEMENT_READY_TIMEOUT, 15.0)
        self.assertLessEqual(launcher.MOVEMENT_READY_TIMEOUT, 20.0)

    def test_new_game_wait_has_no_default_timeout(self):
        import inspect

        parameter = inspect.signature(launcher.wait_gameplay).parameters["timeout"]
        self.assertIsNone(parameter.default)

    def test_game_lifetime_uses_exit_code_instead_of_remote_memory_probe(self):
        class HandleProcess:
            handle = 123

            def read(self, _address, _size):
                raise AssertionError("lifetime must not use ReadProcessMemory")

        exit_codes = iter((launcher.STILL_ACTIVE, 0))

        def get_exit_code(_handle, output):
            ct_output = launcher.ct.cast(
                output,
                launcher.ct.POINTER(launcher.wt.DWORD),
            )
            ct_output.contents.value = next(exit_codes)
            return True

        with mock.patch.object(
            launcher.kernel32,
            "GetExitCodeProcess",
            side_effect=get_exit_code,
        ):
            self.assertTrue(launcher.game_alive(HandleProcess()))
            self.assertFalse(launcher.game_alive(HandleProcess()))

    def test_failed_lifetime_query_is_not_misclassified_as_game_exit(self):
        class HandleProcess:
            handle = 123

            def read(self, _address, _size):
                raise AssertionError("uncertain lifetime must not probe memory")

        with mock.patch.object(
            launcher.kernel32,
            "GetExitCodeProcess",
            return_value=False,
        ):
            self.assertTrue(launcher.game_alive(HandleProcess()))

    def test_wait_gameplay_survives_a_transient_actor_read_failure(self):
        actors = [(0, 0x12340000)]
        clock = iter((0.0, 0.0, 0.1, 0.1, 2.2, 2.2))
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(
                launcher.coop,
                "resolve_active_actors",
                side_effect=[OSError("loading"), actors, actors],
            ),
            mock.patch.object(
                launcher.coop,
                "resolve_active_party_actor_slots",
                return_value={0: (0, 0x12340000)},
            ),
            mock.patch.object(launcher, "party_layouts_ready", return_value=True),
            mock.patch.object(launcher.time, "monotonic", side_effect=clock),
            mock.patch.object(launcher.time, "sleep"),
        ):
            actual = launcher.wait_gameplay(object(), timeout=10.0)

        self.assertEqual(actual, actors)

    def test_identity_sync_retries_if_loading_replaces_party_memory(self):
        identity = launcher.session_state.SessionIdentity(0, 4, 5)
        recover = mock.Mock()
        process = object()
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(
                launcher,
                "sync_identity_to_live_party",
                side_effect=[OSError("layout replaced"), identity],
            ) as sample,
            mock.patch.object(launcher.time, "sleep") as sleep,
        ):
            actual = launcher.sync_identity_when_stable(
                process,
                [(0, 0x12340000)],
                recover_children=recover,
                poll_interval=0.01,
            )

        self.assertEqual(actual, identity)
        self.assertEqual(sample.call_count, 2)
        sample.assert_has_calls(
            [
                mock.call(process, [(0, 0x12340000)]),
                mock.call(process, None),
            ]
        )
        self.assertEqual(recover.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_launcher_starts_penelo_story_patch_and_live_guard_before_waiting(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        seed_at = source.index("identity = seed_authored_session_identity()")
        story_at = source.index('"story_party_patch.py"')
        guard_at = source.index('"penelo_story_guard.py"')
        wait_at = source.index("actors = wait_gameplay(process")
        self.assertLess(seed_at, wait_at)
        self.assertLess(story_at, wait_at)
        self.assertLess(guard_at, wait_at)

    def test_all_loading_helpers_precede_renderer_wait_and_gameplay(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]
        inject_at = main.index("initialize_runtime_memory_and_renderer")
        factories_at = main.index("helper_factories = runtime_helper_factories")
        story_at = main.index("story_party = start_required_child")
        guard_at = main.index("penelo_guard = start_required_child")
        renderer_ready_at = main.index("RING_READY_MAGIC", inject_at)
        wait_at = main.index("actors = wait_gameplay(process")

        self.assertLess(inject_at, factories_at)
        self.assertLess(factories_at, story_at)
        self.assertLess(story_at, renderer_ready_at)
        self.assertLess(guard_at, renderer_ready_at)
        self.assertLess(renderer_ready_at, wait_at)
        self.assertLess(main.index('"headless_menu_shell.py"'), renderer_ready_at)
        self.assertNotIn('"enemy_variant_runtime.py"', main)
        self.assertNotIn('"weapon_profile_runtime.py"', main)
        self.assertNotIn('"calamity_formula_runtime.py"', main)
        self.assertLess(
            main.index("movement = start_required_child"),
            renderer_ready_at,
        )

    def test_runtime_initialization_publishes_both_caves(self):
        process = mock.Mock()
        process.ensure_cave_allocation.return_value = 0x30000000
        process.ensure_dual_interaction_allocation.return_value = 0x31000000
        process.ensure_damage_number_theme_allocation.return_value = 0x32000000
        identity = mock.Mock(p3_party_member_id=None)
        identity.name.side_effect = lambda role: role.upper()
        with (
            mock.patch.object(launcher, "wait_game_startup_ready"),
            mock.patch.object(launcher, "ensure_runtime_cave_bootstrap"),
            mock.patch.object(
                launcher,
                "seed_authored_session_identity",
                return_value=identity,
            ),
            mock.patch.object(launcher, "inject_render_dll"),
            mock.patch.dict(launcher.os.environ, {}, clear=False),
        ):
            launcher.initialize_runtime_memory_and_renderer(process, 77)
            self.assertEqual(
                launcher.os.environ[launcher.runtime_paths.CAVE_BASE_ENV],
                "0x30000000",
            )
            self.assertEqual(
                launcher.os.environ[launcher.runtime_paths.DUAL_CAVE_BASE_ENV],
                "0x31000000",
            )
            self.assertEqual(
                launcher.os.environ[launcher.runtime_paths.DAMAGE_CAVE_BASE_ENV],
                "0x32000000",
            )

    def test_renderer_readiness_requires_a_live_present_heartbeat(self):
        base = 0x5BAA0000
        launcher.wait_renderer_ready(
            FakeRendererProcess(base, [10, 11]),
            base,
            timeout=0.02,
            poll_interval=0.001,
        )
        with self.assertRaisesRegex(RuntimeError, "nao apresentou frames"):
            launcher.wait_renderer_ready(
                FakeRendererProcess(base, [10]),
                base,
                timeout=0.005,
                poll_interval=0.001,
            )

    def test_menu_watchdog_observes_publisher_not_renderer_activity(self):
        now_ms = 100_000
        snapshot = (
            launcher.d3d_panel_transport.MAGIC,
            launcher.d3d_panel_transport.VERSION,
            launcher.d3d_panel_transport.HEADER_SIZE,
            launcher.d3d_panel_transport.PANEL_COUNT,
            launcher.d3d_panel_transport.SLOT_SIZE,
            launcher.d3d_panel_transport.PIXEL_CAPACITY,
            1,
            0,
            now_ms - 10,
            0,
            0,
        )
        with (
            mock.patch.object(
                launcher,
                "panel_transport_snapshot",
                return_value=snapshot,
            ),
            mock.patch.object(
                launcher.d3d_panel_transport,
                "tick_ms",
                return_value=now_ms,
            ),
        ):
            ready, diagnostic = launcher.menu_publisher_liveness_snapshot(77)

        self.assertTrue(ready, diagnostic)

        stale = (*snapshot[:8], now_ms - 10_000, *snapshot[9:])
        with (
            mock.patch.object(
                launcher,
                "panel_transport_snapshot",
                return_value=stale,
            ),
            mock.patch.object(
                launcher.d3d_panel_transport,
                "tick_ms",
                return_value=now_ms,
            ),
        ):
            ready, diagnostic = launcher.menu_publisher_liveness_snapshot(77)

        self.assertFalse(ready)
        self.assertIn("age_ms=10000", diagnostic)

    def test_menu_starts_early_and_all_helpers_are_ready_before_gameplay(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]
        headless_at = main.index('"headless_menu_shell.py"')
        marker_at = main.index('"HEADLESS_MENU_V2_READY"', headless_at)
        movement_at = main.index("movement = start_required_child")
        wait_at = main.index("actors = wait_gameplay(process", marker_at)

        self.assertLess(headless_at, movement_at)
        self.assertLess(headless_at, marker_at)
        # start_required_child does not return this exact process until its
        # fresh marker is proven, so movement can start only afterwards.
        self.assertLess(marker_at, movement_at)
        self.assertLess(marker_at, wait_at)

    def test_fresh_launcher_closes_role_admission_before_headless_starts(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]
        initialized = main.index("initialize_runtime_memory_and_renderer")
        fence = main.index(
            "_publish_role_admission_fence_confirmed(process, True)",
            initialized,
        )
        headless = main.index("menu = start_required_child", fence)
        movement = main.index("movement = start_required_child", headless)

        self.assertLess(initialized, fence)
        self.assertLess(fence, headless)
        self.assertLess(headless, movement)

    def test_bootstrap_arms_the_session_before_waiting_for_gameplay(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]

        self.assertLess(
            main.index('print(f"CO-OP ARMADO'),
            main.index("actors = wait_gameplay(process"),
        )

    def test_frozen_workers_reuse_the_current_onefile_extraction(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        self.assertIn('environment.pop("PYINSTALLER_RESET_ENVIRONMENT", None)', source)
        self.assertNotIn('environment["PYINSTALLER_RESET_ENVIRONMENT"]', source)

    def test_movement_starts_before_gameplay_wait(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]
        self.assertLess(
            main.index("movement = start_required_child"),
            main.index("actors = wait_gameplay(process"),
        )

    def test_every_initial_ready_helper_uses_persistent_startup_recovery(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]

        for role in ("story_party", "penelo_guard", "movement"):
            with self.subTest(role=role):
                assignment = f"{role} = start_required_child"
                self.assertIn(assignment, main)
        self.assertNotIn("story_party = start_child", main)
        self.assertNotIn("penelo_guard = start_ready_child", main)
        self.assertNotIn("movement = start_movement_helper", main)

    def test_wait_child_ready_ignores_old_marker_and_requires_a_new_one(self):
        marker = "HEADLESS_MENU_V2_READY"
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "headless.out.log"
            stderr_path = Path(temporary) / "headless.err.log"
            stdout_path.write_text(marker + " stale\n", encoding="utf-8")
            stderr_path.write_bytes(b"")
            child = FakeChild()
            child._ff12_stdout_start = stdout_path.stat().st_size
            child_record = (
                child,
                FakeStream(name="stdout"),
                FakeStream(name="stderr"),
                stdout_path,
                stderr_path,
            )

            with self.assertRaises((RuntimeError, TimeoutError)):
                launcher.wait_child_ready(
                    child_record,
                    marker,
                    timeout=0.02,
                    poll_interval=0.001,
                )

            with stdout_path.open("a", encoding="utf-8") as output:
                output.write(marker + " current\n")
            launcher.wait_child_ready(
                child_record,
                marker,
                timeout=0.02,
                poll_interval=0.001,
            )

    def test_wait_child_ready_reports_the_current_stderr_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "penelo_guard.out.log"
            stderr_path = Path(temporary) / "penelo_guard.err.log"
            stdout_path.write_bytes(b"")
            stderr_path.write_text("old failure\n", encoding="utf-8")
            child = FakeChild(returncode=1)
            child._ff12_stdout_start = 0
            child._ff12_stderr_start = stderr_path.stat().st_size
            with stderr_path.open("a", encoding="utf-8") as output:
                output.write("RuntimeError: unexpected addPartyMember build\n")
            child_record = (
                child,
                FakeStream(name="stdout"),
                FakeStream(name="stderr"),
                stdout_path,
                stderr_path,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "unexpected addPartyMember build",
            ):
                launcher.wait_child_ready(child_record, "PENELO_GUARD ready")

    def test_wait_child_ready_reopens_a_temporarily_unavailable_log(self):
        marker = "HEADLESS_MENU_V2_READY"
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "headless.out.log"
            stderr_path = Path(temporary) / "headless.err.log"
            stdout_path.write_text(marker + "\n", encoding="utf-8")
            stderr_path.write_bytes(b"")
            child = FakeChild()
            child._ff12_stdout_start = 0
            child_record = (
                child,
                FakeStream(name="stdout"),
                FakeStream(name="stderr"),
                stdout_path,
                stderr_path,
            )
            original_open = Path.open
            reads = 0

            def intermittent_open(path, *args, **kwargs):
                nonlocal reads
                if path == stdout_path and args and args[0] == "rb":
                    reads += 1
                    if reads == 1:
                        raise OSError("profile volume briefly unavailable")
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", autospec=True, side_effect=intermittent_open):
                launcher.wait_child_ready(
                    child_record,
                    marker,
                    timeout=0.1,
                    poll_interval=0.001,
                )

        self.assertGreaterEqual(reads, 2)

    def test_wait_child_ready_preserves_a_marker_split_across_reads(self):
        marker = "STORY_PARTY_PATCH ready"
        split = len(marker) // 2
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "story_party.out.log"
            stderr_path = Path(temporary) / "story_party.err.log"
            stdout_path.write_bytes(marker[:split].encode("utf-8"))
            stderr_path.write_bytes(b"")
            child = FakeChild()
            child._ff12_stdout_start = 0
            child_record = (
                child,
                FakeStream(name="stdout"),
                FakeStream(name="stderr"),
                stdout_path,
                stderr_path,
            )
            appended = False

            def finish_marker(_delay):
                nonlocal appended
                if appended:
                    return
                appended = True
                with stdout_path.open("ab") as output:
                    output.write(marker[split:].encode("utf-8") + b"\n")

            with mock.patch.object(launcher.time, "sleep", side_effect=finish_marker):
                launcher.wait_child_ready(
                    child_record,
                    marker,
                    timeout=0.1,
                    poll_interval=0.001,
                )

        self.assertTrue(appended)

    def test_wait_child_ready_rejects_a_replaced_stale_marker_file(self):
        marker = "PENELO_GUARD ready"
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "penelo_guard.out.log"
            stderr_path = Path(temporary) / "penelo_guard.err.log"
            stdout_path.write_bytes(b"")
            stderr_path.write_bytes(b"")
            with stdout_path.open("ab", buffering=0) as original:
                original_identity = launcher.stream_file_identity(original)
            stdout_path.unlink()
            stdout_path.write_text(marker + " stale replacement\n", encoding="utf-8")
            child = FakeChild()
            child._ff12_stdout_start = 0
            child._ff12_stdout_identity = original_identity
            child_record = (
                child,
                FakeStream(name="stdout"),
                FakeStream(name="stderr"),
                stdout_path,
                stderr_path,
            )

            with self.assertRaisesRegex(TimeoutError, "replaced"):
                launcher.wait_child_ready(
                    child_record,
                    marker,
                    timeout=0.02,
                    poll_interval=0.001,
                )

    def test_ready_child_retries_once_after_an_early_exit(self):
        first = fake_child_record(returncode=1, wait_timeouts=0)
        second = fake_child_record(returncode=None, wait_timeouts=0)
        with (
            mock.patch.object(
                launcher,
                "start_child",
                side_effect=[first, second],
            ) as start_child,
            mock.patch.object(
                launcher,
                "wait_child_ready",
                side_effect=[RuntimeError("first failed"), None],
            ),
            mock.patch.object(launcher, "cleanup_child") as cleanup_child,
        ):
            result = launcher.start_ready_child(
                Path("logs"),
                "penelo_guard",
                "penelo_story_guard.py",
                "PENELO_GUARD ready",
            )

        self.assertIs(result, second)
        self.assertEqual(start_child.call_count, 2)
        cleanup_child.assert_called_once_with(first)

    def test_ready_child_never_replaces_a_live_child_after_log_timeout(self):
        live = fake_child_record(returncode=None, wait_timeouts=0)
        with (
            mock.patch.object(launcher, "start_child", return_value=live) as start_child,
            mock.patch.object(
                launcher,
                "wait_child_ready",
                side_effect=[TimeoutError("log unavailable"), None],
            ) as wait_ready,
            mock.patch.object(launcher, "cleanup_child") as cleanup_child,
            mock.patch.object(launcher.time, "sleep"),
        ):
            result = launcher.start_ready_child(
                Path("logs"),
                "headless_menu",
                "headless_menu_shell.py",
                "HEADLESS_MENU_V2_READY",
            )

        self.assertIs(result, live)
        start_child.assert_called_once()
        self.assertEqual(wait_ready.call_count, 2)
        cleanup_child.assert_not_called()

    def test_ready_child_cleans_live_child_only_after_proven_game_exit(self):
        live = fake_child_record(returncode=None, wait_timeouts=0)
        process = object()
        with (
            mock.patch.object(launcher, "start_child", return_value=live),
            mock.patch.object(
                launcher,
                "wait_child_ready",
                side_effect=TimeoutError("marker storage unavailable"),
            ),
            mock.patch.object(launcher, "game_alive", return_value=False),
            mock.patch.object(launcher, "cleanup_child") as cleanup_child,
        ):
            with self.assertRaisesRegex(RuntimeError, "game exited"):
                launcher.start_ready_child(
                    Path("logs"),
                    "story_party",
                    "story_party_patch.py",
                    "STORY_PARTY_PATCH ready",
                    game_process=process,
                )

        cleanup_child.assert_called_once_with(live)

    def test_failed_helper_is_replaced_in_place_by_its_role_factory(self):
        failed = fake_child_record(returncode=7, wait_timeouts=0)
        replacement = fake_child_record(returncode=None, wait_timeouts=0)
        children = [failed]
        factory = mock.Mock(return_value=replacement)

        with mock.patch.object(launcher, "game_alive", return_value=True):
            recovered = launcher.recover_failed_children(
                object(),
                children,
                {"movement": factory},
                backoff=0,
            )

        self.assertTrue(recovered)
        self.assertIs(children[0], replacement)
        factory.assert_called_once_with()
        self.assertTrue(failed[1].closed)
        self.assertTrue(failed[2].closed)

    def test_headless_only_recovery_preserves_the_ready_movement_consumer(self):
        launcher.coop.set_cave_base(0x61000000)
        launcher.coop.set_dual_interaction_base(0x62000000)
        events = []
        process = RoleFenceProcess(events)
        headless = role_child_record("headless_menu", returncode=7)
        movement = role_child_record("movement", returncode=None)
        replacement_headless = role_child_record("headless_menu")
        children = [headless, movement]

        def cleanup(record, *, preserve_resident_runtime=False):
            self.assertTrue(preserve_resident_runtime)
            events.append(f"stop:{launcher.child_role(record)}")
            record[0].returncode = 0

        def start_headless():
            self.assertEqual(
                process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
                1,
            )
            events.append("start:headless_menu")
            return replacement_headless

        checker = mock.Mock(return_value=(True, "ready"))
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
            mock.patch.object(
                launcher.coop,
                "repair_abandoned_native_leader_role_claim_for_restart",
                side_effect=lambda _process: events.append("repair") or False,
            ),
        ):
            recovered = launcher.recover_failed_children(
                process,
                children,
                {
                    "headless_menu": start_headless,
                    "movement": mock.Mock(),
                },
                attempts=1,
                backoff=0,
                health_checks={"movement": checker},
            )

        self.assertTrue(recovered)
        self.assertIs(children[0], replacement_headless)
        self.assertIs(children[1], movement)
        self.assertEqual(
            [event for event in events if isinstance(event, str)],
            ["stop:headless_menu", "repair", "start:headless_menu"],
        )
        self.assertEqual(checker.call_count, 2)
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            0,
        )

    def test_movement_or_dual_failure_restarts_consumer_before_producer(self):
        for failed_roles in ({"movement"}, {"headless_menu", "movement"}):
            with self.subTest(failed_roles=failed_roles):
                launcher.coop.set_cave_base(0x61100000)
                launcher.coop.set_dual_interaction_base(0x62100000)
                events = []
                process = RoleFenceProcess(events)
                headless = role_child_record(
                    "headless_menu",
                    returncode=7 if "headless_menu" in failed_roles else None,
                )
                movement = role_child_record(
                    "movement",
                    returncode=7 if "movement" in failed_roles else None,
                )
                replacement_movement = role_child_record("movement")
                replacement_headless = role_child_record("headless_menu")
                children = [headless, movement]

                def cleanup(record, *, preserve_resident_runtime=False):
                    self.assertTrue(preserve_resident_runtime)
                    events.append(f"stop:{launcher.child_role(record)}")
                    record[0].returncode = 0

                def start_movement():
                    events.append("start:movement")
                    # Movement opens after install + heartbeat; the supervisor
                    # must close it again before starting headless.
                    process.write(
                        launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
                        struct.pack("<I", 0),
                    )
                    return replacement_movement

                def start_headless():
                    self.assertEqual(
                        process.u32(
                            launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE
                        ),
                        1,
                    )
                    events.append("start:headless_menu")
                    return replacement_headless

                with (
                    mock.patch.object(launcher, "game_alive", return_value=True),
                    mock.patch.object(
                        launcher,
                        "cleanup_child",
                        side_effect=cleanup,
                    ),
                    mock.patch.object(
                        launcher.coop,
                        "repair_abandoned_native_leader_role_claim_for_restart",
                        side_effect=lambda _process: events.append("repair") or False,
                    ),
                ):
                    recovered = launcher.recover_failed_children(
                        process,
                        children,
                        {
                            "headless_menu": start_headless,
                            "movement": start_movement,
                        },
                        attempts=1,
                        backoff=0,
                        health_checks={
                            "movement": lambda _record: (True, "ready")
                        },
                    )

                self.assertTrue(recovered)
                ordered = [event for event in events if isinstance(event, str)]
                self.assertLess(
                    ordered.index("stop:headless_menu"),
                    ordered.index("repair"),
                )
                self.assertLess(
                    ordered.index("repair"),
                    ordered.index("stop:movement"),
                )
                self.assertLess(
                    ordered.index("stop:movement"),
                    ordered.index("start:movement"),
                )
                self.assertLess(
                    ordered.index("start:movement"),
                    ordered.index("start:headless_menu"),
                )
                self.assertEqual(
                    process.u32(
                        launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE
                    ),
                    0,
                )

    def test_landed_fence_close_is_confirmed_before_pair_recovery(self):
        launcher.coop.set_cave_base(0x61200000)
        launcher.coop.set_dual_interaction_base(0x62200000)
        events = []
        process = RoleFenceProcess(events)
        process.landing_faults.add(
            launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE
        )
        children = [
            role_child_record("headless_menu", returncode=7),
            role_child_record("movement"),
        ]

        def cleanup(record, *, preserve_resident_runtime=False):
            self.assertTrue(preserve_resident_runtime)
            record[0].returncode = 0

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
            mock.patch.object(
                launcher.coop,
                "repair_abandoned_native_leader_role_claim_for_restart",
                return_value=False,
            ),
        ):
            self.assertTrue(
                launcher.recover_failed_children(
                    process,
                    children,
                    {
                        "headless_menu": lambda: role_child_record(
                            "headless_menu"
                        ),
                        "movement": mock.Mock(),
                    },
                    attempts=1,
                    backoff=0,
                    health_checks={
                        "movement": lambda _record: (True, "ready")
                    },
                )
            )
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            0,
        )

    def test_pair_recovery_failure_keeps_fence_closed_and_producer_quiescent(self):
        launcher.coop.set_cave_base(0x61300000)
        launcher.coop.set_dual_interaction_base(0x62300000)
        process = RoleFenceProcess()
        children = [
            role_child_record("headless_menu"),
            role_child_record("movement", returncode=7),
        ]
        cleaned = []

        def cleanup(record, *, preserve_resident_runtime=False):
            self.assertTrue(preserve_resident_runtime)
            cleaned.append(launcher.child_role(record))
            record[0].returncode = 0

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
            mock.patch.object(
                launcher.coop,
                "repair_abandoned_native_leader_role_claim_for_restart",
                return_value=False,
            ),
            self.assertRaisesRegex(RuntimeError, "movement did not recover"),
        ):
            launcher.recover_failed_children(
                process,
                children,
                {
                    "headless_menu": mock.Mock(),
                    "movement": mock.Mock(side_effect=OSError("cold")),
                },
                attempts=1,
                backoff=0,
                health_checks={"movement": mock.Mock()},
            )

        self.assertEqual(cleaned, ["headless_menu", "movement"])
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            1,
        )

    def test_external_live_producer_blocks_pair_mutation_and_reopen(self):
        launcher.coop.set_cave_base(0x61400000)
        launcher.coop.set_dual_interaction_base(0x62400000)
        process = RoleFenceProcess()
        children = [
            role_child_record("headless_menu", external=True),
            role_child_record("movement", returncode=7),
        ]
        with (
            mock.patch.object(launcher, "cleanup_child") as cleanup,
            mock.patch.object(
                launcher.coop,
                "repair_abandoned_native_leader_role_claim_for_restart",
            ) as repair,
        ):
            recovered = launcher.recover_failed_children(
                process,
                children,
                {
                    "headless_menu": mock.Mock(),
                    "movement": mock.Mock(),
                },
                attempts=1,
                backoff=0,
                health_checks={"movement": mock.Mock()},
            )

        self.assertFalse(recovered)
        cleanup.assert_not_called()
        repair.assert_not_called()
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            1,
        )

    def test_headless_death_mid_claim_is_repaired_before_replacement_spawn(self):
        launcher.coop.set_cave_base(0x61500000)
        launcher.coop.set_dual_interaction_base(0x62500000)
        process = RoleFenceProcess()
        role_mode = (
            launcher.coop.P2_DEVICE_FILTER_STATE
            + launcher.coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
        )
        process.write(
            role_mode,
            struct.pack("<I", launcher.coop.NATIVE_LEADER_ROLE_CLAIMING),
        )
        children = [
            role_child_record("headless_menu", returncode=7),
            role_child_record("movement"),
        ]

        def cleanup(record, *, preserve_resident_runtime=False):
            self.assertTrue(preserve_resident_runtime)
            record[0].returncode = 0

        def start_headless():
            self.assertEqual(process.u32(role_mode), 0)
            return role_child_record("headless_menu")

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
        ):
            self.assertTrue(
                launcher.recover_failed_children(
                    process,
                    children,
                    {
                        "headless_menu": start_headless,
                        "movement": mock.Mock(),
                    },
                    attempts=1,
                    backoff=0,
                    health_checks={
                        "movement": lambda _record: (True, "ready")
                    },
                )
            )

        self.assertEqual(process.u32(role_mode), 0)
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            0,
        )

    def test_failed_producer_quiescence_never_touches_movement(self):
        launcher.coop.set_cave_base(0x61600000)
        launcher.coop.set_dual_interaction_base(0x62600000)
        process = RoleFenceProcess()
        headless = role_child_record("headless_menu")
        movement = role_child_record("movement", returncode=7)
        children = [headless, movement]
        movement_factory = mock.Mock()

        def cleanup(record, *, preserve_resident_runtime=False):
            self.assertTrue(preserve_resident_runtime)
            self.assertIs(record, headless)
            raise RuntimeError("producer quiescence unavailable")

        with (
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
            mock.patch.object(
                launcher.coop,
                "repair_abandoned_native_leader_role_claim_for_restart",
            ) as repair,
            self.assertRaisesRegex(RuntimeError, "quiescence unavailable"),
        ):
            launcher.recover_failed_children(
                process,
                children,
                {
                    "headless_menu": mock.Mock(),
                    "movement": movement_factory,
                },
                attempts=1,
                backoff=0,
                health_checks={"movement": mock.Mock()},
            )

        repair.assert_not_called()
        movement_factory.assert_not_called()
        self.assertIsNone(headless[0].returncode)
        self.assertEqual(movement[0].returncode, 7)
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            1,
        )

    def test_post_spawn_health_failure_requiesces_new_producer(self):
        launcher.coop.set_cave_base(0x61700000)
        launcher.coop.set_dual_interaction_base(0x62700000)
        process = RoleFenceProcess()
        replacement_movement = role_child_record("movement")
        replacement_headless = role_child_record("headless_menu")
        children = [
            role_child_record("headless_menu"),
            role_child_record("movement", returncode=7),
        ]
        cleaned = []

        def cleanup(record, *, preserve_resident_runtime=False):
            self.assertTrue(preserve_resident_runtime)
            cleaned.append(record)
            record[0].returncode = 0

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
            mock.patch.object(
                launcher.coop,
                "repair_abandoned_native_leader_role_claim_for_restart",
                return_value=False,
            ),
            self.assertRaisesRegex(RuntimeError, "consumer is not ready"),
        ):
            launcher.recover_failed_children(
                process,
                children,
                {
                    "movement": lambda: replacement_movement,
                    "headless_menu": lambda: replacement_headless,
                },
                attempts=1,
                backoff=0,
                health_checks={
                    "movement": lambda _record: (False, "heartbeat stale")
                },
            )

        self.assertIn(replacement_headless, cleaned)
        self.assertEqual(replacement_headless[0].returncode, 0)
        self.assertIs(children[0], replacement_headless)
        self.assertIs(children[1], replacement_movement)
        self.assertEqual(
            process.u32(launcher.coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE),
            1,
        )

    def test_helper_recovery_retries_factory_without_cleaning_healthy_helpers(self):
        failed = fake_child_record(returncode=3, wait_timeouts=0)
        healthy = fake_child_record(returncode=None, wait_timeouts=0)
        healthy = (*healthy[:3], Path("story_party.out.log"), healthy[4])
        replacement = fake_child_record(returncode=None, wait_timeouts=0)
        children = [failed, healthy]
        factory = mock.Mock(side_effect=[OSError("cold"), replacement])

        with mock.patch.object(launcher, "game_alive", return_value=True):
            launcher.recover_failed_children(
                object(),
                children,
                {"movement": factory, "story_party": mock.Mock()},
                attempts=2,
                backoff=0,
            )

        self.assertIs(children[0], replacement)
        self.assertIs(children[1], healthy)
        self.assertFalse(healthy[1].closed)
        self.assertEqual(factory.call_count, 2)

    def test_production_recovery_continues_past_the_startup_attempt_limit(self):
        failed = fake_child_record(returncode=3, wait_timeouts=0)
        replacement = fake_child_record(returncode=None, wait_timeouts=0)
        children = [failed]
        factory = mock.Mock(
            side_effect=[
                RuntimeError("cold-1"),
                RuntimeError("cold-2"),
                replacement,
            ]
        )

        with mock.patch.object(launcher, "game_alive", return_value=True):
            launcher.recover_failed_children(
                object(),
                children,
                {"movement": factory},
                backoff=0,
            )

        self.assertIs(children[0], replacement)
        self.assertEqual(factory.call_count, launcher.CHILD_START_ATTEMPTS + 1)

    def test_unrecoverable_helper_reports_role_after_bounded_attempts(self):
        failed = fake_child_record(returncode=9, wait_timeouts=0)
        children = [failed]
        factory = mock.Mock(side_effect=RuntimeError("still broken"))

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            self.assertRaisesRegex(RuntimeError, "helper movement did not recover"),
        ):
            launcher.recover_failed_children(
                object(),
                children,
                {"movement": factory},
                attempts=2,
                backoff=0,
            )

        self.assertEqual(factory.call_count, 2)

    def test_required_helper_keeps_retrying_while_the_game_is_alive(self):
        replacement = fake_child_record(returncode=None, wait_timeouts=0)
        factory = mock.Mock(side_effect=[OSError("cold"), replacement])

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher.time, "sleep"),
        ):
            actual = launcher.start_required_child(
                object(),
                "movement",
                factory,
                backoff=0,
            )

        self.assertIs(actual, replacement)
        self.assertEqual(factory.call_count, 2)

    def test_required_helper_stops_retrying_only_after_the_game_exits(self):
        factory = mock.Mock(side_effect=RuntimeError("still cold"))

        with (
            mock.patch.object(launcher, "game_alive", side_effect=[True, False]),
            mock.patch.object(launcher.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "game exited while helper movement"),
        ):
            launcher.start_required_child(
                object(),
                "movement",
                factory,
                backoff=0,
            )

        factory.assert_called_once_with()

    def test_required_component_retries_a_transient_readiness_timeout(self):
        waiter = mock.Mock(side_effect=[TimeoutError("minimized"), None])
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher.time, "sleep"),
        ):
            launcher.wait_required_component(
                object(),
                "renderer heartbeat",
                waiter,
                backoff=0,
            )

        self.assertEqual(waiter.call_count, 2)

    def test_stale_live_movement_helper_is_debounced_then_replaced(self):
        stalled = fake_child_record(returncode=None, wait_timeouts=0)
        replacement = fake_child_record(returncode=None, wait_timeouts=0)
        children = [stalled]
        factory = mock.Mock(return_value=replacement)
        health_state = {}

        def reap(record):
            record[0].returncode = 0

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child", side_effect=reap) as cleanup,
        ):
            launcher.supervise_children(
                object(),
                children,
                {"movement": factory},
                {"movement": lambda _record: (False, "stale heartbeat")},
                health_state,
                now=10.0,
                stall_grace=5.0,
                restart_live_stalls=True,
            )
            self.assertIs(children[0], stalled)
            cleanup.assert_not_called()

            launcher.supervise_children(
                object(),
                children,
                {"movement": factory},
                {"movement": lambda _record: (False, "stale heartbeat")},
                health_state,
                now=15.1,
                stall_grace=5.0,
                restart_live_stalls=True,
            )

        self.assertIs(children[0], replacement)
        self.assertGreaterEqual(cleanup.call_count, 1)
        factory.assert_called_once_with()
        self.assertNotIn("movement", health_state)

    def test_production_watchdog_never_kills_a_live_helper_from_stale_pulse(self):
        child = fake_child_record(returncode=None, wait_timeouts=0)
        factory = mock.Mock()
        health_state = {}

        with mock.patch.object(launcher, "cleanup_child") as cleanup:
            for now in (10.0, 20.0, 120.0):
                launcher.supervise_children(
                    object(),
                    [child],
                    {"movement": factory},
                    {"movement": lambda _record: (False, "stale heartbeat")},
                    health_state,
                    now=now,
                    stall_grace=5.0,
                )

        cleanup.assert_not_called()
        factory.assert_not_called()
        self.assertIsNone(child[0].returncode)
        self.assertIn("movement", health_state)

    def test_transient_bad_heartbeat_is_forgotten_after_a_healthy_sample(self):
        child = fake_child_record(returncode=None, wait_timeouts=0)
        health_state = {}
        samples = iter(((False, "one bad read"), (True, "fresh")))

        with mock.patch.object(launcher, "game_alive", return_value=True):
            for now in (1.0, 20.0):
                launcher.supervise_children(
                    object(),
                    [child],
                    {"movement": mock.Mock()},
                    {"movement": lambda _record: next(samples)},
                    health_state,
                    now=now,
                    stall_grace=5.0,
                )

        self.assertEqual(health_state, {})
        self.assertIsNone(child[0].returncode)

    def test_watchdog_never_force_restarts_cooperative_only_guard(self):
        child = fake_child_record(returncode=None, wait_timeouts=0)
        child = (
            child[0],
            child[1],
            child[2],
            Path("penelo_guard.out.log"),
            Path("penelo_guard.err.log"),
        )
        health_state = {}
        factory = mock.Mock()

        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "cleanup_child") as cleanup,
        ):
            for now in (0.0, 30.0):
                launcher.supervise_children(
                    object(),
                    [child],
                    {"penelo_guard": factory},
                    {"penelo_guard": lambda _record: (False, "stale")},
                    health_state,
                    now=now,
                    stall_grace=1.0,
                )

        cleanup.assert_not_called()
        factory.assert_not_called()
        self.assertIsNone(child[0].returncode)

    def test_movement_lifetime_marker_is_reset_before_the_child_starts(self):
        base = 0x5BAA0000
        process = FakeMovementProcess(
            base,
            b"\x90" * 5,
            lifetime_ready=False,
        )
        child_record = fake_child_record()
        events = []
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(
                launcher,
                "reset_movement_helper_ready",
                side_effect=lambda *_args: events.append("reset"),
            ),
            mock.patch.object(
                launcher,
                "start_child",
                side_effect=lambda *_args: (events.append("start") or child_record),
            ),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "ready"),
            ),
        ):
            launcher.start_movement_helper(
                process,
                base,
                Path("logs"),
                ["--active"],
                ready_timeout=0.0,
            )

        self.assertEqual(events, ["reset", "start"])

    def test_new_game_seeds_basch_penelo_with_vaan_as_p1(self):
        roster = launcher.coop_roster.CoopRoster(4, 5, True)
        expected = launcher.session_state.SessionIdentity(0, 4, 5)
        with (
            mock.patch.object(
                launcher.session_state,
                "load_config",
                return_value=launcher.session_state.default_config(),
            ),
            mock.patch.object(launcher.session_state, "save_identity") as save_identity,
        ):
            actual = launcher.seed_authored_session_identity(roster)
        self.assertEqual(actual, expected)
        save_identity.assert_called_once_with(expected)

    def test_existing_ashe_p1_survives_basch_penelo_roster_seed(self):
        roster = launcher.coop_roster.CoopRoster(4, 5, True)
        config = {
            "version": 1,
            "players": {
                "p1": {"party_member_id": 1},
                "p2": {"party_member_id": 5},
            },
        }
        expected = launcher.session_state.SessionIdentity(1, 4, 5)
        with (
            mock.patch.object(
                launcher.session_state, "load_config", return_value=config
            ),
            mock.patch.object(launcher.session_state, "save_identity") as save_identity,
        ):
            actual = launcher.seed_authored_session_identity(roster)
        self.assertEqual(actual, expected)
        save_identity.assert_called_once_with(expected)

    def test_existing_p1_collision_with_a_follower_falls_back_to_vaan(self):
        roster = launcher.coop_roster.CoopRoster(4, 5, True)
        for colliding_p1 in (4, 5):
            with self.subTest(colliding_p1=colliding_p1):
                identity = launcher.authored_session_identity(
                    roster,
                    existing_p1=colliding_p1,
                )
                self.assertEqual(
                    identity,
                    launcher.session_state.SessionIdentity(0, 4, 5),
                )

    def test_invalid_existing_p1_falls_back_to_vaan(self):
        roster = launcher.coop_roster.CoopRoster(4, 5, True)
        self.assertEqual(
            launcher.authored_session_identity(roster, existing_p1=13),
            launcher.session_state.SessionIdentity(0, 4, 5),
        )

    def test_authored_two_player_roster_keeps_p3_absent(self):
        roster = launcher.coop_roster.CoopRoster(4, None, True)
        expected = launcher.session_state.SessionIdentity(0, 4, None)
        with (
            mock.patch.object(
                launcher.session_state,
                "load_config",
                return_value=launcher.session_state.default_config(),
            ),
            mock.patch.object(launcher.session_state, "save_identity") as save_identity,
        ):
            actual = launcher.seed_authored_session_identity(roster)
        self.assertEqual(actual, expected)
        save_identity.assert_called_once_with(expected)

    def test_missing_authored_p2_promotes_the_only_companion(self):
        roster = launcher.coop_roster.CoopRoster(None, 5)
        self.assertEqual(
            launcher.authored_session_identity(roster),
            launcher.session_state.SessionIdentity(0, 5, None),
        )

    def test_movement_readiness_survives_transient_gameplay_inactivity(self):
        base = 0x5BAA0000
        patch = b"\xe9\x11\x22\x33\x44"
        process = FakeMovementProcess(
            base,
            patch,
            lifetime_ready=True,
            logical_active=False,
        )
        self.assertTrue(launcher.movement_readiness_snapshot(process, base, patch)[0])
        self.assertNotIn(base + 0x208, process.read_addresses)

    def test_movement_readiness_requires_a_coherent_liveness_heartbeat(self):
        base = 0x5BAA0000
        patch = b"\xe9\x11\x22\x33\x44"
        process = FakeMovementProcess(
            base,
            patch,
            lifetime_ready=False,
            logical_active=True,
        )
        self.assertFalse(launcher.movement_readiness_snapshot(process, base, patch)[0])

    def test_movement_readiness_rejects_stale_foreign_or_torn_liveness(self):
        base = 0x5BAA0000
        patch = b"\xe9\x11\x22\x33\x44"
        cases = (
            ("stale", 2, 4321, 1),
            (
                "foreign-owner",
                2,
                9999,
                max(1, int(time.monotonic() * 1000.0)),
            ),
            (
                "odd-sequence",
                3,
                4321,
                max(1, int(time.monotonic() * 1000.0)),
            ),
        )
        for label, sequence, owner_pid, heartbeat_ms in cases:
            with self.subTest(label=label):
                process = FakeMovementProcess(
                    base,
                    patch,
                    lifetime_ready=True,
                    owner_pid=owner_pid,
                )
                process.liveness[:] = (
                    launcher.coop.MOVEMENT_HELPER_LIVENESS_STRUCT.pack(
                        sequence,
                        launcher.coop.MOVEMENT_HELPER_LIVENESS_MAGIC,
                        launcher.coop.MOVEMENT_HELPER_LIVENESS_VERSION,
                        owner_pid,
                        heartbeat_ms,
                        0,
                        0,
                    )
                )
                ready, diagnostic = launcher.movement_readiness_snapshot(
                    process,
                    base,
                    patch,
                    expected_owner_pid=4321,
                )
                self.assertFalse(ready, diagnostic)

    def test_movement_readiness_requires_the_expected_hook(self):
        base = 0x5BAA0000
        expected = b"\xe9\x11\x22\x33\x44"
        process = FakeMovementProcess(
            base,
            b"\x90" * 5,
            lifetime_ready=True,
            logical_active=True,
        )
        self.assertFalse(
            launcher.movement_readiness_snapshot(process, base, expected)[0]
        )

    def test_startup_expected_hook_matches_runtime_installer_after_relocation(self):
        base = 0x5CAA0000
        previous_base = launcher.coop.COMMAND_BLOCK - 0x200

        class InstallerProcess:
            def __init__(self):
                self.patches = []

            def read(self, address, size):
                if address == launcher.coop.MOVEMENT_HOOK_SITE:
                    return b"\x40\x53\x56\x41\x56"[:size]
                if address == launcher.coop.MOVEMENT_CAVE:
                    return b"\x00" * size
                raise OSError(f"unexpected read at {address:#x}")

            def write(self, _address, _data):
                return None

            def patch_code(self, address, data):
                self.patches.append((address, bytes(data)))

        process = InstallerProcess()
        try:
            launcher.coop.set_cave_base(base)
            launcher.coop.install_movement_override(process)
        finally:
            launcher.coop.set_cave_base(previous_base)

        startup_expected = launcher.expected_movement_hook_patch(base)
        legacy_expected = b"\xe9" + launcher.coop.rel32(
            launcher.coop.MOVEMENT_HOOK_SITE,
            5,
            base + 0x100,
        )
        self.assertEqual(
            process.patches[-1], (launcher.coop.MOVEMENT_HOOK_SITE, startup_expected)
        )
        self.assertNotEqual(startup_expected, legacy_expected)

    def test_reset_clears_a_stale_movement_lifetime_marker(self):
        base = 0x5BAA0000
        patch = b"\xe9\x11\x22\x33\x44"
        process = FakeMovementProcess(
            base,
            patch,
            lifetime_ready=True,
            logical_active=True,
        )
        launcher.reset_movement_helper_ready(process, base)
        self.assertFalse(launcher.movement_readiness_snapshot(process, base, patch)[0])

    def test_movement_helper_success_returns_owned_live_child(self):
        base = 0x5BAA0000
        patch = b"\xe9" + launcher.coop.rel32(
            launcher.coop.MOVEMENT_HOOK_SITE,
            5,
            base + launcher.coop.MOVEMENT_CAVE_OFFSET,
        )
        process = FakeMovementProcess(
            base,
            patch,
            lifetime_ready=False,
        )
        child_record = fake_child_record()
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(
                launcher, "start_child", return_value=child_record
            ) as start_child,
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "ready"),
            ),
        ):
            actual = launcher.start_movement_helper(
                process,
                base,
                Path("logs"),
                ["--active"],
                ready_timeout=0.0,
            )

        self.assertIs(actual, child_record)
        start_child.assert_called_once_with(
            Path("logs"),
            "movement",
            "coop_runtime.py",
            "--active",
        )
        self.assertEqual(
            process.write_addresses,
            [base + launcher.MOVEMENT_LIFETIME_READY_OFFSET],
        )
        self.assertFalse(child_record[1].closed)
        self.assertFalse(child_record[2].closed)

    def test_movement_helper_retries_once_after_first_timeout(self):
        base = 0x5BAA0000
        expected = b"\xe9" + launcher.coop.rel32(
            launcher.coop.MOVEMENT_HOOK_SITE,
            5,
            base + launcher.coop.MOVEMENT_CAVE_OFFSET,
        )
        process = FakeMovementProcess(
            base,
            expected,
            lifetime_ready=False,
        )
        first = fake_child_record()
        second = fake_child_record()
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(
                launcher, "start_child", side_effect=[first, second]
            ) as start_child,
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                side_effect=[(False, "cold"), (True, "ready")],
            ),
            mock.patch("builtins.print") as output,
        ):
            actual = launcher.start_movement_helper(
                process,
                base,
                Path("logs"),
                ["--active", "--camera-focus-v2"],
                ready_timeout=0.0,
            )

        self.assertIs(actual, second)
        self.assertEqual(start_child.call_count, 2)
        self.assertEqual(len(process.write_addresses), 2)
        self.assertEqual(first[0].terminate_calls, 1)
        self.assertEqual(
            first[0].wait_calls,
            [
                launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT,
                launcher.CHILD_TERMINATE_TIMEOUT,
            ],
        )
        self.assertTrue(first[1].closed)
        self.assertTrue(first[2].closed)
        self.assertFalse(second[1].closed)
        self.assertFalse(second[2].closed)
        self.assertTrue(
            any(
                call.args and str(call.args[0]).startswith("CO-OP MOVEMENT TIMEOUT")
                for call in output.call_args_list
            )
        )

    def test_replacement_movement_retry_preserves_resident_remote_state(self):
        base = 0x5BAA0000
        expected = b"\xe9" + launcher.coop.rel32(
            launcher.coop.MOVEMENT_HOOK_SITE,
            5,
            base + launcher.coop.MOVEMENT_CAVE_OFFSET,
        )
        process = FakeMovementProcess(base, expected, lifetime_ready=False)
        first = fake_child_record()
        second = fake_child_record()
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(
                launcher,
                "start_child",
                side_effect=[first, second],
            ),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                side_effect=[(False, "cold"), (True, "ready")],
            ),
            mock.patch.object(launcher, "cleanup_child") as cleanup,
            mock.patch("builtins.print"),
        ):
            actual = launcher.start_movement_helper(
                process,
                base,
                Path("logs"),
                ["--active", "--preserve-resident-state"],
                ready_timeout=0.0,
            )

        self.assertIs(actual, second)
        cleanup.assert_called_once_with(
            first,
            preserve_resident_runtime=True,
        )

    def test_movement_helper_child_exit_is_cleaned_before_retry(self):
        base = 0x5BAA0000
        expected = b"\xe9" + launcher.coop.rel32(
            launcher.coop.MOVEMENT_HOOK_SITE,
            5,
            base + launcher.coop.MOVEMENT_CAVE_OFFSET,
        )
        process = FakeMovementProcess(
            base,
            expected,
            lifetime_ready=False,
        )
        exited = fake_child_record(returncode=23)
        healthy = fake_child_record()
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "start_child", side_effect=[exited, healthy]),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "ready"),
            ) as ready,
            mock.patch("builtins.print"),
        ):
            actual = launcher.start_movement_helper(
                process,
                base,
                Path("logs"),
                ["--active"],
                ready_timeout=0.0,
            )

        self.assertIs(actual, healthy)
        ready.assert_called_once()
        self.assertEqual(exited[0].terminate_calls, 0)
        self.assertTrue(exited[1].closed)
        self.assertTrue(exited[2].closed)

    def test_movement_timeout_diagnostic_reports_hook_and_liveness(self):
        base = 0x5BAA0000
        expected = b"\xe9\x11\x22\x33\x44"
        current = b"\x90\x90\x90\x90\x90"
        process = FakeMovementProcess(
            base,
            current,
            lifetime_ready=True,
        )
        diagnostic = launcher.movement_readiness_snapshot(
            process,
            base,
            expected,
        )[1]
        self.assertIn("base=0x5baa0000", diagnostic)
        self.assertIn("hook_atual=90 90 90 90 90", diagnostic)
        self.assertIn("hook_esperado=e9 11 22 33 44", diagnostic)
        self.assertIn("liveness=ready=1 coherent=1", diagnostic)

    def test_movement_helper_raises_only_after_second_timeout(self):
        base = 0x5BAA0000
        expected = b"\xe9" + launcher.coop.rel32(
            launcher.coop.MOVEMENT_HOOK_SITE,
            5,
            base + launcher.coop.MOVEMENT_CAVE_OFFSET,
        )
        process = FakeMovementProcess(
            base,
            expected,
            lifetime_ready=False,
        )
        attempts = [fake_child_record(), fake_child_record()]
        diagnostic = (
            "base=0x5baa0000 hook_atual=90 90 90 90 90 "
            "hook_esperado=e9 00 00 00 00 lifetime=0"
        )
        with (
            mock.patch.object(launcher, "game_alive", return_value=True),
            mock.patch.object(launcher, "start_child", side_effect=attempts),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(False, diagnostic),
            ),
            mock.patch("builtins.print") as output,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "movimento P2 nao ficou pronto apos 2 tentativas",
            ) as raised:
                launcher.start_movement_helper(
                    process,
                    base,
                    Path("logs"),
                    ["--active"],
                    ready_timeout=0.0,
                )

        self.assertIn("tentativa 1", str(raised.exception))
        self.assertIn("tentativa 2", str(raised.exception))
        self.assertIn(diagnostic, str(raised.exception))
        timeout_lines = [
            str(call.args[0])
            for call in output.call_args_list
            if call.args and str(call.args[0]).startswith("CO-OP MOVEMENT TIMEOUT")
        ]
        self.assertEqual(len(timeout_lines), 2)
        self.assertTrue(all(diagnostic in line for line in timeout_lines))
        self.assertEqual(len(process.write_addresses), 2)
        for child, stdout, stderr, *_paths in attempts:
            self.assertEqual(child.terminate_calls, 1)
            self.assertTrue(stdout.closed)
            self.assertTrue(stderr.closed)

    def test_movement_game_death_is_terminal_and_cleans_current_child(self):
        base = 0x5BAA0000
        process = FakeMovementProcess(
            base,
            b"\x90" * 5,
            lifetime_ready=False,
        )
        child_record = fake_child_record()
        with (
            mock.patch.object(
                launcher,
                "game_alive",
                side_effect=[True, False],
            ),
            mock.patch.object(
                launcher,
                "start_child",
                return_value=child_record,
            ) as start_child,
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
            ) as snapshot,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "jogo encerrou enquanto o movimento P2 iniciava",
            ):
                launcher.start_movement_helper(
                    process,
                    base,
                    Path("logs"),
                    ["--active"],
                    ready_timeout=0.0,
                )

        start_child.assert_called_once()
        snapshot.assert_not_called()
        self.assertEqual(child_record[0].terminate_calls, 1)
        self.assertEqual(
            child_record[0].wait_calls,
            [
                launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT,
                launcher.CHILD_TERMINATE_TIMEOUT,
            ],
        )
        self.assertTrue(child_record[1].closed)
        self.assertTrue(child_record[2].closed)

    def test_movement_does_not_retry_when_cleanup_cannot_reap_child(self):
        base = 0x5BAA0000
        process = FakeMovementProcess(
            base,
            b"\x90" * 5,
            lifetime_ready=False,
        )
        child_record = fake_child_record()
        with (
            mock.patch.object(
                launcher,
                "game_alive",
                return_value=True,
            ),
            mock.patch.object(
                launcher,
                "start_child",
                return_value=child_record,
            ) as start_child,
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(False, "not-ready"),
            ),
            mock.patch.object(
                launcher,
                "cleanup_child",
                side_effect=RuntimeError("nao confirmou encerramento"),
            ),
            mock.patch("builtins.print"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "nao confirmou encerramento",
            ):
                launcher.start_movement_helper(
                    process,
                    base,
                    Path("logs"),
                    ["--active"],
                    ready_timeout=0.0,
                )

        start_child.assert_called_once()
        self.assertEqual(len(process.write_addresses), 1)

    def test_cleanup_child_kills_after_terminate_timeout(self):
        events = []
        child_record = fake_child_record(
            wait_timeouts=2,
            events=events,
        )

        launcher.cleanup_child(child_record)

        self.assertEqual(child_record[0].terminate_calls, 1)
        self.assertEqual(child_record[0].kill_calls, 1)
        self.assertEqual(
            child_record[0].wait_calls,
            [
                launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT,
                launcher.CHILD_TERMINATE_TIMEOUT,
                launcher.CHILD_TERMINATE_TIMEOUT,
            ],
        )
        self.assertEqual(
            events,
            [
                "poll",
                "wait",
                "terminate",
                "wait",
                "kill",
                "wait",
                "close_stdout",
                "close_stderr",
            ],
        )

    def test_cleanup_child_prefers_cooperative_shutdown_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            stdout_path = Path(directory) / "headless_menu.out.log"
            stderr_path = Path(directory) / "headless_menu.err.log"
            shutdown_path = Path(directory) / "headless_menu.shutdown"

            class CooperativeChild(FakeChild):
                def __init__(self):
                    super().__init__()
                    self.saw_shutdown_request = False

                def wait(self, timeout=None):
                    self.wait_calls.append(timeout)
                    self.saw_shutdown_request = shutdown_path.is_file()
                    self.returncode = 0
                    return 0

            child = CooperativeChild()
            record = (
                child,
                FakeStream(),
                FakeStream(),
                stdout_path,
                stderr_path,
            )

            launcher.cleanup_child(record)

            self.assertTrue(child.saw_shutdown_request)
            self.assertEqual(
                child.wait_calls,
                [launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT],
            )
            self.assertEqual(child.terminate_calls, 0)
            self.assertEqual(child.kill_calls, 0)
            self.assertFalse(shutdown_path.exists())

    def test_cleanup_child_uses_the_effective_fallback_shutdown_path(self):
        with tempfile.TemporaryDirectory() as directory:
            primary_stdout_path = Path(directory) / "primary" / "movement.out.log"
            fallback_shutdown_path = (
                Path(directory) / "fallback" / "movement.shutdown"
            )
            fallback_shutdown_path.parent.mkdir(parents=True)

            class CooperativeChild(FakeChild):
                def __init__(self):
                    super().__init__()
                    self.saw_fallback_request = False

                def wait(self, timeout=None):
                    self.wait_calls.append(timeout)
                    self.saw_fallback_request = fallback_shutdown_path.is_file()
                    self.returncode = 0
                    return 0

            child = CooperativeChild()
            child._ff12_shutdown_path = fallback_shutdown_path
            record = (
                child,
                FakeStream(),
                FakeStream(),
                primary_stdout_path,
                primary_stdout_path.with_name("movement.err.log"),
            )

            launcher.cleanup_child(record)

            self.assertTrue(child.saw_fallback_request)
            self.assertEqual(
                child.wait_calls,
                [launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT],
            )
            self.assertFalse(fallback_shutdown_path.exists())
            self.assertFalse(
                primary_stdout_path.with_name("movement.shutdown").exists()
            )

    def test_cleanup_never_force_terminates_party_guard_native_call_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            stdout_path = Path(directory) / "penelo_guard.out.log"
            stderr_path = Path(directory) / "penelo_guard.err.log"
            child = FakeChild(wait_timeouts=1)
            record = (
                child,
                FakeStream(),
                FakeStream(),
                stdout_path,
                stderr_path,
            )

            launcher.cleanup_child(record)

        self.assertEqual(
            child.wait_calls,
            [
                launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT,
                launcher.CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT,
            ],
        )
        self.assertEqual(child.terminate_calls, 0)
        self.assertEqual(child.kill_calls, 0)

    def test_cleanup_children_uses_reverse_order_and_continues_after_error(self):
        events = []

        def cleanup(record):
            events.append(record)
            if record == "second":
                raise RuntimeError("cleanup failed")

        with mock.patch.object(
            launcher,
            "cleanup_child",
            side_effect=cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                launcher.cleanup_children(["first", "second", "third"])

        self.assertEqual(events, ["third", "second", "first"])

    def test_cleanup_children_quiesces_role_producer_before_movement(self):
        def record(role):
            child_record = list(fake_child_record(returncode=0))
            child_record[3] = Path(f"{role}.out.log")
            child_record[4] = Path(f"{role}.err.log")
            return tuple(child_record)

        headless = record("headless_menu")
        story = record("story_party")
        movement = record("movement")
        events = []
        producer_live = True

        def cleanup(child_record):
            nonlocal producer_live
            role = launcher.child_role(child_record)
            events.append(role)
            if role == "headless_menu":
                producer_live = False
            if role == "movement":
                self.assertFalse(
                    producer_live,
                    "movement cleanup raced a still-live role producer",
                )

        with mock.patch.object(launcher, "cleanup_child", side_effect=cleanup):
            launcher.cleanup_children([headless, story, movement])

        self.assertEqual(events, ["headless_menu", "story_party", "movement"])

    def test_cleanup_children_skips_movement_when_headless_quiescence_fails(self):
        def record(role):
            child_record = list(fake_child_record(returncode=0))
            child_record[3] = Path(f"{role}.out.log")
            child_record[4] = Path(f"{role}.err.log")
            return tuple(child_record)

        headless = record("headless_menu")
        story = record("story_party")
        movement = record("movement")
        cleaned = []
        detached = []

        def cleanup(child_record):
            role = launcher.child_role(child_record)
            cleaned.append(role)
            if role == "headless_menu":
                raise RuntimeError("producer quiescence was not confirmed")

        with (
            mock.patch.object(launcher, "cleanup_child", side_effect=cleanup),
            mock.patch.object(
                launcher,
                "detach_child",
                side_effect=lambda child_record: detached.append(
                    launcher.child_role(child_record)
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "quiescence"):
                launcher.cleanup_children([headless, story, movement])

        self.assertEqual(cleaned, ["headless_menu", "story_party"])
        self.assertEqual(detached, ["movement"])

    def test_external_headless_detaches_and_cannot_authorize_movement_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            headless = launcher.external_child_record(
                Path(directory), 77, "headless_menu"
            )
            movement_record = list(fake_child_record(returncode=None))
            movement_record[3] = Path(directory) / "movement.out.log"
            movement_record[4] = Path(directory) / "movement.err.log"
            movement = tuple(movement_record)

            with mock.patch.object(launcher, "cleanup_child") as cleanup:
                launcher.cleanup_children([headless, movement])

        cleanup.assert_not_called()
        self.assertTrue(movement[1].closed)
        self.assertTrue(movement[2].closed)
        self.assertEqual(movement[0].terminate_calls, 0)
        self.assertEqual(movement[0].kill_calls, 0)

    def test_start_child_closes_both_logs_when_spawn_fails(self):
        stdout = FakeStream()
        stderr = FakeStream()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                Path,
                "open",
                side_effect=[stdout, stderr],
            ),
            mock.patch.object(
                launcher.subprocess,
                "Popen",
                side_effect=OSError("spawn failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                launcher.start_child(
                    Path(directory),
                    "movement",
                    "coop_runtime.py",
                )

        self.assertTrue(stdout.closed)
        self.assertTrue(stderr.closed)

    def test_stream_close_failures_do_not_mask_the_spawn_failure(self):
        class BrokenCloseStream(FakeStream):
            def close(self):
                self.closed = True
                raise RuntimeError("diagnostic stream close failed")

        stdout = BrokenCloseStream()
        stderr = BrokenCloseStream()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "open", side_effect=[stdout, stderr]),
            mock.patch.object(
                launcher.subprocess,
                "Popen",
                side_effect=OSError("spawn failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                launcher.start_child(
                    Path(directory),
                    "movement",
                    "coop_runtime.py",
                )

        self.assertTrue(stdout.closed)
        self.assertTrue(stderr.closed)

    def test_start_child_falls_back_without_losing_readiness_logs(self):
        primary_stdout = FakeStream()
        fallback_stdout = FakeStream()
        fallback_stderr = FakeStream()
        child = FakeChild()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                launcher.tempfile,
                "gettempdir",
                return_value=directory,
            ),
            mock.patch.object(
                Path,
                "open",
                side_effect=[
                    primary_stdout,
                    OSError("stderr failed"),
                    fallback_stdout,
                    fallback_stderr,
                ],
            ),
            mock.patch.object(
                launcher.subprocess,
                "Popen",
                return_value=child,
            ),
        ):
            record = launcher.start_child(
                Path(directory) / "primary" / "4242",
                "movement",
                "coop_runtime.py",
            )

        self.assertTrue(primary_stdout.closed)
        self.assertIs(record[1], fallback_stdout)
        self.assertIs(record[2], fallback_stderr)
        expected_directory = (
            Path(directory)
            / "Ivalice Together"
            / "runtime_logs_fallback"
            / "4242"
        )
        self.assertEqual(record[3], expected_directory / "movement.out.log")
        self.assertEqual(record[4], expected_directory / "movement.err.log")
        self.assertEqual(
            child._ff12_shutdown_path,
            expected_directory / "movement.shutdown",
        )

    def test_start_child_falls_back_after_non_os_stream_failure(self):
        class BrokenCloseStream(FakeStream):
            def close(self):
                self.closed = True
                raise RuntimeError("primary stream cannot close cleanly")

        primary_stdout = BrokenCloseStream()
        fallback_stdout = FakeStream()
        fallback_stderr = FakeStream()
        child = FakeChild()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                launcher.tempfile,
                "gettempdir",
                return_value=directory,
            ),
            mock.patch.object(
                Path,
                "open",
                side_effect=[
                    primary_stdout,
                    RuntimeError("primary stderr filter failed"),
                    fallback_stdout,
                    fallback_stderr,
                ],
            ),
            mock.patch.object(launcher.subprocess, "Popen", return_value=child),
        ):
            record = launcher.start_child(
                Path(directory) / "primary" / "4242",
                "story_party",
                "story_party_patch.py",
            )

        self.assertTrue(primary_stdout.closed)
        self.assertIs(record[1], fallback_stdout)
        self.assertIs(record[2], fallback_stderr)

    def test_accepts_any_two_members_shared_by_live_layouts(self):
        process = FakeProcess((0, 5, 0xFFFF), (5, 0, 0xFFFF))
        self.assertTrue(launcher.party_layouts_ready(process))

    def test_accepts_one_stable_story_leader(self):
        process = FakeProcess((0,), (0,))
        self.assertTrue(launcher.party_layouts_ready(process))

    def test_does_not_require_character_saved_in_previous_session(self):
        process = FakeProcess((0, 5), (0, 5))
        self.assertTrue(launcher.party_layouts_ready(process))

    def test_rejects_transient_or_disagreeing_layouts(self):
        self.assertFalse(launcher.party_layouts_ready(FakeProcess((0,), (0, 5))))
        self.assertFalse(launcher.party_layouts_ready(FakeProcess((0, 5), (2, 4))))

    def test_final_commit_requires_every_configured_role_in_both_layouts(self):
        identity = launcher.session_state.SessionIdentity(0, 4, 5)
        current = (0, 4, 5, 0xFFFF) + (0xFFFF,) * 5
        ui = (4, 5, 0, 0xFFFF) + (0xFFFF,) * 5
        role_members, role_slots = launcher.configured_role_slots(
            identity,
            current,
            ui,
            leader=0,
        )
        self.assertEqual(role_members, {"p1": 0, "p2": 4, "p3": 5})
        self.assertEqual(role_slots, {"p1": 0, "p2": 1, "p3": 2})

        missing_p3 = (0, 4, 0xFFFF, 0xFFFF) + (0xFFFF,) * 5
        self.assertIsNone(
            launcher.configured_role_slots(
                identity,
                missing_p3,
                missing_p3,
                leader=0,
            )
        )

    def test_special_story_leader_can_start_with_latent_followers(self):
        identity = launcher.session_state.SessionIdentity(5, 4, 3)
        reks_only = (6, 0xFFFF, 0xFFFF, 0xFFFF) + (0xFFFF,) * 5
        role_members, role_slots = launcher.configured_role_slots(
            identity,
            reks_only,
            reks_only,
            leader=6,
            allow_latent_followers=True,
        )
        self.assertEqual(role_members, {"p1": 6, "p2": 4, "p3": 3})
        self.assertEqual(role_slots, {"p1": 0})

    def test_final_commit_uses_the_stable_live_roles_during_story_roster_changes(self):
        configured = launcher.session_state.SessionIdentity(1, 4, 3)
        live_pair = (0, 5, 0xFFFF, 0xFFFF) + (0xFFFF,) * 5

        role_members, role_slots = launcher.runtime_role_slots(
            configured,
            live_pair,
            live_pair,
            leader=0,
        )

        self.assertEqual(role_members, {"p1": 0, "p2": 5})
        self.assertEqual(role_slots, {"p1": 0, "p2": 1})

    def test_main_party_solo_exposes_only_the_live_player_role(self):
        configured = launcher.session_state.SessionIdentity(5, 4, 3)
        penelo_only = (5, 0xFFFF, 0xFFFF, 0xFFFF) + (0xFFFF,) * 5

        role_members, role_slots = launcher.runtime_role_slots(
            configured,
            penelo_only,
            penelo_only,
            leader=5,
        )

        self.assertEqual(role_members, {"p1": 5})
        self.assertEqual(role_slots, {"p1": 0})

    def test_final_commit_expects_basch_emerald_not_fallback_gold(self):
        selection = launcher.menu_themes.ThemeSelection(
            player_1="blue",
            player_2="white",
            player_3="gold",
            character_overrides=(
                (0, "blue"),
                (4, "emerald"),
                (5, "gold"),
            ),
            assignment_mode="character",
        )
        palettes = launcher.expected_role_theme_palettes(
            {"p1": 0, "p2": 4, "p3": 5},
            selection,
        )
        self.assertEqual(
            struct.unpack("<8i", palettes["p2"]),
            launcher.menu_themes.gauge_palette("emerald"),
        )
        self.assertNotEqual(
            palettes["p2"],
            struct.pack(
                "<8i",
                *launcher.menu_themes.gauge_palette("gold"),
            ),
        )
        self.assertEqual(
            struct.unpack("<8i", palettes["p3"]),
            launcher.menu_themes.gauge_palette("gold"),
        )

    def test_absent_saved_p2_falls_back_to_an_available_member(self):
        configured = launcher.session_state.SessionIdentity(0, 4)
        actual = launcher.available_identity(
            configured,
            (0, 5, 0xFFFF),
            (0, 5, 0xFFFF),
            leader=0,
        )
        self.assertEqual(actual, launcher.session_state.SessionIdentity(0, 5))

    def test_present_roles_are_preserved_across_party_reorder(self):
        configured = launcher.session_state.SessionIdentity(0, 4)
        actual = launcher.available_identity(
            configured,
            (4, 0, 5),
            (0, 5, 4),
            leader=0,
        )
        self.assertEqual(actual, configured)

    def test_startup_preserves_p2_when_dead_p1_promoted_that_actor(self):
        configured = launcher.session_state.SessionIdentity(4, 2, 1)
        actual = launcher.startup_identity(
            configured,
            (4, 2, 1, 0xFFFF),
            (4, 2, 1, 0xFFFF),
            leader=2,
            p1_alive=False,
        )
        self.assertEqual(actual, configured)

    def test_startup_preserves_p3_when_dead_p1_promoted_that_actor(self):
        configured = launcher.session_state.SessionIdentity(4, 2, 1)
        actual = launcher.startup_identity(
            configured,
            (4, 2, 1, 0xFFFF),
            (4, 2, 1, 0xFFFF),
            leader=1,
            p1_alive=False,
        )
        self.assertEqual(actual, configured)

    def test_startup_still_applies_an_intentional_live_leader_change(self):
        configured = launcher.session_state.SessionIdentity(4, 2, 1)
        actual = launcher.startup_identity(
            configured,
            (4, 2, 1, 0xFFFF),
            (4, 2, 1, 0xFFFF),
            leader=2,
            p1_alive=True,
        )
        self.assertEqual(
            actual,
            launcher.session_state.SessionIdentity(2, 4, 1),
        )

    def test_startup_never_persists_a_coherent_guest_leader(self):
        configured = launcher.session_state.SessionIdentity(4, 2, 1)
        actual = launcher.startup_identity(
            configured,
            (13, 2, 1, 0xFFFF),
            (13, 2, 1, 0xFFFF),
            leader=13,
            p1_alive=None,
        )
        self.assertEqual(actual, configured)

    def test_launcher_never_rewrites_configured_player_gambits(self):
        source = (SOURCE / "start_headless_coop.py").read_text(encoding="utf-8")
        self.assertNotIn("configure_member_gambit", source)
        self.assertNotIn("Foe lowest HP", source)


if __name__ == "__main__":
    unittest.main()
