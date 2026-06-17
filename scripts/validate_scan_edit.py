"""Offscreen validation: does color+damage make a scanned-page edit blend?
Run from repo root: PYTHONPATH=. .venv/bin/python scripts/validate_scan_edit.py
Writes ~/Desktop/ocr_demos/val_isolated.png. NOTE: the word composite here is
illustrative (spacing is approximate); it isolates the color+damage variable.
The intended-ink mask comes from the clean render (in-app: the OCR text render)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2, fitz
from pdftexteditor.ocr import degrade as D

DPI = 200
OUT = os.path.expanduser('~/Desktop/ocr_demos'); os.makedirs(OUT, exist_ok=True)
SERIF = '/System/Library/Fonts/Supplemental/Times New Roman.ttf'
LINES = ["Total amount due on the account is", "sixty dollars and no cents only"]


def render(lines, size=15):
    doc = fitz.open(); pg = doc.new_page(width=460, height=150)
    f = fitz.Font(fontfile=SERIF); tw = fitz.TextWriter(pg.rect); y = 50
    for ln in lines:
        tw.append((40, y), ln, font=f, fontsize=size); y += size * 2.0
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()


def aug_scan(rgb, seed=5):
    from augraphy import AugraphyPipeline, Letterpress, LowInkRandomLines, Faxify, InkBleed, Dithering
    np.random.seed(seed)
    p = AugraphyPipeline(ink_phase=[InkBleed(p=0.7), Letterpress(p=1.0), LowInkRandomLines(p=0.8)],
                         paper_phase=[], post_phase=[Faxify(p=1.0), Dithering(p=0.5)])
    out = np.asarray(p(rgb))
    if out.ndim == 2:
        out = cv2.cvtColor(out.astype(np.uint8), cv2.COLOR_GRAY2RGB)
    return out[..., :3].astype(np.uint8)


def word_patch(text, size=15, ink=(15, 15, 15)):
    f = fitz.Font(fontfile=SERIF); w = f.text_length(text, size) + 2 * size
    doc = fitz.open(); pg = doc.new_page(width=w, height=size * 3)
    tw = fitz.TextWriter(pg.rect); tw.append((size, size * 2), text, font=f, fontsize=size)
    tw.write_text(pg, color=tuple(c / 255 for c in ink))
    pm = pg.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    cov = (255 - img.mean(2)) / 255.0
    ys = np.where(cov.max(1) > 0.15)[0]; xs = np.where(cov.max(0) > 0.15)[0]
    return img[ys.min() - 2:ys.max() + 3, xs.min() - 2:xs.max() + 3]


def main():
    clean = render(LINES)
    # the word 'sixty' sits on line 2; find its rough box by re-rendering with a marker
    f = fitz.Font(fontfile=SERIF)
    # line 1 (LINES[1]) baseline = 50 + 1*30 = 80pt; 'sixty' is the first word
    w_pt = f.text_length("sixty", 15)
    bx0 = int(40 * DPI / 72) - 3; bx1 = int((40 + w_pt) * DPI / 72) + 3
    by0 = int((80 - 13) * DPI / 72); by1 = int((80 + 4) * DPI / 72)

    patch = word_patch("forty")

    clean_mask = clean.mean(2) < 140      # intended-ink locations (in-app: OCR render)

    def run(scan, tag):
        ink_mask = clean_mask              # aligned to the scan (same page geometry)
        ink, paper = D.sample_ink_paper(scan, ink_mask)
        sev = D.local_severity(scan, ink_mask, (bx0, by0, bx1, by1))

        def comp(p):
            out = scan.copy(); rw, rh = bx1 - bx0, by1 - by0
            out[by0:by1, bx0:bx1] = paper.astype(np.uint8)
            ph = int(rh * 1.05); sc = ph / p.shape[0]; pw = max(1, int(p.shape[1] * sc))
            pw = min(pw, rw + int(0.3 * rw))                 # keep near the word width
            q = cv2.resize(p, (pw, ph)); py = by0; px = bx0
            H, W = out.shape[:2]; pw = min(pw, W - px); ph = min(ph, H - py); q = q[:ph, :pw]
            a = ((255 - q.mean(2)) / 255.0)[..., None]
            reg = out[py:py + ph, px:px + pw].astype(np.float32)
            out[py:py + ph, px:px + pw] = (reg * (1 - a) + q.astype(np.float32) * a).astype(np.uint8)
            return out
        cl = comp(patch)
        dg = comp(D.degrade_patch(patch, ink, paper, sev, seed=9))
        print(f"  {tag}: ink {ink.astype(int)} paper {paper.astype(int)} local_severity {sev:.2f}")
        return scan, cl, dg, sev

    s_a = run(aug_scan(clean), "AUGRAPHY")
    R = np.random.RandomState(3)
    hd = D.hard_degrade(clean.astype(np.float32), np.array([20, 20, 24], np.float32),
                        np.array([248, 247, 244], np.float32), 0.40, R)
    s_h = run(hd, "HARD_DEGRADE")

    def lab(img, t):
        bar = np.full((20, img.shape[1], 3), 245, np.uint8)
        cv2.putText(bar, t, (6, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (10, 10, 10), 1, cv2.LINE_AA)
        return np.vstack([bar, img])

    def block(trip, title, sev):
        scan, cl, dg, _ = trip
        crop = lambda im: im[by0 - 14:by1 + 14]
        sep = np.full((6, scan.shape[1], 3), 200, np.uint8)
        s = np.vstack([lab(crop(scan), f"{title} scan (sev={sev:.2f})"), sep,
                       lab(crop(cl), "clean black edit 'forty'"), sep,
                       lab(crop(dg), "recolored+degraded edit")])
        return cv2.resize(s, None, fx=2.4, fy=2.4, interpolation=cv2.INTER_NEAREST)
    a_img = block(s_a, "AUGRAPHY", s_a[3]); h_img = block(s_h, "HARD_DEGRADE", s_h[3])
    w = max(a_img.shape[1], h_img.shape[1])
    pad = lambda x: np.pad(x, ((0, 0), (0, w - x.shape[1]), (0, 0)), constant_values=255)
    cv2.imwrite(os.path.join(OUT, "val_isolated.png"),
                cv2.cvtColor(np.vstack([pad(a_img), np.full((12, w, 3), 120, np.uint8), pad(h_img)]),
                             cv2.COLOR_RGB2BGR))
    print("saved val_isolated.png")


if __name__ == "__main__":
    main()
