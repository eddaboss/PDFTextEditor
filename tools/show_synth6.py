#!/usr/bin/env python3
"""REAL 05/22/2026 vs the app's 05/22/2026 where ONLY the last digit (5->6) is a synth
glyph through the current pipeline (with the clumping degradation update); the rest of the
field stays real scan pixels. Ground truth: the original page has a real 05/22/2026 field
(the one the app bumps to 2027), cropped straight from the original.

  REAL  = original PDF, the 05/22/2026 field (Field B), all real scan
  SYNTH = edited PDF,   the 05/22/2025->2026 field (Field A): real 05/22/202 + synth 6

    python tools/show_synth6.py <edited.pdf>
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import cv2
from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import get_engine, pack
pack.ensure_on_path()

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
OUT = os.path.expanduser("~/Desktop/ocr_demos")
PI = 2


def render(path):
    d = PDFDocument(path); d.normalize_orientations()
    rgb = d.render_page_image(PI, 300.0); d.close()
    return rgb


def line_bbox(rgb, target):
    norm = lambda s: s.replace(" ", "")
    lines = [l for l in get_engine("auto").recognize(rgb) if target in norm(l.text)]
    if not lines:
        return None
    l = min(lines, key=lambda l: len(norm(l.text)))
    return [int(v) for v in l.bbox]


def crop(rgb, bb, padl=6, padr=6, padv=6):
    x0, y0, x1, y1 = bb
    H, W = rgb.shape[:2]
    return rgb[max(0, y0 - padv):min(H, y1 + padv), max(0, x0 - padl):min(W, x1 + padr)].copy()


def date_only(c):
    """From a line crop, keep ONLY the date: split off everything left of the word-space
    (the big gap before 05/22/...), then tighten to the ink. A standalone date has only
    small inter-digit gaps, so it is returned whole."""
    g = c.mean(2).astype(np.float32)
    colink = np.clip(255.0 - g, 0, None).sum(0)
    ink = colink > colink.max() * 0.05
    Wd = len(ink)
    gaps, i = [], 0
    while i < Wd:
        if not ink[i]:
            j = i
            while j < Wd and not ink[j]:
                j += 1
            if i > 1 and j < Wd - 1:
                gaps.append((i, j, j - i))
            i = j
        else:
            i += 1
    if gaps:
        widths = sorted(g[2] for g in gaps)
        big = max(gaps, key=lambda g: g[2])
        med = widths[len(widths) // 2]
        if big[2] > max(18, 1.8 * med):          # a real word-space, not an inter-digit gap
            c = c[:, big[1]:]
    # tighten to ink
    g2 = c.mean(2).astype(np.float32)
    cov = np.clip(255.0 - g2, 0, None)
    cols = np.where(cov.sum(0) > cov.sum(0).max() * 0.04)[0]
    rows = np.where(cov.sum(1) > cov.sum(1).max() * 0.04)[0]
    if cols.size and rows.size:
        c = c[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    return c


def main():
    edited = sys.argv[1]
    a = render(ORIG)
    e = render(edited)
    bbA = line_bbox(a, "05/22/2025")     # Field A location (becomes synth 2026 in edited)
    bbB = line_bbox(a, "05/22/2026")     # Field B = the REAL 05/22/2026
    if not bbA or not bbB:
        print(f"bbA={bbA} bbB={bbB} -- not found"); return
    real = date_only(crop(a, bbB))                  # real 05/22/2026
    synth = date_only(crop(e, bbA, padr=30))        # 05/22/2026 with synth 6 (rest real scan)

    def row(img, tag, H=190):
        sc = H / img.shape[0]
        up = cv2.resize(img, (int(img.shape[1] * sc), H), interpolation=cv2.INTER_NEAREST)
        bar = np.full((24, max(up.shape[1], 300), 3), 245, np.uint8)
        cv2.putText(bar, tag, (8, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (130, 60, 0), 1, cv2.LINE_AA)
        up = np.pad(up, ((0, 0), (0, max(0, bar.shape[1] - up.shape[1])), (0, 0)), constant_values=255)
        return np.vstack([bar, up])

    r1 = row(real, "REAL")
    r2 = row(synth, "SYNTH (5->6)")
    W = max(r1.shape[1], r2.shape[1])
    pad = lambda im: np.pad(im, ((0, 0), (0, W - im.shape[1]), (0, 0)), constant_values=255)
    stack = np.vstack([pad(r1), np.full((14, W, 3), 220, np.uint8), pad(r2)])
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "date_synth6_vs_real.png")
    cv2.imwrite(out, cv2.cvtColor(stack, cv2.COLOR_RGB2BGR))

    # ACTUAL document scale (1x) -- invisibility is judged here, not at 9x
    def nat(img, tag):
        bar = np.full((13, max(img.shape[1], 110), 3), 245, np.uint8)
        cv2.putText(bar, tag, (3, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (130, 60, 0), 1, cv2.LINE_AA)
        img = np.pad(img, ((0, 0), (0, max(0, bar.shape[1] - img.shape[1])), (0, 0)), constant_values=255)
        return np.vstack([bar, img])
    n1, n2 = nat(real, "REAL 1x"), nat(synth, "SYNTH 1x")
    Wn = max(n1.shape[1], n2.shape[1])
    padn = lambda im: np.pad(im, ((0, 0), (0, Wn - im.shape[1]), (0, 0)), constant_values=255)
    natstack = np.vstack([padn(n1), np.full((6, Wn, 3), 220, np.uint8), padn(n2)])
    out2 = os.path.join(OUT, "date_synth6_actual.png")
    cv2.imwrite(out2, cv2.cvtColor(natstack, cv2.COLOR_RGB2BGR))
    print(f"saved {out}\nsaved {out2}")


if __name__ == "__main__":
    main()
