"""Unit tests for the scanned-edit color+damage module (pdftexteditor.ocr.degrade).

Runnable headless: `python tests/test_degrade.py`. Asserts the three guarantees the
edit pipeline relies on: hue-preserving colour recovery, HARD (non-smooth) damage
output, and severity that tracks the actual degradation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from pdftexteditor.ocr import degrade as D


def _word(ink=(20, 20, 20), paper=(250, 250, 250)):
    img = np.full((40, 160, 3), paper, np.uint8)
    img[12:28, 12:148] = ink
    return img


def test_color_recovery_preserves_hue():
    scan = _word(ink=(30, 35, 120))                 # blue-black ink
    ink, paper = D.sample_ink_paper(scan, scan.mean(2) < 120)
    assert ink[2] > ink[0] + 40, f"blue hue lost: {ink}"
    assert paper.mean() > 230, f"paper not recovered: {paper}"
    assert np.linalg.norm(ink - np.array([30, 35, 120])) < 30, f"ink off: {ink}"
    print("  ok color recovery preserves hue", ink.astype(int), paper.astype(int))


def test_hard_output_not_smooth():
    scan = _word()
    out = D.degrade_patch(scan, np.array([20., 20, 20]), np.array([250., 250, 250]), 0.45, seed=1)
    reg = out[12:28, 12:148].mean(2)
    # HARD: within the stroke region there are both near-ink and near-paper pixels
    # (dropout), not a uniform mid-grey ramp.
    assert reg.min() < 60, f"no solid ink survived: min {reg.min()}"
    assert reg.max() > 200, f"no paper dropout: max {reg.max()}"
    mid = ((reg > 90) & (reg < 200)).mean()         # smooth-grey fraction should be low
    assert mid < 0.45, f"too much smooth mid-grey: {mid:.2f}"
    print(f"  ok hard output (min {reg.min():.0f} max {reg.max():.0f} mid-frac {mid:.2f})")


def test_severity_tracks_degradation():
    clean = _word()
    mask = clean.mean(2) < 120
    sevs = []
    for s in (0.05, 0.30, 0.65):
        deg = D.hard_degrade(clean.astype(np.float32), np.array([20., 20, 20]),
                             np.array([250., 250, 250]), s, np.random.RandomState(0))
        sevs.append(D.local_severity(deg, mask, (12, 12, 148, 28)))
    assert sevs[0] < sevs[2], f"severity should rise with degradation: {sevs}"
    print(f"  ok severity tracks degradation: {[round(x, 2) for x in sevs]}")


if __name__ == "__main__":
    test_color_recovery_preserves_hue()
    test_hard_output_not_smooth()
    test_severity_tracks_degradation()
    print("test_degrade: all passed")
