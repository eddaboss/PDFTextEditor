"""On-device OCR engine adapters (OCR_SPEC §2).

One tiny interface -- ``recognize(image_rgb) -> list[OcrLine]`` -- with a
RapidOCR (PaddleOCR PP-OCR on ONNX Runtime) primary that runs identically on
macOS and Windows, plus an optional Apple Vision fast path on macOS. The engine
session is built once and cached (model load is the cost); everything downstream
is engine-agnostic.

No cloud. No PyTorch. RapidOCR ships as a pure-Python wheel over a single
onnxruntime shared lib, which is what makes it bundleable.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

import numpy as np

from . import pack


class OcrPackMissing(RuntimeError):
    """RapidOCR is selected but its downloadable OCR component is not installed.
    The UI catches this to offer the one-time download."""


@dataclass
class OcrLine:
    """One recognized text line. ``quad`` is 4 (x, y) corner points in image
    pixels (clockwise from top-left); ``text`` the recognized string;
    ``confidence`` in 0..1."""

    quad: list
    text: str
    confidence: float

    @property
    def bbox(self) -> tuple:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        return (min(xs), min(ys), max(xs), max(ys))


class OcrEngine:
    """Abstract recognizer."""

    name = "base"

    def recognize(self, image_rgb: np.ndarray) -> "list[OcrLine]":
        raise NotImplementedError


class RapidOcrEngine(OcrEngine):
    """RapidOCR (PP-OCR on ONNX Runtime). Detection + recognition + angle in one
    call, per-line boxes + confidence. The session is heavy to build, so it is
    created lazily and reused across pages/documents."""

    name = "rapidocr"
    _engine = None
    _lock = threading.Lock()
    # Inference is serialized: one RapidOCR session is shared across pages, and
    # its ONNX Runtime sessions already saturate the CPU with intra-op threads,
    # so running recognitions concurrently would only oversubscribe the cores
    # (and the Python wrapper is not guaranteed reentrant). With page OCR now
    # running on a thread pool, the CPU win comes from overlapping this step
    # with the parallel reconstruct of OTHER pages, not from parallel inference.
    _infer_lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        """True once the downloadable OCR component is installed + importable."""
        return pack.ensure_on_path()

    @classmethod
    def _session(cls):
        if cls._engine is None:
            with cls._lock:
                if cls._engine is None:
                    pack.ensure_on_path()
                    try:
                        from rapidocr_onnxruntime import RapidOCR
                    except ImportError as exc:
                        raise OcrPackMissing(
                            "The OCR component is not installed.") from exc
                    cls._engine = RapidOCR()
        return cls._engine

    def recognize(self, image_rgb: np.ndarray) -> "list[OcrLine]":
        ocr = self._session()
        # RapidOCR accepts an ndarray; it expects BGR/!-agnostic uint8.
        with self._infer_lock:
            result, _ = ocr(np.ascontiguousarray(image_rgb))
        out: list[OcrLine] = []
        for row in result or []:
            quad, text, conf = row[0], row[1], row[2]
            try:
                c = float(conf)
            except (TypeError, ValueError):
                c = 0.0
            if text and text.strip():
                out.append(OcrLine(quad=[[float(p[0]), float(p[1])] for p in quad],
                                   text=text, confidence=c))
        return out


class AppleVisionEngine(OcrEngine):
    """Apple Vision (``VNRecognizeTextRequest`` accurate) via ``ocrmac`` -- a
    macOS fast path that uses the Neural Engine and adds zero bundle bytes.
    Line-level only (accurate-mode per-char boxes are unreliable, OCR_SPEC §2).
    Optional: only used when ``ocrmac`` is importable."""

    name = "applevision"

    @staticmethod
    def available() -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import ocrmac  # noqa: F401
            return True
        except Exception:
            return False

    def recognize(self, image_rgb: np.ndarray) -> "list[OcrLine]":
        from ocrmac import ocrmac
        from PIL import Image
        h, w = image_rgb.shape[:2]
        pil = Image.fromarray(image_rgb)
        ann = ocrmac.OCR(pil, recognition_level="accurate").recognize()
        out: list[OcrLine] = []
        for text, conf, box in ann:
            # ocrmac boxes are normalized (x, y, w, h) with origin BOTTOM-left.
            bx, by, bw, bh = box
            x0 = bx * w
            x1 = (bx + bw) * w
            y0 = (1.0 - (by + bh)) * h
            y1 = (1.0 - by) * h
            quad = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            if text and text.strip():
                out.append(OcrLine(quad=quad, text=text,
                                   confidence=float(conf)))
        return out


def get_engine(name: str = "auto") -> OcrEngine:
    """Select an engine. ``auto`` prefers Apple Vision on macOS when available
    (fast, Neural Engine), else RapidOCR (the cross-platform default)."""
    if name == "rapidocr":
        return RapidOcrEngine()
    if name == "applevision":
        return AppleVisionEngine()
    if name == "auto" and AppleVisionEngine.available():
        return AppleVisionEngine()
    return RapidOcrEngine()
