from __future__ import annotations

import ctypes
from datetime import datetime
from pathlib import Path

import winutil
from ocr_backend import normalize
from reader import Reader, find_line, match_score, parse_timer


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_craft_page(lines) -> bool:
    text = "".join(normalize(line.text) for line in lines)
    return any(key in text for key in ("一键补齐", "放入生产图纸", "参考价格", "仓库数量"))


def run_env_check(config: dict, reader: Reader, base_dir) -> str:
    report: list[str] = []
    report.append(f"=== 环境检查 {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    report.append(f"管理员权限: {'是' if _is_admin() else '否'}")
    report.append(
        f"桌面锁屏: {'是（无法操作）' if winutil.is_desktop_locked() else '否'}"
    )
    problems: list[str] = []

    wegame = winutil.find_window(exe_hint=config.get("wegeame_process_hint", ""))
    if wegame is None:
        report.append("WeGame: 未运行")
    else:
        try:
            image, lines = reader.ocr(wegame)
            width, height = image.size
            report.append(
                f"WeGame: 运行中  {width}x{height}"
                f"  DPI {winutil.get_dpi_scale(wegame.hwnd) * 100:.0f}%"
            )
            sidebar = [
                line
                for line in lines
                if match_score("三角洲行动", line.text) >= 0.72
                and line.cx < width * 0.35
            ]
            start = find_line(
                lines,
                "启动",
                min_score=0.85,
                y_min=height * 0.6,
                x_min=width * 0.5,
            )
            report.append(
                f"  侧栏「三角洲行动」: {'✓ 找到' if sidebar else '✗ 未找到'}"
            )
            report.append(
                f"  详情页「启动」: {'✓ 找到' if start else '✗ 未找到'}"
            )
        except Exception as exc:
            report.append(f"WeGame: OCR 失败 - {exc}")
            problems.append("WeGame OCR 失败")

    game = reader.find_game()
    if game is None:
        report.append("三角洲: 未运行")
    else:
        try:
            image, lines = reader.ocr(game)
            width, height = image.size
            report.append(
                f"三角洲: 运行中  {width}x{height}"
                f"  DPI {winutil.get_dpi_scale(game.hwnd) * 100:.0f}%"
            )
            labels: dict = {}
            for station in config["stations"]:
                hit = find_line(lines, station, y_min=height * 0.08)
                if hit is not None:
                    labels[station] = hit
            timers = []
            for line in lines:
                seconds = parse_timer(line.text)
                if seconds is not None:
                    timers.append((line, seconds))

            text = "".join(normalize(line.text) for line in lines)
            if _is_craft_page(lines):
                page = "制作页"
            elif "获得奖励" in text:
                page = "领取奖励页"
            elif "前往游玩" in text:
                page = "模式选择页"
            elif "开始游戏" in text and "特勤处" in text:
                page = "大厅"
            elif len(labels) >= 3:
                page = "特勤处"
            else:
                page = "未知"
            report.append(f"  当前页面: {page}")
            report.append(
                f"  工位标签: {len(labels)}/4"
                + (f"（{'、'.join(labels)}）" if labels else "")
            )

            assigned = (
                reader._assign_timers(labels, timers, height) if labels else {}
            )
            if assigned:
                for station, data in assigned.items():
                    report.append(f"  倒计时 {station}: {data['text']}")
            elif timers:
                report.append(f"  倒计时文本: {len(timers)} 个（未能归属工位）")
            else:
                report.append("  倒计时: 未识别到")

            if page == "制作页":
                fill = find_line(
                    lines, "一键补齐", min_score=0.85,
                    y_min=height * 0.72, x_min=width * 0.55,
                )
                produce = find_line(
                    lines, "生产", min_score=0.85,
                    y_min=height * 0.72, x_min=width * 0.55,
                )
                report.append(f"  「一键补齐」按钮: {'✓' if fill else '✗'}")
                report.append(f"  「生产」按钮: {'✓' if produce else '✗'}")
            elif page == "模式选择页":
                go = find_line(lines, "前往游玩")
                report.append(f"  「前往游玩」: {'✓' if go else '✗'}")
            elif page == "大厅":
                tab = find_line(lines, "特勤处", y_max=height * 0.2)
                report.append(f"  顶部「特勤处」: {'✓' if tab else '✗'}")

            if page == "未知":
                problems.append("页面未识别")
            if page == "特勤处" and not timers:
                problems.append("未读到倒计时")
        except Exception as exc:
            report.append(f"三角洲: OCR 失败 - {exc}")
            problems.append("三角洲 OCR 失败")

    if winutil.is_desktop_locked():
        problems.append("桌面锁屏")
    report.append("")
    if game is None:
        conclusion = "基础检查通过（三角洲未运行，游戏内页面未检查）"
    elif problems:
        conclusion = "存在问题：" + "、".join(problems)
    else:
        conclusion = "基础识别正常"
    report.append("结论: " + conclusion)

    out_dir = Path(base_dir) / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"envcheck_{datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text("\n".join(report), encoding="utf-8")
    report.append(f"报告已保存: {path}")
    return "\n".join(report)
