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
#  fine re-rank
#
#  The coarse 24px-blurred descriptor cannot tell glyph SHAPE apart on a heavy or
#  degraded scan -- it sees mostly stroke WEIGHT, so it ranks bold sans above the
#  true monospace/serif (every heavy font ties ~0.88-0.90, margin ~0.004). This
#  stage RENDERS the top coarse candidates at higher resolution WITH a Sobel-edge
#  channel (serif terminals + stroke contrast the coarse pass blurs away) and
#  re-ranks by how well each font reconstructs the scanned glyphs. It runs ONCE per
#  page after OCR -- never on the per-keystroke edit path. On a real heavy scan it
#  pulls the right face (e.g. Courier New) from coarse rank ~37 to #1 with a clear
#  margin, where the coarse pass alone returns Arial Bold.
# --------------------------------------------------------------------------- #
_RENDER_EM = 64.0
_FINE_S = 32
_FINE_WEDGE = 0.8
# int8 quantization scale for the PREBUILT fine bank (tools/build_fine_bank.py ->
# font_fine_int8.npy). The fine descriptor is 2048-dim unit-norm so |components|
# stay small (~0.11 max); ×700 uses the int8 range without clipping and keeps
# cos(stored, live) ~ 0.9998, so prebuilt matching == live matching.
_FINE_QSCALE = 700.0


def _unit(v: np.ndarray) -> "np.ndarray | None":
    v = v - v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else None


def _fine_from_cov(cov: np.ndarray) -> "np.ndarray | None":
    """High-res coverage + Sobel-edge descriptor (zero-mean, unit-norm). Coverage is
    the silhouette; the edge channel adds the serif/stroke detail the coarse loses."""
    t = _normtile(cov, _FINE_S)
    if t is None:
        return None
    cb = _unit(cv2.GaussianBlur(t, (0, 0), 0.5).reshape(-1).astype(np.float32))
    if cb is None:
        return None
    gx = cv2.Sobel(t, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(t, cv2.CV_32F, 0, 1, ksize=3)
    e = _unit(cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), 0.5).reshape(
        -1).astype(np.float32))
    if e is None:
        return cb
    v = np.concatenate([cb, e * _FINE_WEDGE])
    return v / np.linalg.norm(v)


def _render_glyph_cov(f, ch: str, em: float = _RENDER_EM) -> np.ndarray:
    """Coverage of one char rendered isolated, cropped to the advance + baseline band
    (the same crop convention the scanned query uses)."""
    import fitz
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


# Rendered-candidate caches: a candidate font's fine descriptor for a char is the
# SAME every time, but it is the hot cost of _refine (open font + rasterize + descriptor).
# Memoizing per (path, char) means the FIRST match (OCR) renders each candidate once
# and every later match -- crucially the live per-caret font lookup on an edit -- reuses
# them, so a keystroke does not pay a full bank render and the editor never freezes.
_CAND_FONT_CACHE: dict = {}
_CAND_DESC_CACHE: dict = {}
_CAND_FEAT_CACHE: dict = {}


def _cand_font(path: str):
    if path not in _CAND_FONT_CACHE:
        try:
            import fitz
            _CAND_FONT_CACHE[path] = fitz.Font(fontfile=path)
        except Exception:
            _CAND_FONT_CACHE[path] = None
    return _CAND_FONT_CACHE[path]


def _cand_char_desc(path: str, ch: str):
    key = (path, ch)
    if key in _CAND_DESC_CACHE:
        return _CAND_DESC_CACHE[key]
    f = _cand_font(path)
    d = None
    try:
        if f is not None and f.has_glyph(ord(ch)):
            d = _fine_from_cov(_render_glyph_cov(f, ch))
    except Exception:
        d = None
    _CAND_DESC_CACHE[key] = d
    return d


def _glyph_features(cov: np.ndarray) -> "np.ndarray | None":
    """Scale-invariant TYPEFACE measurements of one glyph coverage tile -- the axes the
    shape-cosine descriptor normalizes away, so they discriminate fonts the descriptor
    ties. All ratios (no absolute size):
      aspect     ink width / height            -- condensed vs wide
      fill       ink area / bbox area          -- dense grotesque vs open humanist
      stroke     stroke width / height         -- weight (light .. bold)
      contrast   stroke-width spread / mean    -- modulation (mono/grotesque .. serif/didone)
      round      4*pi*area / perimeter^2       -- round bowls vs angular/square
      vh         |dy| edge energy / |dx|       -- horizontal vs vertical stroke structure
    Counter (enclosed-hole) ratio is deliberately OMITTED: a degraded scan fills its
    counters, so it reads ~0 and would mismatch every clean font."""
    bw = (cov > 0.4).astype(np.uint8)
    ys, xs = np.where(bw)
    if len(ys) < 8:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    h, w = int(y1 - y0), int(x1 - x0)
    c = bw[y0:y1, x0:x1]
    area = float(c.sum())
    if area < 6 or h < 3 or w < 3:
        return None
    dt = cv2.distanceTransform(c, cv2.DIST_L2, 3)
    dv = dt[c > 0]
    mean_dt = max(float(dv.mean()), 1e-3)
    cs, _hi = cv2.findContours(c, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    perim = float(sum(cv2.arcLength(k, True) for k in cs)) or 1.0
    gx = cv2.Sobel(c.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(c.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    sgx, sgy = float(np.abs(gx).sum()), float(np.abs(gy).sum())
    return np.array([
        w / h,                                       # aspect
        area / (h * w),                              # fill
        2.0 * mean_dt / h,                           # stroke (weight)
        float(dv.std()) / mean_dt,                   # contrast (modulation)
        4.0 * np.pi * area / (perim * perim),        # roundness
        # v/h structure as a BOUNDED [0,1] proportion of vertical edge energy.
        # The raw ratio |gy|/|gx| explodes (~1e5) for a clean vertical stem like
        # 'i'/'l'/'1' (near-zero horizontal gradient), and ONE exploded glyph
        # poisoned a whole font's feature penalty to -999 -- silently excluding
        # it from the match (Arimo could not even match its own render). A
        # proportion cannot blow up and stays calibrated to _FEAT_SD[5].
        sgy / (sgx + sgy + 1e-6),                    # v/h structure
    ], np.float32)


def _cand_char_feat(path: str, ch: str):
    key = (path, ch)
    cache = _CAND_FEAT_CACHE
    if key in cache:
        return cache[key]
    f = _cand_font(path)
    v = None
    try:
        if f is not None and f.has_glyph(ord(ch)):
            v = _glyph_features(_render_glyph_cov(f, ch))
    except Exception:
        v = None
    cache[key] = v
    return v


# Per-feature spreads to z-normalize the feature distance so each axis contributes
# comparably (measured across the bank's render population); kept conservative.
_FEAT_SD = np.array([0.10, 0.10, 0.02, 0.06, 0.05, 0.12], np.float32)
_FEAT_LAMBDA = 0.02          # gentle: a TIEBREAKER, so shape still dominates (a decorative
#                              font with matching fill/weight must not beat a real shape match)


def _refine(scan_rgb: np.ndarray, cells: dict, cand_paths: list,
            max_chars: int = 16) -> list:
    """Re-rank candidate font files by how well their rendered glyphs match the
    scanned text. The score is the fine shape-descriptor cosine MINUS a small typeface-
    feature penalty (``_glyph_features``: aspect/fill/weight/contrast/roundness/structure),
    so when several fonts tie on silhouette the one whose proportions + weight + modulation
    actually match the scan wins -- a condensed/bold font can no longer beat a medium scan
    on shape alone. Returns ``[(path, score)]`` best-first."""
    import fitz
    perq: dict = {}
    featq: dict = {}
    for ch, box in cells.values():
        if ch not in _CIDX:
            continue
        x0, y0, x1, y1 = box
        cell = scan_rgb[max(0, y0):y1, max(0, x0):x1]
        if cell.size == 0 or cell.shape[0] < 4 or cell.shape[1] < 4:
            continue
        alpha = _to_alpha(cell)
        d = _fine_from_cov(alpha)
        if d is not None:
            perq.setdefault(ch, []).append(d)
        fv = _glyph_features(alpha)
        if fv is not None:
            featq.setdefault(ch, []).append(fv)
    if not perq:
        return [(p, -1.0) for p in cand_paths]
    chars = sorted(perq, key=lambda c: -len(perq[c]))[:max_chars]
    q = {c: _unit(np.mean(perq[c], 0)) for c in chars}
    q = {c: v for c, v in q.items() if v is not None}
    # The scan's typeface-feature vector per char (median over its instances).
    sf = {c: np.median(featq[c], 0) for c in featq if featq.get(c)}
    scored = []
    for p in cand_paths:
        s, n = 0.0, 0
        fpen, fn = 0.0, 0
        for c in q:
            cd = _cand_char_desc(p, c)        # cached per (path, char): no re-render
            if cd is not None:
                s += float(cd @ q[c])
                n += 1
            if c in sf and c.isalnum():       # letters/digits only -- punctuation like '/'
                cf = _cand_char_feat(p, c)    # has no typeface-discriminating proportions
                if cf is not None:
                    fpen += float(np.sqrt(np.sum(((sf[c] - cf) / _FEAT_SD) ** 2)))
                    fn += 1
        shape = s / n if n else -1.0
        penalty = _FEAT_LAMBDA * (fpen / fn) if fn else 0.0
        scored.append((p, shape - penalty))
    scored.sort(key=lambda x: -x[1])
    return scored


def _refine_bank(scan_rgb: np.ndarray, cells: dict, cand_idx, max_chars: int = 16):
    """Same re-rank as ``_refine``, but the candidate fonts' fine descriptors +
    typeface features come from the PREBUILT bank (``tools/build_fine_bank.py``)
    instead of being rendered live. Only the SCAN's glyphs are measured here; the
    library values are looked up by font index. ``cand_idx`` indexes the coarse
    fingerprint bank (``_load_fingerprints()['paths']``). Returns ``[(idx, score)]``."""
    fbk = _load_fine_bank()
    fine, feat, pres = fbk["fine"], fbk["feat"], fbk["present"]
    perq: dict = {}
    featq: dict = {}
    for ch, box in cells.values():
        if ch not in _CIDX:
            continue
        x0, y0, x1, y1 = box
        cell = scan_rgb[max(0, y0):y1, max(0, x0):x1]
        if cell.size == 0 or cell.shape[0] < 4 or cell.shape[1] < 4:
            continue
        alpha = _to_alpha(cell)
        d = _fine_from_cov(alpha)
        if d is not None:
            perq.setdefault(ch, []).append(d)
        fv = _glyph_features(alpha)
        if fv is not None:
            featq.setdefault(ch, []).append(fv)
    if not perq:
        return [(int(i), -1.0) for i in cand_idx]
    chars = sorted(perq, key=lambda c: -len(perq[c]))[:max_chars]
    q = {c: _unit(np.mean(perq[c], 0)) for c in chars}
    q = {c: v for c, v in q.items() if v is not None}
    sf = {c: np.median(featq[c], 0) for c in featq if featq.get(c)}
    scored = []
    for idx in cand_idx:
        idx = int(idx)
        s, n = 0.0, 0
        fpen, fn = 0.0, 0
        for c in q:
            cidx = _CIDX[c]
            if not pres[idx, cidx]:
                continue
            cd = fine[idx, cidx].astype(np.float32) / _FINE_QSCALE
            s += float(cd @ q[c])
            n += 1
            if c in sf and c.isalnum():
                cf = feat[idx, cidx]
                if cf.any():
                    fpen += float(np.sqrt(np.sum(((sf[c] - cf) / _FEAT_SD) ** 2)))
                    fn += 1
        shape = s / n if n else -1.0
        penalty = _FEAT_LAMBDA * (fpen / fn) if fn else 0.0
        scored.append((idx, shape - penalty))
    scored.sort(key=lambda x: -x[1])
    return scored


# --------------------------------------------------------------------------- #
#  bank: int8 fingerprints + lazily-decompressed TTF cache
# --------------------------------------------------------------------------- #
_LOADED: dict | None = None
_FINE_BANK: "dict | None" = None
_FINE_BANK_TRIED = False


def _load_fine_bank() -> "dict | None":
    """Memory-map the PREBUILT fine bank (font_fine_int8/feat/present.npy). None
    when it has not been built yet -- callers then fall back to live rendering."""
    global _FINE_BANK, _FINE_BANK_TRIED
    if _FINE_BANK_TRIED:
        return _FINE_BANK
    _FINE_BANK_TRIED = True
    fi = _find("font_fine_int8.npy")
    ff = _find("font_fine_feat.npy")
    fp = _find("font_fine_present.npy")
    if not (fi and ff and fp):
        return None
    try:
        _FINE_BANK = {
            "fine": np.load(fi, mmap_mode="r"),
            "feat": np.load(ff, mmap_mode="r"),
            "present": np.load(fp, mmap_mode="r"),
        }
    except Exception:
        _FINE_BANK = None
    return _FINE_BANK


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


_TTF_DIR_CACHE: "str | None" = None


def _ensure_ttf_cache() -> "str | None":
    """Decompress ``fontbank.tar.xz`` to ``<bank>/ttf`` once (stdlib lzma -> TTF,
    the format PyMuPDF embeds). Returns the ttf dir, or None if the archive is
    absent. Idempotent: a populated ttf dir is reused.

    Memoized in a module global: this is called once PER candidate-glyph render
    (~1400 times per OCR page) and the old early-return ran os.listdir() over the
    ~3948-file ttf dir EVERY call -- ~1.8s/page of pure stat churn. Resolve the
    path once, then it's a dict-free constant."""
    global _TTF_DIR_CACHE
    if _TTF_DIR_CACHE is not None:
        return _TTF_DIR_CACHE
    bdir = bank_dir()
    ttf_dir = os.path.join(bdir, "ttf")
    if os.path.isdir(ttf_dir) and os.listdir(ttf_dir):
        _TTF_DIR_CACHE = ttf_dir
        return ttf_dir
    archive = _find("fontbank.tar.xz")
    if archive is None:
        return None
    os.makedirs(bdir, exist_ok=True)
    with lzma.open(archive, "rb") as xz:
        with tarfile.open(fileobj=xz, mode="r") as tf:
            tf.extractall(bdir)                          # writes <bank>/ttf/*.ttf
    if os.path.isdir(ttf_dir):
        _TTF_DIR_CACHE = ttf_dir
        return ttf_dir
    return None


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
#  variant siblings -- the REAL bold/italic FILE of a matched bank face
# --------------------------------------------------------------------------- #
# {normalized_family: {(bold, italic): "NNNNN.ttf"}}. The bank's TTFs keep their
# OpenType name tables even after subsetting, so a matched face's family + its
# sibling variants are recoverable per-file. Built once, cached to a pickle.
_VARIANT_INDEX: "dict | None" = None


def _build_variant_index() -> dict:
    """Group every bank TTF by family -> {(bold,italic): filename}. Reads each
    file's name table via ``font_engine.detect_ttf_style`` (the one shared
    detector). One-time ~3948-file scan, persisted to ``<bank>/variant_index.pkl``
    keyed by the file COUNT so a re-extracted bank rebuilds; lazy on first
    request. Returns {} when the bank is absent."""
    global _VARIANT_INDEX
    if _VARIANT_INDEX is not None:
        return _VARIANT_INDEX
    ttf_dir = _ensure_ttf_cache()
    if ttf_dir is None:
        _VARIANT_INDEX = {}
        return _VARIANT_INDEX
    files = sorted(f for f in os.listdir(ttf_dir) if f.lower().endswith(".ttf"))
    sig = len(files)
    pkl = os.path.join(bank_dir(), "variant_index.pkl")
    if os.path.exists(pkl):
        try:
            import pickle
            with open(pkl, "rb") as fh:
                blob = pickle.load(fh)
            if isinstance(blob, dict) and blob.get("sig") == sig:
                _VARIANT_INDEX = blob.get("index") or {}
                return _VARIANT_INDEX
        except Exception:
            pass
    from ..font_engine import detect_ttf_style, FontEngine
    index: dict = {}
    for fn in files:
        fam, bold, ital = detect_ttf_style(os.path.join(ttf_dir, fn))
        if not fam:
            continue
        key = FontEngine.normalize(fam)
        if not key:
            continue
        # First file wins a cell (stable across runs given sorted order).
        index.setdefault(key, {}).setdefault((bool(bold), bool(ital)), fn)
    _VARIANT_INDEX = index
    try:
        import pickle
        with open(pkl, "wb") as fh:
            pickle.dump({"sig": sig, "index": index}, fh)
    except Exception:
        pass
    return _VARIANT_INDEX


def _nearest_cell(cells: dict, bold: bool, italic: bool) -> "str | None":
    """The (bold,italic) cell, else the best partial match that actually supplies
    some of the REQUESTED emphasis. Returns None when the only thing available is
    plain regular but emphasis was asked for -- so the caller can fall back to a
    family that has a real variant instead of an un-emphasized face."""
    want = (bool(bold), bool(italic))
    if want in cells:
        return cells[want]
    best, best_score = None, 0
    for (b, i), fn in cells.items():
        # Credit emphasis the user WANTED, penalize emphasis they did NOT, so a
        # bold-only request never picks a bold-italic file, and a family with only
        # the regular cell scores 0 (-> None) when emphasis is wanted so the caller
        # falls back rather than rendering an un-emphasized face as if styled.
        match = (1 if (b and want[0]) else 0) + (1 if (i and want[1]) else 0)
        extra = (1 if (b and not want[0]) else 0) \
            + (1 if (i and not want[1]) else 0)
        score = match - extra
        if score > best_score:
            best, best_score = fn, score
    return best


def variant_file_for(matched, bold: bool, italic: bool) -> "str | None":
    """The sibling variant TTF (same family, desired bold/italic) of a
    scan-matched bank face. ``matched`` is a 'ScanFont-NNNNN' name OR a bank TTF
    path/filename. Returns the absolute sibling path, or None when no usable
    sibling exists (caller falls back to the bundled set or no emphasis -- this
    NEVER synth-bolds; every returned path is a real variant file)."""
    ttf_dir = _ensure_ttf_cache()
    if ttf_dir is None or not matched:
        return None
    m = str(matched)
    if m.startswith("ScanFont-"):
        path = font_file_for(m)
    elif os.path.exists(m):
        path = m
    else:
        cand = os.path.join(ttf_dir, os.path.basename(m))
        path = cand if os.path.exists(cand) else None
    if path is None:
        return None
    from ..font_engine import detect_ttf_style, FontEngine
    fam, _b, _i = detect_ttf_style(path)
    if not fam:
        return None
    cells = _build_variant_index().get(FontEngine.normalize(fam))
    if not cells:
        return None
    fn = _nearest_cell(cells, bold, italic)
    if fn is None:
        return None
    out = os.path.join(ttf_dir, fn)
    return out if os.path.exists(out) else None


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
    # FINE re-rank a wide coarse shortlist: the true font can sit deep in the coarse
    # list (a heavy mono lands ~rank 37 behind bold sans), so the window must reach
    # it before the render-and-match stage can pull it to #1.
    coarse_k = 50
    # FAST PATH: the fine re-rank inputs are PREBUILT (tools/build_fine_bank.py),
    # so we look up the shortlist's descriptors by index instead of rendering the
    # whole library every OCR (that render was ~8500 fitz pixmaps PER PAGE -- the
    # cause of OCR taking minutes). Only the SCAN's glyphs are measured here.
    fine_bank = _load_fine_bank()
    if fine_bank is not None and fine_bank["fine"].shape[0] == F and n_glyphs:
        reranked = _refine_bank(scan_rgb, cells, order[:coarse_k])
        top = [(fb["paths"][int(i)], float(s)) for i, s in reranked][:topk]
        best_s = top[0][1]
        second = top[1][1] if len(top) > 1 else 0.0
        return dict(best=top[0][0], confidence=best_s, margin=best_s - second,
                    topk=top, n_glyphs=n_glyphs, refined="fine-bank")
    coarse_keys = [fb["paths"][i] for i in order[:max(coarse_k, topk)]]
    ttf_dir = _ensure_ttf_cache()
    if ttf_dir is not None and n_glyphs:
        keys = coarse_keys[:coarse_k]
        reranked = _refine(scan_rgb, cells,
                           [os.path.join(ttf_dir, k) for k in keys])
        by_path = {os.path.join(ttf_dir, k): k for k in keys}
        top = [(by_path.get(p, os.path.basename(p)), s)
               for p, s in reranked][:topk]
        best_s = top[0][1]
        second = top[1][1] if len(top) > 1 else 0.0
        return dict(best=top[0][0], confidence=best_s, margin=best_s - second,
                    topk=top, n_glyphs=n_glyphs, refined="fine")
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


# --------------------------------------------------------------------------- #
#  RENDER-AND-COMPARE matcher (edge/contour signature)
#
#  The coverage-cosine matcher above is serif-BLIND (blur erases serifs), so a serif
#  scan only ever matches a sans font. This matcher compares each font's CONTOUR/EDGE
#  signature (where serifs live) to the scanned glyphs -- the fast form of rendering
#  every candidate and seeing which reconstructs the scan. Validated: serif scan ->
#  serif font, sans scan -> sans font. The signature is built locally from the TTFs
#  (``tools/build_edge_bank.py`` -> ``font_edge_int8.npz``); no new download.
# --------------------------------------------------------------------------- #
_EDGE_TILE = 32
_edge_bank = None


def _edge_norm_tile(cov: np.ndarray, size: int = _EDGE_TILE) -> "np.ndarray | None":
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


def _edge_unit(v: np.ndarray) -> "np.ndarray | None":
    v = v.astype(np.float32).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else None


def _edge_cov_edge(tile: np.ndarray):
    """(coverage_unit, edge_unit) for a normalized glyph tile -- the SAME descriptor the
    bank build computes, so scan and bank vectors are comparable."""
    gx = cv2.Sobel(tile, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(tile, cv2.CV_32F, 0, 1, ksize=3)
    edge = np.sqrt(gx * gx + gy * gy)
    return _edge_unit(cv2.GaussianBlur(tile, (0, 0), 0.6)), _edge_unit(edge)


# --- decorative-font toggle ---------------------------------------------------
# When EXCLUDE_DECORATIVE is on, the matchers only CONSIDER the curated text-class fonts
# (text_fonts.txt: display / script / handwriting / decorative faces removed). This is the
# toggle that stops the matcher landing on novelty faces (Redacted, Jersey, Bahianita,
# Wavefont) on a degraded scan -- it does NOT modify the bank, only the candidate set.
EXCLUDE_DECORATIVE = True
_TEXT_KEYS = None
# Fonts removed from the library ENTIRELY -- never a match candidate, regardless of the
# decorative toggle. 02771 = Workbench (a dot-matrix display face Edward flagged as junk).
BLOCKED_FONTS = {"02771.ttf"}


def _text_keys():
    """Set of bank TTF basenames that are text-class fonts (from text_fonts.txt). Cached."""
    global _TEXT_KEYS
    if _TEXT_KEYS is None:
        _TEXT_KEYS = set()
        p = _find("text_fonts.txt")
        if p:
            try:
                with open(p) as fh:
                    _TEXT_KEYS = {os.path.basename(ln.strip())
                                  for ln in fh if ln.strip()}
            except Exception:
                _TEXT_KEYS = set()
    return _TEXT_KEYS


def candidate_ok(key) -> bool:
    """Whether ``key`` (a bank TTF name/path) may be a match candidate right now. With the
    decorative toggle on, only curated text-class fonts qualify; with it off, everything."""
    base = os.path.basename(key)
    if base in BLOCKED_FONTS:
        return False
    if not EXCLUDE_DECORATIVE:
        return True
    tk = _text_keys()
    return (not tk) or (base in tk)


def _load_edge_bank():
    global _edge_bank
    if _edge_bank is None:
        # Prefer the TEXT-only bank (display/handwriting/barcode fonts removed) -- those
        # novelty faces are what the matcher kept wrongly latching onto.
        p = _find("font_edge_text_int8.npz") or _find("font_edge_int8.npz")
        if p is None:
            _edge_bank = False
            return None
        z = np.load(p, allow_pickle=False)
        sc = float(z["scale"])
        chars = str(z["chars"])
        _edge_bank = {
            "cov": z["cov"].astype(np.float32) / sc,
            "edge": z["edge"].astype(np.float32) / sc,
            "paths": z["paths"],
            "cidx": {c: i for i, c in enumerate(chars)},
        }
    return _edge_bank or None


def edge_bank_available() -> bool:
    return _load_edge_bank() is not None


def match_edge(scan_tiles: dict, topk: int = 6) -> "dict | None":
    """Match a scanned font by EDGE/contour render-and-compare. ``scan_tiles`` maps a
    character to its CLEAN coverage tile (0..1, ink>0) cut at the corrected glyph
    position. Returns ``{best, topk}`` (best is a TTF key) or None. Weight 0.6 edge +
    0.4 coverage -- edge carries the serif signal, coverage anchors weight/proportion."""
    fb = _load_edge_bank()
    if fb is None or not scan_tiles:
        return None
    cov, edge, cidx = fb["cov"], fb["edge"], fb["cidx"]
    F = cov.shape[0]
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    for ch, tile2d in scan_tiles.items():
        if ch not in cidx:
            continue
        t = _edge_norm_tile(tile2d)
        if t is None:
            continue
        cu, eu = _edge_cov_edge(t)
        if cu is None or eu is None:
            continue
        ci = cidx[ch]
        be = edge[:, ci, :]
        pres = np.any(be != 0, axis=1)
        s = 0.4 * (cov[:, ci, :] @ cu) + 0.6 * (be @ eu)
        score += np.where(pres, s, 0.0)
        wsum += pres.astype(np.float32)
    valid = wsum > 0
    if not valid.any():
        return None
    ms = np.full(F, -1.0, np.float32)
    ms[valid] = score[valid] / wsum[valid]
    paths = fb["paths"]
    # DROP explicitly-BLOCKED fonts from the RESULT, not just the coarse candidate set: the edge
    # bank still carries their fingerprints (Workbench 02771 wrongly sits in text_fonts.txt), so
    # without this the best edge score could be a font Edward pulled from the bank. Only the
    # blocklist here -- the edge bank is already the text-only bank, so re-applying the full
    # decorative/text-keys gate would wrongly drop legit faces that just are not in text_fonts.txt.
    order = [int(i) for i in np.argsort(-ms)
             if ms[i] >= 0 and os.path.basename(str(paths[i])) not in BLOCKED_FONTS][:topk]
    if not order:
        return None
    return {"best": str(paths[order[0]]),
            "topk": [(str(paths[i]), float(ms[i])) for i in order]}
