from __future__ import annotations

import ctypes
import json
import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from apscheduler.schedulers.base import STATE_PAUSED

import autodetect
from accounts import get_accounts
from exchange import format_accounts_history
from planner import compute_schedule


class SettingsWindow:
    def __init__(self, config, logger, reader, schedule, base_dir):
        self.config = config
        self.logger = logger
        self.reader = reader
        self.schedule = schedule
        self.base_dir = Path(base_dir)
        self._last_log = ""

        self.root = tk.Tk()
        self.root.title("三角洲特勤处助手")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(860, max(720, screen_width - 120))
        window_height = min(1000, max(560, screen_height - 60))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(700, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._build()
        self._load_fields()
        self._refresh_schedule()
        self._refresh_exchange_status()
        self._refresh_log()

    # ---------- UI ----------
    def _build(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", side="bottom", padx=10, pady=(0, 8))
        ttk.Button(footer, text="保存配置", command=self._save).pack(side="left")
        ttk.Button(footer, text="重新加载", command=self._reload).pack(
            side="left", padx=6
        )
        ttk.Button(footer, text="打开配置文件", command=self._open_config).pack(
            side="left"
        )
        ttk.Button(
            footer, text="取消当前运行(F12)", command=self._cancel_run
        ).pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(footer, textvariable=self.status_var).pack(side="right")

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        main = ttk.Frame(canvas, padding=10)
        canvas_window = canvas.create_window((0, 0), window=main, anchor="nw")

        def _sync_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event):
            canvas.itemconfig(canvas_window, width=event.width)
            _sync_scrollregion()

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        main.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_width)
        canvas.bind_all("<MouseWheel>", _on_wheel)

        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(
            header, text="三角洲特勤处助手", font=("Microsoft YaHei UI", 14, "bold")
        ).pack(side="left")
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        self.admin_var = tk.StringVar(
            value=f"管理员权限：{'是' if is_admin else '否'}"
        )
        ttk.Label(header, textvariable=self.admin_var).pack(side="right")

        paths = ttk.LabelFrame(main, text="基础配置", padding=8)
        paths.pack(fill="x", pady=(8, 0))
        self.wegame_var = tk.StringVar()
        self.proc_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self._path_row(paths, "WeGame 路径", self.wegame_var, self._auto_wegame, browse=True)
        self._path_row(paths, "游戏进程名", self.proc_var, self._auto_game)
        self._path_row(paths, "游戏窗口标题", self.title_var, self._auto_game)

        items = ttk.LabelFrame(
            main, text="四工位目标物品（留空 = 只领取不制作）", padding=8
        )
        items.pack(fill="x", pady=(8, 0))
        self.item_vars: dict[str, tk.StringVar] = {}
        for station in self.config["stations"]:
            row = ttk.Frame(items)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=station, width=10).pack(side="left")
            var = tk.StringVar()
            self.item_vars[station] = var
            ttk.Entry(row, textvariable=var).pack(
                side="left", fill="x", expand=True, padx=(4, 6)
            )
            ttk.Button(
                row,
                text="自动获取",
                command=lambda s=station: self._auto_item(s),
            ).pack(side="left")
        ttk.Button(
            items, text="一键自动获取全部（路径 + 游戏 + 物品）", command=self._auto_all
        ).pack(anchor="e", pady=(6, 0))

        rules = ttk.LabelFrame(main, text="规则", padding=8)
        rules.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(rules)
        row.pack(fill="x")
        ttk.Label(row, text="分组阈值(分钟)").pack(side="left")
        self.threshold_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.threshold_var, width=8).pack(
            side="left", padx=(4, 16)
        )
        ttk.Label(row, text="启动缓冲(秒)").pack(side="left")
        self.buffer_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.buffer_var, width=8).pack(
            side="left", padx=(4, 16)
        )
        ttk.Label(row, text="一键补齐上限(留空=不限)").pack(side="left")
        self.limit_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.limit_var, width=12).pack(
            side="left", padx=(4, 0)
        )
        self.exit_after_var = tk.BooleanVar(value=True)
        self.close_wegame_var = tk.BooleanVar(value=False)
        self.close_wegame_delay_var = tk.StringVar(value="5")
        self.prevent_sleep_var = tk.BooleanVar(value=True)
        self.catch_up_var = tk.StringVar(value="24")
        behavior = ttk.Frame(rules)
        behavior.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(
            behavior, text="读取完成后自动退出游戏", variable=self.exit_after_var
        ).pack(side="left")
        ttk.Checkbutton(
            behavior, text="运行期间阻止睡眠", variable=self.prevent_sleep_var
        ).pack(side="left", padx=(16, 0))
        ttk.Label(behavior, text="补跑窗口(小时)").pack(side="left", padx=(16, 0))
        ttk.Entry(behavior, textvariable=self.catch_up_var, width=6).pack(
            side="left", padx=(4, 0)
        )

        close_wegame_row = ttk.Frame(rules)
        close_wegame_row.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(
            close_wegame_row,
            text="结束后关闭 WeGame 窗口",
            variable=self.close_wegame_var,
        ).pack(side="left")
        ttk.Label(close_wegame_row, text="延迟关闭(秒)").pack(
            side="left", padx=(12, 0)
        )
        ttk.Entry(
            close_wegame_row,
            textvariable=self.close_wegame_delay_var,
            width=5,
        ).pack(side="left", padx=(4, 0))

        exchange_row = ttk.Frame(rules)
        exchange_row.pack(fill="x", pady=(6, 0))
        self.exchange_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            exchange_row,
            text="先交易行兑换子弹",
            variable=self.exchange_enabled_var,
        ).pack(side="left")
        ttk.Label(exchange_row, text="最低折扣(%)").pack(
            side="left", padx=(16, 0)
        )
        self.exchange_discount_var = tk.StringVar(value="20")
        ttk.Entry(
            exchange_row, textvariable=self.exchange_discount_var, width=6
        ).pack(side="left", padx=(4, 0))

        exchange_items_row = ttk.Frame(rules)
        exchange_items_row.pack(fill="x", pady=(4, 0))
        ttk.Label(exchange_items_row, text="兑换子弹(用 | 分隔)").pack(
            side="left"
        )
        self.exchange_items_var = tk.StringVar()
        ttk.Entry(
            exchange_items_row, textvariable=self.exchange_items_var
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        accounts_row = ttk.Frame(rules)
        accounts_row.pack(fill="x", pady=(4, 0))
        ttk.Label(accounts_row, text="执行账号（至少选一个）").pack(side="left")
        self.account_vars: dict[str, tk.BooleanVar] = {}
        for account_item in self.config.get("accounts", {}).get("items", []):
            account_key = str(
                account_item.get("key") or account_item.get("label") or ""
            )
            if not account_key:
                continue
            var = tk.BooleanVar(
                value=bool(account_item.get("enabled", True))
            )
            self.account_vars[account_key] = var
            ttk.Checkbutton(
                accounts_row,
                text=str(account_item.get("label") or account_key),
                variable=var,
            ).pack(side="left", padx=(8, 0))

        sched = ttk.LabelFrame(main, text="下次自动启动", padding=8)
        sched.pack(fill="x", pady=(8, 0))
        self.schedule_label = ttk.Label(sched, text="暂无", justify="left")
        self.schedule_label.pack(anchor="w")
        buttons = ttk.Frame(sched)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="立即读取倒计时", command=self._read_now).pack(
            side="left"
        )
        self.run_mode_var = tk.StringVar(value="各排各的")
        ttk.Combobox(
            buttons,
            textvariable=self.run_mode_var,
            values=["各排各的", "两个都跑"],
            width=9,
            state="readonly",
        ).pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="立即执行", command=self._run_selected).pack(
            side="left", padx=6
        )
        ttk.Label(buttons, text="合并窗口(分钟)").pack(
            side="left", padx=(8, 0)
        )
        self.merge_var = tk.StringVar(value="10")
        ttk.Entry(buttons, textvariable=self.merge_var, width=5).pack(
            side="left", padx=(4, 0)
        )
        ttk.Button(
            buttons, text="暂停/恢复自动启动", command=self._toggle_pause
        ).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="环境检查", command=self._env_check).pack(
            side="left", padx=(6, 0)
        )

        today_frame = ttk.LabelFrame(main, text="今日兑换（交易行）", padding=8)
        today_frame.pack(fill="x", pady=(8, 0))
        today_row = ttk.Frame(today_frame)
        today_row.pack(fill="x")
        self.exchange_status_label = ttk.Label(
            today_row, text="暂无记录", justify="left"
        )
        self.exchange_status_label.pack(side="left", anchor="w")
        ttk.Button(
            today_row,
            text="清除今日记录",
            command=self._clear_exchange_today,
        ).pack(side="right")

        logs = ttk.LabelFrame(main, text="运行日志", padding=8)
        logs.pack(fill="both", expand=True, pady=(8, 0))
        log_body = ttk.Frame(logs)
        log_body.pack(fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_body, orient="vertical")
        log_scroll.pack(side="right", fill="y")
        self.log_text = tk.Text(
            log_body,
            height=10,
            state="disabled",
            wrap="none",
            yscrollcommand=log_scroll.set,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.configure(command=self.log_text.yview)

        def _log_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        self.log_text.bind("<MouseWheel>", _log_wheel)



    def _path_row(self, parent, label, var, auto_command, browse=False) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=12).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(
            side="left", fill="x", expand=True, padx=(4, 6)
        )
        ttk.Button(row, text="自动获取", command=auto_command).pack(side="left")
        if browse:
            ttk.Button(row, text="浏览", command=self._browse_wegame).pack(
                side="left", padx=(6, 0)
            )

    # ---------- data ----------
    def _load_fields(self) -> None:
        self.wegame_var.set(self.config.get("wegeame_exe", ""))
        self.proc_var.set(self.config.get("game_process_hint", ""))
        self.title_var.set(self.config.get("game_title_hint", ""))
        rule = self.config.get("rule", {})
        self.threshold_var.set(str(rule.get("group_threshold_minutes", 10)))
        self.buffer_var.set(str(rule.get("buffer_seconds", 60)))
        limit = self.config.get("craft", {}).get("max_purchase_total")
        self.limit_var.set("" if limit is None else str(limit))
        self.exit_after_var.set(
            bool(self.config.get("exit_game_after_read", True))
        )
        self.close_wegame_var.set(
            bool(self.config.get("close_wegame_after_run", False))
        )
        self.close_wegame_delay_var.set(
            str(self.config.get("close_wegame_delay_seconds", 5))
        )
        self.prevent_sleep_var.set(bool(self.config.get("prevent_sleep", True)))
        self.catch_up_var.set(str(self.config.get("catch_up_hours", 24)))
        self.run_mode_var.set(
            "各排各的"
            if self.config.get("multi_account_mode", "per_account") == "per_account"
            else "两个都跑"
        )
        self.merge_var.set(str(self.config.get("schedule_merge_minutes", 10)))
        exchange = self.config.get("exchange", {})
        self.exchange_enabled_var.set(bool(exchange.get("enabled", True)))
        self.exchange_discount_var.set(
            str(exchange.get("min_discount_percent", 20))
        )
        self.exchange_items_var.set(" | ".join(exchange.get("items", [])))
        for account_item in self.config.get("accounts", {}).get("items", []):
            account_key = str(account_item.get("key") or "")
            if account_key in self.account_vars:
                self.account_vars[account_key].set(
                    bool(account_item.get("enabled", True))
                )
        for item in self.config.get("craft", {}).get("items", []):
            station = item.get("station")
            if station in self.item_vars:
                self.item_vars[station].set(item.get("name", ""))

    def _save(self) -> None:
        chosen_accounts = [
            key for key, var in self.account_vars.items() if var.get()
        ]
        if self.account_vars and not chosen_accounts:
            messagebox.showerror(
                "账号未选择", "至少勾选一个账号（QQ 或 微信）"
            )
            return
        try:
            threshold = float(self.threshold_var.get())
            buffer_seconds = int(self.buffer_var.get())
            limit_text = self.limit_var.get().strip()
            limit = int(limit_text) if limit_text else None
            catch_up_hours = float(self.catch_up_var.get().strip() or "0")
            exchange_discount = float(
                self.exchange_discount_var.get().strip() or "0"
            )
            close_wegame_delay = float(
                self.close_wegame_delay_var.get().strip() or "0"
            )
            if close_wegame_delay < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "配置错误",
                "阈值 / 缓冲 / 上限 / 补跑窗口 / 折扣 / 关闭延迟必须是数字，"
                "且关闭延迟不能为负",
            )
            return

        self.config["wegeame_exe"] = self.wegame_var.get().strip()
        self.config["game_process_hint"] = self.proc_var.get().strip()
        self.config["game_title_hint"] = self.title_var.get().strip()
        self.config.setdefault("rule", {})
        self.config["rule"]["group_threshold_minutes"] = threshold
        self.config["rule"]["buffer_seconds"] = buffer_seconds
        self.config.setdefault("craft", {})
        self.config["craft"]["max_purchase_total"] = limit
        self.config["exit_game_after_read"] = bool(self.exit_after_var.get())
        self.config["close_wegame_after_run"] = bool(
            self.close_wegame_var.get()
        )
        self.config["close_wegame_delay_seconds"] = close_wegame_delay
        self.config["prevent_sleep"] = bool(self.prevent_sleep_var.get())
        self.config["catch_up_hours"] = catch_up_hours
        for account_item in self.config.get("accounts", {}).get("items", []):
            account_key = str(account_item.get("key") or "")
            if account_key in self.account_vars:
                account_item["enabled"] = bool(
                    self.account_vars[account_key].get()
                )
        self.config.setdefault("exchange", {})
        self.config["exchange"]["enabled"] = bool(
            self.exchange_enabled_var.get()
        )
        self.config["exchange"]["min_discount_percent"] = exchange_discount
        self.config["exchange"]["items"] = [
            part.strip()
            for part in self.exchange_items_var.get().split("|")
            if part.strip()
        ]
        import winutil

        winutil.set_system_awake(bool(self.prevent_sleep_var.get()))
        for item in self.config["craft"].get("items", []):
            station = item.get("station")
            if station in self.item_vars:
                item["name"] = self.item_vars[station].get().strip()

        path = self.base_dir / "config.json"
        try:
            temp = path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(path)
            self.logger("[gui] 配置已保存")
            self._set_status("配置已保存")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def _reload(self) -> None:
        try:
            data = json.loads(
                (self.base_dir / "config.json").read_text(encoding="utf-8")
            )
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        self.config.clear()
        self.config.update(data)
        self._load_fields()
        self._set_status("配置已重新加载")

    def _open_config(self) -> None:
        import os

        try:
            os.startfile(self.base_dir / "config.json")
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def _browse_wegame(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 WeGame.exe",
            filetypes=[("WeGame", "wegame.exe"), ("可执行文件", "*.exe")],
        )
        if path:
            self.wegame_var.set(path)

    # ---------- auto detect ----------
    def _auto_wegame(self) -> None:
        def work():
            path = autodetect.detect_wegame_exe()
            self.root.after(0, lambda: self._finish_auto("WeGame 路径", path, self.wegame_var))

        threading.Thread(target=work, daemon=True).start()

    def _auto_game(self) -> None:
        def work():
            info = autodetect.detect_game_info()
            if info is None:
                self.root.after(0, lambda: self._set_status("未找到运行中的三角洲窗口"))
                return
            def apply():
                self.proc_var.set(info["exe"])
                self.title_var.set(info["title"])
                self._set_status("已获取游戏进程信息，请保存")
            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _auto_item(self, station: str) -> None:
        def work():
            try:
                items = autodetect.detect_station_items(self.config, self.reader)
            except Exception as exc:
                self.logger(f"[gui] 自动获取物品失败: {exc}")
                items = None
            name = (items or {}).get(station)
            self.root.after(0, lambda: self._finish_auto(f"{station} 物品", name, self.item_vars[station]))

        threading.Thread(target=work, daemon=True).start()

    def _auto_all(self) -> None:
        def work():
            self.root.after(0, lambda: self._set_status("自动获取中…"))
            wegame = autodetect.detect_wegame_exe()
            info = autodetect.detect_game_info()
            items = None
            try:
                items = autodetect.detect_station_items(self.config, self.reader)
            except Exception as exc:
                self.logger(f"[gui] 自动获取物品失败: {exc}")
            def apply():
                if wegame:
                    self.wegame_var.set(wegame)
                if info:
                    self.proc_var.set(info["exe"])
                    self.title_var.set(info["title"])
                if items:
                    for station, name in items.items():
                        if station in self.item_vars:
                            self.item_vars[station].set(name)
                missing = [
                    station
                    for station in self.config["stations"]
                    if station not in (items or {})
                ]
                self._set_status(
                    "自动获取完成，请核对后保存"
                    + (f"（{ '、'.join(missing) } 未识别到物品）" if missing else "")
                )
                self.show()
            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _finish_auto(self, label, value, var) -> None:
        if not value:
            self.show()
            self._set_status(f"{label} 自动获取失败（游戏可能未运行或不在特勤处）")
            return
        var.set(value)
        self.show()
        self._set_status(f"{label} 已获取，请保存")

    # ---------- actions ----------
    def _read_now(self) -> None:
        def work():
            self.root.after(0, lambda: self._set_status("读取中…"))
            try:
                result = self.reader.read()
                timers = result.get("timers", {})
                lines = "；".join(
                    f"{name} {data['text']}" for name, data in timers.items()
                ) or "无"
                points = compute_schedule(
                    datetime.fromisoformat(result["captured_at"]),
                    timers,
                    self.config["rule"]["group_threshold_minutes"],
                    self.config["rule"]["buffer_seconds"],
                )
                preview = "；".join(
                    f"{p['anchor']} {p['at'][5:16].replace('T', ' ')}"
                    for p in points
                ) or "无"
                self.root.after(0, self.show)
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "读取结果",
                        f"倒计时：\n{lines}\n\n下次启动（仅预览，未排程）：\n{preview}",
                    ),
                )
                self.root.after(0, lambda: self._set_status("读取完成"))
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda: messagebox.showerror("读取失败", message))
                self.root.after(0, lambda: self._set_status("读取失败"))

        threading.Thread(target=work, daemon=True).start()

    def _run_selected(self) -> None:
        mode = (
            "per_account"
            if self.run_mode_var.get() == "各排各的"
            else "both"
        )
        try:
            merge_minutes = float(self.merge_var.get().strip() or "0")
        except ValueError:
            messagebox.showerror("配置错误", "合并窗口必须是数字")
            return
        self.config["multi_account_mode"] = mode
        self.config["schedule_merge_minutes"] = merge_minutes
        self.schedule.mode = mode
        self._run_mode(mode)

    def _run_mode(self, mode: str) -> None:
        keys = None
        if self.account_vars:
            keys = [
                key for key, var in self.account_vars.items() if var.get()
            ]
            if not keys:
                messagebox.showerror(
                    "账号未选择", "至少勾选一个账号（QQ 或 微信）"
                )
                return
            for account_item in self.config.get("accounts", {}).get("items", []):
                account_key = str(account_item.get("key") or "")
                if account_key in self.account_vars:
                    account_item["enabled"] = bool(
                        self.account_vars[account_key].get()
                    )
        self.schedule.trigger_now("界面手动", mode=mode, account_keys=keys)
        label = "各排各的" if mode == "per_account" else "两个都跑"
        self._set_status(
            f"已开始完整流程（{label}），完成后排程会自动刷新"
        )

    def _env_check(self) -> None:
        def work():
            self.root.after(0, lambda: self._set_status("环境检查中…"))
            try:
                from envcheck import run_env_check

                text = run_env_check(self.config, self.reader, self.base_dir)
                self.logger("[envcheck] " + text.replace("\n", " | "))
                self.root.after(0, self.show)
                self.root.after(
                    0, lambda: messagebox.showinfo("环境检查", text)
                )
                self.root.after(0, lambda: self._set_status("环境检查完成"))
            except Exception as exc:
                message = str(exc)
                self.root.after(
                    0, lambda: messagebox.showerror("环境检查失败", message)
                )
                self.root.after(0, lambda: self._set_status("环境检查失败"))

        threading.Thread(target=work, daemon=True).start()

    def _toggle_pause(self) -> None:
        paused = self.schedule.toggle_pause()
        self._set_status("已恢复自动启动" if paused else "已暂停自动启动")

    def _cancel_run(self) -> None:
        if self.schedule.request_cancel():
            self._set_status("已请求取消当前流程")
        else:
            self._set_status("当前没有运行中的流程")

    # ---------- periodic ----------
    def _refresh_exchange_status(self) -> None:
        try:
            data = json.loads(
                (self.base_dir / "exchange_history.json").read_text(
                    encoding="utf-8"
                )
            )
            history = data.get("entries", [])
        except Exception:
            history = []
        items = [
            part.strip()
            for part in self.exchange_items_var.get().split("|")
            if part.strip()
        ]
        if not items:
            items = list(self.config.get("exchange", {}).get("items", []))
        try:
            threshold = float(self.exchange_discount_var.get().strip() or "20")
        except ValueError:
            threshold = 20
        self.exchange_status_label.config(
            text=format_accounts_history(
                history, get_accounts(self.config), items, threshold
            )
        )

    def _clear_exchange_today(self) -> None:
        path = self.base_dir / "exchange_history.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {"entries": []}
        today = date.today().isoformat()
        entries = [
            entry
            for entry in data.get("entries", [])
            if entry.get("date") != today
        ]
        try:
            temp = path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(path)
            self._refresh_exchange_status()
            self._set_status("已清除今日兑换记录")
        except OSError as exc:
            messagebox.showerror("清除失败", str(exc))

    def _refresh_log(self) -> None:
        log_path = self.base_dir / "logs" / "app.log"
        try:
            data = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            data = ""
        if data != self._last_log:
            self._last_log = data
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", data)
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(2000, self._refresh_log)

    def _refresh_schedule(self) -> None:
        text = self.schedule.next_points_text()
        if self.schedule.scheduler.state == STATE_PAUSED:
            text = "（已暂停）\n" + text
        self.schedule_label.config(text=text)
        self._refresh_exchange_status()
        self.root.after(2000, self._refresh_schedule)

    # ---------- window ----------
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def hide(self) -> None:
        self.root.withdraw()

    def close_for_exit(self) -> None:
        self.root.destroy()
