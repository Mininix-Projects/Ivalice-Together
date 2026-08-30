import ctypes as ct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import inject_dll


class _PidLookupReached(Exception):
    """Stops the test before it can touch a live process."""


class _FakeRemoteProcess:
    def __init__(self):
        self.handle = 0x1234
        self.writes = []
        self.closed = False

    def write(self, address, payload):
        self.writes.append((address, payload))

    def close(self):
        self.closed = True


class InjectDllTests(unittest.TestCase):
    def test_module_snapshot_retries_transient_bad_length(self):
        invalid = ct.c_void_p(-1).value
        errors = iter([inject_dll.ERROR_BAD_LENGTH, inject_dll.ERROR_BAD_LENGTH])
        with (
            mock.patch.object(
                inject_dll.kernel32,
                "CreateToolhelp32Snapshot",
                side_effect=[invalid, invalid, 0x1234],
            ) as create_snapshot,
            mock.patch.object(inject_dll.ct, "get_last_error", side_effect=errors),
            mock.patch.object(inject_dll.time, "sleep") as sleep,
        ):
            snapshot = inject_dll._create_module_snapshot(4242)

        self.assertEqual(snapshot, 0x1234)
        self.assertEqual(create_snapshot.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_module_snapshot_does_not_retry_permanent_failure(self):
        invalid = ct.c_void_p(-1).value
        with (
            mock.patch.object(
                inject_dll.kernel32,
                "CreateToolhelp32Snapshot",
                return_value=invalid,
            ) as create_snapshot,
            mock.patch.object(inject_dll.ct, "get_last_error", return_value=5),
            mock.patch.object(inject_dll.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(OSError, "CreateToolhelp32Snapshot"):
                inject_dll._create_module_snapshot(4242)

        create_snapshot.assert_called_once()
        sleep.assert_not_called()

    def test_remote_procedure_uses_the_target_process_module_base(self):
        with (
            mock.patch.object(
                inject_dll.kernel32,
                "GetModuleHandleW",
                return_value=0x7FF8_1000_0000,
            ),
            mock.patch.object(
                inject_dll.kernel32,
                "GetProcAddress",
                return_value=0x7FF8_1001_2340,
            ),
            mock.patch.object(
                inject_dll,
                "_local_module_containing",
                return_value=("kernel32.dll", 0x7FF8_1000_0000),
            ),
            mock.patch.object(
                inject_dll,
                "_loaded_module_entries",
                return_value=[
                    (
                        "KERNEL32.DLL",
                        r"C:\Windows\System32\kernel32.dll",
                        0x7FFA_2000_0000,
                    ),
                ],
            ),
        ):
            address = inject_dll.remote_procedure_address(
                4242,
                "kernel32.dll",
                b"LoadLibraryW",
            )

        self.assertEqual(address, 0x7FFA_2001_2340)

    def test_remote_procedure_follows_the_actual_owner_module(self):
        with (
            mock.patch.object(
                inject_dll.kernel32,
                "GetModuleHandleW",
                return_value=0x7FF8_1000_0000,
            ),
            mock.patch.object(
                inject_dll.kernel32,
                "GetProcAddress",
                return_value=0x7FF9_3000_4560,
            ),
            mock.patch.object(
                inject_dll,
                "_local_module_containing",
                return_value=("KernelBase.dll", 0x7FF9_3000_0000),
            ),
            mock.patch.object(
                inject_dll,
                "_loaded_module_entries",
                return_value=[
                    (
                        "KERNELBASE.DLL",
                        r"C:\Windows\System32\KernelBase.dll",
                        0x7FFA_4000_0000,
                    ),
                ],
            ),
        ):
            address = inject_dll.remote_procedure_address(
                4242,
                "kernel32.dll",
                b"ForwardedApi",
            )

        self.assertEqual(address, 0x7FFA_4000_4560)

    def test_virtual_memory_api_preserves_64_bit_addresses(self):
        self.assertIs(inject_dll.kernel32.VirtualAllocEx.restype, ct.c_void_p)
        self.assertEqual(
            inject_dll.kernel32.VirtualAllocEx.argtypes,
            [
                inject_dll.wt.HANDLE,
                ct.c_void_p,
                ct.c_size_t,
                inject_dll.wt.DWORD,
                inject_dll.wt.DWORD,
            ],
        )

    def test_existing_dll_reaches_pid_lookup_after_path_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "renderer.dll"
            dll_path.touch()

            with mock.patch.object(
                inject_dll.coop,
                "find_pid",
                side_effect=_PidLookupReached,
            ) as find_pid:
                with self.assertRaises(_PidLookupReached):
                    inject_dll.inject(dll_path)

            find_pid.assert_called_once_with()

    def test_explicit_pid_is_validated_and_used_for_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "renderer.dll"
            dll_path.touch()
            with (
                mock.patch.object(
                    inject_dll.coop,
                    "find_pid",
                    return_value=4242,
                ) as find_pid,
                mock.patch.object(
                    inject_dll,
                    "loaded_modules",
                    return_value=[("renderer.dll", str(dll_path))],
                ),
            ):
                inject_dll.inject(dll_path, pid=4242)

        find_pid.assert_called_once_with(4242)

    def test_retry_adopts_same_dll_basename_from_different_onefile_path(self):
        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "p2_menu_color_dx11_v73.dll"
            dll_path.touch()
            extracted_path = (
                r"C:\Users\Player\AppData\Local\Temp\_MEI12345"
                r"\p2_menu_color_dx11_v73.dll"
            )

            with (
                mock.patch.object(inject_dll.coop, "find_pid", return_value=16056),
                mock.patch.object(
                    inject_dll,
                    "loaded_modules",
                    return_value=[
                        ("P2_MENU_COLOR_DX11_V73.DLL", extracted_path),
                    ],
                ) as loaded_modules,
                mock.patch.object(inject_dll.coop, "RemoteProcess") as remote_process,
            ):
                result = inject_dll.inject(dll_path)

            self.assertIsNone(result)
            loaded_modules.assert_called_once_with(16056)
            remote_process.assert_not_called()

    def test_inject_keeps_high_remote_pointer_and_releases_after_completion(self):
        remote_address = 0x1234_5678_9ABC
        process = _FakeRemoteProcess()

        def publish_exit_code(_thread, output):
            output._obj.value = 0x5678
            return True

        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "renderer.dll"
            dll_path.touch()
            with (
                mock.patch.object(inject_dll.coop, "find_pid", return_value=16056),
                mock.patch.object(
                    inject_dll,
                    "loaded_modules",
                    side_effect=[[], [("renderer.dll", str(dll_path))]],
                ),
                mock.patch.object(
                    inject_dll.coop,
                    "RemoteProcess",
                    return_value=process,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "VirtualAllocEx",
                    return_value=remote_address,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "VirtualFreeEx",
                    return_value=True,
                ) as free_remote,
                mock.patch.object(
                    inject_dll,
                    "remote_procedure_address",
                    return_value=0x2000,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "CreateRemoteThread",
                    return_value=0x3000,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "WaitForSingleObject",
                    return_value=inject_dll.WAIT_OBJECT_0,
                ) as wait,
                mock.patch.object(
                    inject_dll.kernel32,
                    "GetExitCodeThread",
                    side_effect=publish_exit_code,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "CloseHandle",
                    return_value=True,
                ),
            ):
                inject_dll.inject(dll_path)

        self.assertEqual(process.writes[0][0], remote_address)
        self.assertTrue(process.closed)
        wait.assert_called_once_with(0x3000, inject_dll.LOAD_LIBRARY_TIMEOUT_MS)
        free_remote.assert_called_once_with(
            process.handle,
            remote_address,
            0,
            inject_dll.MEM_RELEASE,
        )

    def test_inject_trusts_remote_module_confirmation_not_truncated_handle(self):
        process = _FakeRemoteProcess()

        def publish_zero_exit_code(_thread, output):
            output._obj.value = 0
            return True

        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "renderer.dll"
            dll_path.touch()
            with (
                mock.patch.object(inject_dll.coop, "find_pid", return_value=16056),
                mock.patch.object(
                    inject_dll,
                    "loaded_modules",
                    side_effect=[[], [("renderer.dll", str(dll_path))]],
                ),
                mock.patch.object(
                    inject_dll.coop,
                    "RemoteProcess",
                    return_value=process,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "VirtualAllocEx",
                    return_value=0x5000,
                ),
                mock.patch.object(
                    inject_dll.kernel32, "VirtualFreeEx", return_value=True
                ),
                mock.patch.object(
                    inject_dll, "remote_procedure_address", return_value=0x2000
                ),
                mock.patch.object(
                    inject_dll.kernel32, "CreateRemoteThread", return_value=0x3000
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "WaitForSingleObject",
                    return_value=inject_dll.WAIT_OBJECT_0,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "GetExitCodeThread",
                    side_effect=publish_zero_exit_code,
                ),
                mock.patch.object(
                    inject_dll.kernel32, "CloseHandle", return_value=True
                ),
            ):
                inject_dll.inject(dll_path)

    def test_inject_timeout_is_finite_and_does_not_free_live_argument(self):
        process = _FakeRemoteProcess()
        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "renderer.dll"
            dll_path.touch()
            with (
                mock.patch.object(inject_dll.coop, "find_pid", return_value=16056),
                mock.patch.object(inject_dll, "loaded_modules", return_value=[]),
                mock.patch.object(
                    inject_dll.coop,
                    "RemoteProcess",
                    return_value=process,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "VirtualAllocEx",
                    return_value=0x1234_5678_9ABC,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "VirtualFreeEx",
                    return_value=True,
                ) as free_remote,
                mock.patch.object(
                    inject_dll,
                    "remote_procedure_address",
                    return_value=0x2000,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "CreateRemoteThread",
                    return_value=0x3000,
                ),
                mock.patch.object(
                    inject_dll.kernel32,
                    "WaitForSingleObject",
                    return_value=inject_dll.WAIT_TIMEOUT,
                ) as wait,
                mock.patch.object(
                    inject_dll.kernel32,
                    "GetExitCodeThread",
                ) as exit_code,
                mock.patch.object(
                    inject_dll.kernel32,
                    "CloseHandle",
                    return_value=True,
                ) as close_handle,
            ):
                with self.assertRaisesRegex(TimeoutError, "15 seconds"):
                    inject_dll.inject(dll_path)

        wait.assert_called_once_with(0x3000, inject_dll.LOAD_LIBRARY_TIMEOUT_MS)
        exit_code.assert_not_called()
        free_remote.assert_not_called()
        close_handle.assert_called_once_with(0x3000)
        self.assertTrue(process.closed)

    def test_retry_still_rejects_a_different_resident_renderer_version(self):
        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "p2_menu_color_dx11_v73.dll"
            dll_path.touch()
            resident_path = (
                r"C:\Users\Player\AppData\Local\Temp\_MEI12345"
                r"\p2_menu_color_dx11_v72.dll"
            )

            with (
                mock.patch.object(inject_dll.coop, "find_pid", return_value=16056),
                mock.patch.object(
                    inject_dll,
                    "loaded_modules",
                    return_value=[
                        ("p2_menu_color_dx11_v72.dll", resident_path),
                    ],
                ),
                mock.patch.object(inject_dll.coop, "RemoteProcess") as remote_process,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"refusing to stack D3D11 renderer hooks.*v72\.dll",
                ):
                    inject_dll.inject(dll_path)

            remote_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
