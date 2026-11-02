from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import cancel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_PAUSED

import winutil
from accounts import get_accounts
from cancel import RunCancelled
from planner import compute_schedule


class ScheduleManager:
    def __init__(
        self,
        config: dict,
        launcher,
        logger=print,
        notify=None,
        base_dir: str | Path | None = None,
        state_path: str | Path | None = None,
    ):
        self.config = config
        self.launcher = launcher
        self.log = logger
        self.notify = notify or (lambda title, message, error=False: None)
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        self.state_path = (
            Path(state_path) if state_path else self.base_dir / "state.json"
        )
        self.scheduler = BackgroundScheduler()
        self._lock = threading.Lock()
        self._busy = False
        self._lock_retries = 0
        self._pending: list[tuple] = []
        self.mode = config.get("multi_account_mode", "per_account")
        self.points: list[dict] = []

    def start(self) -> None:
        self.scheduler.start()
        self._restore()

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    def request_cancel(self, quiet: bool = False) -> bool:
        if not self.is_busy():
            if not quiet:
                self.log("[sched] 当前没有运行中的流程，取消请求忽略")
            return False
        cancel.request()
        self.log("[sched] 已请求取消当前流程")
        return True

    def _restore(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log(f"[sched] 读取 state.json 失败: {exc}")
            return
        self.mode = data.get("mode", self.mode)
        now = datetime.now()
        catch_up_hours = float(self.config.get("catch_up_hours", 24) or 0)
        future: list[dict] = []
        overdue: list[dict] = []
        for point in data.get("points", []):
            try:
                at = datetime.fromisoformat(point["at"])
            except Exception:
                continue
            if at > now:
                future.append(point)
            elif catch_up_hours > 0 and now - at <= timedelta(hours=catch_up_hours):
                overdue.append(point)
        self.points = future
        self._apply(future)
        if overdue:
            keys = None
            if self.mode == "per_account":
                keys = sorted(
                    {p.get("account") for p in overdue if p.get("account")}
                ) or None
            self.log(
                f"[sched] 发现 {len(overdue)} 个已错过的时间点"
                f"（补跑窗口 {catch_up_hours:g} 小时），立即补跑"
            )
            self.trigger_now("补跑", self.mode, keys)

    def trigger_now(
        self, reason: str = "手动", mode: str | None = None, account_keys=None
    ) -> None:
        mode = mode or self.mode
        threading.Thread(
            target=self._run_pipeline,
            args=(reason, mode, account_keys),
            daemon=True,
        ).start()

    def _run_pipeline(
        self,
        reason: str = "手动",
        mode: str = "per_account",
        account_keys=None,
    ) -> None:
        with self._lock:
            if self._busy:
                self._pending.append((mode, account_keys, datetime.now()))
                self.log("[sched] 流程忙，本次已加入排队")
                return
            self._busy = True
        self.log(f"[sched] 执行流程（{reason}，模式 {mode}）")
        cancel.reset()
        completed_any = False
        cancelled = False
        try:
            if winutil.is_desktop_locked():
                self.log("[sched] 当前为锁屏/安全桌面，跳过本次流程")
                self._schedule_lock_retry(reason, mode, account_keys)
                return
            accounts = get_accounts(self.config)
            if account_keys is not None:
                wanted = set(account_keys)
                accounts = [a for a in accounts if a["key"] in wanted]
            if not accounts:
                self.log("[sched] 没有可执行的账号")
                return
            self.log(
                "[sched] 本次账号: "
                + "、".join(a["label"] for a in accounts)
            )
            points: list[dict] = []
            results: dict[str, dict] = {}
            for account in accounts:
                cancel.check()
                try:
                    result = self.launcher.run(account)
                    completed_any = True
                    results[account["key"]] = result
                    captured = datetime.fromisoformat(result["captured_at"])
                    new_points = compute_schedule(
                        captured,
                        result["timers"],
                        self.config["rule"]["group_threshold_minutes"],
                        self.config["rule"]["buffer_seconds"],
                    )
                    for point in new_points:
                        point["account"] = account["key"]
                        point["label"] = account["label"]
                    points.extend(new_points)
                except RunCancelled:
                    raise
                except Exception as exc:
                    self.log(f"[sched] {account['label']} 流程失败: {exc}")
                    self.notify(
                        f"{account['label']} 流程失败", str(exc), error=True
                    )

            self.mode = mode
            self.config["multi_account_mode"] = mode
            if mode == "both":
                enabled = get_accounts(self.config)
                all_keys = [a["key"] for a in enabled]
                all_labels = [a["label"] for a in enabled]
                merged: list[dict] = []
                seen: set[str] = set()
                for point in points:
                    if point["at"] in seen:
                        continue
                    seen.add(point["at"])
                    item = dict(point)
                    item["account"] = ""
                    item["label"] = "+".join(all_labels)
                    item["accounts"] = all_keys
                    merged.append(item)
                self.set_points(merged, mode)
            else:
                keys = {a["key"] for a in accounts}
                kept = [
                    p
                    for p in self.points
                    if p.get("account") not in keys
                    and not (set(p.get("accounts") or []) & keys)
                ]
                self.set_points(kept + points, mode)

            parts: list[str] = []
            for account in accounts:
                result = results.get(account["key"])
                if not result:
                    continue
                timers = result.get("timers", {})
                text = "；".join(
                    f"{name} {data.get('text') or data.get('seconds')}"
                    for name, data in timers.items()
                ) or "无倒计时"
                parts.append(f"{account['label']}：{text}")
            self.notify("流程完成", " | ".join(parts) or "无结果")
        except RunCancelled:
            cancelled = True
            self.log("[sched] 用户取消，已停止本次流程")
            self.notify("流程已取消", "用户取消当前流程")
        except Exception as exc:
            self.log(f"[sched] 流程失败: {exc}")
            self.notify("自动流程失败", str(exc), error=True)
        finally:
            if cancelled:
                with self._lock:
                    self._pending.clear()
            if (
                completed_any
                and not cancelled
                and self.config.get("close_wegame_after_run", False)
            ):
                try:
                    delay = float(
                        self.config.get("close_wegame_delay_seconds", 5) or 0
                    )
                    self.launcher.close_wegame_window(delay)
                except RunCancelled:
                    pass
                except Exception as exc:
                    self.log(f"[sched] 关闭 WeGame 失败: {exc}")
            cancel.reset()
            with self._lock:
                self._busy = False
            self._drain_pending()

    def _drain_pending(self) -> None:
        with self._lock:
            if self._busy or not self._pending:
                return
            catch_up = float(self.config.get("catch_up_hours", 24) or 0)
            now = datetime.now()
            while self._pending:
                mode, keys, queued_at = self._pending.pop(0)
                if catch_up and now - queued_at > timedelta(hours=catch_up):
                    continue
                threading.Thread(
                    target=self._run_pipeline,
                    args=("排队补跑", mode, keys),
                    daemon=True,
                ).start()
                return

    def _merge_points(self, points: list[dict]) -> list[dict]:
        minutes = float(self.config.get("schedule_merge_minutes", 10) or 0)
        if minutes <= 0 or len(points) < 2:
            return points
        window = timedelta(minutes=minutes)
        ordered = sorted(points, key=lambda p: p["at"])
        used: set[int] = set()
        merged: list[dict] = []
        for i, point in enumerate(ordered):
            if i in used:
                continue
            used.add(i)
            group = [point]
            accounts = {point.get("account")} if point.get("account") else set()
            for j in range(i + 1, len(ordered)):
                if j in used:
                    continue
                candidate = ordered[j]
                if datetime.fromisoformat(candidate["at"]) - datetime.fromisoformat(point["at"]) > window:
                    break
                key = candidate.get("account")
                if key and key in accounts:
                    continue
                group.append(candidate)
                used.add(j)
                if key:
                    accounts.add(key)
            if len(group) == 1:
                merged.append(point)
                continue
            latest = max(group, key=lambda p: p["at"])
            labels = list(dict.fromkeys(g.get("label") for g in group if g.get("label")))
            merged.append({
                **latest,
                "account": "",
                "label": "+".join(labels),
                "accounts": sorted(a for a in accounts if a),
            })
        return merged

    def set_points(self, points: list[dict], mode: str | None = None) -> None:
        if mode:
            self.mode = mode
        now = datetime.now()
        self.points = [
            point
            for point in points
            if datetime.fromisoformat(point["at"]) > now
        ]
        self.points.sort(key=lambda point: point["at"])
        self._apply(self.points)
        self._save()

    def _apply(self, points: list[dict]) -> None:
        for job in self.scheduler.get_jobs():
            if job.id.startswith("auto-"):
                self.scheduler.remove_job(job.id)
        catch_up_hours = float(self.config.get("catch_up_hours", 24) or 0)
        grace = int(max(1800, min(catch_up_hours * 3600, 86400)))
        ordered = sorted(points, key=lambda p: p["at"])
        used: set[int] = set()
        window = timedelta(
            minutes=float(self.config.get("schedule_merge_minutes", 10) or 0)
        )
        for index, point in enumerate(ordered):
            if index in used:
                continue
            used.add(index)
            group = [point]
            keys = set(point.get("accounts") or ([point["account"]] if point.get("account") else []))
            labels = [point.get("label") or point.get("account") or "默认"]
            for other_index in range(index + 1, len(ordered)):
                if other_index in used:
                    continue
                candidate = ordered[other_index]
                if datetime.fromisoformat(candidate["at"]) - datetime.fromisoformat(point["at"]) > window:
                    break
                candidate_keys = set(
                    candidate.get("accounts")
                    or ([candidate["account"]] if candidate.get("account") else [])
                )
                if keys & candidate_keys:
                    continue
                group.append(candidate)
                used.add(other_index)
                keys |= candidate_keys
                labels.append(candidate.get("label") or candidate.get("account") or "默认")
            latest = max(group, key=lambda p: p["at"])
            at = datetime.fromisoformat(latest["at"])
            if at <= datetime.now():
                continue
            label = "+".join(dict.fromkeys(labels))
            job_id = "auto-" + at.strftime("%Y%m%d%H%M%S") + "-" + "-".join(sorted(keys))
            self.scheduler.add_job(
                self._run_pipeline,
                "date",
                run_date=at,
                id=job_id,
                kwargs={
                    "reason": "定时",
                    "mode": self.mode,
                    "account_keys": sorted(keys) or None,
                },
                misfire_grace_time=grace,
                coalesce=True,
            )
            self.log(
                f"[sched] 计划 {at:%Y-%m-%d %H:%M:%S} 自动启动"
                f"（{label} / {latest.get('anchor', '')}）"
            )

    def _save(self) -> None:
        data = {
            "mode": self.mode,
            "points": self.points,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            self.log(f"[sched] 保存状态失败: {exc}")

    def _schedule_lock_retry(
        self, reason: str, mode: str, account_keys
    ) -> None:
        minutes = max(1, int(self.config.get("lock_retry_minutes", 5)))
        max_retries = int(self.config.get("lock_retry_max", 24))
        if self._lock_retries >= max_retries:
            self.log("[sched] 锁屏重试次数已达上限，放弃本轮")
            self.notify("自动流程未执行", "电脑长时间处于锁屏状态", error=True)
            return
        self._lock_retries += 1
        fire_at = datetime.now() + timedelta(minutes=minutes)
        self.scheduler.add_job(
            self._run_pipeline,
            "date",
            run_date=fire_at,
            id="retry-" + fire_at.strftime("%Y%m%d%H%M%S"),
            kwargs={
                "reason": "锁屏重试",
                "mode": mode,
                "account_keys": account_keys,
            },
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        self.log(
            f"[sched] 锁屏状态，{minutes} 分钟后重试"
            f"（第 {self._lock_retries}/{max_retries} 次）"
        )

    def next_points_text(self) -> str:
        mode_text = "各排各的" if self.mode == "per_account" else "两个都跑"
        header = f"模式：{mode_text}"
        if self.scheduler.state == STATE_PAUSED:
            header = "（已暂停）" + header
        if not self.points:
            return header + "\n暂无自动启动计划"
        lines = [
            f"{point['at'][:19].replace('T', ' ')}  "
            f"{point.get('label') or '默认'}  {point.get('anchor', '')}"
            for point in self.points
        ]
        return header + "\n" + "\n".join(lines)

    def toggle_pause(self) -> bool:
        if self.scheduler.state == STATE_PAUSED:
            self.scheduler.resume()
            self.log("[sched] 已恢复自动启动")
            return True
        self.scheduler.pause()
        self.log("[sched] 已暂停自动启动")
        return False
