from __future__ import annotations

import re
import time
from datetime import datetime

import cancel
import winutil
from cancel import RunCancelled
from ocr_backend import OcrLine, normalize
from reader import BASE_DIR, Reader, find_line, parse_timer


class WorkshopError(RuntimeError):
    pass


class Workshop:
    def __init__(self, config: dict, logger=print, reader: Reader | None = None):
        self.config = config
        self.log = logger
        self.reader = reader or Reader(config, logger)
        self.craft_config = config.get("craft", {})
        self.items = {
            item["station"]: item for item in self.craft_config.get("items", [])
        }

    def run(self, game) -> list[dict]:
        cancel.check()
        if not self.craft_config.get("enabled", True):
            self.log("[craft] craft.enabled=false，跳过领取与制作")
            return []

        snapshot = self._snapshot(game)
        if snapshot is None:
            raise WorkshopError("特勤处页面 OCR 失败，无法开始领取/制作")
        image, lines = snapshot
        size = image.size
        labels = self._labels(lines, size)
        if len(labels) < len(self.config["stations"]):
            raise WorkshopError("特勤处工位标签不完整，无法开始领取/制作")

        producing = set(
            self.reader._assign_timers(labels, self._timers(lines), size[1]).keys()
        )
        self.log(f"[craft] 生产中的工位: {sorted(producing) or '无'}")

        results: list[dict] = []
        for station in self.config["stations"]:
            cancel.check()
            item = self.items.get(station, {})
            name = (item.get("name") or "").strip()

            if station in producing:
                self.log(f"[craft] {station} 仍在生产，跳过")
                results.append(
                    {"station": station, "claimed": False, "crafted": False,
                     "skipped": "producing"}
                )
                continue

            try:
                state = self._open_station(game, station)
                claimed = False
                if state == "reward":
                    self._dismiss_reward(game)
                    claimed = True
                    self.log(f"[craft] {station} 已领取奖励")
                    if not name:
                        results.append(
                            {"station": station, "claimed": True,
                             "crafted": False, "skipped": "no_item"}
                        )
                        continue
                    state = self._open_station(game, station)

                if not name:
                    self.log(f"[craft] {station} 未配置目标物品，只领取不制作")
                    self._back_to_teqin(game)
                    results.append(
                        {"station": station, "claimed": claimed,
                         "crafted": False, "skipped": "no_item"}
                    )
                    continue

                self._craft_on_page(game, station, item)
                results.append(
                    {"station": station, "claimed": claimed, "crafted": True}
                )
            except RunCancelled:
                raise
            except Exception as exc:
                self.log(f"[craft] {station} 处理失败: {exc}")
                self._back_to_teqin(game)
                results.append(
                    {"station": station, "claimed": False, "crafted": False,
                     "error": str(exc)}
                )

        self._back_to_teqin(game)
        self.log(f"[craft] 本轮结果: {results}")
        return results

    # ---------- 页面辅助 ----------
    def _snapshot(self, game):
        cancel.check()
        try:
            return self.reader.ocr(game)
        except Exception as exc:
            self.log(f"[craft] OCR 失败: {exc}")
            return None

    def _snapshot_lines(self, game) -> list[OcrLine]:
        snapshot = self._snapshot(game)
        return [] if snapshot is None else snapshot[1]

    @staticmethod
    def _text(lines: list[OcrLine]) -> str:
        return "".join(normalize(line.text) for line in lines)

    def _labels(self, lines: list[OcrLine], size) -> dict[str, OcrLine]:
        height = size[1]
        labels: dict[str, OcrLine] = {}
        for name in self.config["stations"]:
            line = find_line(lines, name, y_min=height * 0.08)
            if line is not None:
                labels[name] = line
        return labels

    @staticmethod
    def _timers(lines: list[OcrLine]) -> list[tuple[OcrLine, int]]:
        timers = []
        for line in lines:
            seconds = parse_timer(line.text)
            if seconds is not None:
                timers.append((line, seconds))
        return timers

    @staticmethod
    def _is_craft_page(lines: list[OcrLine]) -> bool:
        text = Workshop._text(lines)
        return any(
            keyword in text
            for keyword in ("一键补齐", "放入生产图纸", "参考价格", "仓库数量")
        )

    def _wait_for_teqin(self, game, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                if len(self._labels(lines, image.size)) >= 3:
                    return True
            time.sleep(1.0)
        return False

    def _back_to_teqin(self, game) -> None:
        snapshot = self._snapshot(game)
        if snapshot is not None:
            image, lines = snapshot
            if (
                len(self._labels(lines, image.size)) >= 3
                and "清单" not in self._text(lines)
            ):
                return
        winutil.press_key("escape")
        if self._wait_for_teqin(game, 7):
            return
        winutil.press_key("escape")
        if self._wait_for_teqin(game, 7):
            return
        self.log("[craft] 警告：ESC 后没有回到特勤处页面")

    def _open_station(self, game, station: str) -> str:
        snapshot = self._snapshot(game)
        if snapshot is None:
            raise WorkshopError("特勤处页面 OCR 失败")
        image, lines = snapshot
        size = image.size
        label = self._labels(lines, size).get(station)
        if label is None:
            raise WorkshopError(f"页面上找不到工位「{station}」")

        points = [
            (label.cx, label.cy + size[1] * 0.125),
            (label.cx, label.cy + size[1] * 0.165),
            (label.cx, label.cy + size[1] * 0.09),
        ]
        for px, py in points:
            self.log(f"[craft] 打开 {station} @({px:.0f},{py:.0f})")
            winutil.click_client(game.hwnd, px, py)
            deadline = time.time() + 8
            while time.time() < deadline:
                lines = self._snapshot_lines(game)
                text = self._text(lines)
                if "获得奖励" in text:
                    return "reward"
                if self._is_craft_page(lines):
                    return "craft"
                time.sleep(1.0)
        raise WorkshopError(f"{station} 点击后未进入奖励页或制作页")

    def _dismiss_reward(self, game) -> None:
        winutil.press_key("space")
        if self._wait_for_teqin(game, 8):
            return
        winutil.press_key("space")
        if self._wait_for_teqin(game, 8):
            return
        raise WorkshopError("奖励页未能关闭（空格无效）")

    def _bottom_button(
        self, lines: list[OcrLine], size, label: str
    ) -> OcrLine | None:
        width, height = size
        return find_line(
            lines,
            label,
            min_score=0.85,
            y_min=height * 0.72,
            x_min=width * 0.55,
        )

    def _find_item(self, lines: list[OcrLine], size, name: str) -> OcrLine | None:
        width, height = size
        hit = find_line(
            lines,
            name,
            min_score=0.8,
            x_max=width * 0.42,
            y_min=height * 0.08,
            y_max=height * 0.92,
        )
        if hit is not None:
            return hit
        return find_line(
            lines,
            name,
            min_score=0.8,
            y_min=height * 0.12,
            y_max=height * 0.92,
        )

    @staticmethod
    def _modal_price(lines: list[OcrLine], size) -> tuple[OcrLine, int] | None:
        width, height = size
        if not any("清单" in normalize(line.text) for line in lines):
            return None
        pattern = re.compile(r"\d{1,3}(?:,\d{3})+|\d{3,}")
        best: tuple[OcrLine, int] | None = None
        for line in lines:
            if line.cy < height * 0.5 or line.cy > height * 0.9:
                continue
            if line.cx < width * 0.3 or line.cx > width * 0.7:
                continue
            match = pattern.search(normalize(line.text))
            if not match:
                continue
            try:
                value = int(match.group(0).replace(",", ""))
            except ValueError:
                continue
            if value < 100:
                continue
            if best is None or value > best[1]:
                best = (line, value)
        return best

    @staticmethod
    def _find_green_button(image) -> tuple[int, int] | None:
        width, height = image.size
        pixels = image.load()
        y_start, y_end = int(height * 0.5), int(height * 0.9)
        x_start, x_end = int(width * 0.25), int(width * 0.75)
        step_y = max(1, height // 600)
        step_x = max(1, width // 640)

        def is_green(pixel) -> bool:
            r, g, b = pixel[0], pixel[1], pixel[2]
            return g > 110 and g > r + 35 and g > b + 5

        best_y = None
        best_count = 0
        for y in range(y_start, y_end, step_y):
            count = 0
            for x in range(x_start, x_end, step_x):
                if is_green(pixels[x, y]):
                    count += 1
            if count > best_count:
                best_count, best_y = count, y
        if best_y is None or best_count < 5:
            return None

        xs = [
            x
            for x in range(x_start, x_end, max(1, step_x // 2))
            if is_green(pixels[x, best_y])
        ]
        if not xs:
            return None
        x0, x1 = min(xs), max(xs)
        if x1 - x0 < width * 0.06:
            return None
        cx = (x0 + x1) // 2
        ys = [
            y
            for y in range(y_start, y_end, max(1, step_y // 2))
            if is_green(pixels[cx, y])
        ]
        if not ys:
            return None
        y0, y1 = min(ys), max(ys)
        if y1 - y0 < height * 0.015:
            return None
        return cx, (y0 + y1) // 2

    def _click_button(self, game, label: str, line: OcrLine) -> None:
        self.log(f"[craft] 点击「{label}」@({line.cx:.0f},{line.cy:.0f})")
        winutil.click_client(game.hwnd, line.cx, line.cy)

    def _save_craft_failure(self, image, station: str) -> None:
        if image is None:
            return
        try:
            out_dir = BASE_DIR / "logs" / "debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = (
                out_dir
                / f"craft_fail_{station}_{datetime.now():%Y%m%d_%H%M%S}.png"
            )
            image.save(path)
            self.log(f"[craft] 已保存失败现场: {path}")
        except Exception:
            pass

    def _find_item_scrolling(self, game, size, name: str) -> OcrLine | None:
        width, height = size
        sx, sy = width * 0.2, height * 0.5
        for _ in range(6):
            winutil.scroll_client(game.hwnd, sx, sy, 5)
        time.sleep(1.0)
        lines = self._snapshot_lines(game)
        hit = self._find_item(lines, size, name)
        if hit is not None:
            return hit
        for _ in range(6):
            winutil.scroll_client(game.hwnd, sx, sy, -3)
            time.sleep(0.8)
            lines = self._snapshot_lines(game)
            hit = self._find_item(lines, size, name)
            if hit is not None:
                return hit
        return None

    def _craft_on_page(self, game, station: str, item: dict) -> None:
        name = (item.get("name") or "").strip()
        snapshot = self._snapshot(game)
        if snapshot is None:
            raise WorkshopError("制作页 OCR 失败")
        image, lines = snapshot
        size = image.size

        hit = self._find_item(lines, size, name)
        if hit is None:
            self.log(f"[craft] 列表首屏未找到「{name}」，开始滚动查找")
            hit = self._find_item_scrolling(game, size, name)
        if hit is not None:
            self.log(f"[craft] 选择物品「{name}」@({hit.cx:.0f},{hit.cy:.0f})")
            winutil.click_client(game.hwnd, hit.cx, hit.cy)
        else:
            fallback = item.get("fallback_xy")
            if not fallback:
                raise WorkshopError(
                    f"制作列表未找到「{name}」，可在 config 里配置 fallback_xy"
                )
            self.log(
                f"[craft] 使用坐标兜底选择「{name}」@({fallback[0]},{fallback[1]})"
            )
            winutil.click_client(game.hwnd, fallback[0], fallback[1])
        time.sleep(1.8)

        snapshot = self._snapshot(game)
        if snapshot is None:
            raise WorkshopError("选择物品后 OCR 失败")
        image, lines = snapshot
        size = image.size

        produce = self._bottom_button(lines, size, "生产")
        if produce is not None:
            self._click_button(game, "生产", produce)
            time.sleep(2.0)
            self._back_to_teqin(game)
            return

        fill = self._bottom_button(lines, size, "一键补齐")
        if fill is None:
            raise WorkshopError("未找到「一键补齐」或「生产」按钮")
        self._click_button(game, "一键补齐", fill)
        time.sleep(1.8)

        price_line: OcrLine | None = None
        total: int | None = None
        last_image = None
        deadline = time.time() + 18
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                size = image.size
                last_image = image
                found = self._modal_price(lines, size)
                if found is not None:
                    price_line, total = found
                    break
                if self._bottom_button(lines, size, "生产") is not None:
                    break
            time.sleep(1.2)

        max_total = self.craft_config.get("max_purchase_total")
        if price_line is None:
            produce = self._bottom_button(lines, size, "生产")
            if produce is not None:
                self._click_button(game, "生产", produce)
                time.sleep(2.0)
                self._back_to_teqin(game)
                return
            button = (
                self._find_green_button(last_image)
                if last_image is not None
                else None
            )
            if button is not None and max_total is None:
                self.log(
                    f"[craft] 价格未识别，按绿色按钮定位购买"
                    f" @({button[0]},{button[1]})"
                )
                winutil.click_client(game.hwnd, button[0], button[1])
                time.sleep(2.0)
            else:
                self._save_craft_failure(last_image, station)
                raise WorkshopError("一键补齐清单未出现购买按钮")
        else:
            if max_total is not None and total is not None and total > max_total:
                self.log(f"[craft] 清单总价 {total} 超过上限 {max_total}，放弃购买")
                winutil.press_key("escape")
                time.sleep(1.5)
                self._back_to_teqin(game)
                raise WorkshopError(f"购买总价 {total} 超过上限 {max_total}")

            self.log(f"[craft] 一键补齐 总价 {total}，点击购买")
            winutil.click_client(game.hwnd, price_line.cx, price_line.cy)
            time.sleep(2.0)

        produced = False
        deadline = time.time() + 20
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                size = image.size
                produce = self._bottom_button(lines, size, "生产")
                if produce is not None:
                    self._click_button(game, "生产", produce)
                    produced = True
                    break
            time.sleep(1.5)
        if not produced:
            raise WorkshopError("购买完成后未出现「生产」按钮")
        time.sleep(2.0)
        self._back_to_teqin(game)
