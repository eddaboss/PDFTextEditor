#!/usr/bin/env python3
"""Decisive probe for the degradation rework: on the REAL scanned 05/22/2026 date,
compare four renders of the SAME clean digits, at the estimator's native scale:

  REAL      the all-real scanned date crop (ground truth)
  CURRENT   degrade.apply_measured_damage (the live pipeline filter that made the
            deliverable) -- also prints whether measure_damage bailed to None
  ESTIMATOR letterfilter.hard_degrade driven by the trained stylenet Style knobs
  DEFAULT   letterfilter.hard_degrade with the default (pre-ML) Style

Everything runs at height 40 (EM~28 scale the estimator was trained at) so the
filter's px-scale knobs (clump_cell, blur, noise) are applied at the scale they were
learned at; the stack is upscaled NEAREST for viewing. Writes to ~/Desktop/ocr_demos/.

    python tools/probe_estimator.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

import numpy as np
import cv2
from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import pack, get_engine, degrade
pack.ensure_on_path()

from scan_degrade import stylenet as SN, datagen, letterfilter as LF

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
NARROW = "/System/Library/Fonts/Supplemental/Arial Narrow.ttf"
OUT = os.path.expanduser("~/Desktop/ocr_demos")
TARGET = "05/22/2026"


def real_date_crop(page=2, pad=6):
    doc = PDFDocument(ORIG)
    doc.normalize_orientations()
    rgb = doc.render_page_image(page, 300.0)
    lines = get_engine("auto").recognize(rgb)
    norm = lambda s: s.replace(" ", "")
    cands = [ln for ln in lines if TARGET in norm(ln.text)]
    ln = min(cands, key=lambda l: len(norm(l.text)))
    x0, y0, x1, y1 = (int(v) for v in ln.bbox)
    H, W = rgb.shape[:2]
    crop = rgb[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)].copy()
    doc.close()
    return crop


def to_h40(img):
    s = 40.0 / img.shape[0]
    return cv2.resize(img, (max(1, int(round(img.shape[1] * s))), 40), interpolation=cv2.INTER_AREA)


def label(img, txt):
    bar = np.full((16, img.shape[1], 3), 245, np.uint8)
    cv2.putText(bar, txt, (4, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (130, 60, 0), 1, cv2.LINE_AA)
    return np.vstack([bar, img])


def main():
    R = np.random.RandomState(7)
    real = real_date_crop()
    real40 = to_h40(real)
    Wd = real40.shape[1]

    # clean digits in a condensed sans, matched to the real crop footprint
    clean_full = datagen.render_clean(TARGET, NARROW, 30, [40, 40, 40], [250, 250, 250])
    clean40 = cv2.resize(clean_full, (Wd, 40), interpolation=cv2.INTER_AREA)

    # ink / paper recovered from the real crop, under its own stroke mask
    ink_mask = degrade.coverage(real) > 0.45
    ink, paper = degrade.sample_ink_paper(real, ink_mask)
    print(f"ink={ink.round(0)} paper={paper.round(0)}")

    # severity: directly measured from the real crop (the estimator supplies STYLE only)
    fp = degrade.coverage(real) > 0.45
    sev = float(degrade.local_severity(real, fp, (0, 0, real.shape[1], real.shape[0])))
    print(f"measured local severity = {sev:.3f}")

    # CURRENT live filter: measure the neighbours' damage and copy it
    prof = degrade.measure_damage(real40, None)
    print(f"measure_damage -> {'None (bails to crisp recolour)' if prof is None else 'profile: edge_dark=%s' % prof.get('edge_dark')}")
    cur = degrade.apply_measured_damage(clean40, ink, paper, prof, np.random.RandomState(11))

    # ESTIMATOR: read real-vs-clean, get Style knobs + the net's OWN severity
    est_sev, est_style, p = SN.estimate(real, clean_full)
    print("estimated style: " + "  ".join(f"{k} {p[k]:.2f}" for k in SN.ORDER))
    print(f"estimator severity = {est_sev:.3f}  (vs measured local_severity {sev:.3f})")

    # Severity sweep with the estimator's STYLE knobs, to see where REAL actually sits
    # (the net predicts {est_sev:.2f}; local_severity floors at {sev:.2f}).
    sweep = [0.12, 0.22, 0.35, est_sev]
    rows = [label(real40, "REAL (scanned ground truth) " + TARGET),
            label(cur, "CURRENT  apply_measured_damage (live pipeline)")]
    sep = np.full((6, Wd, 3), 215, np.uint8)
    out_rows = [rows[0], sep, rows[1], sep]
    for s in sweep:
        g = LF.hard_degrade(clean40.astype(np.float32), ink, paper, float(s),
                            np.random.RandomState(21), style=est_style)
        tag = f"ESTIMATOR style @ sev {s:.2f}" + ("  <- net's prediction" if abs(s - est_sev) < 1e-6 else "")
        out_rows += [label(g, tag), sep]
    stack = np.vstack(out_rows[:-1])
    stack = cv2.resize(stack, None, fx=5, fy=5, interpolation=cv2.INTER_NEAREST)
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "probe_estimator_date.png")
    cv2.imwrite(out, cv2.cvtColor(stack, cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
