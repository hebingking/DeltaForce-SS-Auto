from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageDraw

import winutil
from ocr_backend import OcrLine, create_backend, normalize

TIMER_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
LOGIN_KEYWORDS = ("扫码", "验证码", "实名认证", "安全验证", "密码登录", "请登录")

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else BASE_DIR / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def match_score(target: str, text: str) -> float:
    target = normalize(target)
    text = normalize(text)
    if not target or not text:
        return 0.0
    if target in text:
        return 1.0
    best = 0.0
    for length in range(max(1, len(target) - 1), len(target) + 2):
        if length > len(text):
            continue
        for start in range(len(text) - length + 1):
            best = max(
                best,
                SequenceMatcher(None, target, text[start : start + length]).ratio(),
            )
    return best


def find_line(
    lines: list[OcrLine],
    target: str,
    min_score: float = 0.72,
    y_max: float | None = None,
    y_min: float | None = None,
    x_max: float | None = None,
    x_min: float | None = None,
) -> OcrLine | None:
    best: OcrLine | None = None
    best_score = 0.0
    for line in lines:
        if y_max is not None and line.cy > y_max:
            continue
        if y_min is not None and line.cy < y_min:
            continue
        if x_max is not None and line.cx > x_max:
            continue
        if x_min is not None and line.cx < x_min:
            continue
        score = match_score(target, line.text)
        if score >= min_score and score > best_score:
            best, best_score = line, score
    return best


def parse_timer(text: str) -> int | None:
    match = TIMER_RE.search(normalize(text))
    if not match:
        return None
    hours, minutes, seconds = (int(match.group(i)) for i in range(1, 4))
    if hours > 47 or minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


class ReaderError(RuntimeError):
    pass


class Reader:
    def __init__(self, config: dict, logger=print):
        self.config = config
        self.log = logger
        self.backend = None

    def _backend(self):
        if self.backend is None:
            self.backend = create_backend(self.config, self.log)
        return self.backend

    def find_game(self) -> winutil.WindowInfo | None:
        return winutil.find_window(
            exe_hint=self.config["game_process_hint"],
            title_contains=self.config.get("game_title_hint", ""),
        )

    def _fast_params(self) -> tuple[int, float]:
        ocr_config = self.config.get("ocr", {})
        return (
            int(ocr_config.get("fast_max_dimension", 2000)),
            float(ocr_config.get("fast_upscale", 1.0)),
        )

    def ocr(
        self,
        window: winutil.WindowInfo,
        region=None,
        fast: bool = False,
    ) -> tuple[Image.Image, list[OcrLine]]:
        """截图 + OCR。

        region 为客户端像素坐标 (x0, y0, x1, y1)，只识别该区域；
        返回的仍是整幅截图，识别框坐标已还原到整幅坐标系，调用方无需改阈值。
        fast=True 用于高频轮询（兑换流程），用更低分辨率换取速度。
        """
        hwnd = window.hwnd
        already_focused = winutil.is_foreground(hwnd)
        if not winutil.activate(hwnd):
            raise ReaderError("游戏窗口无法切到前台")
        if not already_focused:
            time.sleep(0.15)
        image = winutil.capture_client(hwnd)
        kwargs = {}
        if fast:
            max_dim, upscale = self._fast_params()
            kwargs = {"max_dim": max_dim, "upscale": upscale}
        return image, self._backend().recognize(
            image, region=region, fast=fast, **kwargs
        )

    def read(
        self,
        window: winutil.WindowInfo | None = None,
        click_tab: bool = True,
        debug_dir: str | Path | None = None,
    ) -> dict:
        window = window or self.find_game()
        if not window:
            raise ReaderError("三角洲进程未运行")

        last_image: Image.Image | None = None
        last_lines: list[OcrLine] = []
        for attempt in range(6):
            current = self.find_game()
            if current is not None:
                window = current
            image, lines = self.ocr(window)
            last_image, last_lines = image, lines
            parsed = self._parse(lines, image.size)
            if parsed is not None:
                idle = [
                    name
                    for name in self.config["stations"]
                    if name not in parsed
                ]
                result = {
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "window": {"title": window.title, "exe": window.exe_path},
                    "client_size": list(image.size),
                    "timers": parsed,
                    "idle_stations": idle,
                }
                if debug_dir:
                    self._save_debug(image, lines, parsed, debug_dir)
                return result

            text = "".join(normalize(line.text) for line in lines)
            if any(keyword in text for keyword in LOGIN_KEYWORDS):
                raise ReaderError("游戏处于登录/验证界面，无法读取")
            labels_present = self._count_stations(lines)
            timer_count = sum(
                1 for line in lines if parse_timer(line.text) is not None
            )
            self.log(
                f"[reader] 第{attempt + 1}次未解析: 工位标签 {labels_present}/4,"
                f" 倒计时候选 {timer_count}"
            )
            for name in self.config["stations"]:
                label = find_line(lines, name, y_min=0)
                if label is not None:
                    self.log(
                        f"[reader]   {name} 标签@({label.left:.0f},{label.top:.0f})"
                        f" {label.width:.0f}x{label.height:.0f}"
                    )
            for line in lines:
                if parse_timer(line.text) is not None:
                    self.log(
                        f"[reader]   倒计时 {normalize(line.text)}@"
                        f"({line.left:.0f},{line.top:.0f})"
                        f" {line.width:.0f}x{line.height:.0f}"
                    )
            self._save_debug(
                image, lines, None, BASE_DIR / "logs" / "debug" / "fail"
            )
            if click_tab and attempt < 2 and labels_present == 0:
                if self._click_teqin_tab(window, lines, image.size):
                    self.log("[reader] 尝试点击顶部「特勤处」")
                    time.sleep(1.5)
                    continue
            time.sleep(1.2)

        if debug_dir and last_image is not None:
            self._save_debug(last_image, last_lines, None, debug_dir)
        raise ReaderError("未识别到四个工位倒计时，请确认停在特勤处页面")

    def _parse(
        self, lines: list[OcrLine], size: tuple[int, int]
    ) -> dict[str, dict] | None:
        height = size[1]
        labels: dict[str, OcrLine] = {}
        for name in self.config["stations"]:
            label = find_line(lines, name, y_min=height * 0.08)
            if label is None:
                return None
            labels[name] = label

        timers: list[tuple[OcrLine, int]] = []
        for line in lines:
            seconds = parse_timer(line.text)
            if seconds is not None:
                timers.append((line, seconds))
        return self._assign_timers(labels, timers, height)

    def _assign_timers(
        self,
        labels: dict[str, OcrLine],
        timers: list[tuple[OcrLine, int]],
        height: int,
    ) -> dict[str, dict]:
        typical_gap = height * 0.1825
        tolerance = height * 0.06
        used_timers: set[int] = set()
        result: dict[str, dict] = {}
        complete = True
        for station, label in labels.items():
            candidates: list[tuple[float, float, OcrLine, int]] = []
            for timer_line, seconds in timers:
                if id(timer_line) in used_timers:
                    continue
                if label.cy >= timer_line.cy:
                    continue
                overlap = min(label.right, timer_line.right) - max(
                    label.left, timer_line.left
                )
                if overlap <= 0:
                    continue
                ratio = overlap / max(1.0, min(label.width, timer_line.width))
                if ratio < 0.3:
                    continue
                gap = timer_line.cy - label.cy
                deviation = abs(gap - typical_gap)
                if deviation > tolerance:
                    continue
                candidates.append((deviation, -ratio, timer_line, seconds))
            if not candidates:
                complete = False
                continue
            candidates.sort(key=lambda item: (item[0], item[1]))
            _deviation, _neg_ratio, timer_line, seconds = candidates[0]
            used_timers.add(id(timer_line))
            result[station] = {
                "text": normalize(timer_line.text),
                "seconds": seconds,
            }
        if complete and len(result) == len(labels):
            return result
        fallback = self._pair_row_major(labels, timers, height)
        return fallback if fallback is not None else result

    @staticmethod
    def _pair_row_major(
        labels: dict[str, OcrLine],
        timers: list[tuple[OcrLine, int]],
        height: int,
    ) -> dict[str, dict] | None:
        band = max(40.0, height * 0.1)
        label_items = sorted(
            labels.items(), key=lambda item: (round(item[1].cy / band), item[1].cx)
        )
        timer_items = sorted(
            timers, key=lambda item: (round(item[0].cy / band), item[0].cx)
        )
        if len(label_items) != len(timer_items):
            return None
        result: dict[str, dict] = {}
        for (station, label), (timer_line, seconds) in zip(label_items, timer_items):
            if timer_line.cy <= label.cy:
                return None
            overlap = min(label.right, timer_line.right) - max(
                label.left, timer_line.left
            )
            if overlap <= 0:
                return None
            ratio = overlap / max(1.0, min(label.width, timer_line.width))
            if ratio < 0.15:
                return None
            result[station] = {
                "text": normalize(timer_line.text),
                "seconds": seconds,
            }
        return result

    def _count_stations(self, lines: list[OcrLine]) -> int:
        return sum(
            1
            for name in self.config["stations"]
            if find_line(lines, name, y_min=0) is not None
        )

    def _click_teqin_tab(
        self, window: winutil.WindowInfo, lines: list[OcrLine], size: tuple[int, int]
    ) -> bool:
        _width, height = size
        hit = find_line(lines, "特勤处", y_max=height * 0.2)
        if hit is None:
            return False
        winutil.click_client(window.hwnd, hit.cx, hit.cy)
        return True

    def _save_debug(
        self,
        image: Image.Image,
        lines: list[OcrLine],
        parsed: dict | None,
        debug_dir: str | Path,
    ) -> None:
        out_dir = Path(debug_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        draw = ImageDraw.Draw(image)
        for line in lines:
            draw.rectangle(
                [line.left, line.top, line.right, line.bottom],
                outline=(0, 255, 0),
                width=2,
            )
            draw.text(
                (line.left, max(0, line.top - 12)),
                normalize(line.text)[:32],
                fill=(0, 255, 0),
            )
        path = out_dir / f"reader_{datetime.now():%Y%m%d_%H%M%S}.png"
        image.save(path)
        self.log(f"[reader] debug 截图: {path} (parsed={bool(parsed)})")


def main() -> None:
    parser = argparse.ArgumentParser(description="读取三角洲行动特勤处四个工位倒计时")
    parser.add_argument("--image", help="离线识别指定截图")
    parser.add_argument("--debug", action="store_true", help="保存标注截图")
    args = parser.parse_args()

    config = load_config()
    reader = Reader(config)

    debug_dir = BASE_DIR / "logs" / "debug" if args.debug else None

    if args.image:
        image = Image.open(args.image).convert("RGB")
        lines = reader._backend().recognize(image)
        parsed = reader._parse(lines, image.size)
        if debug_dir:
            reader._save_debug(image, lines, parsed, debug_dir)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return

    result = reader.read(debug_dir=debug_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
