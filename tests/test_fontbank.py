#!/usr/bin/env python3
"""Regression tests for the bank font matcher (pdftexteditor/ocr/fontbank.py).

Self-contained: builds a tiny 3-font bank from the BUNDLED fonts (Tinos / Arimo /
Cousine) in a temp dir -- no 100 MB artifact needed in CI -- and checks that the
runtime matcher recovers the right font from a synthetic scan, and that it falls
back cleanly (returns None, never raises) when no bank is present.

Run:  python tests/test_fontbank.py
"""
import io
import lzma
import os
import shutil
import sys
import tarfile
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fitz  # noqa: E402
import numpy as np  # noqa: E402

from pdftexteditor.ocr import fontbank as fb  # noqa: E402

_FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
_BANK_FONTS = [
    ("tinos", os.path.join(_FONTS, "Tinos-Regular.ttf")),
    ("arimo", os.path.join(_FONTS, "Arimo[wght].ttf")),
    ("cousine", os.path.join(_FONTS, "Cousine-Regular.ttf")),
]


def _render_cov(f: "fitz.Font", ch: str, em: int = 64) -> np.ndarray:
    """Coverage of one isolated char -- the SAME convention make_bank fingerprints
    with, so the query and the bank are comparable."""
    adv = f.text_length(ch, em)
    doc = fitz.open()
    pg = doc.new_page(width=int(adv + 2 * em), height=int(em * 3))
    by = em * 2.0
    tw = fitz.TextWriter(pg.rect)
    tw.append((em, by), ch, font=f, fontsize=em)
    tw.write_text(pg)
    pm = pg.get_pixmap(alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3]
    cov = (255 - img.mean(2)) / 255.0
    yt, yb = int(round(by - 0.80 * em)), int(round(by + 0.26 * em))
    return cov[max(0, yt):yb, int(round(em)):int(round(em + adv))]


def _build_mini_bank(out_dir: str) -> None:
    """Fingerprint the bundled fonts into an int8 npz + tar.xz, the shipped layout."""
    ttf_dir = os.path.join(out_dir, "ttf")
    os.makedirs(ttf_dir, exist_ok=True)
    keys, descs = [], []
    for i, (_, fp) in enumerate(_BANK_FONTS):
        f = fitz.Font(fontfile=fp)
        per_char = np.zeros((len(fb.REF_CHARS), fb.S * fb.S), np.float32)
        for ci, ch in enumerate(fb.REF_CHARS):
            d = fb._descriptor(_render_cov(f, ch))
            if d is not None:
                per_char[ci] = d
        key = f"{i:05d}.ttf"
        shutil.copy(fp, os.path.join(ttf_dir, key))
        keys.append(key)
        descs.append(per_char.astype(np.float16))
    arr = np.stack(descs)
    q = np.clip(np.round(arr.astype(np.float32) * 127.0), -127, 127).astype(np.int8)
    np.savez_compressed(os.path.join(out_dir, "font_fingerprints_int8.npz"),
                        desc=q, scale=np.float32(127.0), paths=np.array(keys),
                        chars=fb.REF_CHARS, S=fb.S)
    with lzma.open(os.path.join(out_dir, "fontbank.tar.xz"), "wb") as xz:
        with tarfile.open(fileobj=xz, mode="w") as tf:
            tf.add(ttf_dir, arcname="ttf")


def _scan_cells(font_path: str, text: str):
    """Render ``text`` in a font and return (scan_rgb, cells) the way the OCR path
    feeds the matcher (cells boxed to the glyph band)."""
    f = fitz.Font(fontfile=font_path)
    fs, base, sc = 26, 70, 2.0
    doc = fitz.open()
    pg = doc.new_page(width=620, height=160)
    pg.draw_rect(pg.rect, color=(1, 1, 1), fill=(1, 1, 1), width=0)
    tw = fitz.TextWriter(pg.rect)
    tw.append((20, base), text, font=f, fontsize=fs)
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(matrix=fitz.Matrix(sc, sc), alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3].copy()
    top, bot = base - f.ascender * fs, base - f.descender * fs
    x, cells = 20 * sc, {}
    for ch in text:
        w = f.text_length(ch, fs) * sc
        if ch.strip():
            cells[len(cells)] = (ch, (int(x), int(top * sc), int(x + w), int(bot * sc)))
        x += w
    return img, cells


def test_fallback_without_bank() -> None:
    """No bank present -> available() False, match_font None, no exception. This is
    the path CI and any un-provisioned install run on."""
    os.environ["OCR_FONT_BANK_DIR"] = "/tmp/pdfte_no_such_bank_dir"
    fb._LOADED = None
    assert fb.available() is False
    img, cells = _scan_cells(_BANK_FONTS[0][1], "Sample 123")
    assert fb.match_font(img, cells) is None
    assert fb.font_file_for("Helvetica") is None
    print("  ok  no bank -> clean fallback (no crash)")


def test_mini_bank_identifies_font() -> None:
    """The matcher recovers the rendered font from a 3-font bank, and match_font
    returns embeddable TTF bytes + a ScanFont name."""
    with tempfile.TemporaryDirectory() as d:
        _build_mini_bank(d)
        os.environ["OCR_FONT_BANK_DIR"] = d
        fb._LOADED = None
        assert fb.available() is True
        for idx, (label, fp) in enumerate(_BANK_FONTS):
            img, cells = _scan_cells(fp, "The quick brown Fox 1234567890")
            res = fb.identify(img, cells)
            want = f"{idx:05d}.ttf"
            assert res["best"] == want, (
                f"{label}: matcher picked {res['best']} not {want} "
                f"(conf {res['confidence']:.2f})")
            matched = fb.match_font(img, cells)
            assert matched is not None, f"{label}: match_font returned None"
            data, name = matched
            assert data[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO") and \
                name == f"ScanFont-{idx:05d}", \
                f"{label}: bad bytes/name {name}"
        print("  ok  mini-bank recovers Tinos/Arimo/Cousine + returns TTF bytes")
    os.environ.pop("OCR_FONT_BANK_DIR", None)
    fb._LOADED = None


def main() -> None:
    test_fallback_without_bank()
    test_mini_bank_identifies_font()
    print("\n2 fontbank tests passed.")


if __name__ == "__main__":
    main()
