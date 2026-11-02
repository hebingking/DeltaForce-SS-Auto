from __future__ import annotations

import threading


class RunCancelled(RuntimeError):
    pass


_event = threading.Event()


def reset() -> None:
    _event.clear()


def request() -> None:
    _event.set()


def requested() -> bool:
    return _event.is_set()


def check() -> None:
    if _event.is_set():
        raise RunCancelled("用户取消")
