from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

from PIL import Image

import cancel


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_set_dpi_awareness()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9

ULONG_PTR = ctypes.c_ulonglong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800

VK = {
    "space": 0x20,
    "tab": 0x09,
    "escape": 0x1B,
    "enter": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E,
    "control": 0x11,
    "f4": 0x73,
    "alt": 0xA4,
}


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    exe_path: str

    @property
    def process_name(self) -> str:
        return self.exe_path.rsplit("\\", 1)[-1]


def _get_exe_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows(include_hidden: bool = False) -> list[WindowInfo]:
    windows: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        if not include_hidden and not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        windows.append(
            WindowInfo(
                int(hwnd),
                int(pid.value),
                title,
                cls_buf.value,
                _get_exe_path(pid.value),
            )
        )
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return windows


def find_window(exe_hint: str = "", title_contains: str = "") -> WindowInfo | None:
    exe_hint = exe_hint.lower()
    for win in list_windows():
        if exe_hint and exe_hint not in win.exe_path.lower():
            continue
        if title_contains and title_contains not in win.title:
            continue
        return win
    return None


def close_window(hwnd: int) -> bool:
    return bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right, rect.bottom


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def is_foreground(hwnd: int) -> bool:
    return int(user32.GetForegroundWindow()) == int(hwnd)


def activate(hwnd: int, timeout: float = 3.0) -> bool:
    cancel.check()
    deadline = time.time() + timeout
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    while time.time() < deadline:
        if is_foreground(hwnd):
            return True
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if is_foreground(hwnd):
            return True
        _key_down(VK["alt"])
        _key_up(VK["alt"])
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
    return is_foreground(hwnd)


def capture_client(hwnd: int) -> Image.Image:
    import mss

    left, top, width, height = client_rect(hwnd)
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return Image.frombytes("RGB", shot.size, shot.rgb)


def _send_inputs(*inputs: INPUT) -> None:
    cancel.check()
    array = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _make_key(vk: int, up: bool) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    union = _INPUTUNION(ki=KEYBDINPUT(0, scan, flags, 0, 0))
    return INPUT(INPUT_KEYBOARD, union)


def _key_down(vk: int) -> None:
    _send_inputs(_make_key(vk, False))


def _key_up(vk: int) -> None:
    _send_inputs(_make_key(vk, True))


def _vk_for(key: str) -> int:
    name = key.lower()
    if name in VK:
        return VK[name]
    if len(name) == 1:
        if name.isalpha():
            return ord(name.upper())
        if name.isdigit():
            return ord(name)
    raise KeyError(key)


def press_key(name: str) -> None:
    vk = _vk_for(name)
    _key_down(vk)
    time.sleep(0.05)
    _key_up(vk)
    time.sleep(0.1)


def press_alt_f4() -> None:
    _key_down(VK["alt"])
    time.sleep(0.05)
    _key_down(VK["f4"])
    time.sleep(0.05)
    _key_up(VK["f4"])
    _key_up(VK["alt"])
    time.sleep(0.1)


def press_ctrl(key: str) -> None:
    vk = _vk_for(key)
    _key_down(VK["control"])
    time.sleep(0.03)
    _key_down(vk)
    time.sleep(0.05)
    _key_up(vk)
    _key_up(VK["control"])
    time.sleep(0.1)


KEYEVENTF_UNICODE = 0x0004


def type_text_unicode(text: str) -> None:
    for char in text:
        units = char.encode("utf-16-le")
        for index in range(0, len(units), 2):
            code = int.from_bytes(units[index : index + 2], "little")
            down = _INPUTUNION(
                ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
            )
            up = _INPUTUNION(
                ki=KEYBDINPUT(
                    0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0
                )
            )
            _send_inputs(INPUT(INPUT_KEYBOARD, down))
            _send_inputs(INPUT(INPUT_KEYBOARD, up))
            time.sleep(0.01)


def set_clipboard_text(text: str) -> None:
    cancel.check()
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, size)
        if not handle:
            return
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return
        ctypes.memmove(pointer, buffer, size)
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def click_client(hwnd: int, x: float, y: float) -> None:
    if not activate(hwnd):
        raise RuntimeError("目标窗口无法切到前台")
    left, top, _w, _h = client_rect(hwnd)
    user32.SetCursorPos(left + int(x), top + int(y))
    time.sleep(0.08)
    down = _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0))
    up = _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0))
    _send_inputs(INPUT(INPUT_MOUSE, down))
    time.sleep(0.06)
    _send_inputs(INPUT(INPUT_MOUSE, up))
    time.sleep(0.15)


def scroll_client(hwnd: int, x: float, y: float, notches: float) -> None:
    if not activate(hwnd):
        raise RuntimeError("目标窗口无法切到前台")
    left, top, _w, _h = client_rect(hwnd)
    user32.SetCursorPos(left + int(x), top + int(y))
    time.sleep(0.05)
    delta = int(notches * 120) & 0xFFFFFFFF
    wheel = _INPUTUNION(mi=MOUSEINPUT(0, 0, delta, MOUSEEVENTF_WHEEL, 0, 0))
    _send_inputs(INPUT(INPUT_MOUSE, wheel))
    time.sleep(0.15)


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def set_system_awake(enabled: bool) -> None:
    try:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    except Exception:
        pass


def is_desktop_locked() -> bool:
    try:
        handle = user32.OpenInputDesktop(0, False, 0x0100)
        if not handle:
            return True
        user32.CloseDesktop(handle)
        return False
    except Exception:
        return False


def get_dpi_scale(hwnd: int) -> float:
    try:
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    try:
        dc = user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
        user32.ReleaseDC(0, dc)
        return (dpi or 96) / 96.0
    except Exception:
        return 1.0
