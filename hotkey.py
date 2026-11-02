from __future__ import annotations

import ctypes
import threading

VK_F12 = 0x7B

_user32 = ctypes.windll.user32
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short

_stop = threading.Event()


def _pressed() -> bool:
    return bool(_user32.GetAsyncKeyState(VK_F12) & 0x8000)


def start(callback) -> threading.Thread:
    _stop.clear()

    def run() -> None:
        was_down = False
        while not _stop.wait(0.08):
            down = _pressed()
            if down and not was_down:
                try:
                    callback()
                except Exception:
                    pass
            was_down = down

    thread = threading.Thread(target=run, name="f12-cancel", daemon=True)
    thread.start()
    return thread


def stop() -> None:
    _stop.set()
