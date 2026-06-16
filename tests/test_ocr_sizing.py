#!/usr/bin/env python3
"""Regression tests for OCR line-size estimation (pdftexteditor/ocr/reconstruct).

Run directly:  python tests/test_ocr_sizing.py

Guards the fix for the "OCR rebuild is oversized / overlapping" bug: an all-caps
or sparse line has no true lowercase x-height, so the old xh/x_ratio estimate
sized it ~1.5-2x too tall and it overlapped its neighbors. _line_em_px anchors to
the reliable OCR box height instead, using x-height only when it agrees.

All inputs are synthetic numbers and placeholder words (no real document text).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pdftexteditor.ocr.reconstruct import (  # noqa: E402
    _line_em_px, _BOX_EM_CAPS, _BOX_EM_MIXED,
)

X = 0.45  # the x_ratio reconstruct passes in pass 1


def test_caps_line_ignores_bogus_xheight():
    # On an all-caps line the connected-component "x-height" lands at cap height
    # (~0.72 * box). The old code did em = xh/X, sizing the line ~1.6x its box.
    box = 50.0
    bogus_xh = 0.72 * box
    em = _line_em_px(box, "ORDER READ BACK AND VERIFIED", bogus_xh, X)
    assert abs(em - box / _BOX_EM_CAPS) < 1e-6, "caps line must be box-anchored"
    assert em < bogus_xh / X, "must not reproduce the ~1.6x oversize"
    assert em <= box * 1.5, "a line never renders far past its own box height"


def test_mixed_line_uses_trustworthy_xheight():
    box = 40.0
    em_box = box / _BOX_EM_MIXED
    good_xh = em_box * X * 1.1  # x-height implies an em 1.1x the box estimate
    em = _line_em_px(box, "Patient Name On File", good_xh, X)
    assert abs(em - good_xh / X) < 1e-6, "mixed line should trust a good x-height"


def test_mixed_line_rejects_wild_xheight():
    box = 40.0
    wild_xh = box * 2.0  # absurd; would imply a huge em
    em = _line_em_px(box, "Ordering Physician", wild_xh, X)
    assert abs(em - box / _BOX_EM_MIXED) < 1e-6, "wild x-height must fall back to box"


def test_missing_xheight_falls_back_to_box():
    box = 30.0
    em = _line_em_px(box, "some mixed text", 0.0, X)
    assert abs(em - box / _BOX_EM_MIXED) < 1e-6


def test_digits_only_line_is_not_caps():
    # No alphabetic letters -> treated as mixed (not caps).
    box = 20.0
    em = _line_em_px(box, "12:34:56", 0.0, X)
    assert abs(em - box / _BOX_EM_MIXED) < 1e-6


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} ocr-sizing tests passed.")


if __name__ == "__main__":
    main()
