"""On-the-fly font verifier: rebuild a scanned word in a candidate font and compare
it, glyph by glyph, against the actual scanned glyphs across the matcher's identity
metrics, then gate on an exact match.

This module is the pure metric CORE (no Qt / document deps), so the offline
calibration harness (tools/calibrate_verify.py) and the live editor path share one
implementation. The nine identity metrics are exactly the ones the live matcher
computes (supermatch / fontbank):

  cov_cos   coverage-channel cosine     (blurred silhouette)          fontbank._fine_from_cov
  edge_cos  Sobel-edge-channel cosine   (serif / stroke detail)       fontbank._fine_from_cov
  aspect    ink width / height          \
  fill      ink area / bbox area         \
  stroke    stroke width / height         >  fontbank._glyph_features (ratio agreements)
  contrast  stroke-width spread / mean   /
  round     4*pi*area / perimeter^2     /
  vh        |dy| edge energy / |dx|    /
  advance   advance / ink-height        word-level spacing agreement

The exact-match gate is the AND of every metric clearing its per-metric bar. The
bars are calibrated from labeled data (calibrate_verify.py); EXACT_BARS holds the
pre-calibration starting values.
"""
from __future__ import annotations

import cv2
import numpy as np

from . import fontbank as FB

_FEAT_NAMES = ("aspect", "fill", "stroke", "contrast", "round", "vh")
METRICS = ("cov_cos", "edge_cos", "advance") + _FEAT_NAMES

# Per-metric exact-match bars. Cosines are compared directly; the feature/advance
# ratios are compared as min/max agreement (0.98 == within ~2%). Calibration
# (tools/calibrate_verify.py) overwrites these with max(distractor_p95, true_p10).
EXACT_BARS = {
    "cov_cos": 0.97, "edge_cos": 0.92, "advance": 0.97,
    "aspect": 0.96, "fill": 0.95, "stroke": 0.95,
    "contrast": 0.90, "round": 0.88, "vh": 0.90,
}


def _cov_edge_units(cov: np.ndarray):
    """Coverage and Sobel-edge unit vectors, split out of fontbank._fine_from_cov so
    coverage cosine and edge cosine are SEPARATE metrics. Returns (cov_u, edge_u);
    either may be None when the tile is too thin."""
    t = FB._normtile(cov, FB._FINE_S)
    if t is None:
        return None, None
    cb = FB._unit(cv2.GaussianBlur(t, (0, 0), 0.5).reshape(-1).astype(np.float32))
    gx = cv2.Sobel(t, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(t, cv2.CV_32F, 0, 1, ksize=3)
    e = FB._unit(cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), 0.5)
                 .reshape(-1).astype(np.float32))
    return cb, e


def _agree(a: float, b: float) -> float:
    """Scale-free agreement of two non-negative ratios in 0..1 (1 == identical,
    0.98 == within ~2%). Both ~0 counts as agreement."""
    a, b = abs(float(a)), abs(float(b))
    hi = max(a, b)
    if hi < 1e-6:
        return 1.0
    return min(a, b) / hi


def _ink_h(cov: np.ndarray) -> float:
    ys = np.where((cov > 0.4).any(axis=1))[0]
    return float(ys.max() - ys.min() + 1) if len(ys) >= 2 else 0.0


# The clean candidate render is identical every time (the hot fitz cost), so memoize
# the coverage per (font path, char, em) and the clean units per (path, char). The
# DEGRADE-matched units depend on the scan's per-pixel residual profile, so they vary
# per scan and are computed fresh.
_CAND_COV_CACHE: dict = {}
_CAND_CLEAN_UNITS: dict = {}


def _clean_cov(path: str, ch: str, em: float):
    k = (path, ch, round(em, 1))
    if k in _CAND_COV_CACHE:
        return _CAND_COV_CACHE[k]
    cov = None
    try:
        f = FB._cand_font(path)
        if f is not None and f.has_glyph(ord(ch)):
            cov = FB._render_glyph_cov(f, ch, em)
    except Exception:
        cov = None
    _CAND_COV_CACHE[k] = cov
    return cov


def _units_from_cov(cov: np.ndarray):
    cu, eu = _cov_edge_units(cov)
    if cu is None:
        return None
    return cu, eu, FB._glyph_features(cov)


def _degrade_cov(cov: np.ndarray, residual) -> np.ndarray:
    """Stamp the scan's measured residual profile onto a clean candidate coverage, so
    the candidate is compared in the SAME degraded space as the scan. ``residual`` is
    (prof, ink, paper, seed)."""
    from . import degrade as DG
    prof, ink, paper, seed = residual
    strip = np.repeat((255.0 * (1.0 - cov))[..., None], 3, axis=2).astype(np.uint8)
    deg = DG.apply_residual_filter(strip, ink, paper, prof, np.random.RandomState(seed))
    return DG.coverage(deg)


def _cand_units(path: str, ch: str, em: float, residual=None):
    """Split units + features + advance ratio for one candidate glyph. With
    ``residual`` set, the candidate is degraded with the scan's profile first
    (degrade-matched); otherwise it is the clean render (cached)."""
    cov = _clean_cov(path, ch, em)
    if cov is None:
        return None
    ih = _ink_h(cov)
    try:
        advr = float(FB._cand_font(path).text_length(ch, em)) / ih if ih else None
    except Exception:
        advr = None
    if residual is None:
        u = _CAND_CLEAN_UNITS.get((path, ch), "_")
        if u == "_":
            u = _units_from_cov(cov)
            _CAND_CLEAN_UNITS[(path, ch)] = u
    else:
        try:
            u = _units_from_cov(_degrade_cov(cov, residual))
        except Exception:
            u = None
    if u is None:
        return None
    return (u[0], u[1], u[2], advr)


def glyph_metrics(scan_cov: np.ndarray, cand_cov: np.ndarray):
    """Shape metrics for ONE glyph pair (scan coverage vs candidate coverage).
    Returns a dict of {cov_cos, edge_cos, <6 feature agreements>} or None."""
    cbs, es = _cov_edge_units(scan_cov)
    cbc, ec = _cov_edge_units(cand_cov)
    if cbs is None or cbc is None:
        return None
    m = {"cov_cos": float(cbs @ cbc),
         "edge_cos": float(es @ ec) if (es is not None and ec is not None) else 0.0}
    sf = FB._glyph_features(scan_cov)
    cf = FB._glyph_features(cand_cov)
    if sf is not None and cf is not None:
        for i, name in enumerate(_FEAT_NAMES):
            m[name] = _agree(sf[i], cf[i])
    return m


def scan_word_repr(scan_rgb: np.ndarray, cells):
    """Pool the scanned word ONCE into a per-character representation, reused across
    every candidate. Instances of the same character are pooled (mean of unit vectors
    / features), because a single scanned glyph is too damaged to trust -- the
    matcher's super-resolution insight, applied per word. Returns {char: (cov_u,
    edge_u, feat, advance_ratio)}."""
    import collections

    from . import supermatch as SM

    by_char: dict = collections.defaultdict(list)
    for ch, box in cells:
        if ch not in FB._CIDX or not ch.strip():
            continue
        cov = SM._cov(scan_rgb, box)        # tight ink-crop before _to_alpha
        if cov is None:
            continue
        cu, eu = _cov_edge_units(cov)
        if cu is None:
            continue
        x0, y0, x1, y1 = box
        ih = _ink_h(cov)
        advr = (x1 - x0) / ih if ih else None
        by_char[ch].append((cu, eu, FB._glyph_features(cov), advr))

    rep: dict = {}
    for ch, insts in by_char.items():
        pcu = np.mean([i[0] for i in insts], 0)
        npc = np.linalg.norm(pcu)
        if npc < 1e-6:
            continue
        peu = None
        eus = [i[1] for i in insts if i[1] is not None]
        if eus:
            e = np.mean(eus, 0)
            ne = np.linalg.norm(e)
            peu = e / ne if ne > 1e-6 else None
        fts = [i[2] for i in insts if i[2] is not None]
        advrs = [i[3] for i in insts if i[3] is not None]
        rep[ch] = (pcu / npc, peu,
                   np.mean(fts, 0) if fts else None,
                   float(np.median(advrs)) if advrs else None)
    return rep


def score_against(rep: dict, cand_path: str, em: float = 64.0, residual=None):
    """Score one candidate font against a pooled scan representation. Returns the nine
    word-level identity metrics or None. Every metric is scale-free, so the candidate
    renders at a fixed em with no condense warp, keeping advance an honest signal."""
    rows: dict = {k: [] for k in ("cov_cos", "edge_cos") + _FEAT_NAMES}
    adv_agree = []
    for ch, (pcu, peu, mf, madv) in rep.items():
        cand = _cand_units(cand_path, ch, em, residual)
        if cand is None:
            continue
        ccu, ceu, cft, cadvr = cand
        rows["cov_cos"].append(float(pcu @ ccu))
        if peu is not None and ceu is not None:
            rows["edge_cos"].append(float(peu @ ceu))
        if mf is not None and cft is not None:
            for j, name in enumerate(_FEAT_NAMES):
                rows[name].append(_agree(mf[j], cft[j]))
        if madv is not None and cadvr is not None:
            adv_agree.append(_agree(madv, cadvr))
    if not rows["cov_cos"]:
        return None
    out = {k: float(np.median(v)) for k, v in rows.items() if v}
    out["advance"] = float(np.median(adv_agree)) if adv_agree else 0.0
    return out


def word_metrics(scan_rgb: np.ndarray, cells, cand_path: str, em: float = 64.0,
                 residual=None):
    """Convenience: pool the scan and score one candidate. Returns (metrics, n_chars).
    Hot paths should call scan_word_repr once and score_against per candidate."""
    rep = scan_word_repr(scan_rgb, cells)
    if not rep:
        return None, 0
    m = score_against(rep, cand_path, em, residual)
    return (m, len(rep)) if m is not None else (None, 0)


def combined(metrics: dict) -> float:
    """Single ranking score: the shape-cosine signal (coverage + edge), the matcher's
    own discriminator. Used to pick the best candidate when no one clears the gate."""
    return 0.5 * metrics.get("cov_cos", 0.0) + 0.5 * metrics.get("edge_cos", 0.0)


def is_exact(metrics: dict, bars: "dict | None" = None) -> bool:
    """True when EVERY metric clears its bar (the exact-match short-circuit)."""
    bars = bars or EXACT_BARS
    return all(metrics.get(k, 0.0) >= b for k, b in bars.items())


# --------------------------------------------------------------------------- #
#  SHORT-FIELD matcher: render the whole word in each candidate, degrade it with the
#  field's OWN measured damage, compare per glyph. Used for fields too short to
#  super-resolve (a date), where the descriptor cosine picks a too-heavy look-alike.
#  Lands on Arial Bold or a font that renders identically to it. cv2-only (no skimage).
# --------------------------------------------------------------------------- #
def _win_ssim(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a.astype(np.float32), b.astype(np.float32)
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    mua = cv2.GaussianBlur(a, (7, 7), 1.5)
    mub = cv2.GaussianBlur(b, (7, 7), 1.5)
    mua2, mub2, muab = mua * mua, mub * mub, mua * mub
    va = cv2.GaussianBlur(a * a, (7, 7), 1.5) - mua2
    vb = cv2.GaussianBlur(b * b, (7, 7), 1.5) - mub2
    vab = cv2.GaussianBlur(a * b, (7, 7), 1.5) - muab
    s = ((2 * muab + c1) * (2 * vab + c2)) / ((mua2 + mub2 + c1) * (va + vb + c2) + 1e-12)
    return float(s.mean())


def _ink_box(c: np.ndarray):
    rows = np.where((c > 0.3).any(1))[0]
    cols = np.where((c > 0.3).any(0))[0]
    if not len(rows) or not len(cols):
        return None
    return c[rows.min():rows.max() + 1, cols.min():cols.max() + 1]


def _split_word(cov: np.ndarray, n: int):
    """Cut a word coverage into n glyph columns at the n-1 lowest-ink valleys."""
    col = cov.sum(0)
    if col.max() <= 1e-6:
        return None
    xs = np.where(col > 0.10 * col.max())[0]
    if len(xs) < n:
        return None
    a0, a1 = int(xs.min()), int(xs.max()) + 1
    sm = np.convolve(col, np.ones(3) / 3, mode="same")
    minsep = max(2, (a1 - a0) // (n * 2))
    chosen = []
    for idx in np.argsort(sm):
        x = int(idx)
        if a0 + 2 < x < a1 - 2 and all(abs(x - c) >= minsep for c in chosen):
            chosen.append(x)
        if len(chosen) >= n - 1:
            break
    chosen.sort()
    bnd = [a0] + chosen + [a1]
    return [(bnd[i], bnd[i + 1]) for i in range(len(bnd) - 1)]


def _glyph_ssim(scan_glyphs, synth_cov, n):
    sp = _split_word(synth_cov, n)
    if not sp or len(sp) != n:
        return -1.0
    sims = []
    for sg, (a, b) in zip(scan_glyphs, sp):
        cg = _ink_box(synth_cov[:, a:b])
        if sg is None or cg is None or sg.size < 9 or cg.size < 9:
            continue
        cr = cv2.resize(cg.astype(np.float32), (max(sg.shape[1], 2), max(sg.shape[0], 2)))
        sims.append(_win_ssim(sg.astype(np.float32), cr))
    return float(np.mean(sims)) if sims else -1.0


def _fit_em(font_bytes, text, h_target, ppi):
    import fitz
    f = fitz.Font(fontbuffer=font_bytes)
    em0 = 100.0
    W = int(f.text_length(text, em0) + 2 * em0)
    d = fitz.open()
    pg = d.new_page(width=W, height=em0 * 3)
    tw = fitz.TextWriter(pg.rect)
    tw.append((em0, em0 * 2), text, font=f, fontsize=em0)
    tw.write_text(pg)
    pm = pg.get_pixmap(alpha=False)
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
    rows = np.where(((255.0 - a.mean(2)) / 255.0 > 0.3).any(1))[0]
    h = (rows.max() - rows.min() + 1) if len(rows) else 1
    return em0 * (h_target / max(h, 1)) / ppi


def match_by_synth(doc, region, text, ttf_dir, top_k: int = 18, seeds: int = 4):
    """Best font for a SHORT scanned field (a date) that super-resolution can't pool.
    Renders ``text`` in each of the coarse top-K candidates via the real synth engine,
    degrades each to the field's OWN measured damage, compares per glyph (SSIM),
    seed-averaged to cancel the degradation randomness. Returns the tar-member key of the
    best font (Arial Bold or a font that renders identically to it), or None. ``region``
    is the field's scan crop; segmentation is measured from it, not from the OCR boxes."""
    import os

    from . import degrade as DG
    from . import supermatch as SM

    ppi = 300.0 / 72.0
    Hr = region.shape[0]
    scan_cov = DG.coverage(region)
    chars = [c for c in text if c.strip()]
    spans = _split_word(scan_cov, len(chars))
    if not spans or len(spans) != len(chars):
        return None
    scan_glyphs = [_ink_box(scan_cov[:, a:b]) for (a, b) in spans]
    char_boxes = [(a, 0, b, Hr) for (a, b) in spans]
    inkrows = np.where((scan_cov > 0.3).any(1))[0]
    if not len(inkrows):
        return None
    h_ink = inkrows.max() - inkrows.min() + 1
    base_y = float(inkrows.max() + 1)
    ink, paper = DG.sample_ink_paper(region)
    n = len(chars)

    keys = FB._load_fingerprints()["paths"]
    cells = [(chars[i], char_boxes[i]) for i in range(n)]
    ms = SM._coarse_scores(region, cells)
    # Restrict to text-class candidates when the decorative toggle is on (filter BEFORE the
    # top-K cut, so a dropped novelty face is replaced by the next real font, not lost).
    cand = [keys[int(i)] for i in np.argsort(-ms)
            if FB.candidate_ok(keys[int(i)])][:top_k]

    # DETERMINISM: the seed loop averages out the synth's DEGRADATION randomness, but
    # _synth_strip degrades off the GLOBAL numpy RNG -- unseeded, so every call drew fresh
    # noise and the averaged score (hence the winning font) drifted run-to-run: the same
    # date matched Cossette one render and Gothic the next, so an edit "changed the font".
    # Seed each pass to a FIXED value so all candidates are compared under the IDENTICAL
    # degradation and the result is reproducible. The global RNG state is saved + restored
    # so seeding here never makes any other randomness in the app deterministic.
    best, bestv = None, -1.0
    _rng = np.random.get_state()
    try:
        for key in cand:
            try:
                fb = open(os.path.join(ttf_dir, key), "rb").read()
                em = _fit_em(fb, text, h_ink, ppi)
                vals = []
                for s in range(seeds):
                    np.random.seed(0x5CA1AB1E + s)
                    sctx = {"ppi": ppi, "Hr": Hr, "base_y": base_y,
                            "paper": paper.astype(np.float32),
                            "ink": ink.astype(np.float32),
                            "region": region, "geom": {"char_boxes": char_boxes},
                            "rect": (0, 0, Hr, s)}
                    strip, _ = doc._synth_strip(sctx, text, em=em, font_bytes=fb,
                                                base_y=base_y)
                    vals.append(_glyph_ssim(scan_glyphs, DG.coverage(strip), n))
                if vals:
                    v = float(np.mean(vals))
                    if v > bestv:
                        bestv, best = v, key
            except Exception:
                continue
    finally:
        np.random.set_state(_rng)
    return best
