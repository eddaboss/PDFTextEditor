"""Filter the font bank to STANDARD TEXT fonts only.

The bank is the Google Fonts collection (~3948 files), and ~1/4 of it is DISPLAY /
HANDWRITING / barcode / symbol families that scanned documents never use but that the
matcher keeps latching onto. Using Google's own per-family category (via the fontsource
API), keep serif / sans-serif / monospace and drop display / handwriting (plus barcode/
symbol by name). Writes ``font_edge_text_int8.npz`` (the edge bank restricted to the kept
fonts) and ``text_fonts.txt`` (the kept TTF keys).

    python tools/filter_text_fonts.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import numpy as np
from fontTools.ttLib import TTFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pdftexteditor.ocr import fontbank  # noqa: E402

KEEP_CATS = {"serif", "sans-serif", "monospace"}
NAME_JUNK = ("barcode", "symbol", "emoji", "dingbat", "wingding", "ornament")

# Obviously-stylized / decorative sub-styles to drop by NAME (a serif/sans category still
# lets these novelty cuts through, and the matcher mis-picks them).
STYLE_JUNK = ("collegiate", "highlight", " sc", "inline", "outline", "shadow", "stencil",
              "pixel", "marker", "brush", "script", "chalk", "comic", "papyrus", "felt",
              "casual", "graffiti", "western", "sketch", "cartoon", "balloon", "doodle")

# The COMMON OFFICE / SYSTEM Latin text fonts that scanned BUSINESS documents are actually
# typed in (Word/Office/macOS defaults). These are NOT Google fonts, so they have no
# fontsource category and were dropped by the workhorse whitelist -- which meant the
# matcher could never pick Arial/Verdana/Times on a real letterhead and settled for the
# nearest Google clone. Keep them explicitly. CJK / non-Latin script variants are excluded
# by name (an "Arial Unicode MS" or "... CJK" is a huge multi-script font, not the text face).
BUSINESS = [
    "arial", "verdana", "tahoma", "times new roman", "times", "georgia", "courier new",
    "trebuchet", "calibri", "cambria", "candara", "consolas", "constantia", "corbel",
    "century gothic", "book antiqua", "franklin gothic", "gill sans", "palatino", "segoe ui",
    "helvetica", "futura", "optima", "geneva", "menlo", "monaco", "andale mono", "impact",
    "rockwell", "baskerville", "didot", "copperplate", "lucida", "myriad", "frutiger",
    "univers", "gotham", "avenir", "din alternate", "din condensed", "garamond",
]
CJK_MARK = ("unicode ms", "cjk", " sc", " tc", " hk", " jp", " kr", "hiragino", "pingfang",
            "songti", "heiti", "kaiti", "mincho", "ms gothic", "yu gothic", "hebrew",
            "arabic", "thai", "devanagari", "tamil", "telugu", "kannada", "gujarati",
            "gurmukhi", "bengali", "khmer", " lao", "sinhala", "myanmar", " kana",
            "ethiopic", "tibetan", "armenian", "georgian ")


def _is_business(fam: str) -> bool:
    """A common office/system Latin text font (Arial, Verdana, Times, ...) -- kept even
    though it is not a Google workhorse, EXCEPT its CJK / non-Latin multi-script variants."""
    fl = fam.lower().strip()
    if any(m in fl for m in CJK_MARK):
        return False
    return any(fl == b or fl.startswith(b + " ") or fl.startswith(b) for b in BUSINESS)


def _categories() -> dict:
    cache = "/tmp/fontsrc.json"
    if not os.path.exists(cache):
        with urllib.request.urlopen("https://api.fontsource.org/v1/fonts",
                                    timeout=60) as r:
            open(cache, "wb").write(r.read())
    data = json.load(open(cache))
    return {e["family"].lower(): e.get("category", "") for e in data if e.get("family")}


def _family(path: str) -> str:
    try:
        t = TTFont(path, fontNumber=0, lazy=True)
        nm = t["name"]
        r = nm.getName(16, 3, 1) or nm.getName(1, 3, 1) or nm.getName(1, 1, 0)
        return str(r) if r else ""
    except Exception:
        return ""


def _category(fam: str, cats: dict, fam_keys: list) -> str:
    f = fam.lower().strip()
    if f in cats:
        return cats[f]
    # longest fontsource family that is a prefix of this name (handles weight/style/
    # script suffixes like "Open Sans Hebrew Condensed Bold").
    best = ""
    for k in fam_keys:
        if f.startswith(k) and len(k) > len(best):
            best = k
    return cats.get(best, "")


def main():
    cats = _categories()
    fam_keys = sorted(cats.keys(), key=len, reverse=True)
    ttf = fontbank._ensure_ttf_cache()
    keys = sorted(os.listdir(ttf))
    keep = []
    cnt = {"keep_text": 0, "keep_business": 0, "drop_name": 0,
           "drop_style": 0, "drop_cat": 0, "drop_nonlatin": 0}
    for k in keys:
        fam = _family(os.path.join(ttf, k))
        fl = fam.lower().strip()
        if not fl or any(j in fl for j in NAME_JUNK):       # barcode/symbol/emoji/dingbat
            cnt["drop_name"] += 1
            continue
        if any(s in fl for s in STYLE_JUNK):                # outline/stencil/brush/script...
            cnt["drop_style"] += 1
            continue
        c = _category(fam, cats, fam_keys)
        if c in KEEP_CATS:                # a real Google TEXT font (serif/sans/mono): KEEP
            keep.append(k)
            cnt["keep_text"] += 1
            continue
        if c in ("display", "handwriting", "icons", "other"):   # Google novelty face: drop
            cnt["drop_cat"] += 1
            continue
        if _is_business(fam):             # uncategorised SYSTEM font: keep Latin office text
            keep.append(k)               # (Arial/Verdana/Times/...); CJK + decorative drop
            cnt["keep_business"] += 1
            continue
        cnt["drop_nonlatin"] += 1
    print("counts:", cnt, " total kept:", len(keep), "/", len(keys))
    # restrict the prebuilt edge bank to the kept fonts
    src = fontbank._find("font_edge_int8.npz")
    z = np.load(src, allow_pickle=False)
    paths = [str(p) for p in z["paths"]]
    idx = {p: i for i, p in enumerate(paths)}
    rows = [idx[k] for k in keep if k in idx]
    out = os.path.join(fontbank.bank_dir(), "font_edge_text_int8.npz")
    np.savez_compressed(
        out, cov=z["cov"][rows], edge=z["edge"][rows],
        paths=np.array([paths[r] for r in rows]),
        chars=z["chars"], S=z["S"], scale=z["scale"])
    open(os.path.join(fontbank.bank_dir(), "text_fonts.txt"), "w").write("\n".join(keep))
    print(f"wrote {out} ({os.path.getsize(out)/1e6:.0f} MB) with {len(rows)} fonts")


if __name__ == "__main__":
    main()
