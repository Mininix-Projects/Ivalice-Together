from __future__ import annotations

import atexit
import ctypes as ct
from ctypes import wintypes as wt


ERROR_ALREADY_EXISTS = 183
ERROR_FILE_NOT_FOUND = 2
SYNCHRONIZE = 0x00100000

ROLE_MUTEX_PREFIXES = {
    # Preserve the established menu mutex so an upgraded supervisor can also
    # recognize a menu helper created by an older compatible build.
    "headless_menu": "FF12CoopMenu",
    "movement": "FF12CoopMovement",
    "story_party": "FF12CoopStoryParty",
    "penelo_guard": "FF12CoopPeneloGuard",
}


kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ct.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenMutexW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL


def mutex_name(game_pid: int, role: str) -> str:
    try:
        prefix = ROLE_MUTEX_PREFIXES[str(role)]
    except KeyError as error:
        raise ValueError(f"unknown co-op helper role: {role!r}") from error
    game_pid = int(game_pid)
    if game_pid <= 0:
        raise ValueError("co-op helper lease requires a positive game PID")
    return f"Local\\{prefix}_{game_pid}"


def active(game_pid: int, role: str) -> bool:
    """Observe a helper lease without creating or taking ownership of it."""

    ct.set_last_error(0)
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name(game_pid, role))
    if not handle:
        error = ct.get_last_error()
        if error in (0, ERROR_FILE_NOT_FOUND):
            return False
        raise OSError(error, "OpenMutexW")
    kernel32.CloseHandle(handle)
    return True


class HelperLease:
    """Process-lifetime named lease proving one live owner for a helper role."""

    def __init__(self, game_pid: int, role: str) -> None:
        self.game_pid = int(game_pid)
        self.role = str(role)
        self.name = mutex_name(self.game_pid, self.role)
        ct.set_last_error(0)
        self.handle = kernel32.CreateMutexW(None, True, self.name)
        if not self.handle:
            raise OSError(ct.get_last_error(), "CreateMutexW")
        if ct.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError(
                f"co-op helper {self.role} is already running for game "
                f"{self.game_pid}"
            )
        atexit.register(self.close)

    def close(self) -> None:
        handle = self.handle
        if not handle:
            return
        self.handle = None
        kernel32.CloseHandle(handle)

    def __enter__(self) -> HelperLease:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


__all__ = [
    "HelperLease",
    "ROLE_MUTEX_PREFIXES",
    "active",
    "mutex_name",
]
