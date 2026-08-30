import ctypes as ct
import os
import sys
import time
from ctypes import wintypes as wt

import coop_runtime as coop

kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetProcAddress.argtypes = [wt.HMODULE, ct.c_char_p]
kernel32.GetProcAddress.restype = ct.c_void_p
kernel32.GetModuleHandleExW.argtypes = [wt.DWORD, ct.c_void_p, ct.POINTER(wt.HMODULE)]
kernel32.GetModuleHandleExW.restype = wt.BOOL
kernel32.GetModuleFileNameW.argtypes = [wt.HMODULE, wt.LPWSTR, wt.DWORD]
kernel32.GetModuleFileNameW.restype = wt.DWORD
kernel32.VirtualAllocEx.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    wt.DWORD,
    wt.DWORD,
]
kernel32.VirtualAllocEx.restype = ct.c_void_p
kernel32.VirtualFreeEx.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    wt.DWORD,
]
kernel32.VirtualFreeEx.restype = wt.BOOL
kernel32.CreateRemoteThread.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    ct.c_void_p,
    ct.c_void_p,
    wt.DWORD,
    ct.POINTER(wt.DWORD),
]
kernel32.CreateRemoteThread.restype = wt.HANDLE
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.GetExitCodeThread.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
kernel32.GetExitCodeThread.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_PATH = 260
ERROR_NO_MORE_FILES = 18
ERROR_BAD_LENGTH = 24
MODULE_SNAPSHOT_ATTEMPTS = 5
MODULE_SNAPSHOT_RETRY_SECONDS = 0.01
GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT = 0x00000002
GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS = 0x00000004


class MODULEENTRY32W(ct.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ct.POINTER(ct.c_ubyte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", wt.WCHAR * 256),
        ("szExePath", wt.WCHAR * MAX_PATH),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Module32FirstW.argtypes = [wt.HANDLE, ct.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wt.BOOL
kernel32.Module32NextW.argtypes = [wt.HANDLE, ct.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wt.BOOL


def _create_module_snapshot(pid: int):
    invalid = ct.c_void_p(-1).value
    for attempt in range(MODULE_SNAPSHOT_ATTEMPTS):
        snapshot = kernel32.CreateToolhelp32Snapshot(
            TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
        )
        if snapshot != invalid:
            return snapshot
        error = ct.get_last_error()
        if error != ERROR_BAD_LENGTH or attempt + 1 == MODULE_SNAPSHOT_ATTEMPTS:
            raise OSError(error, "CreateToolhelp32Snapshot")
        time.sleep(MODULE_SNAPSHOT_RETRY_SECONDS)
    raise AssertionError("unreachable module snapshot retry state")


def _loaded_module_entries(pid):
    snapshot = _create_module_snapshot(pid)
    modules = []
    entry = MODULEENTRY32W()
    entry.dwSize = ct.sizeof(entry)
    try:
        more = kernel32.Module32FirstW(snapshot, ct.byref(entry))
        if not more:
            error = ct.get_last_error()
            if error != ERROR_NO_MORE_FILES:
                raise OSError(error, "Module32FirstW")
            return modules
        while more:
            base = int(ct.cast(entry.modBaseAddr, ct.c_void_p).value or 0)
            modules.append((entry.szModule, os.path.abspath(entry.szExePath), base))
            more = kernel32.Module32NextW(snapshot, ct.byref(entry))
        error = ct.get_last_error()
        if error != ERROR_NO_MORE_FILES:
            raise OSError(error, "Module32NextW")
    finally:
        kernel32.CloseHandle(snapshot)
    return modules


def loaded_modules(pid):
    return [(name, path) for name, path, _base in _loaded_module_entries(pid)]


def _local_module_containing(address: int) -> tuple[str, int]:
    module = wt.HMODULE()
    flags = (
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS
        | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT
    )
    if not kernel32.GetModuleHandleExW(flags, ct.c_void_p(address), ct.byref(module)):
        raise OSError(ct.get_last_error(), "GetModuleHandleExW(FROM_ADDRESS)")
    path = ct.create_unicode_buffer(32768)
    length = kernel32.GetModuleFileNameW(module, path, len(path))
    if not length or length >= len(path):
        raise OSError(ct.get_last_error(), "GetModuleFileNameW")
    return os.path.basename(path.value), int(module.value)


def remote_procedure_address(pid: int, module_name: str, procedure: bytes) -> int:
    local_module = kernel32.GetModuleHandleW(module_name)
    if not local_module:
        raise OSError(ct.get_last_error(), f"GetModuleHandleW({module_name})")
    local_procedure = kernel32.GetProcAddress(local_module, procedure)
    if not local_procedure:
        decoded = procedure.decode("ascii", errors="replace")
        raise OSError(ct.get_last_error(), f"GetProcAddress({decoded})")
    owner_name, owner_base = _local_module_containing(int(local_procedure))
    offset = int(local_procedure) - owner_base
    expected = owner_name.casefold()
    remote_base = next(
        (
            base
            for name, _path, base in _loaded_module_entries(pid)
            if name.casefold() == expected
        ),
        0,
    )
    if not remote_base:
        raise RuntimeError(
            f"{owner_name}, which owns {procedure!r}, is not loaded in PID {pid}"
        )
    return remote_base + offset


MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
LOAD_LIBRARY_TIMEOUT_MS = 15_000


def inject(path, *, pid: int | None = None):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    pid = coop.find_pid() if pid is None else coop.find_pid(pid)
    module_name = os.path.basename(path).lower()
    modules = loaded_modules(pid)
    for loaded_name, loaded_path in modules:
        if os.path.normcase(loaded_path) == os.path.normcase(path):
            print(f"Already loaded {path} in PID {pid}")
            return

        if loaded_name.lower() == module_name:
            print(f"Already loaded {loaded_name} from {loaded_path} in PID {pid}")
            return
    if module_name.startswith("p2_menu_color_dx11_v"):
        resident = [
            loaded_name
            for loaded_name, _loaded_path in modules
            if loaded_name.lower().startswith("p2_menu_color_dx11_v")
        ]
        if resident:
            raise RuntimeError(
                "refusing to stack D3D11 renderer hooks; restart FF12 first "
                f"(resident: {', '.join(resident)})"
            )
    process = coop.RemoteProcess(pid)
    encoded = (path + "\0").encode("utf-16-le")
    remote = None
    thread = None
    thread_completed = False
    try:
        remote = kernel32.VirtualAllocEx(
            process.handle,
            None,
            len(encoded),
            MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE,
        )
        if not remote:
            raise OSError(ct.get_last_error(), "VirtualAllocEx")
        process.write(remote, encoded)
        load_library = remote_procedure_address(
            pid,
            "kernel32.dll",
            b"LoadLibraryW",
        )
        thread_id = wt.DWORD()
        thread = kernel32.CreateRemoteThread(
            process.handle,
            None,
            0,
            load_library,
            remote,
            0,
            ct.byref(thread_id),
        )
        if not thread:
            raise OSError(ct.get_last_error(), "CreateRemoteThread")
        wait_result = kernel32.WaitForSingleObject(
            thread,
            LOAD_LIBRARY_TIMEOUT_MS,
        )
        if wait_result == WAIT_TIMEOUT:
            raise TimeoutError(
                "LoadLibraryW did not finish in the game process within "
                f"{LOAD_LIBRARY_TIMEOUT_MS / 1000:g} seconds"
            )
        if wait_result == WAIT_FAILED:
            raise OSError(ct.get_last_error(), "WaitForSingleObject")
        if wait_result != WAIT_OBJECT_0:
            raise RuntimeError(f"unexpected LoadLibraryW wait result: {wait_result:#x}")
        thread_completed = True
        result = wt.DWORD()
        if not kernel32.GetExitCodeThread(thread, ct.byref(result)):
            raise OSError(ct.get_last_error(), "GetExitCodeThread")
    finally:
        if thread:
            kernel32.CloseHandle(thread)

        if remote and (thread is None or thread_completed):
            kernel32.VirtualFreeEx(
                process.handle,
                remote,
                0,
                MEM_RELEASE,
            )
        process.close()
    confirmed_path = next(
        (
            loaded_path
            for loaded_name, loaded_path in loaded_modules(pid)
            if os.path.normcase(loaded_path) == os.path.normcase(path)
            or loaded_name.casefold() == module_name.casefold()
        ),
        None,
    )
    if confirmed_path is None:
        raise RuntimeError(
            "LoadLibraryW finished, but the DLL is not present in the game "
            f"module list (thread result low32={result.value:#x})"
        )
    print(f"Injected {confirmed_path} into PID {pid}; module low32={result.value:#x}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: inject_dll.py path-to-dll")
    inject(sys.argv[1])
