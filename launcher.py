from __future__ import annotations

import ctypes
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import cancel
import winutil
from accounts import AccountStore, parse_uid
from cancel import RunCancelled
from exchange import ExchangeRunner
from ocr_backend import normalize
from reader import LOGIN_KEYWORDS, Reader, find_line, match_score
from workshop import Workshop


class LaunchError(RuntimeError):
    pass


class Launcher:
    def __init__(self, config: dict, logger=print, reader: Reader | None = None):
        self.config = config
        self.log = logger
        self.reader = reader or Reader(config, logger)
        self.workshop = Workshop(config, logger, self.reader)
        self.exchange = ExchangeRunner(config, logger, self.reader)

    def run(self, account: dict | None = None) -> dict:
        label = account["label"] if account else "默认"
        self.log(
            "=" * 24
            + f" 流程开始 {datetime.now():%H:%M:%S}（{label}） "
            + "=" * 24
        )
        cancel.check()
        if winutil.is_desktop_locked():
            raise LaunchError("当前为锁屏/安全桌面，无法操作游戏")
        game = self._find_game()
        if game:
            uid = self._read_uid(game)
            expected = self._expected_uid(account) if account else ""
            if account and (uid is None or (expected and uid != expected)):
                self.log(
                    f"[account] 当前游戏账号 {uid or '未知'} 与目标 {label} 不符，"
                    f"退出后重新启动"
                )
                self._quit_game(game)
                game = None
            else:
                self.log(
                    f"[launcher] 游戏已在运行（UID {uid or '未读到'}），直接接管"
                )
                winutil.activate(game.hwnd)
        if game is None:
            self._open_delta_and_launch(account)
            game = self._wait_game_window()
        self._verify_account(game, account)
        self._drive_to_teqin(game)
        time.sleep(2.0)
        if self.config.get("exchange", {}).get("enabled", True):
            try:
                self.exchange.run(game, account["key"] if account else "")
            except RunCancelled:
                raise
            except Exception as exc:
                self.log(f"[launcher] 交易行兑换失败: {exc}")
            time.sleep(1.5)
        if self.config.get("craft", {}).get("enabled", True):
            try:
                self.workshop.run(game)
            except RunCancelled:
                raise
            except Exception as exc:
                self.log(f"[launcher] 领取/制作流程失败: {exc}")
            time.sleep(1.5)
        cancel.check()
        result = self.reader.read(game)
        cancel.check()
        self.log(
            "[launcher] 读取结果: "
            + json.dumps(result["timers"], ensure_ascii=False)
        )
        if account:
            result["account"] = account["key"]
            result["account_label"] = account["label"]
        if self.config.get("exit_game_after_read", True):
            self._quit_game(game)
        return result

    def _expected_uid(self, account: dict | None) -> str:
        if not account:
            return ""
        if account.get("uid"):
            return account["uid"]
        return AccountStore().load_uids().get(account["key"], "")

    def _read_uid(self, game: winutil.WindowInfo) -> str | None:
        cancel.check()
        try:
            _image, lines = self.reader.ocr(game)
            return parse_uid(lines)
        except Exception:
            return None

    def _verify_account(
        self, game: winutil.WindowInfo, account: dict | None
    ) -> None:
        if not account:
            return
        expected = self._expected_uid(account)
        deadline = time.time() + 90
        while time.time() < deadline:
            cancel.check()
            try:
                _image, lines = self.reader.ocr(game)
            except Exception:
                time.sleep(2)
                continue
            text = "".join(normalize(line.text) for line in lines)
            if any(keyword in text for keyword in LOGIN_KEYWORDS):
                raise LaunchError(
                    f"{account['label']} 处于登录/验证界面，已中止（需要你手动登录）"
                )
            uid = parse_uid(lines)
            if uid:
                if expected and uid != expected:
                    self.log(
                        f"[account] UID 不匹配：期望 {expected}，实际 {uid}"
                    )
                    self._quit_game(game)
                    raise LaunchError(
                        f"账号校验失败：目标 {account['label']}，实际 UID {uid}"
                    )
                if not expected:
                    store = AccountStore()
                    uids = store.load_uids()
                    uids[account["key"]] = uid
                    store.save_uids(uids)
                    self.log(
                        f"[account] 首次记录 {account['label']} UID {uid}"
                    )
                self.log(
                    f"[account] {account['label']} UID {uid} 校验通过"
                )
                return
            time.sleep(2)
        self._quit_game(game)
        raise LaunchError(f"等待 {account['label']} UID 超时，已退出游戏")

    def _quit_game(self, game: winutil.WindowInfo) -> None:
        self.log("[launcher] 读取完成，退出游戏")
        try:
            winutil.activate(game.hwnd)
            winutil.press_alt_f4()
        except RunCancelled:
            raise
        except Exception as exc:
            self.log(f"[launcher] 退出游戏失败: {exc}")
            return
        started = time.time()
        retried = False
        while time.time() - started < 15:
            if self._find_game() is None:
                self.log("[launcher] 游戏已退出")
                return
            if not retried and time.time() - started > 6:
                retried = True
                try:
                    winutil.activate(game.hwnd)
                    winutil.press_alt_f4()
                except RunCancelled:
                    raise
                except Exception:
                    pass
            time.sleep(1)
        self.log("[launcher] 等待游戏退出超时")

    def _find_game(self) -> winutil.WindowInfo | None:
        return winutil.find_window(
            exe_hint=self.config["game_process_hint"],
            title_contains=self.config.get("game_title_hint", ""),
        )

    def _find_wegame(self) -> winutil.WindowInfo | None:
        return winutil.find_window(exe_hint=self.config["wegeame_process_hint"])

    def close_wegame_window(self, delay_seconds: float = 0) -> None:
        delay = max(0.0, float(delay_seconds or 0))
        if delay > 0:
            self.log(f"[wegame] {delay:g} 秒后关闭 WeGame 窗口")
            deadline = time.time() + delay
            while time.time() < deadline:
                if cancel.requested():
                    self.log("[wegame] 已取消关闭 WeGame 窗口")
                    return
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
        window = self._find_wegame()
        if window is None:
            self.log("[wegame] 未发现 WeGame 窗口（可能已在托盘）")
            return
        self.log("[wegame] 关闭 WeGame 窗口")
        try:
            winutil.close_window(window.hwnd)
        except Exception as exc:
            self.log(f"[wegame] 关闭 WeGame 窗口失败: {exc}")
            return
        deadline = time.time() + 8
        while time.time() < deadline:
            if self._find_wegame() is None:
                self.log(
                    "[wegame] WeGame 窗口已关闭"
                    "（按 WeGame 设置可能最小化到托盘）"
                )
                return
            time.sleep(0.5)
        self.log("[wegame] WeGame 窗口关闭超时（可能弹出了确认框）")

    def _ocr(self, window: winutil.WindowInfo):
        cancel.check()
        try:
            image, lines = self.reader.ocr(window)
            return image.size, lines
        except Exception as exc:
            self.log(f"[launcher] OCR 失败: {exc}")
            return None, []

    @staticmethod
    def _text(lines) -> str:
        return "".join(normalize(line.text) for line in lines)

    def _click(self, window: winutil.WindowInfo, line) -> None:
        self.log(
            f"[launcher] 点击「{normalize(line.text)}」@({line.cx:.0f},{line.cy:.0f})"
        )
        winutil.click_client(window.hwnd, line.cx, line.cy)

    def _ensure_wegame(self) -> winutil.WindowInfo:
        window = self._find_wegame()
        if window:
            self.log("[launcher] WeGame 已在运行")
            winutil.activate(window.hwnd)
            return window

        exe = self.config["wegeame_exe"]
        self.log(f"[launcher] 启动 WeGame: {exe}")
        self._launch_exe(exe)
        timeout = self.config["timeouts"]["wegame_window"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cancel.check()
            window = self._find_wegame()
            if window:
                winutil.activate(window.hwnd)
                return window
            time.sleep(2)
        raise LaunchError("等待 WeGame 窗口超时")

    def _launch_exe(self, exe: str) -> None:
        try:
            subprocess.Popen([exe], cwd=str(Path(exe).parent))
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) != 740:
                raise
            self.log("[launcher] WeGame 需要管理员权限，调用 ShellExecute 触发 UAC")
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "open", exe, None, str(Path(exe).parent), 1
            )
            if result <= 32:
                raise LaunchError(
                    f"启动 WeGame 失败（ShellExecute 返回 {result}，可能拒绝了 UAC）"
                )

    def _open_delta_and_launch(self, account: dict | None = None) -> None:
        window = self._ensure_wegame()
        timeout = self.config["timeouts"]["wegame_home"]
        deadline = time.time() + timeout
        clicked_my_games = False
        opened_detail = False
        while time.time() < deadline:
            cancel.check()
            current = self._find_wegame()
            if current is not None and current.hwnd != window.hwnd:
                window = current
                winutil.activate(window.hwnd)
            size, lines = self._ocr(window)
            if not lines:
                time.sleep(2)
                continue
            width, height = size
            text = self._text(lines)
            if any(keyword in text for keyword in LOGIN_KEYWORDS):
                raise LaunchError("WeGame 处于登录界面，已中止（需要你手动登录）")

            sidebar = [
                line
                for line in lines
                if match_score("三角洲行动", line.text) >= 0.72
                and line.cx < width * 0.35
            ]
            if sidebar:
                sidebar.sort(key=lambda line: line.cx)
                self._click(window, sidebar[0])
                opened_detail = True
                time.sleep(3)
                break

            if not clicked_my_games:
                my_games = find_line(lines, "我的游戏")
                if my_games is not None:
                    self._click(window, my_games)
                    clicked_my_games = True
            time.sleep(2)

        if not opened_detail:
            raise LaunchError("WeGame 首页未找到「三角洲行动」")

        deadline = time.time() + self.config["timeouts"]["detail_page"]
        while time.time() < deadline:
            cancel.check()
            size, lines = self._ocr(window)
            if not lines:
                time.sleep(2)
                continue
            width, height = size
            starts = [
                line
                for line in lines
                if match_score("启动", line.text) >= 0.85
                and line.cx > width * 0.5
                and line.cy > height * 0.6
            ]
            if starts:
                starts.sort(key=lambda line: (line.cy, line.cx))
                start = starts[-1]
                if account is not None:
                    self._switch_wegame_account(
                        window, account, start, size, lines
                    )
                    fresh = self._wait_start_button(window)
                    if fresh is not None:
                        start = fresh
                self._click(window, start)
                return
            time.sleep(2)
        raise LaunchError("三角洲详情页未找到「启动」按钮")

    def _wait_start_button(self, window: winutil.WindowInfo):
        deadline = time.time() + 8
        while time.time() < deadline:
            cancel.check()
            size, lines = self._ocr(window)
            if lines:
                width, height = size
                starts = [
                    line
                    for line in lines
                    if match_score("启动", line.text) >= 0.85
                    and line.cx > width * 0.5
                    and line.cy > height * 0.6
                ]
                if starts:
                    starts.sort(key=lambda line: (line.cy, line.cx))
                    return starts[-1]
            time.sleep(1.0)
        return None

    def _account_identifiers(self) -> list[str]:
        values = {"QQ", "微信"}
        for item in self.config.get("accounts", {}).get("items", []):
            if not isinstance(item, dict):
                continue
            for value in (
                item.get("key"),
                item.get("label"),
                *item.get("matches", []),
            ):
                if value:
                    values.add(str(value))
        return [normalize(value).lower() for value in values if normalize(value)]

    def _find_account_button(self, lines, start, size):
        width, height = size
        band = max(height * 0.035, start.height)
        x_min = start.cx - width * 0.32
        x_max = start.cx - width * 0.015
        identifiers = self._account_identifiers()
        known = []
        generic = []
        for line in lines:
            if abs(line.cy - start.cy) > band:
                continue
            if line.cx < x_min or line.cx > x_max:
                continue
            norm = normalize(line.text).lower()
            if not norm or "启动" in norm or "下载" in norm:
                continue
            if any(identifier in norm for identifier in identifiers):
                known.append(line)
            elif len(norm) <= 10:
                generic.append(line)
        pool = known or generic
        if not pool:
            return None
        pool.sort(
            key=lambda line: (
                line.score,
                -abs(line.cy - start.cy),
                line.cx,
            )
        )
        return pool[-1]

    def _line_matches_account(self, line, account: dict) -> bool:
        norm = normalize(line.text).lower()
        for value in (
            account.get("key"),
            account.get("label"),
            *account.get("matches", []),
        ):
            if value and normalize(str(value)).lower() in norm:
                return True
        return False

    def _wait_account_menu(self, window, timeout: float = 8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            cancel.check()
            _size, lines = self._ocr(window)
            if lines:
                text = self._text(lines)
                if "无痕登录" in text or any(
                    "注销" in normalize(line.text) for line in lines
                ):
                    return lines
            time.sleep(1.0)
        return []

    def _switch_wegame_account(
        self,
        window: winutil.WindowInfo,
        account: dict,
        start,
        size,
        lines=None,
    ) -> None:
        width, height = size
        button = self._find_account_button(lines or [], start, size)
        if button is None:
            time.sleep(1.0)
            _size, retry_lines = self._ocr(window)
            button = self._find_account_button(retry_lines, start, size)
        legacy_x = start.cx - width * 0.145
        attempts = []
        if button is not None:
            attempts.append(
                (
                    button.cx,
                    button.cy,
                    f"账号按钮「{normalize(button.text)}」",
                )
            )
        if button is None or abs(button.cx - legacy_x) > 2:
            attempts.append((legacy_x, start.cy, "固定偏移"))
        popup_lines = []
        for x, y, label in attempts:
            self.log(
                f"[account] 打开账号选择器（{label}）@({x:.0f},{y:.0f})"
            )
            winutil.click_client(window.hwnd, x, y)
            popup_lines = self._wait_account_menu(window)
            if popup_lines:
                break
            self.log("[account] 未出现无痕登录选择框")
            time.sleep(0.8)
        if not popup_lines:
            _size, current_lines = self._ocr(window)
            current = self._find_account_button(current_lines, start, size)
            if current is not None and self._line_matches_account(
                current, account
            ):
                self.log(
                    f"[account] 选择器未打开，但当前账号已是 "
                    f"{account['label']}，继续"
                )
                return
            raise LaunchError(
                "未能打开 WeGame 账号选择器，已中止（避免用错账号启动）"
            )
        target = None
        for line in popup_lines:
            norm = normalize(line.text)
            if (
                any(match in norm for match in account["matches"])
                and line.cx > width * 0.5
                and line.cy < start.cy - height * 0.02
                and line.cy > height * 0.4
            ):
                target = line
                break
        if target is None:
            raise LaunchError(
                f"账号选择器中未找到「{account['label']}」"
            )
        click_x = target.left + target.width * 0.35
        self.log(
            f"[account] 选择 {account['label']} @({click_x:.0f},{target.cy:.0f})"
        )
        winutil.click_client(window.hwnd, click_x, target.cy)
        time.sleep(1.2)

    def _wait_game_window(self) -> winutil.WindowInfo:
        timeout = self.config["timeouts"]["game_window"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cancel.check()
            window = self._find_game()
            if window:
                self.log("[launcher] 游戏窗口已出现")
                winutil.activate(window.hwnd)
                return window
            time.sleep(2)
        raise LaunchError("等待三角洲游戏窗口超时")

    def _is_teqin_page(self, lines, height: float) -> bool:
        hits = 0
        for name in self.config["stations"]:
            if find_line(lines, name, y_min=height * 0.08) is not None:
                hits += 1
        return hits >= 3

    def _is_lobby(self, lines, height: float) -> bool:
        nav = find_line(lines, "特勤处", y_max=height * 0.2)
        start = find_line(lines, "开始游戏", y_max=height * 0.2)
        return nav is not None and start is not None

    def _drive_to_teqin(self, game: winutil.WindowInfo) -> None:
        timeouts = self.config["timeouts"]
        total = (
            timeouts["mode_select"]
            + timeouts["activity"]
            + timeouts["lobby"]
            + timeouts["teqin"]
            + 60
        )
        interval = self.config["poll"]["interval"]
        deadline = time.time() + total

        go_clicks = 0
        last_go = 0.0
        space_pressed = False
        space_at = 0.0
        last_tab = 0.0

        while time.time() < deadline:
            cancel.check()
            current = self._find_game()
            if current is not None and current.hwnd != game.hwnd:
                game = current
                winutil.activate(game.hwnd)
            size, lines = self._ocr(game)
            if not lines:
                time.sleep(1.5)
                continue
            height = size[1]
            text = self._text(lines)

            if any(keyword in text for keyword in LOGIN_KEYWORDS):
                raise LaunchError("游戏处于登录/验证界面，已中止（需要你手动登录）")

            if self._is_teqin_page(lines, height):
                self.log("[launcher] 已到达特勤处页面")
                return

            if self._is_lobby(lines, height):
                tab = find_line(lines, "特勤处", y_max=height * 0.2)
                if tab is not None:
                    self._click(game, tab)
                    time.sleep(2.5)
                    continue

            go = find_line(lines, "前往游玩")
            if go is not None and time.time() - last_go > 15 and go_clicks < 3:
                self._click(game, go)
                go_clicks += 1
                last_go = time.time()
                time.sleep(6)
                continue

            if ("空格" in text or "空投夺宝" in text) and not space_pressed:
                self.log("[launcher] 活动页面出现 → 按空格")
                winutil.press_key("space")
                space_pressed = True
                space_at = time.time()
                time.sleep(4)
                continue

            hint = find_line(lines, "返回大厅") or find_line(lines, "进入特勤处")
            tab_hint = any(
                "tab" in normalize(line.text).lower()
                and line.cy > height * 0.86
                for line in lines
            )
            now = time.time()
            should_tab = (
                hint is not None
                or tab_hint
                or (space_pressed and now - space_at > 6)
                or (go_clicks and now - last_go > 30)
            )
            if should_tab and now - last_tab > 6:
                reason = (
                    "页面提示"
                    if hint is not None
                    else "底部Tab提示"
                    if tab_hint
                    else "空格后等待"
                    if space_pressed
                    else "前往游玩后兜底"
                )
                self.log(f"[launcher] 按 Tab（{reason}）")
                winutil.press_key("tab")
                last_tab = now
                time.sleep(3)
                continue

            time.sleep(interval)

        raise LaunchError("等待进入大厅/特勤处超时")
