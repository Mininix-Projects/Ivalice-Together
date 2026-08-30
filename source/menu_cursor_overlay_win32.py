import ctypes as ct
from ctypes import wintypes as wt

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
PM_REMOVE = 1
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
BI_RGB = 0
DIB_RGB_COLORS = 0

user32 = ct.WinDLL("user32", use_last_error=True)
gdi32 = ct.WinDLL("gdi32", use_last_error=True)
kernel32 = ct.WinDLL("kernel32", use_last_error=True)


HCURSOR = ct.c_void_p
HGDIOBJ = ct.c_void_p
HMENU = ct.c_void_p
LRESULT = ct.c_ssize_t


class WNDCLASSW(ct.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ct.c_void_p),
        ("cbClsExtra", ct.c_int),
        ("cbWndExtra", ct.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class BITMAPINFOHEADER(ct.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ct.c_long),
        ("biHeight", ct.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ct.c_long),
        ("biYPelsPerMeter", ct.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class RGBQUAD(ct.Structure):
    _fields_ = [
        ("rgbBlue", ct.c_ubyte),
        ("rgbGreen", ct.c_ubyte),
        ("rgbRed", ct.c_ubyte),
        ("rgbReserved", ct.c_ubyte),
    ]


class BITMAPINFO(ct.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


class SIZE(ct.Structure):
    _fields_ = [("cx", ct.c_long), ("cy", ct.c_long)]


class BLENDFUNCTION(ct.Structure):
    _fields_ = [
        ("BlendOp", ct.c_ubyte),
        ("BlendFlags", ct.c_ubyte),
        ("SourceConstantAlpha", ct.c_ubyte),
        ("AlphaFormat", ct.c_ubyte),
    ]


WNDPROC = ct.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ct.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
user32.UnregisterClassW.restype = wt.BOOL
user32.CreateWindowExW.argtypes = [
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ct.c_int,
    ct.c_int,
    ct.c_int,
    ct.c_int,
    wt.HWND,
    HMENU,
    wt.HINSTANCE,
    ct.c_void_p,
]
user32.CreateWindowExW.restype = wt.HWND
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND,
    wt.HDC,
    ct.POINTER(wt.POINT),
    ct.POINTER(SIZE),
    wt.HDC,
    ct.POINTER(wt.POINT),
    wt.DWORD,
    ct.POINTER(BLENDFUNCTION),
    wt.DWORD,
]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ct.c_int
user32.ShowWindow.argtypes = [wt.HWND, ct.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ct.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ct.POINTER(wt.POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.GetAsyncKeyState.argtypes = [ct.c_int]
user32.GetAsyncKeyState.restype = ct.c_short
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.argtypes = [
    wt.HDC,
    ct.POINTER(BITMAPINFO),
    wt.UINT,
    ct.POINTER(ct.c_void_p),
    ct.c_void_p,
    wt.DWORD,
]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


def game_window(pid):
    found = []
    callback_type = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    @callback_type
    def visit(hwnd, _parameter):
        window_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ct.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        raise RuntimeError("FFXII window not found")
    return found[0]
