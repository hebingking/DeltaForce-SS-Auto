from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import cancel
import winutil
from accounts import get_accounts
from cancel import RunCancelled
from ocr_backend import normalize
from reader import BASE_DIR, Reader, find_line
from workshop import Workshop


HISTORY_KEEP_DAYS = 30

EXCHANGE_SUCCESS_WORDS = (
    "购买成功",
    "兑换成功",
    "交易成功",
    "兑换完成",
    "购买完成",
    "已兑换",
)
EXCHANGE_FAILURE_WORDS = (
    "购买失败",
    "兑换失败",
    "交易失败",
    "材料不足",
    "余额不足",
    "库存不足",
    "无法购买",
    "无法兑换",
)
DIALOG_MODAL_WORDS = (
    "清单",
    "军需处兑换",
    "确定用以下物品",
) + EXCHANGE_SUCCESS_WORDS + EXCHANGE_FAILURE_WORDS


class ExchangeError(RuntimeError):
    pass


def format_today_history(history: list[dict], items: list[str], threshold: float) -> str:
    today = date.today().isoformat()
    by_item: dict[str, dict] = {}
    for entry in history:
        if entry.get("date") == today:
            by_item[entry.get("item", "")] = entry
    lines: list[str] = []
    for item in items:
        entry = by_item.get(item)
        if entry is None:
            lines.append(f"待处理    {item}")
            continue
        result = entry.get("result")
        stamp = str(entry.get("time", ""))[11:16]
        discount = entry.get("discount")
        if result == "ok":
            suffix = f"（-{discount}%）" if discount is not None else ""
            lines.append(f"✓ 已兑换 {stamp}{suffix}    {item}")
        elif result == "disabled":
            lines.append(f"跳过 {stamp}（置灰）    {item}")
        elif result == "low_discount":
            lines.append(
                f"跳过 {stamp}（折扣 {discount}% < {threshold:g}%）    {item}"
            )
        elif result == "unknown_discount":
            lines.append(f"跳过 {stamp}（折扣未识别）    {item}")
        elif result == "error":
            lines.append(f"失败 {stamp}（见日志）    {item}")
        else:
            lines.append(f"{result} {stamp}    {item}")
    return "\n".join(lines) if lines else "暂无记录"


def format_accounts_history(
    history: list[dict], accounts: list[dict], items: list[str], threshold: float
) -> str:
    if not accounts:
        return format_today_history(history, items, threshold)
    today = date.today().isoformat()
    sections: list[str] = []
    for account in accounts:
        entries = [
            entry
            for entry in history
            if entry.get("date") == today
            and entry.get("account") == account["key"]
        ]
        sections.append(f"【{account['label']}】")
        sections.append(format_today_history(entries, items, threshold))
    return "\n".join(sections)


class ExchangeRunner:
    def __init__(
        self,
        config: dict,
        logger=print,
        reader: Reader | None = None,
        base_dir=None,
        history_path=None,
    ):
        self.config = config
        self.log = logger
        self.reader = reader or Reader(config, logger)
        self.exchange_config = config.get("exchange", {})
        self.items = [
            str(item).strip()
            for item in self.exchange_config.get("items", [])
            if str(item).strip()
        ]
        self._search_box = None
        self.workshop = Workshop(config, logger, self.reader)
        self.base_dir = Path(base_dir) if base_dir else BASE_DIR
        self.history_path = (
            Path(history_path)
            if history_path
            else self.base_dir / "exchange_history.json"
        )
        self.history = self._load_history()

    def _default_account_key(self) -> str:
        accounts = get_accounts(self.config)
        return accounts[0]["key"] if accounts else ""

    def _load_history(self) -> list[dict]:
        entries: list[dict] = []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            raw = data.get("entries", [])
            if isinstance(raw, list):
                entries = raw
        except Exception:
            entries = []
        default_key = self._default_account_key()
        changed = False
        if default_key:
            for entry in entries:
                if "account" not in entry:
                    entry["account"] = default_key
                    changed = True
        if changed:
            self.history = entries
            self._save_history()
        return entries

    def _save_history(self) -> None:
        cutoff = date.today() - timedelta(days=HISTORY_KEEP_DAYS - 1)
        kept: list[dict] = []
        for entry in self.history:
            try:
                entry_date = date.fromisoformat(str(entry.get("date")))
            except Exception:
                continue
            if entry_date >= cutoff:
                kept.append(entry)
        self.history = kept
        try:
            temp = self.history_path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps({"entries": kept}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self.history_path)
        except OSError as exc:
            self.log(f"[exchange] 保存兑换记录失败: {exc}")

    def _record(self, item: str, result: str, discount=None, account_key: str = "") -> None:
        today = date.today().isoformat()
        account_key = account_key or self._default_account_key()
        entry = {
            "date": today,
            "account": account_key,
            "item": item,
            "time": datetime.now().isoformat(timespec="seconds"),
            "discount": discount,
            "result": result,
        }
        for index, existing in enumerate(self.history):
            if (
                existing.get("date") == today
                and existing.get("item") == item
                and existing.get("account") == account_key
            ):
                self.history[index] = entry
                break
        else:
            self.history.append(entry)
        self._save_history()

    def today_results(self, account_key: str = "") -> dict[str, str]:
        today = date.today().isoformat()
        account_key = account_key or self._default_account_key()
        return {
            entry.get("item", ""): entry.get("result", "")
            for entry in self.history
            if entry.get("date") == today and entry.get("account") == account_key
        }

    def run(self, game, account_key: str = "") -> list[dict]:
        account_key = account_key or self._default_account_key()
        if not self.exchange_config.get("enabled", True):
            self.log("[exchange] 未启用，跳过交易行兑换")
            return []
        if not self.items:
            self.log("[exchange] 没有配置子弹，跳过")
            return []
        if not self._open_market(game):
            raise ExchangeError("未能进入交易行页面")
        cancel.check()
        results: list[dict] = []
        today = self.today_results(account_key)
        for name in self.items:
            cancel.check()
            status = today.get(name)
            if status in ("ok", "disabled"):
                reason = "今日已兑换" if status == "ok" else "今日已置灰"
                self.log(f"[exchange] {name} {reason}，直接跳过")
                results.append(
                    {
                        "item": name,
                        "ok": status == "ok",
                        "skipped": "done_today",
                    }
                )
                continue
            try:
                result = self._exchange_one(game, name)
                results.append(result)
                self._record(
                    name,
                    "ok" if result.get("ok") else str(result.get("skipped") or "error"),
                    result.get("discount"),
                    account_key,
                )
            except RunCancelled:
                raise
            except Exception as exc:
                self.log(f"[exchange] {name} 处理失败: {exc}")
                self._back_to_search(game)
                results.append({"item": name, "ok": False, "error": str(exc)})
                self._record(name, "error", account_key=account_key)
        self._return_to_teqin(game)
        self.log(f"[exchange] 本轮结果: {results}")
        return results

    # ---------- helpers ----------
    def _snapshot(self, game):
        cancel.check()
        try:
            return self.reader.ocr(game)
        except Exception as exc:
            self.log(f"[exchange] OCR 失败: {exc}")
            return None

    @staticmethod
    def _text(lines) -> str:
        return "".join(normalize(line.text) for line in lines)

    def _market_ready(self, lines, size) -> bool:
        text = self._text(lines)
        if "请输入想要查找的商品" in text:
            return True
        if "购买" in text and ("我的收藏" in text or "弹药" in text):
            return True
        if "出售" in text and "弹药" in text:
            return True
        return False

    def _open_market(self, game) -> bool:
        snapshot = self._snapshot(game)
        if snapshot is None:
            return False
        image, lines = snapshot
        if self._market_ready(lines, image.size):
            return True
        nav = find_line(lines, "交易行", y_max=image.size[1] * 0.12)
        if nav is None:
            return False
        self.log(f"[exchange] 进入交易行 @({nav.cx:.0f},{nav.cy:.0f})")
        winutil.click_client(game.hwnd, nav.cx, nav.cy)
        deadline = time.time() + 15
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                if self._market_ready(lines, image.size):
                    return True
            time.sleep(1.5)
        return False

    def _search_box_xy(self, image, lines):
        width, height = image.size if image else (1920, 1080)
        cached = self._search_box
        if cached is not None and cached[0] == width and cached[1] == height:
            return cached[2], cached[3]
        box = find_line(
            lines,
            "请输入想要查找的商品",
            min_score=0.55,
            x_max=width * 0.22,
        )
        if box is None:
            for name in self.items:
                box = find_line(
                    lines,
                    name,
                    min_score=0.7,
                    x_max=width * 0.22,
                    y_max=height * 0.2,
                )
                if box is not None:
                    break
        if box is not None:
            self._search_box = (width, height, box.cx, box.cy)
            self.log(f"[exchange] 搜索框 @({box.cx:.0f},{box.cy:.0f})")
            return box.cx, box.cy
        if cached is not None:
            self._search_box = None
        return width * 0.115, height * 0.13

    def _search_text(self, game, name: str, use_unicode: bool = False) -> None:
        snapshot = self._snapshot(game)
        image = snapshot[0] if snapshot else None
        lines = snapshot[1] if snapshot else []
        x, y = self._search_box_xy(image, lines)
        winutil.click_client(game.hwnd, x, y)
        time.sleep(0.3)
        winutil.press_ctrl("a")
        time.sleep(0.1)
        if use_unicode:
            winutil.type_text_unicode(name)
            time.sleep(0.4)
        else:
            winutil.set_clipboard_text(name)
            winutil.press_ctrl("v")
            time.sleep(0.6)
        winutil.press_key("enter")
        time.sleep(2.5)

    def _find_card(self, lines, size, name):
        width, height = size
        return find_line(
            lines,
            name,
            min_score=0.7,
            x_min=width * 0.18,
            x_max=width * 0.5,
            y_min=height * 0.1,
            y_max=height * 0.35,
        )

    def _search_and_open(self, game, name: str) -> None:
        self._search_text(game, name)
        if self._wait_card_and_click(game, name, 10):
            return
        self.log("[exchange] 剪贴板搜索未命中，改用 Unicode 输入重试")
        self._search_text(game, name, use_unicode=True)
        if not self._wait_card_and_click(game, name, 8):
            raise ExchangeError("搜索结果中未找到目标物品")

    def _wait_card_and_click(self, game, name: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                card = self._find_card(lines, image.size, name)
                if card is not None:
                    self.log(f"[exchange] 打开搜索结果 @({card.cx:.0f},{card.cy:.0f})")
                    winutil.click_client(game.hwnd, card.cx, card.cy)
                    time.sleep(2.0)
                    return True
            time.sleep(1.5)
        return False

    def _reopen_result(self, game, name: str) -> bool:
        deadline = time.time() + 8
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                card = self._find_card(lines, image.size, name)
                if card is not None:
                    self.log(
                        f"[exchange] 重新打开搜索结果 @({card.cx:.0f},{card.cy:.0f})"
                    )
                    winutil.click_client(game.hwnd, card.cx, card.cy)
                    time.sleep(2.0)
                    return True
            time.sleep(1.2)
        return False

    def _find_exchange_button_once(self, image, lines):
        width, height = image.size
        button = self._exact_button(lines, "兑换", height * 0.8, width * 0.7)
        if button is None:
            button = self._loose_button(lines, "兑换", height * 0.8, width * 0.7)
        return button

    @staticmethod
    def _exact_button(lines, label: str, y_min: float, x_min: float):
        for line in lines:
            if normalize(line.text) == label and line.cy >= y_min and line.cx >= x_min:
                return line
        return None

    @staticmethod
    def _loose_button(lines, label: str, y_min: float, x_min: float):
        for line in lines:
            norm = normalize(line.text)
            if (
                label in norm
                and len(norm) <= len(label) + 2
                and line.cy >= y_min
                and line.cx >= x_min
            ):
                return line
        return None

    @staticmethod
    def _text_brightness(image, line) -> float:
        pixels = image.load()
        left = max(0, int(line.left))
        right = min(image.width - 1, int(line.right))
        top = max(0, int(line.top))
        bottom = min(image.height - 1, int(line.bottom))
        lums = []
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                r, g, b = pixels[x, y]
                lums.append((r + g + b) / 3)
        if not lums:
            return 0.0
        lums.sort()
        return lums[min(len(lums) - 1, int(len(lums) * 0.95))]

    @staticmethod
    def _parse_discount(lines) -> int | None:
        preferred = []
        others = []
        for line in lines:
            text = line.text
            if "%" not in text and "％" not in text:
                continue
            if "兑换成本" in text or "预估" in text:
                preferred.append(line)
            elif line.cy > 60:
                others.append(line)
        for line in preferred + others:
            numbers = re.findall(r"(\d{1,3})\s*[%％]", line.text)
            if numbers:
                return int(numbers[-1])
        return None

    def _find_modal_exchange(self, lines, size):
        width, height = size
        for line in lines:
            if (
                normalize(line.text) == "兑换"
                and height * 0.6 <= line.cy <= height * 0.8
                and width * 0.4 <= line.cx <= width * 0.6
            ):
                return line
        return None

    def _exchange_one(self, game, name: str) -> dict:
        self.log(f"[exchange] === 处理 {name} ===")
        button = None
        image = None
        lines = []
        for open_attempt in range(3):
            if open_attempt == 0:
                self._search_and_open(game, name)
            else:
                if not self._reopen_result(game, name):
                    self._search_and_open(game, name)
            deadline = time.time() + 10
            snapshot = None
            while time.time() < deadline:
                snapshot = self._snapshot(game)
                if snapshot is not None:
                    image, lines = snapshot
                    text = self._text(lines)
                    if "在售最低价" in text or "兑换" in text:
                        break
                time.sleep(1.2)
            if snapshot is not None:
                button = self._find_exchange_button_once(image, lines)
                if button is None:
                    for _ in range(2):
                        time.sleep(1.5)
                        snapshot = self._snapshot(game)
                        if snapshot is not None:
                            image, lines = snapshot
                            button = self._find_exchange_button_once(image, lines)
                            if button is not None:
                                break
            if button is not None:
                break
            if open_attempt < 2:
                self.log(
                    f"[exchange] {name} 第{open_attempt + 1}次未识别到兑换按钮，"
                    f"返回后重试"
                )
                self._back_to_search(game)
                time.sleep(1.0)
                continue
            raise ExchangeError("详情页未找到兑换按钮")
        hint = any("更优惠" in normalize(line.text) for line in lines)
        brightness = self._text_brightness(image, button)
        self.log(
            f"[exchange] 兑换按钮 @({button.cx:.0f},{button.cy:.0f})，"
            f"文字亮度 {brightness:.0f}，优惠提示: {'有' if hint else '无'}"
        )
        if brightness < 140:
            self.log("[exchange] 按钮为灰色（已兑换/不可用），不点击，跳过")
            self._back_to_search(game)
            return {"item": name, "ok": False, "skipped": "disabled"}
        winutil.click_client(game.hwnd, button.cx, button.cy)

        modal = None
        deadline = time.time() + 8
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                text = self._text(lines)
                if "军需处兑换" in text or "预估兑换成本" in text:
                    modal = (image, lines)
                    break
            time.sleep(1.5)
        if modal is None:
            self.log("[exchange] 点击兑换后没有弹窗（按钮应为置灰），跳过")
            self._back_to_search(game)
            return {"item": name, "ok": False, "skipped": "disabled"}

        image, lines = modal
        discount = self._parse_discount(lines)
        if discount is None:
            self.log("[exchange] 未识别到折扣百分比，跳过")
            winutil.press_key("escape")
            time.sleep(1.5)
            self._back_to_search(game)
            return {"item": name, "ok": False, "skipped": "unknown_discount"}
        threshold = float(self.exchange_config.get("min_discount_percent", 20))
        if discount < threshold:
            self.log(
                f"[exchange] 折扣 {discount}% 低于阈值 {threshold:g}%，不兑换"
            )
            winutil.press_key("escape")
            time.sleep(1.5)
            self._back_to_search(game)
            return {
                "item": name,
                "ok": False,
                "skipped": "low_discount",
                "discount": discount,
            }
        self.log(f"[exchange] 折扣 {discount}% 达标，开始兑换")
        ok = self._complete_exchange(game)
        self._back_to_search(game)
        return {"item": name, "ok": ok, "discount": discount}

    @staticmethod
    def _has_any(text: str, words) -> bool:
        return any(word in text for word in words)

    def _find_dismiss_button(self, lines, size):
        width, height = size
        for label in ("确定", "关闭", "完成", "知道了", "好的"):
            button = find_line(
                lines,
                label,
                min_score=0.85,
                x_min=width * 0.3,
                x_max=width * 0.7,
                y_min=height * 0.35,
                y_max=height * 0.9,
            )
            if button is not None:
                return button
        return None

    def _dismiss_result_dialog(self, game) -> bool:
        for _ in range(2):
            snapshot = self._snapshot(game)
            if snapshot is None:
                time.sleep(1.0)
                continue
            image, lines = snapshot
            text = self._text(lines)
            button = self._find_dismiss_button(lines, image.size)
            if button is not None:
                self.log(
                    f"[exchange] 关闭结果弹窗 @({button.cx:.0f},{button.cy:.0f})"
                )
                winutil.click_client(game.hwnd, button.cx, button.cy)
                time.sleep(1.5)
                continue
            if self._has_any(text, DIALOG_MODAL_WORDS):
                winutil.press_key("escape")
                time.sleep(1.2)
                continue
            return True
        snapshot = self._snapshot(game)
        if snapshot is None:
            return True
        return not self._has_any(self._text(snapshot[1]), DIALOG_MODAL_WORDS)

    def _wait_exchange_result(self, game) -> bool:
        deadline = time.time() + 15
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is None:
                time.sleep(1.2)
                continue
            image, lines = snapshot
            width, height = image.size
            text = self._text(lines)
            if self._has_any(text, EXCHANGE_SUCCESS_WORDS):
                self.log("[exchange] 识别到兑换成功")
                self._dismiss_result_dialog(game)
                return True
            if self._has_any(text, EXCHANGE_FAILURE_WORDS):
                self.log("[exchange] 识别到兑换失败/材料或余额不足")
                self._dismiss_result_dialog(game)
                return False
            confirm = find_line(
                lines,
                "确认",
                min_score=0.85,
                y_min=height * 0.5,
                y_max=height * 0.8,
                x_min=width * 0.45,
                x_max=width * 0.7,
            )
            if confirm is not None:
                self.log(
                    f"[exchange] 点击第二层确认 @({confirm.cx:.0f},{confirm.cy:.0f})"
                )
                winutil.click_client(game.hwnd, confirm.cx, confirm.cy)
                time.sleep(2.0)
                continue
            if self._has_any(text, DIALOG_MODAL_WORDS):
                button = self._find_dismiss_button(lines, image.size)
                if button is not None:
                    self.log(
                        f"[exchange] 关闭结果弹窗 @({button.cx:.0f},{button.cy:.0f})"
                    )
                    winutil.click_client(game.hwnd, button.cx, button.cy)
                    time.sleep(1.5)
                    continue
                winutil.press_key("escape")
                time.sleep(1.2)
                continue
            if self._market_ready(lines, image.size) or "兑换" in text:
                self.log("[exchange] 结果弹窗已关闭，按成功处理")
                return True
            time.sleep(1.2)
        self.log("[exchange] 等待兑换结果超时，按失败处理")
        self._dismiss_result_dialog(game)
        return False

    def _complete_exchange(self, game) -> bool:
        deadline = time.time() + 50
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is None:
                time.sleep(1.5)
                continue
            image, lines = snapshot
            width, height = image.size
            text = self._text(lines)

            if self._has_any(text, EXCHANGE_SUCCESS_WORDS):
                self.log("[exchange] 识别到兑换成功")
                self._dismiss_result_dialog(game)
                return True
            if self._has_any(text, EXCHANGE_FAILURE_WORDS):
                self.log("[exchange] 识别到兑换失败/材料或余额不足")
                self._dismiss_result_dialog(game)
                return False

            confirm = find_line(
                lines,
                "确认",
                min_score=0.85,
                y_min=height * 0.5,
                y_max=height * 0.8,
                x_min=width * 0.45,
                x_max=width * 0.7,
            )
            if confirm is not None:
                self.log(f"[exchange] 点击确认 @({confirm.cx:.0f},{confirm.cy:.0f})")
                winutil.click_client(game.hwnd, confirm.cx, confirm.cy)
                time.sleep(2.0)
                return self._wait_exchange_result(game)

            price = self.workshop._modal_price(lines, image.size)
            if price is not None:
                self.log(f"[exchange] 购买缺少材料，总价 {price[1]}")
                winutil.click_client(game.hwnd, price[0].cx, price[0].cy)
                time.sleep(2.5)
                continue

            button = self.workshop._find_green_button(image)
            if button is not None:
                self.log(
                    f"[exchange] 点击绿色按钮 @({button[0]},{button[1]})"
                )
                winutil.click_client(game.hwnd, button[0], button[1])
                time.sleep(2.5)
                continue

            modal_exchange = self._find_modal_exchange(lines, image.size)
            if modal_exchange is not None:
                self.log(
                    f"[exchange] 点击弹窗兑换 @({modal_exchange.cx:.0f},{modal_exchange.cy:.0f})"
                )
                winutil.click_client(
                    game.hwnd, modal_exchange.cx, modal_exchange.cy
                )
                time.sleep(2.0)
                continue

            time.sleep(1.5)
        self.log("[exchange] 兑换流程超时未完成")
        self._dismiss_result_dialog(game)
        return False

    def _search_page_ready(self, lines, size) -> bool:
        if not self._market_ready(lines, size):
            return False
        if find_line(lines, "请输入想要查找的商品", min_score=0.55) is not None:
            return True
        cached = self._search_box
        return bool(
            cached is not None
            and cached[0] == size[0]
            and cached[1] == size[1]
        )

    def _back_to_search(self, game) -> None:
        for _ in range(3):
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                text = self._text(lines)
                if self._has_any(
                    text, EXCHANGE_SUCCESS_WORDS + EXCHANGE_FAILURE_WORDS
                ):
                    self.log("[exchange] 发现结果弹窗，先关闭")
                    self._dismiss_result_dialog(game)
                    continue
                modal = any(
                    key in text for key in ("清单", "军需处兑换", "确定用以下物品")
                )
                if not modal and self._search_page_ready(lines, image.size):
                    return
                dismiss = self._find_dismiss_button(lines, image.size)
                if dismiss is not None:
                    self.log(
                        f"[exchange] 关闭弹窗 @({dismiss.cx:.0f},{dismiss.cy:.0f})"
                    )
                    winutil.click_client(game.hwnd, dismiss.cx, dismiss.cy)
                    time.sleep(1.5)
                    continue
            winutil.press_key("escape")
            time.sleep(1.5)
        snapshot = self._snapshot(game)
        if snapshot is not None:
            image, lines = snapshot
            nav = find_line(lines, "交易行", y_max=image.size[1] * 0.12)
            if nav is not None:
                self.log("[exchange] ESC 未确认交易行，点击顶部交易行恢复")
                winutil.click_client(game.hwnd, nav.cx, nav.cy)
                time.sleep(2.0)
        snapshot = self._snapshot(game)
        if snapshot is not None:
            image, lines = snapshot
            if not self._search_page_ready(lines, image.size):
                self.log("[exchange] 警告：未能回到交易行搜索页")

    def _return_to_teqin(self, game) -> None:
        snapshot = self._snapshot(game)
        if snapshot is not None:
            image, lines = snapshot
            if len(self.workshop._labels(lines, image.size)) >= 3:
                return
            nav = find_line(lines, "特勤处", y_max=image.size[1] * 0.12)
            if nav is not None:
                self.log("[exchange] 返回特勤处")
                winutil.click_client(game.hwnd, nav.cx, nav.cy)
                time.sleep(2.0)
        deadline = time.time() + 15
        while time.time() < deadline:
            snapshot = self._snapshot(game)
            if snapshot is not None:
                image, lines = snapshot
                if len(self.workshop._labels(lines, image.size)) >= 3:
                    return
            time.sleep(1.5)
        self.log("[exchange] 警告：没有回到特勤处页面")
