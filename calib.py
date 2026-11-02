from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from PIL import ImageDraw  # noqa: E402

import winutil  # noqa: E402
from reader import Reader, load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR 标定辅助：截取窗口并标注识别结果")
    parser.add_argument("--target", choices=["game", "wegame"], default="game")
    args = parser.parse_args()

    config = load_config()
    hint = (
        config["game_process_hint"]
        if args.target == "game"
        else config["wegeame_process_hint"]
    )
    window = winutil.find_window(exe_hint=hint)
    if window is None:
        raise SystemExit(f"未找到窗口: {hint}")

    winutil.activate(window.hwnd)
    image = winutil.capture_client(window.hwnd)
    reader = Reader(config, print)
    lines = reader._backend().recognize(image)

    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.rectangle(
            [line.left, line.top, line.right, line.bottom],
            outline=(0, 255, 0),
            width=2,
        )
        draw.text(
            (line.left, max(0, line.top - 12)),
            f"{index}:{line.text[:30]}",
            fill=(0, 255, 0),
        )

    out_dir = BASE_DIR / "logs" / "calib"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"calib_{args.target}_{window.hwnd}.png"
    image.save(path)

    print(f"client_size={image.size} saved={path}")
    for index, line in enumerate(lines):
        print(
            f"{index:>3} x={line.left:6.0f} y={line.top:6.0f}"
            f" w={line.width:6.0f} h={line.height:5.0f}  {line.text}"
        )


if __name__ == "__main__":
    main()
