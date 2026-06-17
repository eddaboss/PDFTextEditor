"""Runtime font identification against the shipped font bank.

At OCR time we know each scanned glyph's CHARACTER and WHERE it sits, so we can
identify the document's actual font by matching the scanned glyph shapes against a
precomputed shape fingerprint of ~4,000 body fonts (system + Google Fonts, Latin
subset). The matched font is then embedded into the edit, so a replacement looks
like the document instead of one of three fallback families.

This is the RUNTIME half of the research matcher (scan_degrade/fontbank.py): the
coarse descriptor match only -- deterministic, no training, ~70 ms per page, true
font in the top result the majority of the time and in the top handful ~98%. The
bank ships as:
  * ``fontbank.tar.xz``            -- Latin-subset TTFs (decompressed once to a
                                       TTF cache; the format PyMuPDF embeds), and
  * ``font_fingerprints_int8.npz`` -- the int8-quantized shape descriptors
                                       (near-lossless: cos > 0.997 vs float).

The bank lives under ``$OCR_FONT_BANK_DIR`` or the app-data ``fontbank/`` dir;
when it is absent ``match_font`` returns None and the caller falls back to the
bundled 3-family classifier, so the app always works without the bank.
"""
from __future__ import annotations

import os
import tarfile
import lzma

import cv2
import numpy as np

REF_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CIDX = {c: i for i, c in enumerate(REF_CHARS)}
S = 24
_FILL = S - 4
_BLUR = 0.6


def bank_dir() -> str:
    """The WRITABLE bank dir (holds the decompressed TTF cache): ``$OCR_FONT_BANK_DIR``
    overrides; else the app-data ``fontbank/`` directory (created on demand)."""
    env = os.environ.get("OCR_FONT_BANK_DIR")
    if env:
        return os.path.expanduser(env)
    base = os.path.expanduser("~/Library/Application Support/PDFTextEditor") \
        if os.path.isdir(os.path.expanduser("~/Library")) \
        else os.path.expanduser("~/.pdftexteditor")
    return os.path.join(base, "fontbank")


def _bundled_dir() -> str:
    """The bank shipped INSIDE the app (PyInstaller copies ``assets/``). Read-only,
    so the archive is decompressed from here into the writable ``bank_dir()``."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "fontbank")


def _find(name: str) -> "str | None":
    """``name`` from the writable bank dir if present, else the bundled copy."""
    for d in (bank_dir(), _bundled_dir()):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------- #
#  descriptor (must match the bank build exactly)
# --------------------------------------------------------------------------- #
def _to_alpha(glyph_rgb: np.ndarray) -> np.ndarray:
    """Coverage 0..1 from a scanned glyph, normalized to its own ink/paper so the
    descriptor is independent of ink colour and faded brightness."""
    g = glyph_rgb.mean(2).astype(np.float32) if glyph_rgb.ndim == 3 \
        else glyph_rgb.astype(np.float32)
    paper_self = np.percentile(g, 85)
    ink_self = np.percentile(g, 8)
    return np.clip((paper_self - g) / max(paper_self - ink_self, 1e-3), 0.0, 1.0)


def _normtile(cov: np.ndarray, size: int = S) -> "np.ndarray | None":
    ys, xs = np.where(cov > 0.15)
    if len(ys) < 4:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = cov[y0:y1, x0:x1]
    h, w = crop.shape
    sc = (size - 4) / max(h, w)
    nh, nw = max(1, int(round(h * sc))), max(1, int(round(w * sc)))
    rs = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)
    tile = np.zeros((size, size), np.float32)
    oy, ox = (size - nh) // 2, (size - nw) // 2
    tile[oy:oy + nh, ox:ox + nw] = rs
    return tile


def _descriptor(cov: np.ndarray) -> "np.ndarray | None":
    tile = _normtile(cov)
    if tile is None:
        return None
    tile = cv2.GaussianBlur(tile, (0, 0), _BLUR)
    v = tile.reshape(-1).astype(np.float32)
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else None


def _glyph_descriptor(scan_rgb: np.ndarray, box) -> "np.ndarray | None":
    x0, y0, x1, y1 = box
    cell = scan_rgb[max(0, y0):y1, max(0, x0):x1]
    if cell.size == 0 or cell.shape[0] < 4 or cell.shape[1] < 4:
        return None
    return _descriptor(_to_alpha(cell))


# --------------------------------------------------------------------------- #
#  bank: int8 fingerprints + lazily-decompressed TTF cache
# --------------------------------------------------------------------------- #
_LOADED: dict | None = None


def _load_fingerprints() -> "dict | None":
    """Load + dequantize the int8 fingerprint cache once. None if the bank file
    is absent (caller falls back to the 3-family classifier)."""
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    npz = _find("font_fingerprints_int8.npz")
    if npz is None:
        return None
    z = np.load(npz, allow_pickle=False)
    scale = float(z["scale"]) if "scale" in z else 127.0
    _LOADED = {
        "desc": z["desc"].astype(np.float32) / scale,    # (F, 62, S*S) dequantized
        "paths": [str(p) for p in z["paths"]],           # tar member keys ("01234.ttf")
        "chars": str(z["chars"]),
    }
    return _LOADED


def _ensure_ttf_cache() -> "str | None":
    """Decompress ``fontbank.tar.xz`` to ``<bank>/ttf`` once (stdlib lzma -> TTF,
    the format PyMuPDF embeds). Returns the ttf dir, or None if the archive is
    absent. Idempotent: a populated ttf dir is reused."""
    bdir = bank_dir()
    ttf_dir = os.path.join(bdir, "ttf")
    if os.path.isdir(ttf_dir) and os.listdir(ttf_dir):
        return ttf_dir
    archive = _find("fontbank.tar.xz")
    if archive is None:
        return None
    os.makedirs(bdir, exist_ok=True)
    with lzma.open(archive, "rb") as xz:
        with tarfile.open(fileobj=xz, mode="r") as tf:
            tf.extractall(bdir)                          # writes <bank>/ttf/*.ttf
    return ttf_dir if os.path.isdir(ttf_dir) else None


def available() -> bool:
    """True when the bank (fingerprints + archive) is present and usable."""
    return _load_fingerprints() is not None and \
        _find("fontbank.tar.xz") is not None


def font_file_for(family_name: str) -> "str | None":
    """Map a matched face name ('ScanFont-01234') back to its TTF in the
    decompressed bank cache, so the edit rasters can render in that exact font.
    None for non-bank families (the caller falls back to the bundled set)."""
    prefix = "ScanFont-"
    if not family_name or not family_name.startswith(prefix):
        return None
    ttf_dir = _ensure_ttf_cache()
    if ttf_dir is None:
        return None
    path = os.path.join(ttf_dir, family_name[len(prefix):] + ".ttf")
    return path if os.path.exists(path) else None


# --------------------------------------------------------------------------- #
#  match
# --------------------------------------------------------------------------- #
def identify(scan_rgb: np.ndarray, cells: dict, topk: int = 5) -> dict:
    """Coarse shape match of the document font from scanned glyphs.

    ``cells``: {key: (char, (x0,y0,x1,y1))} for existing scanned text (image px).
    Returns {best, confidence, margin, topk, n_glyphs}; ``best`` is a tar member
    key, or None when no usable glyph descriptors were found."""
    fb = _load_fingerprints()
    if fb is None:
        return dict(best=None, confidence=0.0, margin=0.0, topk=[], n_glyphs=0)
    bank = fb["desc"]
    F = bank.shape[0]
    per_char: dict[int, list] = {}
    for ch, box in cells.values():
        if ch not in _CIDX:
            continue
        d = _glyph_descriptor(scan_rgb, box)
        if d is not None:
            per_char.setdefault(_CIDX[ch], []).append(d)
    if not per_char:
        return dict(best=None, confidence=0.0, margin=0.0, topk=[], n_glyphs=0)
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    n_glyphs = 0
    for cidx, ds in per_char.items():
        q = np.mean(ds, axis=0)
        q -= q.mean()
        nq = np.linalg.norm(q)
        if nq < 1e-6:
            continue
        q /= nq
        n_glyphs += len(ds)
        col = bank[:, cidx, :]
        present = np.any(col != 0, axis=1)
        sim = col @ q
        score += np.where(present, sim, 0.0)
        wsum += present.astype(np.float32)
    valid = wsum > 0
    mean_score = np.full(F, -1.0, np.float32)
    mean_score[valid] = score[valid] / wsum[valid]
    order = np.argsort(-mean_score)
    top = [(fb["paths"][i], float(mean_score[i])) for i in order[:topk]]
    best_s = top[0][1]
    second = top[1][1] if len(top) > 1 else 0.0
    return dict(best=top[0][0], confidence=best_s, margin=best_s - second,
                topk=top, n_glyphs=n_glyphs)


# Below this confidence (or margin) the match is not trustworthy enough to prefer
# over the bundled 3-family classifier, so the caller falls back.
_MIN_CONFIDENCE = 0.45
_MIN_GLYPHS = 8


def match_font(scan_rgb: np.ndarray, cells: dict) -> "tuple[bytes, str] | None":
    """Identify the document font and return ``(ttf_bytes, face_name)`` for the
    best match -- ready to register as a custom face and embed -- or None when the
    bank is absent, no glyphs matched, or the match is too weak to trust (the
    caller then uses the bundled 3-family classifier)."""
    if not available():
        return None
    res = identify(scan_rgb, cells)
    if not res["best"] or res["n_glyphs"] < _MIN_GLYPHS \
            or res["confidence"] < _MIN_CONFIDENCE:
        return None
    ttf_dir = _ensure_ttf_cache()
    if ttf_dir is None:
        return None
    path = os.path.join(ttf_dir, res["best"])
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        data = fh.read()
    name = "ScanFont-" + os.path.splitext(os.path.basename(res["best"]))[0]
    return data, name
