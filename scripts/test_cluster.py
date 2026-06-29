"""Replicate pagefont.build's word clustering on this page and report, for the date
words, which cluster they land in, who their cluster-mates are, the pooled instances
per char, whether the cluster can super-resolve, and what it matches. Tells us whether
same-font neighbors already pool with the dates or get split off.
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, fontbank as FB  # noqa: E402
from pdftexteditor.ocr import pagefont as PF, supermatch as SM  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()

    units = []   # (word_text, {ch:[tiles]})
    for ln in lines:
        t = ln.text.strip()
        if not t:
            continue
        x0, y0, x1, y1 = ln.bbox
        region = rgb[max(0, int(y0)):int(y1) + 1, max(0, int(x0)):int(x1) + 1]
        try:
            geom = doc._scan_geometry(region, t)
        except Exception:
            geom = None
        cb = (geom or {}).get("char_boxes")
        if not cb or len(cb) != len(t):
            continue
        cov = (255.0 - region.mean(2).astype(np.float32)) / 255.0
        wt, wtext = {}, ""
        for ch, bx in zip(t, cb):
            if ch == " " or bx is None:
                if wt:
                    units.append((wtext, wt))
                wt, wtext = {}, ""
                continue
            bx0, by0, bx1, by1 = (int(v) for v in bx)
            if bx1 - bx0 < 3 or by1 - by0 < 3:
                continue
            tile = cov[by0:by1, bx0:bx1]
            if tile.size and float((tile > 0.3).sum()) >= 6:
                wt.setdefault(ch, []).append(tile)
                wtext += ch
        if wt:
            units.append((wtext, wt))

    # same-font grouping by shared-character shape agreement (the new pagefont logic)
    wdesc = []
    for _txt, wt in units:
        per = {}
        for ch, ts in wt.items():
            if ch in FB._CIDX:
                ds = [d for d in (FB._descriptor(t) for t in ts) if d is not None]
                if ds:
                    per[ch] = np.mean(ds, 0)
        wdesc.append(per)
    n = len(units)
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
            if not wdesc[j]:
                continue
            shared = set(wdesc[i]) & set(wdesc[j])
            if len(shared) < SM._GROUP_MIN_SHARE:
                continue
            if float(np.mean([wdesc[i][c] @ wdesc[j][c] for c in shared])) >= SM._GROUP_SIM:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    label = {i: find(i) for i in range(n) if wdesc[i]}

    clusters = {}
    for ui, c in label.items():
        clusters.setdefault(c, []).append(ui)

    for c, members in sorted(clusters.items()):
        texts = [units[ui][0] for ui in members]
        if not any(("/" in x and sum(ch.isdigit() for ch in x) >= 3) for x in texts):
            continue
        pooled = {}
        for ui in members:
            for ch, ts in units[ui][1].items():
                pooled.setdefault(ch, []).extend(ts)
        inst = {ch: len(v) for ch, v in pooled.items()}
        nsr = sum(1 for ch, v in pooled.items() if SM._superres(v) is not None)
        key = PF._match_cluster_tiles(pooled)
        import fitz
        from skimage.metrics import structural_similarity as ssim

        def fname(k):
            try:
                return fitz.Font(fontfile=os.path.join(ttf, k)).name
            except Exception:
                return k
        print(f"\nCLUSTER {c}: members={texts}")
        print(f"  pooled instances/char={inst}  super-res chars={nsr}")
        print(f"  CURRENT cosine match -> {key} ({fname(key) if key else 'None'})")

        # RENDER-AND-COMPARE rerank: super-resolve each char, then rank the coarse top-40
        # by how well each candidate's RENDER matches the scan super-glyph (image SSIM).
        sgs = {}
        for ch, v in pooled.items():
            sg = SM._superres(v)
            if sg is None:
                hi = [h for h in (SM._hires_tile(t) for t in v) if h is not None]
                sg = (np.mean(hi, 0).astype(np.float32) if len(hi) > 1 else hi[0]) if hi else None
            if sg is not None:
                sgs[ch] = sg
        keys = FB._load_fingerprints()["paths"]
        coarse = [int(i) for i in np.argsort(-PF._coarse_from_tiles(pooled))[:40]]
        scored = []
        for ci in coarse:
            f = FB._cand_font(os.path.join(ttf, keys[ci]))
            if f is None:
                continue
            sims = []
            for ch, sg in sgs.items():
                try:
                    if f.has_glyph(ord(ch)):
                        cr = SM._hires_tile(FB._render_glyph_cov(f, ch))
                        if cr is not None:
                            sims.append(ssim(sg, cr, data_range=1.0))
                except Exception:
                    pass
            if sims:
                scored.append((float(np.mean(sims)), keys[ci]))
        scored.sort(key=lambda x: -x[0])
        print("  RENDER-AND-COMPARE top 5:")
        for s, k in scored[:5]:
            star = " <-- ARIAL FAMILY" if k in ("00121.ttf", "00188.ttf", "00199.ttf", "00247.ttf") else ""
            print(f"    {s:.3f}  {k}  {fname(k)}{star}")
        abr = [r for r, (s, k) in enumerate(scored) if k == "00121.ttf"]
        print(f"    Arial Bold (00121) -> render-compare rank {abr[0] if abr else 'not in top40'}")
    doc.close()


if __name__ == "__main__":
    main()
