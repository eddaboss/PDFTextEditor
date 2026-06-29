#!/usr/bin/env python3
"""Test the bank-curation hypothesis on real scans: does restricting the matcher to
the curated TEXT-font set (text_fonts.txt, display/handwriting dropped) snap the
real-scan matches from oddball Google fonts to sensible document fonts?

Reports font NAMES only (no PHI). Pass scan PDF paths as args; defaults to the two
files we have been validating against.

    python tools/exp_curated.py ["/path/a.pdf" "/path/b.pdf" ...]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import fitz

_V030 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _V030)
sys.path.insert(0, os.path.join(_V030, "tools"))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

from pdftexteditor.ocr import pack, get_engine          # noqa: E402
pack.ensure_on_path()
from pdftexteditor.ocr import fontbank as FB             # noqa: E402
import matcher_v2 as V2                                  # noqa: E402
from validate_real import auto_orient, page_cells, fontname  # noqa: E402


def text_font_indices():
    keys = FB._load_fingerprints()["paths"]
    idx = {k: i for i, k in enumerate(keys)}
    p = FB._find("text_fonts.txt")
    if not p:
        return None
    allow = [idx[k.strip()] for k in open(p) if k.strip() in idx]
    return np.array(sorted(set(allow)), int)


# Genuine DOCUMENT fonts: standard office/system + their open metric clones + the
# body text faces real documents are actually typeset in. Roots matched against the
# bank's family names; EXCLUDE drops display/condensed/heavy/non-Latin cuts.
_ROOTS = ["arial", "arimo", "timesnewroman", "tinos", "couriernew", "cousine",
          "georgia", "gelasio", "verdana", "tahoma", "trebuchet", "carlito", "caladea",
          "palatino", "liberationsans", "liberationserif", "liberationmono",
          "lato", "opensans", "roboto", "sourcesans", "sourceserif", "notosans",
          "notoserif", "ptsans", "ptserif", "merriweather", "ebgaramond", "lora",
          "librefranklin", "librebaskerville", "crimson", "nunito", "mulish",
          "worksans", "inter", "ibmplex", "firasans", "cabin", "karla", "rubik",
          "dmsans", "spectral", "bitter", "domine", "vollkorn", "alegreya", "cardo",
          "gentium", "robotomono", "jetbrainsmono", "sourcecodepro", "inconsolata",
          "robotoslab", "robotoserif"]
_EXCLUDE = ["condensed", "narrow", "black", "thin", "hairline", "display", "outline",
            "expanded", "flex", "cjk", "arabic", "hebrew", "devanagari", "thai",
            "georgian", "adlam", "gothic", "semicondensed"]


def tight_font_indices():
    import pickle
    keys = FB._load_fingerprints()["paths"]
    idx = {k: i for i, k in enumerate(keys)}
    bankdir = FB.bank_dir()
    vi = pickle.load(open(os.path.join(bankdir, "variant_index.pkl"), "rb"))
    fams = vi.get("index") or vi
    allow, names = [], []
    for fam, cells in fams.items():
        fl = fam.lower()
        if any(x in fl for x in _EXCLUDE):
            continue
        if any(r in fl for r in _ROOTS):
            for fn in cells.values():
                if fn in idx:
                    allow.append(idx[fn])
            names.append(fam)
    print(f"  tight set: {len(set(allow))} files from {len(names)} families "
          f"(e.g. {sorted(names)[:10]})")
    return np.array(sorted(set(allow)), int)


def main():
    files = sys.argv[1:] or [
        "/Users/edward/Downloads/doc05154920260624150538.pdf",
        "/Users/edward/Downloads/BFHHI/LEE, VIRGINIA (FEBRUARY 2026) PT EVAL & PT "
        "DISCHARGE & CHHA REFERRAL - FOR MD SIGNATURE (recovered).pdf",
    ]
    tidx = text_font_indices()
    gidx = tight_font_indices()
    keys = FB._load_fingerprints()["paths"]
    print(f"bank {len(keys)} fonts | text set {len(tidx) if tidx is not None else 0} "
          f"| tight set {len(gidx)}")
    eng = get_engine("auto")
    for path in files:
        try:
            d = fitz.open(path)
        except Exception as e:
            print("skip", repr(e)[:50]); continue
        tag = os.path.basename(path)[:14] + ("(phi)" if "recovered" in path else "")
        for page in range(min(d.page_count, 3)):
            pm = d[page].get_pixmap(dpi=300)
            base = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3].copy()
            rgb, lines = auto_orient(base, eng)
            cells, _ = page_cells(rgb, lines)
            if not cells:
                print(f"  {tag} p{page}: no cells"); continue
            full = V2.identify_v2(rgb, cells, topk=3)
            tight = V2.identify_v2(rgb, cells, topk=3, restrict=gidx)
            print(f"  {tag} p{page} (glyphs {full['n_glyphs']}):")
            print(f"      full bank : {fontname(full['best'])}  "
                  f"[{', '.join(fontname(k) for k,_ in full['topk'])}]")
            print(f"      TIGHT     : {fontname(tight['best'])}  conf {tight['confidence']:.3f}  "
                  f"[{', '.join(fontname(k) for k,_ in tight['topk'])}]")


if __name__ == "__main__":
    main()
