from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path


WM_APP = 0x8000
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

TRAY_CALLBACK = WM_APP + 1
TRAY_SHOW = WM_APP + 2
TRAY_HIDE = WM_APP + 3
TRAY_CLOSE = WM_APP + 4

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

MENU_RESTORE = 1
MENU_EXIT = 2


if os.name == "nt":
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = (
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        )

    class GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        )

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HANDLE),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HANDLE),
        )


class WindowsTrayIcon:
    def __init__(
        self,
        icon_path: str | Path,
        tooltip: str,
        *,
        restore_label: str = "Open launcher",
        exit_label: str = "Exit completely",
    ) -> None:
        self.icon_path = str(icon_path)
        self.tooltip = tooltip[:127]
        self._restore_label = str(restore_label)
        self._exit_label = str(exit_label)
        self._ready = threading.Event()
        self._restore_requested = threading.Event()
        self._exit_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._icon = 0
        self._visible = False
        self._want_visible = False
        self._wndproc = None

    @property
    def available(self) -> bool:
        return bool(
            os.name == "nt" and self._ready.is_set() and self._hwnd and self._icon
        )

    @property
    def visible(self) -> bool:
        return self._visible

    def start(self) -> bool:
        if os.name != "nt":
            return False
        if self._thread and self._thread.is_alive():
            return self.available
        self._thread = threading.Thread(
            target=self._message_loop,
            name="ff12-coop-tray",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(2.0)
        return self.available

    def show(self) -> bool:
        self._want_visible = True
        if not self.available:
            return False
        ctypes.windll.user32.PostMessageW(self._hwnd, TRAY_SHOW, 0, 0)
        return True

    def hide(self) -> None:
        self._want_visible = False
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, TRAY_HIDE, 0, 0)

    def consume_restore_request(self) -> bool:
        if not self._restore_requested.is_set():
            return False
        self._restore_requested.clear()
        return True

    def consume_exit_request(self) -> bool:
        if not self._exit_requested.is_set():
            return False
        self._exit_requested.clear()
        return True

    def set_menu_labels(self, restore_label: str, exit_label: str) -> None:
        self._restore_label = str(restore_label)
        self._exit_label = str(exit_label)

    def close(self) -> None:
        self._want_visible = False
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, TRAY_CLOSE, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _notification(self) -> "NOTIFYICONDATAW":
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = TRAY_CALLBACK
        data.hIcon = self._icon
        data.szTip = self.tooltip
        return data

    def _add_icon(self) -> None:
        if not self._visible and self._hwnd and self._icon:
            self._visible = bool(
                ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_ADD, ctypes.byref(self._notification())
                )
            )

    def _delete_icon(self) -> None:
        if self._visible and self._hwnd:
            ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_DELETE, ctypes.byref(self._notification())
            )
        self._visible = False

    def _show_context_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_RESTORE, self._restore_label)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, self._exit_label)
            user32.SetMenuDefaultItem(menu, MENU_RESTORE, False)

            cursor = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(cursor)):
                return
            user32.SetForegroundWindow(hwnd)
            command = int(
                user32.TrackPopupMenu(
                    menu,
                    TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                    cursor.x,
                    cursor.y,
                    0,
                    hwnd,
                    None,
                )
                or 0
            )
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            if command == MENU_RESTORE:
                self._restore_requested.set()
            elif command == MENU_EXIT:
                self._exit_requested.set()
        finally:
            user32.DestroyMenu(menu)

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32

        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HINSTANCE)
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HANDLE,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadImageW.argtypes = (
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.CreatePopupMenu.restype = wintypes.HANDLE
        user32.AppendMenuW.argtypes = (
            wintypes.HANDLE,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        )
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.SetMenuDefaultItem.argtypes = (
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.BOOL,
        )
        user32.SetMenuDefaultItem.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = (
            wintypes.HANDLE,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        )
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.DestroyMenu.argtypes = (wintypes.HANDLE,)
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostQuitMessage.argtypes = (ctypes.c_int,)
        user32.PostQuitMessage.restype = None
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.DestroyIcon.argtypes = (wintypes.HANDLE,)
        user32.DestroyIcon.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(NOTIFYICONDATAW),
        )
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        class_name = f"FF12CoopTray_{os.getpid()}_{id(self):x}"
        instance = kernel32.GetModuleHandleW(None)

        def window_proc(hwnd, message, wparam, lparam):
            if message == TRAY_CALLBACK and int(lparam) in (
                WM_LBUTTONUP,
                WM_LBUTTONDBLCLK,
            ):
                self._restore_requested.set()
                return 0
            if message == TRAY_CALLBACK and int(lparam) == WM_RBUTTONUP:
                self._show_context_menu(hwnd)
                return 0
            if message == TRAY_SHOW:
                self._add_icon()
                return 0
            if message == TRAY_HIDE:
                self._delete_icon()
                return 0
            if message == taskbar_created:
                self._visible = False
                if self._want_visible:
                    self._add_icon()
                return 0
            if message == TRAY_CLOSE:
                self._delete_icon()
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = WNDPROC(window_proc)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._ready.set()
            return

        try:
            self._hwnd = int(
                user32.CreateWindowExW(
                    0,
                    class_name,
                    self.tooltip,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    instance,
                    None,
                )
                or 0
            )
            self._icon = int(
                user32.LoadImageW(
                    None,
                    self.icon_path,
                    IMAGE_ICON,
                    0,
                    0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE,
                )
                or 0
            )
            self._ready.set()
            if not self._hwnd or not self._icon:
                return

            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._delete_icon()
            if self._icon:
                user32.DestroyIcon(self._icon)
            self._icon = 0
            self._hwnd = 0
            user32.UnregisterClassW(class_name, instance)
            self._ready.set()


__all__ = ["WindowsTrayIcon"]
