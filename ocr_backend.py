from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass

from PIL import Image


@dataclass
class OcrLine:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    score: float = 0.0

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


def normalize(text: str) -> str:
    text = text.replace("：", ":").replace("；", ":")
    return re.sub(r"\s+", "", text)


def _crop_and_scale(
    image: Image.Image,
    region,
    max_dim: int | None,
    upscale: float | None,
    default_max: int,
    default_upscale: float,
):
    """裁剪 ROI 并按目标尺寸缩放。

    返回 (处理后的图像, 缩放比, 偏移x, 偏移y)；偏移用于把识别框坐标还原回整幅截图。
    """
    offset_x = offset_y = 0.0
    if region:
        x0, y0, x1, y1 = (int(value) for value in region)
        x0 = max(0, min(x0, image.width - 1))
        y0 = max(0, min(y0, image.height - 1))
        x1 = max(x0 + 1, min(x1, image.width))
        y1 = max(y0 + 1, min(y1, image.height))
        image = image.crop((x0, y0, x1, y1))
        offset_x, offset_y = x0, y0
    width, height = image.size
    limit = default_max if max_dim is None else max_dim
    up = default_upscale if upscale is None else upscale
    # 允许缩小（旧写法 max(1.0, ...) 使 limit 只能用于放大，无法限制大分辨率）
    scale = min(up, limit / max(width, height))
    if scale > 1.01 or scale < 0.99:
        image = image.resize(
            (round(width * scale), round(height * scale)), Image.LANCZOS
        )
    else:
        scale = 1.0
    return image, scale, offset_x, offset_y


class OcrBackend:
    name = "base"

    def recognize(
        self,
        image: Image.Image,
        region=None,
        max_dim: int | None = None,
        upscale: float | None = None,
        fast: bool = False,
    ) -> list[OcrLine]:
        raise NotImplementedError


class RapidOcrBackend(OcrBackend):
    name = "rapidocr"

    def __init__(
        self,
        max_dimension: int = 3000,
        upscale: float = 2.0,
        fast_det_limit: int = 1152,
    ):
        from rapidocr_onnxruntime import RapidOCR

        # 精读引擎（默认参数）：用于读工位倒计时这类小字，精度优先。
        self._engine = RapidOCR()
        # 快读引擎：rapidocr 1.2.3 默认 limit_type=min，语义是「短边小于 736 才放大」，
        # 于是 768x432 这种 ROI 会被放大到 1310x736（多算 3 倍像素）却没有任何收益。
        # 改成 max 后小 ROI 保持原分辨率、大区域才压缩，实测 ROI 快 1.8x 且结果不变。
        # 注意 1.2.3 的坑：只要传任一 det_* 参数，就必须同时给 det_model_path。
        self._fast_engine = RapidOCR(
            det_limit_type="max",
            det_limit_side_len=int(fast_det_limit),
            det_model_path=None,
        )
        self._max_dim = max_dimension
        self._upscale = upscale

    def recognize(
        self,
        image: Image.Image,
        region=None,
        max_dim: int | None = None,
        upscale: float | None = None,
        fast: bool = False,
    ) -> list[OcrLine]:
        import numpy as np

        image, scale, offset_x, offset_y = _crop_and_scale(
            image, region, max_dim, upscale, self._max_dim, self._upscale
        )
        engine = self._fast_engine if fast else self._engine
        result, _ = engine(np.array(image))
        lines: list[OcrLine] = []
        for item in result or []:
            box, text, score = item[0], str(item[1]), float(item[2])
            xs = [float(point[0]) / scale + offset_x for point in box]
            ys = [float(point[1]) / scale + offset_y for point in box]
            lines.append(
                OcrLine(text, min(xs), min(ys), max(xs), max(ys), score)
            )
        return lines


class WinOcrBackend(OcrBackend):
    name = "winocr"

    def __init__(
        self,
        language: str = "zh-Hans",
        max_dimension: int = 4000,
        upscale: float = 2.0,
    ):
        self._language = language
        self._max_dim = max_dimension
        self._upscale = upscale

    def recognize(
        self,
        image: Image.Image,
        region=None,
        max_dim: int | None = None,
        upscale: float | None = None,
        fast: bool = False,
    ) -> list[OcrLine]:
        return asyncio.run(
            self._recognize(image, region, max_dim, upscale)
        )

    async def _recognize(
        self,
        image: Image.Image,
        region=None,
        max_dim: int | None = None,
        upscale: float | None = None,
    ) -> list[OcrLine]:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        image, scale, offset_x, offset_y = _crop_and_scale(
            image, region, max_dim, upscale, self._max_dim, self._upscale
        )
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, "PNG")

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(buffer.getvalue())
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()

        engine = OcrEngine.try_create_from_language(Language(self._language))
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        result = await engine.recognize_async(bitmap)

        lines: list[OcrLine] = []
        for line in result.lines:
            rects = [word.bounding_rect for word in line.words]
            if not rects:
                continue
            lines.append(
                OcrLine(
                    line.text,
                    min(r.x for r in rects) / scale + offset_x,
                    min(r.y for r in rects) / scale + offset_y,
                    max(r.x + r.width for r in rects) / scale + offset_x,
                    max(r.y + r.height for r in rects) / scale + offset_y,
                )
            )
        return lines


def create_backend(config: dict, log=print) -> OcrBackend:
    ocr_config = config.get("ocr", {})
    choice = ocr_config.get("backend", "auto")
    candidates = ["rapidocr", "winocr"] if choice == "auto" else [choice]
    errors: list[str] = []
    for name in candidates:
        try:
            if name == "rapidocr":
                backend = RapidOcrBackend(
                    ocr_config.get("max_dimension", 3000),
                    ocr_config.get("native_upscale", 2.0),
                    ocr_config.get("fast_det_limit", 1152),
                )
            elif name == "winocr":
                backend = WinOcrBackend(
                    ocr_config.get("language", "zh-Hans"),
                    ocr_config.get("max_dimension", 4000),
                    ocr_config.get("native_upscale", 2.0),
                )
            else:
                raise ValueError(f"未知 OCR 后端: {name}")
            log(f"[ocr] 后端就绪: {backend.name}")
            return backend
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            log(f"[ocr] 后端 {name} 不可用: {exc}")
    raise RuntimeError("没有可用的 OCR 后端: " + "; ".join(errors))
