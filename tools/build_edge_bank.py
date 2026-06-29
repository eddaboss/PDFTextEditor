"""Build the EDGE/CONTOUR font signature bank used by the render-and-compare matcher.

For every TTF in the font bank, render each reference character, normalize it to a
fixed tile, and store BOTH a coverage descriptor and a Sobel-EDGE descriptor (zero-mean,
unit-norm, int8-quantized). The edge descriptor is what lets the matcher tell serif from
sans (serifs are terminal EDGES that the old 24px blurred-coverage vector erased).

Writes ``font_edge_int8.npz`` next to the bank's TTFs. Built locally from the fonts that
are already on disk -- no new download. Run:

    QT_QPA_PLATFORM=offscreen python tools/build_edge_bank.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import cv2
import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pdftexteditor.ocr import fontbank  # noqa: E402

TILE = 32                      # descriptor tile (px); big enough to keep serif terminals
REF_CHARS = "aeonrstilcdhmugbpkwyfvjzxqAERNTHSILOGBCDMPUFKWYVJZXQ0123456789"
EM = 64.0


def _norm_tile(cov: np.ndarray, size: int = TILE):
    ys, xs = np.where(cov > 0.25)
    if len(ys) < 4:
        return None
    c = cov[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = c.shape
    sc = (size - 4) / max(h, w)
    nh, nw = max(1, int(round(h * sc))), max(1, int(round(w * sc)))
    rs = cv2.resize(c.astype(np.float32), (nw, nh), interpolation=cv2.INTER_AREA)
    t = np.zeros((size, size), np.float32)
    oy, ox = (size - nh) // 2, (size - nw) // 2
    t[oy:oy + nh, ox:ox + nw] = rs
    return t


def _unit(v: np.ndarray):
    v = v.astype(np.float32).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else None


def _cov_edge(tile: np.ndarray):
    gx = cv2.Sobel(tile, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(tile, cv2.CV_32F, 0, 1, ksize=3)
    edge = np.sqrt(gx * gx + gy * gy)
    return _unit(cv2.GaussianBlur(tile, (0, 0), 0.6)), _unit(edge)


def _render_cov(f, ch):
    rw = f.text_length(ch, EM)
    if rw <= 0:
        return None
    doc = fitz.open()
    pg = doc.new_page(width=rw + 2 * EM, height=EM * 3)
    tw = fitz.TextWriter(pg.rect)
    tw.append((EM, EM * 2.0), ch, font=f, fontsize=EM)
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
    return (255 - a.mean(2)) / 255.0


def main():
    ttf_dir = fontbank._ensure_ttf_cache()
    keys = sorted(os.listdir(ttf_dir))
    F, C, D = len(keys), len(REF_CHARS), TILE * TILE
    cov = np.zeros((F, C, D), np.float32)
    edge = np.zeros((F, C, D), np.float32)
    t0 = time.time()
    for fi, key in enumerate(keys):
        try:
            f = fitz.Font(fontfile=os.path.join(ttf_dir, key))
        except Exception:
            continue
        for ci, ch in enumerate(REF_CHARS):
            try:
                c = _render_cov(f, ch)
                if c is None:
                    continue
                t = _norm_tile(c)
                if t is None:
                    continue
                cv_, ed_ = _cov_edge(t)
                if cv_ is not None:
                    cov[fi, ci] = cv_
                if ed_ is not None:
                    edge[fi, ci] = ed_
            except Exception:
                continue
        if fi % 400 == 0:
            print(f"  {fi}/{F}  ({time.time() - t0:.0f}s)", flush=True)
    # int8 quantize (unit-norm vectors -> [-127,127] with a shared scale)
    scale = 127.0
    out = os.path.join(fontbank.bank_dir(), "font_edge_int8.npz")
    np.savez_compressed(
        out,
        cov=np.clip(np.round(cov * scale), -127, 127).astype(np.int8),
        edge=np.clip(np.round(edge * scale), -127, 127).astype(np.int8),
        paths=np.array(keys), chars=REF_CHARS, S=TILE, scale=np.float32(scale))
    print(f"wrote {out}  ({os.path.getsize(out)/1e6:.0f} MB) in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
