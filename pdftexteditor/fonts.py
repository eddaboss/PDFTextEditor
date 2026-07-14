"""Map a PDF span's font name + style flags onto a base-14 font code.

This is the MVP font-matching layer. Real PDFs embed subsetted fonts we usually
don't have on disk, so for now we resolve every span to one of the 14 standard
fonts (Helvetica / Times / Courier in regular, bold, italic, bold-italic). That
keeps reinsertion glyph-safe at the cost of exact typeface fidelity. The richer
substitution table is a v2 concern; see README.
"""

# PyMuPDF span flag bits.
FLAG_ITALIC = 1 << 1   # 2
FLAG_SERIF = 1 << 2    # 4
FLAG_MONO = 1 << 3     # 8
FLAG_BOLD = 1 << 4     # 16

# family -> (regular, bold, italic, bold-italic)
_CODES = {
    "helv": ("helv", "hebo", "heit", "hebi"),
    "times": ("tiro", "tibo", "tiit", "tibi"),
    "cour": ("cour", "cobo", "coit", "cobi"),
}

_SERIF_HINTS = ("times", "serif", "georgia", "garamond", "minion", "roman")
_MONO_HINTS = ("courier", "mono", "consol", "menlo")
# Sans-serif family tokens. The font NAME beats the PDF's serif FLAG bit, which
# is unreliable -- e.g. PyMuPDF reports DejaVuSans with the serif flag set, so
# trusting the flag substitutes Times (serif) for an obviously sans face.
_SANS_HINTS = ("sans", "arial", "helvetica", "verdana", "tahoma", "calibri",
               "segoe", "roboto", "dejavu", "lato", "avenir", "futura",
               "myriad", "frutiger", "ubuntu", "noto")


def _family(font_name: str, flags: int) -> str:
    name = (font_name or "").lower()
    # Name hints first (more reliable than the serif/mono flag bits)...
    # 'monotype' is the known false positive: the bare 'mono' hint matches
    # script faces like 'Monotype Corsiva', so exclude it from that match.
    if "courier" in name or "consol" in name or "menlo" in name or (
            "mono" in name and "monotype" not in name):
        return "cour"
    if any(h in name for h in _SANS_HINTS):
        return "helv"
    if any(h in name for h in _SERIF_HINTS):
        return "times"
    # ...then fall back to the flag bits when the name is uninformative.
    if flags & FLAG_MONO:
        return "cour"
    if flags & FLAG_SERIF:
        return "times"
    return "helv"


def is_bold(font_name: str, flags: int) -> bool:
    return bool(flags & FLAG_BOLD) or "bold" in (font_name or "").lower()


def is_italic(font_name: str, flags: int) -> bool:
    name = (font_name or "").lower()
    return bool(flags & FLAG_ITALIC) or "italic" in name or "oblique" in name


def base14_code(font_name: str, flags: int) -> str:
    """Return the PyMuPDF built-in font code best matching this span."""
    family = _family(font_name, flags)
    bold = is_bold(font_name, flags)
    italic = is_italic(font_name, flags)
    idx = (2 if italic else 0) + (1 if bold else 0)
    return _CODES[family][idx]


def classify_family(family_name: str) -> str:
    """Public wrapper over ``_family`` for a USER-PICKED family name (no flags).

    The font engine's ``resolve_family`` base-14 fallback (BUILD_SPEC §2.2 / §7.9)
    needs to classify a picked family string into the helv/times/cour bucket
    WITHOUT importing the private ``_family``. Style (bold/italic) is decided
    separately from the explicit bits the user picked, so this looks only at the
    family name's serif/mono hints (flags=0)."""
    return _family(family_name, 0)


def base14_code_for_family(family_name: str, bold: bool, italic: bool) -> str:
    """The base-14 code for a USER-PICKED family + explicit style bits.

    Unlike ``base14_code`` (which reads bold/italic out of a span name + flags),
    this takes the bold/italic the user chose in the inspector directly, so a
    picked 'Georgia' + bold maps to 'tibo' regardless of the literal name."""
    family = classify_family(family_name)
    idx = (2 if italic else 0) + (1 if bold else 0)
    return _CODES[family][idx]
