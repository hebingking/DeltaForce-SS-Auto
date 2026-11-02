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


class OcrBackend:
    name = "base"

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        raise NotImplementedError


class RapidOcrBackend(OcrBackend):
    name = "rapidocr"

    def __init__(self, max_dimension: int = 3000, upscale: float = 2.0):
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()
        self._max_dim = max_dimension
        self._upscale = upscale

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        import numpy as np

        width, height = image.size
        scale = min(self._upscale, max(1.0, self._max_dim / max(width, height)))
        if scale > 1.01:
            image = image.resize(
                (round(width * scale), round(height * scale)), Image.LANCZOS
            )
        result, _ = self._engine(np.array(image))
        lines: list[OcrLine] = []
        for item in result or []:
            box, text, score = item[0], str(item[1]), float(item[2])
            xs = [float(point[0]) / scale for point in box]
            ys = [float(point[1]) / scale for point in box]
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

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        return asyncio.run(self._recognize(image))

    async def _recognize(self, image: Image.Image) -> list[OcrLine]:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        width, height = image.size
        scale = min(self._upscale, max(1.0, self._max_dim / max(width, height)))
        if scale > 1.01:
            image = image.resize(
                (round(width * scale), round(height * scale)), Image.LANCZOS
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
                    min(r.x for r in rects) / scale,
                    min(r.y for r in rects) / scale,
                    max(r.x + r.width for r in rects) / scale,
                    max(r.y + r.height for r in rects) / scale,
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
