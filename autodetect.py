from __future__ import annotations

import winreg
from pathlib import Path

import winutil
from reader import Reader, find_line, parse_timer


def detect_wegame_exe() -> str | None:
    for win in winutil.list_windows():
        if win.exe_path.lower().endswith("wegame.exe"):
            return win.exe_path
    candidates = [
        r"D:\app\WeGame\wegame.exe",
        r"C:\Program Files (x86)\Tencent\WeGame\wegame.exe",
        r"C:\Program Files\Tencent\WeGame\wegame.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    registry_keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeGame"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\WeGame"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Tencent\WeGame"),
    )
    for root, key in registry_keys:
        try:
            with winreg.OpenKey(root, key) as handle:
                for value_name in ("InstallPath", "InstallDir", "Path"):
                    try:
                        value, _ = winreg.QueryValueEx(handle, value_name)
                    except OSError:
                        continue
                    exe = Path(value) / "wegame.exe"
                    if exe.exists():
                        return str(exe)
        except OSError:
            continue
    return None


def detect_game_info() -> dict | None:
    for win in winutil.list_windows():
        if "deltaforce" in win.exe_path.lower():
            return {
                "exe": win.exe_path.rsplit("\\", 1)[-1],
                "title": win.title.strip(),
                "path": win.exe_path,
            }
    return None


def detect_station_items(config: dict, reader: Reader) -> dict[str, str] | None:
    game = reader.find_game()
    if game is None:
        return None
    image, lines = reader.ocr(game)
    height = image.size[1]
    labels: dict[str, object] = {}
    for station in config["stations"]:
        hit = find_line(lines, station, y_min=height * 0.08)
        if hit is not None:
            labels[station] = hit
    if len(labels) < 3:
        return None
    result: dict[str, str] = {}
    for station, label in labels.items():
        best = None
        best_deviation = 999.0
        for line in lines:
            if line.cy <= label.cy:
                continue
            overlap = min(label.right, line.right) - max(label.left, line.left)
            if overlap <= 0:
                continue
            relative = (line.cy - label.cy) / height
            if not (0.04 <= relative <= 0.12):
                continue
            if parse_timer(line.text) is not None:
                continue
            deviation = abs(relative - 0.075)
            if deviation < best_deviation:
                best, best_deviation = line, deviation
        if best is not None:
            result[station] = best.text.strip()
    return result or None
