"""Word/area-grouped SUPER-RESOLUTION font matching (the new matcher).

A scanned glyph is wrecked by fax/scan damage, but a document has many instances of
each character per font, each with INDEPENDENT random damage. Group the page's text
by font (areas of the same character-independent STYLE -- weight, contrast, size --
pool together so scattered number fields join one group), then for each character in
a group SUPER-RESOLVE: sub-pixel align every instance at high resolution and average,
so the random damage cancels and the clean letterform (serifs, terminals, the
nuances) re-emerges. Match the super-glyphs against the FULL bank by hi-res
coverage+edge cosine. General (no font whitelist); recovers the true font where the
coarse/fine cosine matcher latched onto a wobbly distractor.

``match_areas`` is the pipeline entry: it returns one (otf_bytes, face_name) per
input area, ready to register + embed exactly like ``fontbank.match_font``.
"""
from __future__ import annotations

import os
import threading

import cv2
import numpy as np

from . import fontbank as FB

_H = 128
_PAD = 12
_MIN_INSTANCES = 3      # chars with >= this many instances get super-resolved
_MIN_CHARS = 3          # need >= this many super-glyphs to trust a match
_MIN_SCORE = 0.50       # below this the super-res match is not trusted (caller falls back)
_COARSE_K = 40

# Diagnostics: build_page_map stashes (gid, cells, key) per cluster here so tools can
# inspect which font each group got (e.g. the date field).
LAST_CLUSTERS: list = []


def _cov(image_rgb, box):
    """Coverage for one glyph. The OCR cell box uses the FULL line-height y-band, so
    crop to the glyph's own ink FIRST -- otherwise _to_alpha's ink/paper percentiles
    are computed over a mostly-empty tall box and the style signature comes out noisy
    (which scattered identical fields into different clusters). Tight box == what the
    validated headless run measured."""
    x0, y0, x1, y1 = box
    sub = image_rgb[max(0, y0):y1, max(0, x0):x1]
    if sub.size == 0 or sub.shape[0] < 3 or sub.shape[1] < 3:
        return None
    g = sub.mean(2)
    paper = float(np.percentile(g, 90))
    ink_rows = np.where((g < 0.7 * paper).any(axis=1))[0]
    ink_cols = np.where((g < 0.7 * paper).any(axis=0))[0]
    if len(ink_rows) >= 2 and len(ink_cols) >= 2:
        sub = sub[ink_rows.min():ink_rows.max() + 1, ink_cols.min():ink_cols.max() + 1]
    if sub.shape[0] < 3 or sub.shape[1] < 3:
        return None
    return FB._to_alpha(sub)


def _glyph_style(cov):
    bw = (cov > 0.4).astype(np.uint8)
    ys, xs = np.where(bw)
    if len(ys) < 6:
        return None
    h = ys.max() - ys.min() + 1
    dt = cv2.distanceTransform(bw, cv2.DIST_L2, 3)
    dv = dt[bw > 0]
    if dv.size < 4:
        return None
    mean = max(float(dv.mean()), 1e-3)
    return np.array([2.0 * float(np.percentile(dv, 75)) / max(h, 1),   # weight
                     float(dv.std()) / mean,                            # contrast
                     float(h)], np.float32)                            # size


def _hires_tile(cov, thr=0.2):
    ys, xs = np.where(cov > thr)
    if len(ys) < 6:
        return None
    crop = cov[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    sc = (_H - 2 * _PAD) / max(h, w)
    nh, nw = max(1, round(h * sc)), max(1, round(w * sc))
    rs = cv2.resize(crop.astype(np.float32), (nw, nh), interpolation=cv2.INTER_CUBIC)
    t = np.zeros((_H, _H), np.float32)
    oy, ox = (_H - nh) // 2, (_H - nw) // 2
    t[oy:oy + nh, ox:ox + nw] = np.clip(rs, 0, 1)
    return t


def _superres(covs):
    tiles = [t for t in (_hires_tile(c) for c in covs) if t is not None]
    if len(tiles) < _MIN_INSTANCES:
        return None
    blur = [cv2.GaussianBlur(t, (0, 0), 2.5) for t in tiles]
    ref = np.median(blur, 0).astype(np.float32)
    acc = None
    for _ in range(2):
        acc = np.zeros((_H, _H), np.float32)
        accb = np.zeros((_H, _H), np.float32)
        for t, tb in zip(tiles, blur):
            try:
                (dx, dy), _resp = cv2.phaseCorrelate(tb, ref)
            except Exception:
                dx = dy = 0.0
            M = np.float32([[1, 0, -dx], [0, 1, -dy]])
            acc += cv2.warpAffine(t, M, (_H, _H))
            accb += cv2.warpAffine(tb, M, (_H, _H))
        ref = (accb / len(tiles)).astype(np.float32)
    return acc / len(tiles)


def _hdesc(t):
    def u(v):
        v = v - v.mean()
        n = np.linalg.norm(v)
        return v / n if n > 1e-6 else None
    cb = u(cv2.GaussianBlur(t, (0, 0), 1.0).ravel())
    if cb is None:
        return None
    gx = cv2.Sobel(t, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(t, cv2.CV_32F, 0, 1, ksize=3)
    e = u(np.hypot(gx, gy).ravel())
    v = cb if e is None else np.concatenate([cb, 0.7 * e])
    return v / np.linalg.norm(v)


_DESC_CACHE: dict = {}
_DESC_LOCK = threading.Lock()        # guards _DESC_CACHE / _DESC_INFLIGHT
_DESC_INFLIGHT: dict = {}            # (key, ch) -> Event while one thread renders it


def _cand_desc(key, ch):
    # Hi-res descriptor of one candidate (font, char). Pages OCR in parallel and
    # the pages of one document share the same few fonts, so without dedup all N
    # workers render the SAME candidate glyph at the same time (N x wasted renders
    # -- the hot cost). Dedup IN FLIGHT: the first thread to want a (key, char)
    # renders it; any other thread that wants the same one waits and reuses the
    # result. Distinct (key, char) still render fully in parallel (the render is
    # outside the lock). Identical results -- this only removes redundant work.
    k = (key, ch)
    with _DESC_LOCK:
        if k in _DESC_CACHE:
            return _DESC_CACHE[k]
        ev = _DESC_INFLIGHT.get(k)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _DESC_INFLIGHT[k] = ev
    if not owner:
        ev.wait()                      # someone else is rendering this exact glyph
        return _DESC_CACHE.get(k)
    d = None
    try:
        f = FB._cand_font(os.path.join(FB._ensure_ttf_cache(), key))
        if f is not None and f.has_glyph(ord(ch)):
            t = _hires_tile(FB._render_glyph_cov(f, ch, em=180.0))
            if t is not None:
                d = _hdesc(t)
    except Exception:
        d = None
    with _DESC_LOCK:
        _DESC_CACHE[k] = d
        _DESC_INFLIGHT.pop(k, None)
    ev.set()
    return d


def _cand_desc_deg(key, ch, residual):
    """Hi-res descriptor of a candidate glyph DEGRADED with the scan cluster's residual
    profile, so candidate and scan are compared in the SAME damaged space (validated to
    beat the clean rerank, tools/calibrate_verify.py). Not cached: the residual is
    per-cluster."""
    from . import degrade as DG
    prof, ink, paper, seed = residual
    try:
        f = FB._cand_font(os.path.join(FB._ensure_ttf_cache(), key))
        if f is None or not f.has_glyph(ord(ch)):
            return None
        cov = FB._render_glyph_cov(f, ch, em=180.0)
        strip = np.repeat((255.0 * (1.0 - cov))[..., None], 3, axis=2).astype(np.uint8)
        deg = DG.apply_residual_filter(strip, ink, paper, prof, np.random.RandomState(seed))
        t = _hires_tile(DG.coverage(deg))
        return _hdesc(t) if t is not None else None
    except Exception:
        return None


def _cluster_residual(image_rgb, cells):
    """Build the cluster's residual-degradation context (prof, ink, paper, seed) from
    its scanned strokes, or None if the line is too clean to profile."""
    from . import degrade as DG
    boxes = [b for _c, b in cells]
    if not boxes:
        return None
    prof = DG.build_residual_filter(image_rgb, {"char_boxes": boxes})
    if prof is None:
        return None
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    reg = image_rgb[max(0, int(y0)):int(y1), max(0, int(x0)):int(x1)]
    ink, paper = DG.sample_ink_paper(reg if reg.size else image_rgb)
    return (prof, ink, paper, 7)


def _coarse_scores(image_rgb, cells):
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    per_char: dict = {}
    for ch, box in cells:
        if ch not in FB._CIDX:
            continue
        d = FB._glyph_descriptor(image_rgb, box)
        if d is not None:
            per_char.setdefault(FB._CIDX[ch], []).append(d)
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    for cidx, ds in per_char.items():
        q = np.mean(ds, 0)
        q -= q.mean()
        nq = np.linalg.norm(q)
        if nq < 1e-6:
            continue
        q /= nq
        col = bank[:, cidx, :]
        pres = np.any(col != 0, axis=1)
        score += np.where(pres, col @ q, 0.0)
        wsum += pres.astype(np.float32)
    ms = np.full(F, -1e9, np.float32)
    valid = wsum > 0
    ms[valid] = score[valid] / wsum[valid]
    return ms


def _match_cluster(image_rgb, cells):
    """Super-resolve a font cluster's chars and match vs the full bank. Returns
    (tar_member_key, score) or None when too sparse / weak."""
    by_char: dict = {}
    for ch, box in cells:
        c = _cov(image_rgb, box)
        if c is not None:
            by_char.setdefault(ch, []).append(c)
    sdesc = {}
    for ch, covs in by_char.items():
        sg = _superres(covs)
        if sg is not None:
            hd = _hdesc(sg)
            if hd is not None:
                sdesc[ch] = hd
    if len(sdesc) < _MIN_CHARS:
        return None
    keys = FB._load_fingerprints()["paths"]
    shape = _coarse_scores(image_rgb, cells)
    cand = [int(i) for i in np.argsort(-shape)[:_COARSE_K]]
    # DEGRADE-MATCHED rerank: render each candidate, stamp THIS cluster's measured
    # scan damage on it, then compare in the same space. Beats the clean cosine on
    # degraded scans (the wrong-font-on-edited-digits bug). Toggle off to A/B.
    residual = _cluster_residual(image_rgb, cells) \
        if os.environ.get("SUPERMATCH_VERIFY", "0") != "0" else None
    best, best_s = None, -1.0
    for ci in cand:
        s, n = 0.0, 0
        for ch, sv in sdesc.items():
            cd = _cand_desc_deg(keys[ci], ch, residual) if residual \
                else _cand_desc(keys[ci], ch)
            if cd is not None:
                s += float(cd @ sv)
                n += 1
        sc = s / n if n else -1.0
        if sc > best_s:
            best, best_s = ci, sc
    if best is None or best_s < _MIN_SCORE:
        return None
    return keys[best], best_s


def _otf_for(key):
    ttf = FB._ensure_ttf_cache()
    if ttf is None:
        return None
    p = os.path.join(ttf, key)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        data = fh.read()
    return data, "ScanFont-" + os.path.splitext(os.path.basename(key))[0]


def _word_sig(image_rgb, word):
    feats = [s for s in (_glyph_style(_cov(image_rgb, b)) for _c, b in word)
             if s is not None]
    return np.median(feats, 0) if feats else None


class PageFontMap:
    """Per-glyph font assignments for one page (page px, 300 dpi); query by caret
    region. ``group_path``: gid -> (ttf_path, face_name) | None."""

    def __init__(self, centers, groups, group_path):
        self.centers = (np.asarray(centers, np.float32) if len(centers)
                        else np.zeros((0, 2), np.float32))
        self.groups = list(groups)
        self.group_path = dict(group_path)

    def font_for_rect(self, x0, y0, x1, y1):
        if not len(self.centers):
            return None
        cx, cy = self.centers[:, 0], self.centers[:, 1]
        idx = np.where((cx >= x0) & (cx <= x1) & (cy >= y0) & (cy <= y1))[0]
        if not len(idx):
            mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            idx = [int(np.argmin((cx - mx) ** 2 + (cy - my) ** 2))]
        from collections import Counter
        votes = Counter(self.groups[i] for i in idx
                        if self.group_path.get(self.groups[i]))
        return self.group_path.get(votes.most_common(1)[0][0]) if votes else None


# Grouping by SAME-CHARACTER shape agreement (robust where hand-crafted style
# features are too noisy): two words are the same font when the characters they
# SHARE have matching glyph shapes. Comparing a '2' to a '2' is reliable; comparing
# style heuristics across different letters is not.
_GROUP_SIM = 0.80         # min mean ZNCC over shared chars to link two words
_GROUP_MIN_SHARE = 2      # min shared characters to even compare two words


def build_page_map(image_rgb, words):
    """PageFontMap from page-wide WORDS (each [(char, (x0,y0,x1,y1)) page px]).
    Words are grouped by same-character shape agreement (connected components), each
    group super-resolved + matched once (pooling every same-font glyph -> strong even
    for a lone field), and each glyph tagged with its group's font. The OCR-time map
    the editor queries per caret."""
    if not FB.available() or not words:
        return PageFontMap([], [], {})
    # per-word, per-char coarse descriptor (degradation-robust, ink-tight)
    wdesc: list = []
    for w in words:
        per: dict = {}
        for ch, box in w:
            if ch not in FB._CIDX:
                continue
            cov = _cov(image_rgb, box)
            if cov is not None:
                d = FB._descriptor(cov)
                if d is not None:
                    per.setdefault(ch, []).append(d)
        wdesc.append({c: np.mean(v, 0) for c, v in per.items()})
    n = len(words)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        if not wdesc[i]:
            continue
        for j in range(i + 1, n):
            shared = set(wdesc[i]) & set(wdesc[j])
            if len(shared) < _GROUP_MIN_SHARE:
                continue
            sim = float(np.mean([wdesc[i][c] @ wdesc[j][c] for c in shared]))
            if sim >= _GROUP_SIM:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    label: dict = {i: find(i) for i in range(n) if wdesc[i]}
    if not label:
        return PageFontMap([], [], {})
    cluster_cells: dict = {}
    for i, c in label.items():
        cluster_cells.setdefault(c, []).extend(words[i])
    ttf_dir = FB._ensure_ttf_cache()
    group_path: dict = {}
    dbg = []
    LAST_CLUSTERS.clear()
    for c, cells in cluster_cells.items():
        key = None
        try:
            res = _match_cluster(image_rgb, cells)
            key = res[0] if res else None
        except Exception:
            key = None
        LAST_CLUSTERS.append((c, cells, key))
        if key and ttf_dir and os.path.exists(os.path.join(ttf_dir, key)):
            group_path[c] = (os.path.join(ttf_dir, key),
                             "ScanFont-" + os.path.splitext(os.path.basename(key))[0])
        else:
            group_path[c] = None
        dbg.append((c, len(cells), key))
    centers, groups = [], []
    for i, w in enumerate(words):
        gid = label.get(i)
        for _ch, (bx0, by0, bx1, by1) in w:
            centers.append(((bx0 + bx1) / 2.0, (by0 + by1) / 2.0))
            groups.append(gid)
    if os.environ.get("SUPERMATCH_DEBUG"):
        print(f"[build_page_map] {len(words)} words -> {len(cluster_cells)} clusters",
              flush=True)
        for c, ng, key in sorted(dbg):
            print(f"  cluster {c}: {ng} glyphs -> {key}", flush=True)
    return PageFontMap(centers, groups, group_path)


def match_areas(image_rgb, areas):
    """One (otf_bytes, face_name) per area (None where unmatched).

    ``areas``: list of areas; each area is a list of WORDS; each word is a list of
    (char, (x0,y0,x1,y1)) scanned glyph cells. Words are clustered page-wide by a
    character-independent STYLE signature (weight, contrast, size); each cluster is
    super-resolved and matched ONCE against the full bank (pooling many same-font
    instances = a strong, stable match -- the headless result); each area then takes
    the match of the cluster ITS OWN glyphs mostly belong to. No content assumptions,
    no font whitelist -- fully general."""
    if not FB.available():
        return [None] * len(areas)
    words: list = []
    word_area: list = []
    for ai, area in enumerate(areas):
        for w in area:
            if w:
                words.append(w)
                word_area.append(ai)
    if not words:
        return [None] * len(areas)
    sigs = [_word_sig(image_rgb, w) for w in words]
    valid = [i for i, s in enumerate(sigs) if s is not None]
    if not valid:
        return [None] * len(areas)

    label: dict = {}
    if len(valid) >= 2:
        S = np.array([sigs[i] for i in valid], np.float32)
        Z = (S - S.mean(0)) / (S.std(0) + 1e-6)
        try:
            from scipy.cluster.hierarchy import linkage, fcluster
            cl = fcluster(linkage(Z, method="ward"), t=1.4, criterion="distance")
        except Exception:
            cl = np.ones(len(valid), int)
        for i, c in zip(valid, cl):
            label[i] = int(c)
    else:
        label[valid[0]] = 1

    # one super-res match per cluster (pool every same-style word -> many instances)
    cluster_cells: dict = {}
    for i, c in label.items():
        cluster_cells.setdefault(c, []).extend(words[i])
    cluster_match: dict = {}
    dbg = []
    for c, cells in cluster_cells.items():
        try:
            res = _match_cluster(image_rgb, cells)
        except Exception:
            res = None
        cluster_match[c] = _otf_for(res[0]) if res else None
        dbg.append((c, sum(1 for i in label if label[i] == c), len(cells),
                    (res[0] + f" {res[1]:.2f}") if res else "None"))

    # each area inherits the cluster its OWN glyphs predominantly belong to
    out: list = [None] * len(areas)
    for ai in range(len(areas)):
        weight: dict = {}
        for wi, wa in enumerate(word_area):
            if wa == ai and wi in label:
                weight[label[wi]] = weight.get(label[wi], 0) + len(words[wi])
        if weight:
            out[ai] = cluster_match.get(max(weight, key=weight.get))

    if os.environ.get("SUPERMATCH_DEBUG"):
        print(f"[supermatch] {len(words)} words -> {len(cluster_cells)} clusters", flush=True)
        for c, nw, ng, m in sorted(dbg):
            print(f"  cluster {c}: {nw} words, {ng} glyphs -> {m}", flush=True)
    return out
