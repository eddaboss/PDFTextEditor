#!/usr/bin/env python3
"""Precompute the FINE font-match bank: every (font, char) in the library's fine
shape-descriptor (``_fine_from_cov``) + 6-D typeface features (``_glyph_features``),
computed ONCE so OCR never re-renders the library at match time.

The coarse stage already ships ``font_fingerprints_int8.npz``; this adds the fine
re-rank inputs the matcher was rendering live (~8500 fitz renders PER PAGE). Output
(written next to the coarse bank, found via ``_find``):
  * font_fine_int8.npy     (F, 62, 2048) int8   -- fine descriptor, ×127 quantized
  * font_fine_feat.npy     (F, 62, 6)    float16 -- typeface features
  * font_fine_present.npy  (F, 62)       bool    -- glyph exists for (font,char)

Run once: tools/build_fine_bank.py [--limit N] [--jobs K]
Reuses fontbank's OWN render/descriptor functions, so values are byte-for-byte what
the live path produced -- matching results are unchanged, only the renders go away.
"""
import os, sys, time, argparse
import numpy as np

V030 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V030)

from pdftexteditor.ocr import pack
pack.ensure_on_path()
from pdftexteditor.ocr import fontbank as fb

REF = fb.REF_CHARS
C = len(REF)
DIM = 2 * fb._FINE_S * fb._FINE_S


def _one_font(args):
    """(font_index, ttf_key, ttf_dir) -> (idx, int8[C,DIM], feat[C,6], present[C])."""
    fi, key, ttf_dir = args
    import fitz
    from pdftexteditor.ocr import fontbank as fb
    i8 = np.zeros((C, DIM), np.int8)
    ft = np.zeros((C, 6), np.float32)
    pr = np.zeros(C, bool)
    try:
        f = fitz.Font(fontfile=os.path.join(ttf_dir, key))
    except Exception:
        return fi, i8, ft, pr
    for ci, ch in enumerate(REF):
        try:
            if not f.has_glyph(ord(ch)):
                continue
            cov = fb._render_glyph_cov(f, ch)
            d = fb._fine_from_cov(cov)
            v = fb._glyph_features(cov)
            if d is not None:
                i8[ci] = np.clip(np.round(d * fb._FINE_QSCALE), -127, 127).astype(np.int8)
                pr[ci] = True
            if v is not None:
                ft[ci] = v.astype(np.float32)
        except Exception:
            pass
    return fi, i8, ft, pr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only first N fonts (test)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--out", default="", help="output dir (default: bank dir)")
    args = ap.parse_args()

    fp = fb._load_fingerprints()
    if fp is None:
        print("ERROR: coarse fingerprint bank not found; install the OCR pack first.")
        sys.exit(1)
    paths = fp["paths"]
    ttf_dir = fb._ensure_ttf_cache()
    if ttf_dir is None:
        print("ERROR: TTF cache unavailable (fontbank.tar.xz missing).")
        sys.exit(1)
    if args.limit:
        paths = paths[:args.limit]
    F = len(paths)
    out = args.out or fb.bank_dir()
    print(f"building fine bank: {F} fonts x {C} chars x {DIM} dims  (~{F*C*DIM/1e6:.0f}MB int8)")
    print(f"ttf dir: {ttf_dir}\nout dir: {out}\njobs: {args.jobs}")

    fine = np.zeros((F, C, DIM), np.int8)
    feat = np.zeros((F, C, 6), np.float32)
    present = np.zeros((F, C), bool)

    work = [(i, paths[i], ttf_dir) for i in range(F)]
    t0 = time.time()
    done = 0
    if args.jobs > 1:
        import multiprocessing as mp
        ctx = mp.get_context("fork")     # fork: workers inherit imports, no re-spawn cost
        with ctx.Pool(args.jobs) as pool:
            for fi, i8, ft, pr in pool.imap_unordered(_one_font, work, chunksize=8):
                fine[fi], feat[fi], present[fi] = i8, ft, pr
                done += 1
                if done % 200 == 0 or done == F:
                    print(f"  {done}/{F}  ({time.time()-t0:.0f}s)", flush=True)
    else:
        for w in work:
            fi, i8, ft, pr = _one_font(w)
            fine[fi], feat[fi], present[fi] = i8, ft, pr
            done += 1
            if done % 200 == 0 or done == F:
                print(f"  {done}/{F}  ({time.time()-t0:.0f}s)", flush=True)

    os.makedirs(out, exist_ok=True)
    np.save(os.path.join(out, "font_fine_int8.npy"), fine)
    np.save(os.path.join(out, "font_fine_feat.npy"), feat)
    np.save(os.path.join(out, "font_fine_present.npy"), present)
    cov = present.mean()
    print(f"DONE in {time.time()-t0:.0f}s. glyph coverage {cov*100:.1f}%. "
          f"wrote font_fine_*.npy to {out}")


if __name__ == "__main__":
    main()
