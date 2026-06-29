"""Page-level super-resolution font map (per-glyph font, queried at the caret).

Font is a per-GLYPH property, not a box attribute: one box can hold "Phone:" (bold)
and a number (regular) -- different fonts. So we build, once per page, a map of
every scanned glyph's font:

  1. Collect every glyph on the page from the REAL char map (``_scan_geometry``
     char_boxes) of each OCR box -- the same geometry the editor/caret uses.
  2. Group the WORDS page-wide by a character-independent style signature (so a
     scattered number field's words pool with each other, and a heading stays
     separate). A word is the atomic same-font unit.
  3. Super-resolve each group's characters (align every instance sub-pixel and
     average -> the random fax damage cancels, the true letterform emerges) and
     match the FULL bank. Pooling a whole group gives a strong, stable match where
     a lone sparse field could not.
  4. Assign each glyph its group's font.

``font_for_rect`` then answers "which font at this caret region" by majority over
the glyphs there -- feeding both the edit synth and (later) the formatting menu.
"""
from __future__ import annotations

import os
from collections import Counter

import numpy as np

from . import fontbank as FB
from . import supermatch as SM


def _coarse_from_tiles(tiles_by_char):
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    for ch, tiles in tiles_by_char.items():
        if ch not in FB._CIDX:
            continue
        ds = [d for d in (FB._descriptor(t) for t in tiles) if d is not None]
        if not ds:
            continue
        q = np.mean(ds, 0)
        q -= q.mean()
        nq = np.linalg.norm(q)
        if nq < 1e-6:
            continue
        q /= nq
        col = bank[:, FB._CIDX[ch], :]
        pres = np.any(col != 0, axis=1)
        score += np.where(pres, col @ q, 0.0)
        wsum += pres.astype(np.float32)
    ms = np.full(F, -1e9, np.float32)
    valid = wsum > 0
    ms[valid] = score[valid] / wsum[valid]
    return ms


def _match_cluster_tiles(tiles_by_char, residual=None, deg_cache=None):
    """Super-resolve a group's chars, then match vs the full bank (coarse shortlist
    + hi-res super-glyph rerank). Returns a tar-member key or None.

    When ``residual`` (the page's measured scan damage) is given, the rerank is
    DEGRADE-MATCHED: each candidate is rendered, stamped with that damage, and compared in
    the same degraded space. The plain clean-descriptor rerank matched a degraded scan to
    decorative look-alikes (it picked 'Redacted'/'Wavefont'); degrade-matching lands on the
    real font. ``deg_cache`` memoizes the degraded descriptor per (key, char) -- the
    residual is page-wide and fixed, so every cluster reuses the same renders (fast)."""
    sdesc = {}
    for ch, tiles in tiles_by_char.items():
        sg = SM._superres(tiles)
        if sg is None:
            # Even the same-font group has too few instances of this char to super-resolve
            # (a date plus a couple of same-font numbers). Use the instances we DO have
            # (averaged) instead of dropping the char, so the word still gets its font.
            hires = [h for h in (SM._hires_tile(t) for t in tiles) if h is not None]
            if hires:
                sg = np.mean(hires, 0).astype(np.float32) if len(hires) > 1 else hires[0]
        if sg is not None:
            hd = SM._hdesc(sg)
            if hd is not None:
                sdesc[ch] = hd
    if len(sdesc) < SM._MIN_CHARS:
        return None
    keys = FB._load_fingerprints()["paths"]
    cand = [int(i) for i in np.argsort(-_coarse_from_tiles(tiles_by_char))
            if FB.candidate_ok(keys[int(i)])][:SM._COARSE_K]

    def _cd(ci, ch):
        if residual is None:
            return SM._cand_desc(keys[ci], ch)
        ck = (keys[ci], ch)
        if deg_cache is not None and ck in deg_cache:
            return deg_cache[ck]
        d = SM._cand_desc_deg(keys[ci], ch, residual)
        if deg_cache is not None:
            deg_cache[ck] = d
        return d

    best, best_s = None, -1.0
    for ci in cand:
        s, n = 0.0, 0
        for ch, hd in sdesc.items():
            cd = _cd(ci, ch)
            if cd is not None:
                s += float(cd @ hd)
                n += 1
        sc = s / n if n else -1.0
        if sc > best_s:
            best, best_s = ci, sc
    if best is None or best_s < SM._MIN_SCORE:
        return None
    return keys[best]


class PageFontMap:
    """Per-glyph font assignments for one page; query by caret region."""

    def __init__(self, centers, groups, group_path):
        self.centers = np.asarray(centers, np.float32) if len(centers) else \
            np.zeros((0, 2), np.float32)
        self.groups = list(groups)
        self.group_path = group_path        # gid -> (ttf_path, face_name) | None

    def font_for_rect(self, x0, y0, x1, y1):
        """(ttf_path, face_name) for the glyphs inside the caret region (majority),
        else the nearest glyph's group, else None."""
        if not len(self.centers):
            return None
        cx, cy = self.centers[:, 0], self.centers[:, 1]
        inside = np.where((cx >= x0) & (cx <= x1) & (cy >= y0) & (cy <= y1))[0]
        if not len(inside):
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            inside = [int(np.argmin((cx - mx) ** 2 + (cy - my) ** 2))]
        votes = Counter(self.groups[i] for i in inside
                        if self.group_path.get(self.groups[i]))
        if not votes:
            return None
        return self.group_path.get(votes.most_common(1)[0][0])


def build(doc, page_index: int) -> "PageFontMap":
    # Extract each box's UPRIGHT region + char_boxes through the editor's OWN measure path
    # (measure_box_glyphs), so this works on /Rotate pages too. The old code cropped the
    # display render with text-space covers, which only coincide at /Rotate 0 -- on a rotated
    # page it cropped sideways and recovered nothing (empty map). Word centers are stored in
    # DISPLAY coords, the same frame _edit_synth_font queries per-WORD.
    ppi = 300.0 / 72.0
    units = []          # each: [(char, tile), ...]   one word
    unit_centers = []   # each: [(cx, cy), ...] DISPLAY pts, parallel to units
    _res = None         # (region_rgb, char_boxes) of a rich line -> page damage residual
    for box in doc.new_boxes(page_index):
        try:
            meas = doc.measure_box_glyphs(box)
        except Exception:
            meas = None
        for ln in (meas or {}).get("lines", []):
            if not ln:
                continue
            ctx = ln.get("ctx") or {}
            region = ctx.get("region")
            cb = (ctx.get("geom") or {}).get("char_boxes")
            ltext = ctx.get("orig_text") or ""
            disp = ctx.get("disp_rect")
            cppi = ctx.get("ppi") or ppi
            if region is None or not cb or len(cb) != len(ltext) or not disp:
                continue
            if _res is None and sum(c.isalnum() for c in ltext) >= 6:
                _res = (region, [b for b in cb if b])   # measure the scan damage here
            g = region.mean(2) if region.ndim == 3 else region
            cov = (255.0 - g.astype(np.float32)) / 255.0
            word, wc = [], []
            for ch, bx in zip(ltext, cb):
                if ch == " " or bx is None:
                    if word:
                        units.append(word)
                        unit_centers.append(wc)
                    word, wc = [], []
                    continue
                bx0, by0, bx1, by1 = (int(v) for v in bx)
                if bx1 - bx0 < 3 or by1 - by0 < 3:
                    continue
                tile = cov[by0:by1, bx0:bx1]
                if tile.size and float((tile > 0.3).sum()) >= 6:
                    word.append((ch, tile))
                    wc.append((disp[0] + (bx0 + bx1) / 2.0 / cppi,
                               disp[1] + (by0 + by1) / 2.0 / cppi))
            if word:
                units.append(word)
                unit_centers.append(wc)

    if not units:
        return PageFontMap([], [], {})

    # Group words by SAME-FONT evidence -- shared-character SHAPE agreement, not a coarse
    # weight/size signature. The old style clustering pooled a date with body text and ID
    # numbers in a DIFFERENT font and wrecked the match. Two words link only when they share
    # >= _GROUP_MIN_SHARE characters AND those characters' shapes agree (mean cosine >=
    # _GROUP_SIM), so a short field (a date) pools only with TRULY same-font neighbors -- the
    # other date, same-font numbers -- giving super-resolution enough instances; a word with
    # no same-font neighbor stays its own group and matches from what it has.
    wdesc = []
    for word in units:
        per: dict = {}
        for ch, tile in word:
            if ch in FB._CIDX:
                d = FB._descriptor(tile)
                if d is not None:
                    per.setdefault(ch, []).append(d)
        wdesc.append({c: np.mean(v, 0) for c, v in per.items()})
    n = len(units)
    parent = list(range(n))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        if not wdesc[i]:
            continue
        for j in range(i + 1, n):
            if not wdesc[j]:
                continue
            shared = set(wdesc[i]) & set(wdesc[j])
            if len(shared) < SM._GROUP_MIN_SHARE:
                continue
            sim = float(np.mean([wdesc[i][c] @ wdesc[j][c] for c in shared]))
            if sim >= SM._GROUP_SIM:
                ra, rb = _find(i), _find(j)
                if ra != rb:
                    parent[ra] = rb
    label = {i: _find(i) for i in range(n) if wdesc[i]}

    # one super-res match per cluster (pool every same-style word -> strong, stable)
    ttf_dir = FB._ensure_ttf_cache()
    cluster_tiles: dict = {}
    for ui, c in label.items():
        for ch, tile in units[ui]:
            cluster_tiles.setdefault(c, {}).setdefault(ch, []).append(tile)
    # ONE page-wide damage residual so every cluster's candidates are DEGRADE-MATCHED (the
    # reresolver) rather than clean-matched -- without it the matcher picks decorative
    # look-alikes for a degraded scan ("Redacted"/"Wavefont"). Built once; the degraded
    # candidate renders are cached per (font, char) across clusters so the pass stays fast.
    residual, deg_cache = None, {}
    if _res is not None:
        try:
            from . import degrade as DG
            prof = DG.build_residual_filter(_res[0], {"char_boxes": _res[1]})
            if prof is not None:
                ink, paper = DG.sample_ink_paper(_res[0])
                residual = (prof, ink, paper, 7)
        except Exception:
            residual = None
    group_path: dict = {}
    dbg = []
    for c, tbc in cluster_tiles.items():
        try:
            key = _match_cluster_tiles(tbc, residual, deg_cache)
        except Exception:
            key = None
        if key and ttf_dir:
            p = os.path.join(ttf_dir, key)
            name = "ScanFont-" + os.path.splitext(os.path.basename(key))[0]
            group_path[c] = (p, name) if os.path.exists(p) else None
        else:
            group_path[c] = None
        dbg.append((c, sum(len(v) for v in tbc.values()), key))

    centers, groups = [], []
    for ui, wc in enumerate(unit_centers):
        gid = label.get(ui)
        for (cx, cy) in wc:
            centers.append((cx, cy))
            groups.append(gid)

    if os.environ.get("SUPERMATCH_DEBUG"):
        print(f"[pagefont p{page_index}] {len(units)} words -> {len(cluster_tiles)} "
              f"clusters", flush=True)
        for c, ng, key in sorted(dbg):
            nm = (group_path.get(c) or (None, "None"))[1]
            print(f"  cluster {c}: {ng} glyphs -> {nm}", flush=True)
    return PageFontMap(centers, groups, group_path)
