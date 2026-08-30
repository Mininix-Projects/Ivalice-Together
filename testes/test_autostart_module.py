import unittest
from pathlib import Path


class NativeAutostartContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).resolve().parents[1]
            / "fontes_nativos"
            / "ivalice_together_autostart.cpp"
        ).read_text(encoding="utf-8")

    def test_module_accepts_a_virtualized_module_tree_for_the_verified_host(self):
        self.assertRegex(
            self.source,
            r"GetModuleFileNameW\(nullptr,\s*host_path",
        )
        self.assertIn(
            'equals_ignore_case(filename_part(host_path), L"x64")', self.source
        )
        self.assertIn(
            'equals_ignore_case(filename_part(module_root), L"modules")', self.source
        )
        self.assertIn(
            'equals_ignore_case(filename_part(module_root), L"x64")', self.source
        )
        self.assertIn(
            "equals_ignore_case(filename_part(module_path), kAutostartModule)",
            self.source,
        )
        self.assertNotIn("equals_ignore_case(module_root, host_path)", self.source)
        self.assertNotIn("steamapps", self.source.casefold())

    def test_runtime_receives_game_root_and_package_scoped_config(self):
        self.assertIn('L"FF12_COOP_GAME_ROOT"', self.source)
        self.assertIn('L"FF12_COOP_DATA_DIR"', self.source)
        environment = self.source.index(
            "SetEnvironmentVariableW(kGameRootEnvironment, host_path)"
        )
        process_start = self.source.index("CreateProcessW", environment)
        self.assertLess(environment, process_start)
        self.assertIn("resolve_package_paths(", self.source)
        self.assertIn('L"coop"', self.source)
        self.assertNotIn("resolve_launcher_path(", self.source)
        self.assertNotIn("kLauncherExecutable", self.source)

    def test_runtime_resolution_prefers_the_module_package_tree(self):
        resolver = self.source.split("bool resolve_package_paths(", 1)[1].split(
            "bool environment_entry_has_key(", 1
        )[0]
        module_lookup = resolver.index("module_x64_root")
        virtual_lookup = resolver.index('game_root, L"x64"')
        self.assertLess(module_lookup, virtual_lookup)
        self.assertIn("resolve_runtime_in_root", resolver)
        self.assertNotIn("IvaliceTogetherConfig", resolver)

    def test_module_only_starts_the_existing_late_runtime(self):
        self.assertIn("--ff12-coop-runtime autostart", self.source)
        self.assertIn("CreateProcessW", self.source)
        self.assertIn('kRuntimeDirectory[] = L"IvaliceTogetherRuntime"', self.source)
        self.assertIn('kRuntimeExecutable[] = L"IvaliceTogetherRuntime.exe"', self.source)
        self.assertIn("resolve_package_paths(", self.source)
        self.assertNotIn("copy_text(runtime_path, kPathCapacity, launcher_path)", self.source)
        self.assertNotIn("D3D11CreateDevice", self.source)
        self.assertNotIn("WriteProcessMemory", self.source)

    def test_worker_reserves_and_publishes_all_three_rel32_caves_in_process(self):
        for value in (
            "FF12_COOP_CAVE_BASE",
            "FF12_COOP_DUAL_CAVE_BASE",
            "FF12_COOP_DAMAGE_CAVE_BASE",
            "FF12COOP",
            "FF12INT2",
            "FF12DMG1",
        ):
            self.assertIn(value, self.source)
        reserve = self.source.index("reserve_and_publish_runtime_caves(host_path)")
        process_start = self.source.index("CreateProcessW", reserve)
        self.assertLess(reserve, process_start)
        self.assertIn("VirtualQuery(", self.source)
        self.assertIn("VirtualAlloc(", self.source)
        self.assertIn("SetEnvironmentVariableW", self.source)
        self.assertIn("memory_has_magic(", self.source)
        magic_probe = self.source.split("bool memory_has_magic(", 1)[1].split(
            "void* find_rel32_cave(", 1
        )[0]
        self.assertIn("ReadProcessMemory(", magic_probe)
        self.assertNotIn("memcmp(information.BaseAddress", self.source)
        self.assertIn(
            "caves_ready = reserve_and_publish_runtime_caves(host_path)",
            self.source,
        )

    def test_launcher_injection_reserves_caves_without_starting_a_second_runtime(self):
        reserve = self.source.index("reserve_and_publish_runtime_caves(host_path)")
        mode_gate = self.source.index("if (!module_loader_install)", reserve)
        process_start = self.source.index("CreateProcessW", mode_gate)
        self.assertLess(reserve, mode_gate)
        self.assertLess(mode_gate, process_start)
        self.assertIn("find_rel32_cave", self.source)

    def test_runtime_is_an_independent_pyinstaller_instance(self):
        self.assertIn(
            'L"PYINSTALLER_RESET_ENVIRONMENT=1"',
            self.source,
        )
        self.assertIn("build_runtime_environment()", self.source)
        self.assertIn("CREATE_UNICODE_ENVIRONMENT", self.source)
        self.assertRegex(
            self.source,
            r"CREATE_NO_WINDOW\s*\|\s*CREATE_UNICODE_ENVIRONMENT,\s*"
            r"runtime_environment,",
        )

    def test_external_runtime_starts_without_a_duplicate_native_render_wait(self):
        reserve = self.source.index("reserve_and_publish_runtime_caves(host_path)")
        process_start = self.source.index("CreateProcessW")
        self.assertLess(reserve, process_start)
        self.assertNotIn("wait_for_stable_game", self.source)
        self.assertNotIn("kStartupStableMilliseconds", self.source)
        self.assertNotIn("GetModuleHandleW(L\"d3d11.dll\")", self.source)

    def test_dllmain_keeps_work_outside_the_loader_lock(self):
        dllmain = self.source.split('extern "C" BOOL WINAPI DllMain', 1)[1]
        self.assertIn("DisableThreadLibraryCalls", dllmain)
        self.assertIn("CreateThread", dllmain)
        self.assertNotIn("CreateProcessW", dllmain)
        self.assertNotIn("VirtualAlloc(", dllmain)
        self.assertNotIn("VirtualProtect(", dllmain)

    def test_exact_host_process_and_per_pid_guard_are_required(self):
        self.assertIn('kGameExecutable[] = L"FFXII_TZA.exe"', self.source)
        self.assertIn('L"Local\\\\IvaliceTogetherAutostart_"', self.source)

    def test_native_sentinel_recovers_a_hard_runtime_exit_without_duplicates(self):
        self.assertIn(
            'L"Local\\\\FF12LocalCoopHeadlessLauncher_"',
            self.source,
        )
        mutex_probe = self.source.split("bool runtime_mutex_active(", 1)[1].split(
            "bool pin_autostart_module(", 1
        )[0]
        self.assertIn("SetLastError(ERROR_SUCCESS)", mutex_probe)
        self.assertIn("OpenMutexW(SYNCHRONIZE, FALSE, mutex_name)", mutex_probe)
        self.assertIn("GetLastError() != ERROR_FILE_NOT_FOUND", mutex_probe)
        self.assertIn("CloseHandle(mutex)", mutex_probe)

        sentinel = self.source.split(
            "// This process-lifetime sentinel is the final recovery boundary.",
            1,
        )[1].split("cleanup:", 1)[0]
        mutex_check = sentinel.index("runtime_mutex_active(game_pid)")
        process_start = sentinel.index("CreateProcessW(")
        process_wait = sentinel.index("WaitForSingleObject(")
        restart = sentinel.index(
            'L"runtime stopped while the game is alive; restarting"'
        )
        self.assertLess(mutex_check, process_start)
        self.assertLess(process_start, process_wait)
        self.assertLess(process_wait, restart)
        self.assertIn("for (;;)", sentinel)
        self.assertIn("next_restart_delay(restart_delay)", sentinel)
        self.assertIn(
            "WaitForSingleObject(\n                process.hProcess, INFINITE",
            sentinel,
        )
        self.assertNotIn("TerminateProcess(", sentinel)
        self.assertIn(
            'L"runtime wait is uncertain; retaining the child identity"',
            sentinel,
        )
        proven_exit = sentinel.index("if (wait_result == WAIT_OBJECT_0)")
        process_close = sentinel.index("CloseHandle(process.hProcess)")
        self.assertLess(proven_exit, process_close)
        self.assertLess(process_close, restart)
        child_identity_wait = sentinel.split(
            "bool wait_failure_logged = false;", 1
        )[1].split("const ULONGLONG stopped_at", 1)[0]
        self.assertEqual(child_identity_wait.count("break;"), 1)
        self.assertIn(
            "if (wait_result == WAIT_OBJECT_0)", child_identity_wait
        )
        self.assertIn("Sleep(kRuntimePollMilliseconds)", child_identity_wait)
        self.assertNotIn("CreateProcessW(", child_identity_wait)
        self.assertNotIn("CloseHandle(process.hProcess)", child_identity_wait)
        self.assertNotIn("TerminateProcess(", child_identity_wait)
        self.assertNotIn("WAIT_TIMEOUT", child_identity_wait)
        command_refresh = sentinel.index("copy_text(")
        self.assertLess(command_refresh, process_start)
        self.assertIn("command_line_template", sentinel[command_refresh:process_start])

    def test_sentinel_pins_its_code_and_closes_every_spawn_handle(self):
        pin = self.source.split("bool pin_autostart_module(", 1)[1].split(
            "DWORD next_restart_delay(", 1
        )[0]
        self.assertIn("GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS", pin)
        self.assertIn("GET_MODULE_HANDLE_EX_FLAG_PIN", pin)
        guard = self.source.index("g_process_guard = CreateMutexW")
        pinned = self.source.index("if (!pin_autostart_module())", guard)
        sentinel = self.source.index(
            "// This process-lifetime sentinel is the final recovery boundary.",
            pinned,
        )
        self.assertLess(guard, pinned)
        self.assertLess(pinned, sentinel)
        self.assertIn("CloseHandle(process.hThread)", self.source[sentinel:])
        self.assertIn("CloseHandle(process.hProcess)", self.source[sentinel:])
        duplicate = self.source.split(
            "if (GetLastError() == ERROR_ALREADY_EXISTS)", 1
        )[1].split("if (!pin_autostart_module())", 1)[0]
        self.assertIn("CloseHandle(g_process_guard)", duplicate)
        self.assertIn("g_process_guard = nullptr", duplicate)
        cleanup = self.source.split("cleanup:", 1)[1].split(
            "return 0;", 1
        )[0]
        self.assertIn("if (g_process_guard)", cleanup)
        self.assertIn("CloseHandle(g_process_guard)", cleanup)
        self.assertIn("g_process_guard = nullptr", cleanup)

    def test_module_publishes_name_and_semantic_version_to_the_loader(self):
        self.assertIn(
            'return "Ivalice Together Automatic Runtime";',
            self.source,
        )
        self.assertIn("FF12HgetName", self.source)
        self.assertIn("FF12HgetVer", self.source)
        self.assertIn("version->patch = 13", self.source)


if __name__ == "__main__":
    unittest.main()
