from __future__ import annotations

import ctypes as ct
import os
import time
from ctypes import wintypes as wt
from pathlib import Path
from typing import Mapping

import runtime_paths


PROCESS_NAME = "FFXII_TZA.exe"
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_NO_MORE_FILES = 18
ERROR_BAD_LENGTH = 24
PROCESS_SNAPSHOT_ATTEMPTS = 5
PROCESS_SNAPSHOT_RETRY_SECONDS = 0.01


class PROCESSENTRY32W(ct.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ct.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ct.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def _kernel32():
    if os.name != "nt":
        raise OSError("game process discovery is available only on Windows")
    library = ct.WinDLL("kernel32", use_last_error=True)
    library.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    library.CreateToolhelp32Snapshot.restype = wt.HANDLE
    library.Process32FirstW.argtypes = [wt.HANDLE, ct.POINTER(PROCESSENTRY32W)]
    library.Process32FirstW.restype = wt.BOOL
    library.Process32NextW.argtypes = [wt.HANDLE, ct.POINTER(PROCESSENTRY32W)]
    library.Process32NextW.restype = wt.BOOL
    library.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    library.OpenProcess.restype = wt.HANDLE
    library.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ct.POINTER(wt.DWORD),
    ]
    library.QueryFullProcessImageNameW.restype = wt.BOOL
    library.CloseHandle.argtypes = [wt.HANDLE]
    library.CloseHandle.restype = wt.BOOL
    return library


def normalized_image_path(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(resolved)))


def _create_process_snapshot(kernel32):
    invalid = ct.c_void_p(-1).value
    for attempt in range(PROCESS_SNAPSHOT_ATTEMPTS):
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot != invalid:
            return snapshot
        error = ct.get_last_error()
        if error != ERROR_BAD_LENGTH or attempt + 1 == PROCESS_SNAPSHOT_ATTEMPTS:
            raise OSError(error, "CreateToolhelp32Snapshot")
        time.sleep(PROCESS_SNAPSHOT_RETRY_SECONDS)
    raise AssertionError("unreachable process snapshot retry state")


def process_image_path(pid: int) -> Path:
    if int(pid) <= 0:
        raise ValueError(f"invalid game PID: {pid!r}")
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        int(pid),
    )
    if not handle:
        raise OSError(ct.get_last_error(), f"OpenProcess({int(pid)})")
    try:
        capacity = 32768
        buffer = ct.create_unicode_buffer(capacity)
        length = wt.DWORD(capacity)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ct.byref(length),
        ):
            raise OSError(
                ct.get_last_error(),
                f"QueryFullProcessImageNameW({int(pid)})",
            )
        return Path(buffer.value).resolve(strict=False)
    finally:
        kernel32.CloseHandle(handle)


def named_process_ids(executable: str = PROCESS_NAME) -> tuple[int, ...]:
    if os.name != "nt":
        return ()
    kernel32 = _kernel32()
    snapshot = _create_process_snapshot(kernel32)
    entry = PROCESSENTRY32W()
    entry.dwSize = ct.sizeof(entry)
    result: list[int] = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ct.byref(entry))
        if not ok:
            error = ct.get_last_error()
            if error != ERROR_NO_MORE_FILES:
                raise OSError(error, "Process32FirstW")
            return ()
        while ok:
            if entry.szExeFile.casefold() == executable.casefold():
                result.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ct.byref(entry))
        error = ct.get_last_error()
        if error != ERROR_NO_MORE_FILES:
            raise OSError(error, "Process32NextW")
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(sorted(result))


def matching_process_ids(expected_path: str | Path) -> tuple[int, ...]:
    expected = Path(expected_path).resolve(strict=False)
    normalized_expected = normalized_image_path(expected)
    matches: list[int] = []
    for pid in named_process_ids(expected.name):
        try:
            observed = process_image_path(pid)
        except OSError:
            continue
        if normalized_image_path(observed) == normalized_expected:
            matches.append(pid)
    return tuple(matches)


def validate_process_id(pid: int, expected_path: str | Path) -> int:
    expected = Path(expected_path).resolve(strict=False)
    try:
        observed = process_image_path(int(pid))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"game PID {pid!r} is not accessible") from error
    if normalized_image_path(observed) != normalized_image_path(expected):
        raise RuntimeError(
            f"game PID {int(pid)} belongs to {observed}, expected {expected}"
        )
    return int(pid)


def choose_process_id(
    candidates: tuple[int, ...],
    *,
    preferred_pid: int | None = None,
) -> int | None:
    unique = tuple(dict.fromkeys(int(pid) for pid in candidates if int(pid) > 0))
    if preferred_pid is not None and int(preferred_pid) in unique:
        return int(preferred_pid)
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    joined = ", ".join(str(pid) for pid in unique)
    raise RuntimeError(f"multiple matching FF12 processes are running: {joined}")


def configured_process_id(
    expected_path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> int | None:
    values = os.environ if environment is None else environment
    raw = values.get(runtime_paths.GAME_PID_ENV)
    if raw is None or not str(raw).strip():
        return None
    try:
        pid = int(str(raw), 10)
    except ValueError as error:
        raise RuntimeError(
            f"invalid {runtime_paths.GAME_PID_ENV} value: {raw!r}"
        ) from error
    return validate_process_id(pid, expected_path)


def find_process_id(
    expected_path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    configured = configured_process_id(expected_path, environment=environment)
    if configured is not None:
        return configured
    selected = choose_process_id(matching_process_ids(expected_path))
    if selected is None:
        raise RuntimeError(
            f"{Path(expected_path).name} is not running from {expected_path}"
        )
    return selected


__all__ = [
    "PROCESS_NAME",
    "choose_process_id",
    "configured_process_id",
    "find_process_id",
    "matching_process_ids",
    "named_process_ids",
    "normalized_image_path",
    "process_image_path",
    "validate_process_id",
]
