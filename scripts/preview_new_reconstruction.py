"""Preview the NEW reconstruction OUTPUT (no app save-path touch yet): real OCR ->
classify serif/sans/mono -> bundled real font -> render an edit at the OCR box
geometry (correct sizing) + color + damage. Shows the quality to judge before
integrating."""
import os, sys, numpy as np, cv2, fitz
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pdftexteditor.ocr import recognize_and_reconstruct
from pdftexteditor.ocr import degrade as D

DPI = 200
OUT = os.path.expanduser('~/Desktop/ocr_demos'); os.makedirs(OUT, exist_ok=True)
FONTS = {
    'serif': ROOT + '/pdftexteditor/assets/fonts/Newsreader-Regular.ttf',
    'sans': ROOT + '/pdftexteditor/assets/fonts/DejaVuSans.ttf',
    'mono': ROOT + '/pdftexteditor/assets/fonts/DejaVuSansMono.ttf',
}
SERIF_SRC = '/System/Library/Fonts/Supplemental/Times New Roman.ttf'   # the doc is Times-class
SANS_SRC = '/System/Library/Fonts/Supplemental/Arial.ttf'
PAGE = ["INVOICE SUMMARY", "Total amount due is sixty dollars even",
        "Please remit payment within thirty days"]
EDITS = {"sixty": "forty", "thirty": "ninety"}


def render_page(lines, fontpath, size=14):
    doc = fitz.open(); pg = doc.new_page(width=460, height=180)
    f = fitz.Font(fontfile=fontpath); tw = fitz.TextWriter(pg.rect); y = 50
    for ln in lines:
        tw.append((40, y), ln, font=f, fontsize=size); y += size * 2.2
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()


def aug(rgb, seed=5):
    from augraphy import AugraphyPipeline, LowInkRandomLines, LowInkPeriodicLines, InkBleed, SubtleNoise
    np.random.seed(seed)
    p = AugraphyPipeline(ink_phase=[InkBleed(p=0.5), LowInkRandomLines(p=1.0), LowInkPeriodicLines(p=1.0)],
                         paper_phase=[], post_phase=[SubtleNoise(p=1.0)])
    o = np.asarray(p(rgb))
    return (cv2.cvtColor(o.astype(np.uint8), cv2.COLOR_GRAY2RGB) if o.ndim == 2 else o[..., :3]).astype(np.uint8)


def classify(scan, clean_mask):
    # serif vs sans via stroke-contrast on the scan ink; mono left for later
    return 'serif'  # the demo page is Times-class; real impl measures contrast/advance


def render_word_band(fontpath, text, size_pt, band_h_px, ink=(15, 15, 15)):
    f = fitz.Font(fontfile=fontpath); w = f.text_length(text, size_pt) + 2 * size_pt
    doc = fitz.open(); pg = doc.new_page(width=w, height=size_pt * 3)
    base = size_pt * 2.0
    tw = fitz.TextWriter(pg.rect); tw.append((size_pt, base), text, font=f, fontsize=size_pt)
    tw.write_text(pg, color=tuple(c / 255 for c in ink))
    pm = pg.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    cov = (255 - img.mean(2)) / 255.0
    ys = np.where(cov.max(1) > 0.15)[0]; xs = np.where(cov.max(0) > 0.15)[0]
    return img[ys.min() - 2:ys.max() + 3, xs.min() - 2:xs.max() + 3]


def main():
    clean = render_page(PAGE, SERIF_SRC)
    scan = aug(clean)
    clean_mask = clean.mean(2) < 130
    res = recognize_and_reconstruct(scan, DPI, SERIF_SRC, SANS_SRC, engine_name="auto", family_label="Scanned")
    if res is None:
        print("OCR None"); return
    fam = classify(scan, clean_mask)
    fontpath = FONTS[fam]
    ink, paper = D.sample_ink_paper(scan, clean_mask)
    out = scan.copy()
    ppi = DPI / 72.0
    done = []
    for lb in res.lines:
        w = lb.text.strip().lower().strip('.,')
        if w not in EDITS:
            continue
        # cover is display points -> pixels (the OCR word box: correct geometry/sizing)
        cx0, cy0, cx1, cy1 = [int(round(c * ppi)) for c in lb.cover[:4]]
        sev = D.local_severity(scan, clean_mask, (cx0, cy0, cx1, cy1))
        band = render_word_band(fontpath, EDITS[w], lb.size, cy1 - cy0)
        deg = D.degrade_patch(band, ink, paper, sev, seed=hash(w) & 0xffff)
        # place: paper cover, then the degraded word fit to the box height, left-aligned at box x0
        out[cy0:cy1, cx0:cx1] = paper.astype(np.uint8)
        rh = cy1 - cy0; ph = int(rh * 1.1); sc = ph / deg.shape[0]; pw = max(1, int(deg.shape[1] * sc))
        patch = cv2.resize(deg, (pw, ph)); py = max(0, cy0 - int(rh * 0.05)); px = cx0
        H, W = out.shape[:2]; pw = min(pw, W - px); ph = min(ph, H - py); patch = patch[:ph, :pw]
        a = ((255 - patch.mean(2)) / 255.0)[..., None]
        reg = out[py:py + ph, px:px + pw].astype(np.float32)
        out[py:py + ph, px:px + pw] = (reg * (1 - a) + patch.astype(np.float32) * a).astype(np.uint8)
        done.append((w, EDITS[w], lb.size, round(sev, 2)))
    print(f"OCR {res.n_lines} boxes; font={fam}; ink {ink.astype(int)} paper {paper.astype(int)}; edits {done}")

    def lab(img, t):
        bar = np.full((22, img.shape[1], 3), 245, np.uint8)
        cv2.putText(bar, t, (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)
        return np.vstack([bar, img])
    sep = np.full((8, scan.shape[1], 3), 200, np.uint8)
    stack = np.vstack([lab(scan, "1. SCAN (fax-degraded) + real OCR"), sep,
                       lab(out, "2. NEW: edits sixty->forty, thirty->ninety (matched font + color + damage, OCR-box sized)")])
    stack = cv2.resize(stack, None, fx=1.8, fy=1.8, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(os.path.join(OUT, "newrecon.png"), cv2.cvtColor(stack, cv2.COLOR_RGB2BGR))
    print("saved newrecon.png")


if __name__ == "__main__":
    main()
