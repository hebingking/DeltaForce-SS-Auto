from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
sys.path.insert(0, str(BASE_DIR))

from launcher import Launcher  # noqa: E402
from planner import compute_schedule  # noqa: E402
from reader import Reader, load_config  # noqa: E402
from scheduler import ScheduleManager  # noqa: E402
import hotkey  # noqa: E402
import winutil  # noqa: E402


def setup_logging() -> logging.Logger:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("orc-sjz")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)
    return logger


def emit(text: str) -> None:
    if sys.stdout is not None:
        print(text)


def beep_error() -> None:
    try:
        import winsound

        winsound.Beep(400, 500)
    except Exception:
        pass


def beep_ok() -> None:
    try:
        import winsound

        winsound.Beep(880, 150)
    except Exception:
        pass


def acquire_instance():
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Global\\orc-sjz-mutex")
    if kernel32.GetLastError() == 183:
        return None
    return handle


def make_tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), (14, 16, 20))
    draw = ImageDraw.Draw(image)
    draw.polygon([(32, 6), (58, 54), (6, 54)], outline=(80, 220, 150), width=4)
    draw.line([(32, 24), (32, 42)], fill=(80, 220, 150), width=6)
    draw.line([(27, 46), (37, 46)], fill=(80, 220, 150), width=4)
    return image


def tray_main(config, logger, reader, schedule) -> None:
    import pystray

    from gui import SettingsWindow

    window = SettingsWindow(config, logger.info, reader, schedule, BASE_DIR)
    holder: dict = {}

    def notify(title, message, error=False):
        logger.info(f"[notify] {title}: {message}")
        if config.get("notify", {}).get("beep", True):
            if error:
                beep_error()
            else:
                beep_ok()
        icon = holder.get("icon")
        if icon is not None:
            try:
                icon.notify(str(message), str(title))
            except Exception:
                pass

    schedule.notify = notify

    def show_window(_icon=None, _item=None):
        window.root.after(0, window.show)

    def show_points():
        show_window()
        notify("下次自动启动", schedule.next_points_text())

    def toggle_pause():
        paused = schedule.toggle_pause()
        notify("自动启动", "已暂停" if not paused else "已恢复")

    def open_log():
        try:
            os.startfile(BASE_DIR / "logs" / "app.log")
        except Exception as exc:
            notify("打开日志失败", str(exc), True)

    def quit_app(icon, _item=None):
        hotkey.stop()
        schedule.shutdown()
        try:
            icon.stop()
        except Exception:
            pass
        try:
            window.root.after(0, window.close_for_exit)
        except Exception:
            pass

    icon = pystray.Icon(
        "orc-sjz",
        make_tray_image(),
        "三角洲特勤处助手",
        pystray.Menu(
            pystray.MenuItem("打开主界面", show_window, default=True),
            pystray.MenuItem(
                "立即执行完整流程",
                lambda _icon, _item: schedule.trigger_now("托盘手动"),
            ),
            pystray.MenuItem(
                "取消当前运行 (F12)",
                lambda _icon, _item: schedule.request_cancel(),
            ),
            pystray.MenuItem(
                "查看下次自动启动", lambda _icon, _item: show_points()
            ),
            pystray.MenuItem(
                "暂停/恢复自动启动", lambda _icon, _item: toggle_pause()
            ),
            pystray.MenuItem("打开日志", lambda _icon, _item: open_log()),
            pystray.MenuItem("退出", quit_app),
        ),
    )
    holder["icon"] = icon
    hotkey.start(lambda: schedule.request_cancel(quiet=True))
    schedule.start()
    logger.info("[app] 托盘程序已启动（F12 取消已启用）")
    threading.Thread(target=icon.run, daemon=True).start()
    window.show()
    window.root.mainloop()
    try:
        icon.stop()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="三角洲行动特勤处倒计时助手")
    parser.add_argument("--read", action="store_true", help="只读取一次并输出 JSON")
    parser.add_argument("--run", action="store_true", help="执行一次完整启动流程")
    parser.add_argument("--craft", action="store_true", help="只对当前游戏执行领取/制作")
    parser.add_argument("--envcheck", action="store_true", help="环境检查")
    parser.add_argument("--image", help="离线识别指定截图")
    parser.add_argument("--debug", action="store_true", help="保存 OCR 标注截图")
    args = parser.parse_args()

    config = load_config()
    logger = setup_logging()
    log = logger.info
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    log(f"[app] 管理员权限: {'是' if is_admin else '否'}")
    winutil.set_system_awake(bool(config.get("prevent_sleep", True)))
    log(
        "[app] 阻止睡眠: "
        + ("开启" if config.get("prevent_sleep", True) else "关闭")
    )

    if args.envcheck:
        from envcheck import run_env_check

        reader = Reader(config, log)
        text = run_env_check(config, reader, BASE_DIR)
        log("[envcheck] " + text.replace("\n", " | "))
        emit(text)
        return

    if args.image:
        from PIL import Image

        reader = Reader(config, log)
        image = Image.open(args.image).convert("RGB")
        lines = reader._backend().recognize(image)
        parsed = reader._parse(lines, image.size)
        emit(json.dumps(parsed, ensure_ascii=False, indent=2))
        return

    if args.read:
        reader = Reader(config, log)
        result = reader.read(
            debug_dir=BASE_DIR / "logs" / "debug" if args.debug else None
        )
        log("[app] 读取结果: " + json.dumps(result, ensure_ascii=False))
        emit(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.craft:
        from workshop import Workshop

        reader = Reader(config, log)
        game = reader.find_game()
        if game is None:
            emit("游戏未运行")
            return
        results = Workshop(config, log, reader).run(game)
        emit(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if args.run:
        from accounts import get_accounts

        reader = Reader(config, log)
        launcher = Launcher(config, log, reader)
        accounts = get_accounts(config) or [None]
        output = []
        completed_any = False
        for account in accounts:
            label = account["label"] if account else "默认"
            try:
                result = launcher.run(account)
                completed_any = True
                points = compute_schedule(
                    datetime.fromisoformat(result["captured_at"]),
                    result["timers"],
                    config["rule"]["group_threshold_minutes"],
                    config["rule"]["buffer_seconds"],
                )
                output.append(
                    {
                        "account": label,
                        "timers": result["timers"],
                        "points": points,
                    }
                )
            except Exception as exc:
                log(f"[app] {label} 失败: {exc}")
                output.append({"account": label, "error": str(exc)})
        if completed_any and config.get("close_wegame_after_run", False):
            try:
                delay = float(config.get("close_wegame_delay_seconds", 5) or 0)
                launcher.close_wegame_window(delay)
            except Exception as exc:
                log(f"[app] 关闭 WeGame 失败: {exc}")
        emit(json.dumps(output, ensure_ascii=False, indent=2))
        return

    if acquire_instance() is None:
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "程序已经在运行", "三角洲特勤处助手", 0x40
            )
        except Exception:
            pass
        emit("程序已经在运行")
        return

    reader = Reader(config, log)
    launcher = Launcher(config, log, reader)
    schedule = ScheduleManager(config, launcher, log, base_dir=BASE_DIR)
    tray_main(config, logger, reader, schedule)


if __name__ == "__main__":
    main()
