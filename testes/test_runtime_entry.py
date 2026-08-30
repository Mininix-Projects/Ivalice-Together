import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import runtime_entry
import runtime_paths

SOURCE = Path(__file__).resolve().parents[1] / "source"


class RuntimeEntryTests(unittest.TestCase):
    def test_redirected_runtime_stream_failures_are_nonfatal(self):
        class BrokenStream:
            def write(self, _material):
                raise OSError("log volume unavailable")

            def flush(self):
                raise ValueError("stream closed")

        broken_stdout = BrokenStream()
        broken_stderr = BrokenStream()
        with (
            mock.patch.dict(
                runtime_entry.os.environ,
                {
                    "FF12_COOP_STDOUT_LOG": "worker.out.log",
                    "FF12_COOP_STDERR_LOG": "worker.err.log",
                },
                clear=False,
            ),
            mock.patch.object(runtime_entry.sys, "stdout", broken_stdout),
            mock.patch.object(runtime_entry.sys, "stderr", broken_stderr),
        ):
            runtime_entry._repair_windowed_streams(Path("unused"))
            self.assertIsInstance(
                runtime_entry.sys.stdout,
                runtime_entry._BestEffortStream,
            )
            self.assertIsInstance(
                runtime_entry.sys.stderr,
                runtime_entry._BestEffortStream,
            )
            self.assertEqual(runtime_entry.sys.stdout.write("still running"), 13)
            runtime_entry.sys.stdout.flush()
            self.assertEqual(runtime_entry.sys.stderr.write("diagnostic"), 10)
            runtime_entry.sys.stderr.flush()

    def test_best_effort_stream_rejects_an_invalid_write_count_nonfatally(self):
        class InvalidCountStream:
            def write(self, _material):
                return object()

            def flush(self):
                raise RuntimeError("diagnostic backend failed")

        stream = runtime_entry._BestEffortStream(InvalidCountStream())

        self.assertEqual(stream.write("still running"), len("still running"))
        stream.flush()

    def test_runtime_directory_failure_does_not_block_environment_preparation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(runtime_entry.os.environ, {}, clear=True),
            mock.patch.object(
                runtime_entry.runtime_paths,
                "resolve_game_root",
                return_value=Path(directory) / "game",
            ),
            mock.patch.object(
                runtime_entry.runtime_paths,
                "resolve_config_directory",
                return_value=Path(directory) / "config",
            ),
            mock.patch.object(
                runtime_entry.runtime_paths,
                "resolve_runtime_directory",
                return_value=Path(directory) / "runtime",
            ),
            mock.patch.object(
                runtime_entry.Path,
                "mkdir",
                autospec=True,
                side_effect=(None, OSError("runtime storage unavailable")),
            ) as mkdir,
        ):
            result = runtime_entry._prepare_environment()

        self.assertEqual(result, Path(directory) / "config")
        self.assertEqual(mkdir.call_count, 2)

    def test_autostart_log_failure_does_not_block_exact_pid_configuration(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(runtime_entry.os.environ, {}, clear=True),
            mock.patch.object(
                runtime_entry.Path,
                "mkdir",
                autospec=True,
                side_effect=OSError("sharing violation"),
            ),
        ):
            runtime_directory = Path(directory)
            result = runtime_entry._configure_autostart_environment(
                ("4242",),
                runtime_directory,
            )
            self.assertEqual(result, 4242)
            self.assertEqual(
                runtime_entry.os.environ[runtime_paths.GAME_PID_ENV],
                "4242",
            )
            self.assertEqual(
                runtime_entry.os.environ["FF12_COOP_STDOUT_LOG"],
                str(runtime_directory / "runtime_logs_auto" / "autostart.log"),
            )

    def test_private_runtime_uses_per_monitor_dpi_coordinates(self):
        user32 = SimpleNamespace(
            SetProcessDpiAwarenessContext=mock.Mock(return_value=1),
            SetProcessDPIAware=mock.Mock(),
        )
        fake_ctypes = SimpleNamespace(
            windll=SimpleNamespace(user32=user32),
            c_void_p=lambda value: value,
        )
        with (
            mock.patch.object(runtime_entry.os, "name", "nt"),
            mock.patch.object(runtime_entry, "ct", fake_ctypes),
        ):
            runtime_entry.enable_windows_dpi_awareness()

        user32.SetProcessDpiAwarenessContext.assert_called_once_with(-4)
        user32.SetProcessDPIAware.assert_not_called()

    @staticmethod
    def _write_pe(path: Path, machine: int, *, signature: bytes = b"PE\0\0"):
        payload = bytearray(0x86)
        payload[:2] = b"MZ"
        payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
        payload[0x80:0x84] = signature
        payload[0x84:0x86] = int(machine).to_bytes(2, "little")
        path.write_bytes(payload)

    def test_pe_machine_reads_an_x64_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "runtime.dll"
            self._write_pe(image, runtime_entry.IMAGE_FILE_MACHINE_AMD64)

            self.assertEqual(
                runtime_entry.pe_machine(image),
                runtime_entry.IMAGE_FILE_MACHINE_AMD64,
            )

    def test_pe_machine_rejects_invalid_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_dos = root / "invalid-dos.dll"
            invalid_dos.write_bytes(b"not a PE file")
            invalid_signature = root / "invalid-signature.dll"
            self._write_pe(invalid_signature, 0x8664, signature=b"PX\0\0")

            for image in (invalid_dos, invalid_signature):
                with self.subTest(image=image), self.assertRaises(RuntimeError):
                    runtime_entry.pe_machine(image)

    def test_prepare_environment_honors_an_isolated_data_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                runtime_entry.os.environ,
                {runtime_paths.DATA_DIRECTORY_ENV: directory},
                clear=True,
            ),
        ):
            result = runtime_entry._prepare_environment()

            self.assertEqual(result, Path(directory).resolve())
            self.assertTrue(result.is_dir())
            self.assertIn(
                runtime_paths.GAME_ROOT_ENV,
                runtime_entry.os.environ,
            )

    def test_prepare_environment_publishes_the_resolved_game_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "game"
            data = root / "coop"
            with (
                mock.patch.dict(runtime_entry.os.environ, {}, clear=True),
                mock.patch.object(
                    runtime_entry.runtime_paths,
                    "resolve_game_root",
                    return_value=root,
                ) as resolve_game_root,
                mock.patch.object(
                    runtime_entry.runtime_paths,
                    "resolve_config_directory",
                    return_value=data,
                ),
            ):
                result = runtime_entry._prepare_environment()
                published_root = runtime_entry.os.environ[
                    runtime_paths.GAME_ROOT_ENV
                ]

            self.assertEqual(result, data)
            self.assertEqual(published_root, str(root))
            resolve_game_root.assert_called_once_with(runtime_entry.__file__)

    def test_none_worker_result_is_a_success_exit_code(self):
        self.assertEqual(runtime_entry.exit_code(None), 0)

    def test_safe_dispatch_returns_dispatch_result(self):
        arguments = [runtime_entry.RUNTIME_SWITCH, "movement"]
        with mock.patch.object(runtime_entry, "dispatch", return_value=7) as dispatch:
            result = runtime_entry.safe_dispatch(arguments)

        self.assertEqual(result, 7)
        dispatch.assert_called_once_with(arguments)

    def test_safe_dispatch_converts_exception_to_failure_exit_code(self):
        arguments = [runtime_entry.RUNTIME_SWITCH, "movement"]
        with (
            mock.patch.object(
                runtime_entry,
                "dispatch",
                side_effect=RuntimeError("worker failed"),
            ),
            mock.patch.object(runtime_entry.traceback, "print_exc") as print_exc,
        ):
            result = runtime_entry.safe_dispatch(arguments)

        self.assertEqual(result, 1)
        print_exc.assert_called_once_with(file=sys.stderr)

    def test_frozen_worker_reexecutes_same_one_file_executable(self):
        command = runtime_entry.worker_command(
            "movement",
            Path("ignored.py"),
            "--active",
            executable=Path("C:/Game/IvaliceTogetherConfig.exe"),
            frozen=True,
        )
        self.assertEqual(
            command,
            [
                "C:\\Game\\IvaliceTogetherConfig.exe",
                runtime_entry.RUNTIME_SWITCH,
                "movement",
                "--active",
            ],
        )

    def test_source_worker_uses_current_interpreter_and_script(self):
        command = runtime_entry.worker_command(
            "movement",
            Path("worker.py"),
            "--active",
            executable=Path(sys.executable),
            frozen=False,
        )
        self.assertEqual(command[0], str(Path(sys.executable)))
        self.assertEqual(command[1:3], ["-u", "worker.py"])
        self.assertEqual(command[3:], ["--active"])

    def test_private_switch_is_not_confused_with_public_arguments(self):
        self.assertTrue(
            runtime_entry.is_runtime_invocation(
                [runtime_entry.RUNTIME_SWITCH, "selftest"]
            )
        )
        self.assertFalse(runtime_entry.is_runtime_invocation(["--render-preview"]))

    def test_selftest_materializes_and_reports_the_camera_filter(self):
        source = Path(runtime_entry.__file__).read_text(encoding="utf-8")

        self.assertIn('required["camera_filter"]', source)
        self.assertIn('"camera_filter": str(camera_copy)', source)
        self.assertIn(
            'RuntimeError("camera filter was not materialized persistently")',
            source,
        )

    def test_autostart_pins_pid_and_uses_one_stable_log(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                runtime_entry.os.environ,
                {},
                clear=True,
            ),
        ):
            data_directory = Path(directory)
            pid = runtime_entry._configure_autostart_environment(
                ["4242"],
                data_directory,
            )

            expected_log = data_directory / "runtime_logs_auto" / "autostart.log"
            self.assertEqual(pid, 4242)
            self.assertEqual(
                runtime_entry.os.environ[runtime_paths.GAME_PID_ENV],
                "4242",
            )
            self.assertEqual(
                runtime_entry.os.environ["FF12_COOP_STDOUT_LOG"],
                str(expected_log),
            )
            self.assertTrue(expected_log.is_file())

    def test_autostart_rejects_missing_invalid_and_nonpositive_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            data_directory = Path(directory)
            for arguments in ((), ("not-a-pid",), ("0",), ("1", "2")):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    runtime_entry._configure_autostart_environment(
                        arguments,
                        data_directory,
                    )

    def test_windowed_startup_failure_gets_one_stable_emergency_log(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                runtime_entry.os.environ,
                {runtime_paths.RUNTIME_DIRECTORY_ENV: directory},
                clear=True,
            ),
            mock.patch.object(
                runtime_entry,
                "dispatch",
                side_effect=RuntimeError("startup failed before streams"),
            ),
            mock.patch.object(runtime_entry.sys, "stderr", None),
        ):
            result = runtime_entry.safe_dispatch(
                [runtime_entry.RUNTIME_SWITCH, "autostart", "4242"]
            )
            log = Path(directory) / "runtime_bootstrap_error.log"

            self.assertEqual(result, 1)
            self.assertTrue(log.is_file())
            self.assertIn("startup failed before streams", log.read_text("utf-8"))

    def test_windowed_startup_failure_survives_emergency_log_failure(self):
        with (
            mock.patch.object(
                runtime_entry,
                "dispatch",
                side_effect=RuntimeError("startup failed before streams"),
            ),
            mock.patch.object(runtime_entry.sys, "stderr", None),
            mock.patch.object(
                runtime_entry,
                "_write_emergency_traceback",
                side_effect=RuntimeError("every log backend failed"),
            ),
        ):
            result = runtime_entry.safe_dispatch(
                [runtime_entry.RUNTIME_SWITCH, "autostart", "4242"]
            )

        self.assertEqual(result, 1)

    def test_autostart_validates_exact_process_and_runs_existing_bootstrap(self):
        root = Path("C:/Portable/FF12")
        data_directory = root / "coop"
        events = []
        game_process = SimpleNamespace(
            validate_process_id=lambda pid, path: events.append(("validate", pid, path))
        )
        launcher_core = SimpleNamespace(
            GAME_EXECUTABLE="FFXII_TZA.exe",
            runtime_mutex_active=lambda pid: False,
            ensure_runtime_storage=lambda path: events.append(("storage", path)),
        )
        start_headless_coop = SimpleNamespace(
            main=lambda: events.append(("bootstrap",)) or 0
        )
        with (
            mock.patch.object(
                runtime_entry.runtime_paths,
                "resolve_game_root",
                return_value=root,
            ),
            mock.patch.dict(
                sys.modules,
                {
                    "game_process": game_process,
                    "launcher_core": launcher_core,
                    "start_headless_coop": start_headless_coop,
                },
            ),
        ):
            result = runtime_entry._run_autostart(4242, data_directory)

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                ("validate", 4242, root / "x64" / "FFXII_TZA.exe"),
                ("storage", data_directory),
                ("bootstrap",),
            ],
        )

    def test_autostart_does_not_duplicate_an_active_runtime(self):
        root = Path("C:/Portable/FF12")
        launcher_core = SimpleNamespace(
            GAME_EXECUTABLE="FFXII_TZA.exe",
            runtime_mutex_active=lambda pid: True,
            ensure_runtime_storage=mock.Mock(),
            validate_supported_game_installation=mock.Mock(),
            validate_loader_runtime_configuration=mock.Mock(),
        )
        start_headless_coop = SimpleNamespace(main=mock.Mock())
        with (
            mock.patch.object(
                runtime_entry.runtime_paths,
                "resolve_game_root",
                return_value=root,
            ),
            mock.patch.dict(
                sys.modules,
                {
                    "game_process": SimpleNamespace(validate_process_id=mock.Mock()),
                    "launcher_core": launcher_core,
                    "start_headless_coop": start_headless_coop,
                },
            ),
        ):
            result = runtime_entry._run_autostart(4242, root / "coop")

        self.assertEqual(result, 0)
        launcher_core.ensure_runtime_storage.assert_not_called()
        launcher_core.validate_supported_game_installation.assert_not_called()
        launcher_core.validate_loader_runtime_configuration.assert_not_called()
        start_headless_coop.main.assert_not_called()

    def test_autostart_silently_ignores_a_replaced_bootstrap_pid(self):
        class ReplacedBootstrap(RuntimeError):
            pass

        root = Path("C:/Portable/FF12")
        launcher_core = SimpleNamespace(
            GAME_EXECUTABLE="FFXII_TZA.exe",
            runtime_mutex_active=lambda pid: False,
            ensure_runtime_storage=mock.Mock(),
            validate_supported_game_installation=mock.Mock(),
            validate_loader_runtime_configuration=mock.Mock(),
        )
        start_headless_coop = SimpleNamespace(
            GameExitedBeforeStartup=ReplacedBootstrap,
            main=mock.Mock(side_effect=ReplacedBootstrap("transient process")),
        )
        with (
            mock.patch.object(
                runtime_entry.runtime_paths,
                "resolve_game_root",
                return_value=root,
            ),
            mock.patch.dict(
                sys.modules,
                {
                    "game_process": SimpleNamespace(validate_process_id=mock.Mock()),
                    "launcher_core": launcher_core,
                    "start_headless_coop": start_headless_coop,
                },
            ),
        ):
            result = runtime_entry._run_autostart(4242, root / "coop")

        self.assertEqual(result, 0)
        start_headless_coop.main.assert_called_once_with()


class FrozenLauncherCrashSafetyTests(unittest.TestCase):
    def test_enemy_variant_experiment_is_not_in_the_production_launcher(self):
        root = SOURCE
        runtime_source = (root / "runtime_entry.py").read_text(encoding="utf-8")
        bootstrap_source = (root / "start_headless_coop.py").read_text(encoding="utf-8")
        spec_source = (root / "FF12CoopLauncher.spec").read_text(encoding="utf-8")
        for source in (runtime_source, bootstrap_source, spec_source):
            self.assertNotIn("enemy_variant_runtime", source)
            self.assertNotIn("assets/enemy_variants", source)
            self.assertNotIn("yellow_orange_blue", source)

    def test_private_launcher_runtime_uses_safe_dispatch(self):
        launcher_source = (
            SOURCE / "coop_launcher.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "raise SystemExit(runtime_entry.safe_dispatch(sys.argv[1:]))",
            launcher_source,
        )
        self.assertNotIn(
            "raise SystemExit(runtime_entry.dispatch(sys.argv[1:]))",
            launcher_source,
        )

    def test_frozen_runtime_audits_the_helper_lease_module(self):
        runtime_source = (SOURCE / "runtime_entry.py").read_text(encoding="utf-8")
        builder_source = (
            SOURCE.parent / "ferramentas" / "build_public_release.py"
        ).read_text(encoding="utf-8")

        self.assertIn("import helper_lease", runtime_source)
        self.assertRegex(
            builder_source,
            r'EMBEDDED_SOURCE_CONTRACT\s*=\s*\([\s\S]*?"helper_lease"',
        )
        self.assertIn('"helper_leases": sorted(helper_lease_names)', runtime_source)

    def test_frozen_runtime_explicitly_audits_target_circle_stack(self):
        builder_source = (
            SOURCE.parent / "ferramentas" / "build_public_release.py"
        ).read_text(encoding="utf-8")

        for module_name in (
            "native_target_circle_controller",
            "native_target_circle_router",
            "native_ui_tick_mux",
        ):
            self.assertRegex(
                builder_source,
                rf'EMBEDDED_SOURCE_CONTRACT\s*=\s*\([\s\S]*?"{module_name}"',
            )

    def test_frozen_launcher_disables_pyinstaller_traceback_dialog(self):
        spec_source = (
            SOURCE / "FF12CoopLauncher.spec"
        ).read_text(encoding="utf-8")
        self.assertRegex(
            spec_source,
            r"disable_windowed_traceback\s*=\s*True",
        )

    def test_public_coop_does_not_enable_or_bundle_invoke(self):
        root = SOURCE
        launcher_source = (root / "coop_launcher.py").read_text(encoding="utf-8")
        spec_source = (root / "FF12CoopLauncher.spec").read_text(encoding="utf-8")
        self.assertNotIn(
            'os.environ.setdefault("FF12_INVOKE_ENABLED", "1")',
            launcher_source,
        )
        self.assertNotIn("invoke", spec_source.casefold())
        self.assertNotIn("invoke", launcher_source.casefold())


if __name__ == "__main__":
    unittest.main()
