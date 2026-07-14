"""Font resolution layer: the single source of truth for "what font do we
draw on screen and embed into the saved PDF for an edited span."

Priority order (see BUILD_SPEC.md §3):

  Tier 1  TIER_EMBEDDED  the document's ORIGINAL embedded font buffer, used
                         ONLY when it is glyph-safe for the new text (subset
                         fonts are not) and Qt can render it on screen.
  Tier 2  TIER_SYSTEM    a FULL system font file of the same family on disk,
                         verified with QFontInfo.exactMatch(), so the saved
                         PDF is portable and the preview matches.
  Tier 3  TIER_BASE14    the base-14 floor (fonts.base14_code), glyph-safe
                         always, used only when 1 and 2 both fail.

Both the live editor/preview and document.save_as import and call
``FontEngine.resolve(...)`` so what you type is what gets saved.

Verified against the project venv (PyMuPDF 1.27.2.3, PySide6 6.11.1):
  * a subset Type0 buffer loads via fitz.Font(fontbuffer=...) but reports
    valid_codepoints()==0 and has_glyph()==0 for every char -> unusable.
  * the rawdict span font name is subset-tag-stripped AND carries PostScript
    artifacts (TimesNewRomanPSMT, ArialMT, ComicSansMS) that differ from the
    get_fonts(full=True) basefont (Times New Roman Regular, Arial Regular,
    Comic Sans MS Regular). Exact normalize() misses these, so the bridge
    falls back to a family-level normalization + flag-based style scoring.
  * QFont("ArialMT") / QFont("Times") / QFont("Calibri") mis-resolve to the
    system UI font with exactMatch()==False, so every Tier-2 candidate is
    gated on exactMatch().
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
from dataclasses import dataclass
from functools import lru_cache

import fitz
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo

from .fonts import (
    FLAG_BOLD,
    FLAG_ITALIC,
    FLAG_MONO,
    FLAG_SERIF,
    base14_code,
    base14_code_for_family,
    is_bold,
    is_italic,
    _SANS_HINTS as _SANS_TOKENS,
)

# --- Resolution tiers (also the fidelity-dot colors in the UI) ------------
TIER_EMBEDDED = 1   # original embedded buffer, glyph-safe -> green dot
TIER_SYSTEM = 2     # full system font of the same family -> blue dot
TIER_BASE14 = 3     # base-14 substitute -> amber dot

# Directories enumerated for the system font index (BUILD_SPEC §2.2).
def _bundled_font_dir() -> str:
    """Absolute path to the app's BUNDLED fonts (the free DejaVu family). These
    ship inside the app so a PDF whose font is not installed on THIS machine
    (e.g. an mPDF cert in DejaVu Sans on a Mac/Windows box without it) can still
    be edited and saved in the real face instead of a substitute. Works in dev
    and inside the PyInstaller bundle (sys._MEIPASS)."""
    import sys
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "pdftexteditor", "assets", "fonts")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "fonts")


_BUNDLED_FONT_DIR = _bundled_font_dir()

# The bundled dir is scanned alongside the OS dirs, so the SAVE path finds the
# DejaVu faces by family (system_record_for -> embed) exactly as it finds an
# installed font. Qt RENDERING of them needs a separate addApplicationFont
# (FontEngine.register_bundled_fonts), since they live outside the OS dirs.
_SYSTEM_FONT_DIRS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    # Windows (office machines): the standard font dirs so an installed face
    # still resolves there too.
    os.path.expandvars(r"%WINDIR%\Fonts"),
    os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
    _BUNDLED_FONT_DIR,
)

# Style tokens stripped to reach a bare family name. Order matters: the
# compound forms (bolditalic) must be tried before their parts.
_STYLE_SUFFIXES = (
    "bolditalic", "boldoblique", "bold", "italic", "oblique",
    "semibold", "demibold", "light", "medium", "black", "heavy",
    "thin", "condensed", "extrabold", "ultralight",
)
_WEIGHTLESS_SUFFIXES = ("regular", "roman", "book", "normal")
# PostScript filename artifacts that are not part of the family name.
_PS_ARTIFACT_RE = re.compile(r"(psmt|mt|ps)$")

# Smallest font size we ask Qt to render (avoids a zero/degenerate size).
_MIN_PIXEL_SIZE = 1.0


@dataclass(frozen=True)
class ResolvedFont:
    """The outcome of resolving one edit. Consumed by BOTH the UI and save_as."""

    tier: int                       # TIER_EMBEDDED | TIER_SYSTEM | TIER_BASE14
    exact: bool                     # True only when the literal target typeface was reproduced
    # --- PyMuPDF insertion side (what save_as passes to insert_text) ---
    pdf_fontname: str               # registration name to pass as insert_text(fontname=...)
    pdf_fontbuffer: bytes | None    # font bytes, or None
    base14_code: str | None         # set only when tier == TIER_BASE14 (e.g. "hebo")
    # --- Qt rendering side (what the editor/preview draw with) ---
    qt_family: str                  # the family name to construct QFont(qt_family) with
    qt_bold: bool
    qt_italic: bool
    # --- provenance / UI chip ---
    source_name: str                # human label, e.g. "Comic Sans MS (embedded)"

    def qfont(self, pixel_size: float) -> QFont:
        """Build the QFont the UI draws with, at a device-independent pixel size.

        The caller passes ``span.size * zoom`` (device-independent px). PySide6
        6.11's ``QFont`` does not bind the fractional ``setPixelSizeF``, but it
        DOES bind ``setPointSizeF``, which is fractional. We convert the target
        pixel size to points via the screen's logical DPI (points = px * 72 /
        dpi) so fractional zooms (e.g. 11pt at 1.5x = 16.5px) are not rounded to
        an integer pixel and the preview tracks the saved output more closely.
        Falls back to integer ``setPixelSize`` when no screen/DPI is available.
        The editor and preview both call this same builder, so they stay in
        lockstep regardless.
        """
        f = QFont(self.qt_family)
        f.setBold(self.qt_bold)
        f.setItalic(self.qt_italic)
        size = max(_MIN_PIXEL_SIZE, pixel_size)   # caller passes span.size * zoom
        # PIXEL size, not point size. The editor/preview live in the QGraphics
        # scene whose units are device-independent pixels (span.size * zoom), so
        # the font must be that many PIXELS to match the rendered page pixmap.
        # Routing through setPointSizeF + screen DPI made the inline editor
        # render at a DIFFERENT (smaller) size than the page, so the text
        # appeared to shrink the moment you clicked in to edit. The <=0.5px
        # rounding is invisible; matching the page size is what matters.
        f.setPixelSize(max(1, round(size)))
        f.setStyleStrategy(QFont.PreferMatch)
        return f


@dataclass(frozen=True)
class _FaceRecord:
    """One face indexed from the system font tree."""

    path: str                 # absolute path to .ttf/.otf/.ttc
    face_index: int           # face within a .ttc (0 for single-face files)
    postscript: str           # name ID 6
    family: str               # name ID 16 (typographic) or 1
    subfamily: str            # name ID 17 or 2
    full_name: str            # name ID 4
    is_bold: bool
    is_italic: bool


def _style_from_ttfont(tt, path: str) -> "tuple[str, str, str, str, bool, bool]":
    """Read (postscript, family, subfamily, full_name, bold, italic) off an OPEN
    fontTools TTFont. The ONE place that classifies a face's style, shared by the
    system-index scan (``read_face``) and the standalone ``detect_ttf_style``
    probe so the two can never drift. name IDs 6 / 16-or-1 / 17-or-2 / 4; bold &
    italic primarily from OS/2.fsSelection (italic 0x01, bold 0x20), then
    head.macStyle (bold 0x01, italic 0x02), then a name heuristic last. Returns
    all-empty when there is no usable name table."""
    try:
        name = tt["name"]
    except Exception:
        return ("", "", "", "", False, False)

    def gn(*ids: int) -> str:
        for nid in ids:
            rec = (name.getName(nid, 3, 1, 0x409)
                   or name.getName(nid, 1, 0, 0)
                   or name.getName(nid, 3, 0, 0x409))
            if rec is not None:
                try:
                    return str(rec)
                except Exception:
                    continue
        return ""

    postscript = gn(6)
    family = gn(16, 1)
    subfamily = gn(17, 2)
    full_name = gn(4)
    style_blob = f"{subfamily} {postscript} {full_name}".lower()
    # OS/2 fsSelection / head macStyle are more reliable for bold/italic.
    bold = italic = False
    try:
        fs = tt["OS/2"].fsSelection
        italic = bool(fs & 0x01)
        bold = bool(fs & 0x20)
    except Exception:
        pass
    if not (bold or italic):
        try:
            mac = tt["head"].macStyle
            bold = bool(mac & 0x01)
            italic = bool(mac & 0x02)
        except Exception:
            pass
    if not bold:
        bold = "bold" in style_blob and "semibold" not in subfamily.lower()
    if not italic:
        italic = "italic" in style_blob or "oblique" in style_blob
    if not family:
        family = postscript or os.path.basename(path)
    return (postscript, family, subfamily, full_name, bold, italic)


def detect_ttf_style(path: str, face_index: int = 0) -> "tuple[str, bool, bool]":
    """``(family, bold, italic)`` for ONE font file/face, read straight off its
    OpenType tables. The standalone probe used to learn the style of a
    scan-matched bank face (the bank's files keep their real name tables even
    after subsetting) or a user-picked file. Returns ``("", False, False)`` on an
    unreadable font."""
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, lazy=True, fontNumber=max(0, face_index))
        try:
            _, family, _, _, bold, italic = _style_from_ttfont(tt, path)
        finally:
            try:
                tt.close()
            except Exception:
                pass
        return (family, bold, italic)
    except Exception:
        return ("", False, False)


# --------------------------------------------------------------------------
# Tier-2 alias table (BUILD_SPEC §2.4). Keyed by the bare normalized family
# of the span/PostScript base name; value is the ordered candidate family
# list handed to Qt (every candidate gated on QFontInfo.exactMatch()).
# --------------------------------------------------------------------------
# _SANS_TOKENS (sans name hints, used even when the serif flag is wrongly set) is
# imported from .fonts as _SANS_HINTS -- it was a byte-identical copy.


def _alias_candidates(base_norm: str, raw_norm: str, flags: int) -> list[str]:
    """Map a normalized family to an ordered list of installed family names."""
    n = base_norm
    table: list[tuple[tuple[str, ...], list[str]]] = [
        (("arial",), ["Arial"]),
        (("timesnew", "timesnewroman", "times"), ["Times New Roman", "Times"]),
        (("helvetica",), ["Helvetica"]),
        (("couriernew", "courier"), ["Courier New", "Courier"]),
        (("calibri",), ["Calibri", "Helvetica Neue"]),
        (("georgia",), ["Georgia"]),
        (("comicsansms", "comicsans"), ["Comic Sans MS"]),
        (("consolas", "menlo"), ["Menlo", "Consolas"]),
        # The free DejaVu family ships WITH the app (see _BUNDLED_FONT_DIR), so
        # edits to a PDF set in it -- the default for mPDF/matplotlib/many
        # web-to-PDF tools -- render and save in the real face on any machine,
        # never a substitute. Mono before Sans so "DejaVu Sans Mono" never
        # matches the Sans key.
        (("dejavusansmono", "dejavumono"), ["DejaVu Sans Mono"]),
        (("dejavusans", "dejavu"), ["DejaVu Sans"]),
    ]
    for keys, fams in table:
        if n in keys:
            return fams
    # token-based families
    if "garamond" in raw_norm or n.startswith("cmr") or n.startswith("nimbusrom"):
        return ["Times New Roman", "Times"]
    # 'monotype' is the known false positive: the bare 'mono' hint matches
    # script faces like 'Monotype Corsiva', so exclude it from that match.
    mono_hint = ("mono" in raw_norm and "monotype" not in raw_norm) or any(
        t in raw_norm for t in ("consol", "menlo"))
    if (flags & FLAG_MONO) or mono_hint:
        return ["Menlo", "Courier New"]
    # If the bare family name itself is an installed family, try it directly
    # first (e.g. "Georgia", "Verdana"), then fall back on style heuristics.
    direct = _titlecase_family(base_norm, raw_norm)
    if direct:
        out = [direct]
    else:
        out = []
    # Sans/serif fallback. The NAME beats the unreliable serif FLAG bit (PyMuPDF
    # reports DejaVuSans etc. with the serif flag set despite being sans), so a
    # "sans" name always falls back to a sans family rather than to Times.
    if any(t in raw_norm for t in _SANS_TOKENS):
        out += ["Helvetica", "Arial"]
    elif (flags & FLAG_SERIF) or any(
            t in raw_norm for t in ("serif", "times", "georgia", "roman")):
        out.append("Times New Roman")
    else:
        out.append("Helvetica")
    return out


def _titlecase_family(base_norm: str, raw_norm: str) -> str | None:
    """Best-effort human family name from the raw span name for a direct Qt try."""
    # Use the raw (un-normalized punctuation removed) but recover word breaks
    # is impossible here; only attempt single-word families we can capitalize.
    if base_norm and base_norm.isalpha():
        return base_norm.capitalize()
    return None


class FontEngine:
    """Resolves the best insertable/renderable font for an edited span.

    One instance per open PDFDocument; it caches per-page xref maps and shares
    the (process-wide) system font index and Qt-loaded-family cache on the
    class. Constructed by PDFDocument and shared with the PageView so the live
    editor and the save writer resolve identically.
    """

    # Module-level singletons shared across instances (built once, expensive):
    _system_index: list[_FaceRecord] | None = None         # fontTools face index
    _qt_loaded_families: dict[bytes, str] = {}             # buffer hash -> Qt family
    _available_families: list[str] | None = None           # inspector picker menu
    _bundled_registered = False                            # DejaVu added to Qt once
    # Guards the one-time system-index build so the startup prewarm thread and
    # a concurrent GUI-thread caller (e.g. the family picker opening early)
    # never scan twice or publish a half-built index (perf foundation M4a).
    _index_lock = threading.Lock()

    # The three base-14 display families the picker always offers, even when the
    # exact face is not found by name in the scanned index (they map to a
    # base-14 floor that is always glyph-safe + embeddable).
    _BASE14_DISPLAY_FAMILIES = ("Helvetica", "Times New Roman", "Courier New")

    def __init__(self, doc: fitz.Document) -> None:
        self.doc = doc
        # page_index -> {normalized basefont match -> xref}; built lazily.
        self._xref_cache: dict[tuple, int | None] = {}
        self._resolve_cache: dict[tuple, ResolvedFont] = {}
        # xref -> extracted (basefont,ext,type,buffer) | None. The embedded font for
        # an xref is fixed per doc; memoizing it stops embedded_xref's filter and
        # resolve's fetch from decoding the same ~400 KB buffer twice (and a fresh
        # copy per distinct glyph-set typed -> the cache was unbounded).
        self._embedded_buffer_cache: dict[int, tuple | None] = {}
        # Make the bundled DejaVu faces renderable by Qt (idempotent; needs a
        # live QApplication, which exists by the time a document is opened).
        type(self).register_bundled_fonts()

    @classmethod
    def register_bundled_fonts(cls) -> None:
        """Register the app-bundled DejaVu faces with Qt so the live editor can
        RENDER them (QFontInfo.exactMatch becomes True). The SAVE path already
        finds them through the system index (the bundled dir is scanned), so this
        only covers on-screen drawing. Idempotent; no-ops without a QApplication
        so an early/headless import never crashes -- the next call registers."""
        if cls._bundled_registered:
            return
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            return
        if os.path.isdir(_BUNDLED_FONT_DIR):
            for fn in sorted(os.listdir(_BUNDLED_FONT_DIR)):
                if fn.lower().endswith((".ttf", ".otf")):
                    QFontDatabase.addApplicationFont(
                        os.path.join(_BUNDLED_FONT_DIR, fn))
        cls._bundled_registered = True

    @classmethod
    def register_system_fonts_with_qt(cls, families) -> None:
        """Headless safety net for the SYSTEM tier. The offscreen Qt platform on
        Windows does not load the OS font database, so a family the resolver finds
        on disk still reports unavailable to Qt (``QFontInfo.exactMatch`` False)
        and the SYSTEM tier is skipped. Register each requested family's on-disk
        faces with Qt so a headless run exercises the SAME resolve path the real
        GUI app does (its platform plugin auto-loads them). No-op where Qt already
        knows the family (macOS, real Windows GUI) and without a QApplication."""
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            return
        for fam in families:
            if QFontInfo(QFont(fam)).exactMatch():
                continue
            seen: set = set()
            for bold in (False, True):
                for italic in (False, True):
                    path = cls.system_face_for(fam, bold, italic)
                    if path and path not in seen:
                        seen.add(path)
                        QFontDatabase.addApplicationFont(path)

    # =====================================================================
    # (a) registering / extracting a span's original embedded font
    # =====================================================================
    def embedded_xref(self, page_index: int, span_font: str, flags: int) -> int | None:
        """Bridge a rawdict span's font name to its embedded font xref.

        Matches ``span_font`` against ``page.get_fonts(full=True)``. Tries an
        exact ``normalize()`` match first; when the span carries PostScript
        artifacts that the basefont lacks (TimesNewRomanPSMT vs Times New Roman
        Regular) it falls back to a family-level match, disambiguating a
        multi-face family by the bold/italic bits in ``flags``. Returns the
        xref, or None when the font is not embedded / not matched. Cached per
        (page_index, span_font, flags).
        """
        cache_key = (page_index, span_font, flags)
        if cache_key in self._xref_cache:
            return self._xref_cache[cache_key]

        page = self.doc[page_index]
        # Dedup the 7-tuples by xref, keeping the basefont string.
        by_xref: dict[int, str] = {}
        for entry in page.get_fonts(full=True):
            xref, basefont = entry[0], entry[3]
            by_xref.setdefault(xref, basefont)

        target = self.normalize(span_font)
        candidates = [(x, b) for x, b in by_xref.items()
                      if self.normalize(b) == target]

        if not candidates:
            # Family-level fallback for PostScript-name spans.
            fam = self._family_norm(span_font)
            if fam:
                candidates = [(x, b) for x, b in by_xref.items()
                              if self._family_norm(b) == fam]

        # A font name often matches several entries: a referenced-but-not-
        # embedded base-14 / Type0 reference (extracts to an empty buffer)
        # alongside the real embedded subset(s). Keep ONLY candidates that
        # actually carry embedded bytes, so we never discard a usable embedded
        # face just because a non-embedded sibling sorted first. Return None
        # only when no candidate is embedded (the font genuinely is not in the
        # file and must be substituted downstream).
        embedded = [(x, b) for x, b in candidates
                    if self.extract_embedded(x) is not None]

        result: int | None
        if not embedded:
            result = None
        elif len(embedded) == 1:
            result = embedded[0][0]
        else:
            # Real collision among embedded faces: score each basefont's
            # weight/style tokens against the span's flags and pick the best.
            embedded.sort(
                key=lambda xb: self._style_agreement(xb[1], flags),
                reverse=True,
            )
            result = embedded[0][0]

        self._xref_cache[cache_key] = result
        return result

    def extract_embedded(self, xref: int) -> tuple[str, str, str, bytes] | None:
        """Thin wrapper over ``doc.extract_font(xref)`` (the DEFAULT 4-tuple
        call, never ``info_only`` -- that returns an empty buffer). Returns
        ``(basefont, ext, type, buffer)`` or None when the buffer is empty.
        """
        if xref in self._embedded_buffer_cache:
            return self._embedded_buffer_cache[xref]
        basefont, ext, ftype, buffer = self.doc.extract_font(xref)
        out = None if (not buffer or len(buffer) == 0) else (basefont, ext, ftype, buffer)
        self._embedded_buffer_cache[xref] = out
        return out

    # =====================================================================
    # (b) glyph-availability check
    # =====================================================================
    @staticmethod
    def font_covers(font: "fitz.Font", text: str) -> bool:
        """True iff ``font`` can render EVERY non-space char of ``text``.

        A subset font reports valid_codepoints()==0 and has_glyph()==0 for all
        chars; has_glyph returns a GID (int), 0 == absent.
        """
        if len(font.valid_codepoints()) == 0:
            return False
        return all(font.has_glyph(ord(c)) for c in text if c != " ")

    # =====================================================================
    # (c) THE resolver: priority embedded -> system -> base-14
    # =====================================================================
    def resolve(self, page_index: int, span_font: str, flags: int,
                new_text: str) -> ResolvedFont:
        """Resolve the best font for an edit, in strict priority order.

        Pure (no page mutation, no insertion). Deterministic, so the live
        preview equals the saved output. See BUILD_SPEC §3 for the algorithm.

        Cached on the SET of distinct codepoints the new text needs, as a
        deterministically-ordered tuple ``tuple(sorted(set(new_text)))``. This
        is the actual resolution input (coverage depends on which glyphs are
        needed, not their order or count), and it is an explicit, stable key --
        unlike a bare ``frozenset`` whose intent is opaque.
        """
        glyph_key = tuple(sorted(set(new_text)))
        cache_key = (page_index, span_font, flags, glyph_key)
        cached = self._resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        bold = is_bold(span_font, flags)
        italic = is_italic(span_font, flags)

        # --- Step 1/2: Tier 1, the embedded buffer -----------------------
        xref = self.embedded_xref(page_index, span_font, flags)
        if xref is not None:
            res = self.extract_embedded(xref)
            if res is not None:
                buffer = res[3]
                try:
                    font = fitz.Font(fontbuffer=buffer)
                except Exception:
                    # Damaged/Type1 embedded buffer: fitz raises FzErrorLibrary
                    # (not ValueError). Fall through to Tier 2 so preview == save.
                    font = None
                if font is not None and self.font_covers(font, new_text):
                    qt_family = self.load_qt_family(buffer)
                    if qt_family:
                        rf = ResolvedFont(
                            tier=TIER_EMBEDDED,
                            exact=True,
                            pdf_fontname="EMB",
                            pdf_fontbuffer=buffer,
                            base14_code=None,
                            qt_family=qt_family,
                            qt_bold=bold,
                            qt_italic=italic,
                            source_name=f"{self._strip_name(span_font)} (embedded)",
                        )
                        self._resolve_cache[cache_key] = rf
                        return rf
                    # Qt cannot render the buffer: fall through to Tier 2 so
                    # preview == save (embedded save w/o matching preview
                    # violates the fidelity contract).

        # --- Step 3: Tier 2, a full system font of the same family -------
        base_norm = self._family_norm(span_font)
        raw_norm = self.normalize(span_font)
        for family in _alias_candidates(base_norm, raw_norm, flags):
            qf = QFont(family)
            qf.setBold(bold)
            qf.setItalic(italic)
            if QFontInfo(qf).exactMatch():
                rf = ResolvedFont(
                    tier=TIER_SYSTEM,
                    exact=True,
                    pdf_fontname="SYS",
                    pdf_fontbuffer=None,
                    base14_code=None,
                    qt_family=family,
                    qt_bold=bold,
                    qt_italic=italic,
                    source_name=family,
                )
                self._resolve_cache[cache_key] = rf
                return rf

        # --- Step 4: Tier 3, base-14 floor (glyph-safe always) -----------
        code = base14_code(span_font, flags)
        qt_family = self._base14_qt_family(code)
        rf = ResolvedFont(
            tier=TIER_BASE14,
            exact=False,
            pdf_fontname=code,
            pdf_fontbuffer=None,
            base14_code=code,
            qt_family=qt_family,
            qt_bold=bold,
            qt_italic=italic,
            source_name=f"{qt_family} (substitute)",
        )
        self._resolve_cache[cache_key] = rf
        return rf

    # =====================================================================
    # (d) Qt loading helpers for the UI
    # =====================================================================
    @classmethod
    def load_qt_family(cls, buffer: bytes) -> str | None:
        """Register font bytes with QFontDatabase so the editor can render the
        matching family on screen (Tier 1, the embedded buffer).

        Wraps ``QFontDatabase.addApplicationFontFromData``. Returns the first
        family name on success, None on failure. Cached by buffer hash so
        repeated edits to one span register the font once.
        """
        digest = hashlib.sha1(buffer).digest()
        cached = cls._qt_loaded_families.get(digest)
        if cached is not None:
            return cached or None

        font_id = QFontDatabase.addApplicationFontFromData(QByteArray(buffer))
        if font_id == -1:
            cls._qt_loaded_families[digest] = ""
            return None
        families = QFontDatabase.applicationFontFamilies(font_id)
        family = families[0] if families else None
        cls._qt_loaded_families[digest] = family or ""
        return family

    # Custom faces (scan-built OCR fonts) registered at runtime: family -> path.
    # They ride the SYSTEM tier -- injected into the system index so
    # ``resolve_family`` / ``fitz_font_for`` / ``_register_resolved`` /
    # ``available_families`` treat them like any installed font, with ZERO new
    # bake code (OCR_SPEC §8 Tier 1).
    _custom_faces: dict[str, str] = {}
    # A custom face's runtime ALIAS (e.g. a bank 'ScanFont-NNNNN') -> the family
    # Qt actually loaded it under (its own name-table family, e.g. 'Cousine'). The
    # editor builds its QFont from this so Qt resolves the REAL face on screen
    # instead of a system sans (the fallback that made OCR edits look like Arial).
    _custom_qt_family: dict[str, str] = {}

    @classmethod
    def _custom_face_dir(cls) -> str:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "pdftexteditor_scanfonts")
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def register_custom_face(cls, family: str, otf_bytes: bytes) -> str:
        """Register a runtime-built OTF (e.g. an OCR scan font) under ``family``
        so the whole edit/bake pipeline can use it like an installed face.

        Writes the bytes to a process-temp cache file (``face_bytes`` /
        ``_register_resolved`` read faces from disk), loads it into Qt for
        on-screen rendering, and appends a ``_FaceRecord`` to the system index.
        Idempotent per family. Returns ``family``.
        """
        if family in cls._custom_faces:
            return family
        path = os.path.join(cls._custom_face_dir(),
                            hashlib.sha1(otf_bytes).hexdigest()[:16] + ".otf")
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(otf_bytes)
        # Make Qt render it; the OTF's name table familyName IS ``family`` so
        # QFont(family).exactMatch() holds (the resolve_family Tier-2 gate).
        try:
            fid = QFontDatabase.addApplicationFontFromData(QByteArray(otf_bytes))
            fams = (QFontDatabase.applicationFontFamilies(fid)
                    if fid != -1 else [])
            if fams:
                cls._custom_qt_family[family] = fams[0]
        except Exception:
            pass
        index = cls._build_system_index()       # builds + caches if needed
        index.append(_FaceRecord(
            path=path, face_index=0, postscript=family, family=family,
            subfamily="Regular", full_name=family, is_bold=False,
            is_italic=False))
        cls._custom_faces[family] = path
        cls._available_families = None           # picker re-derives with the new face
        return family

    @classmethod
    def qt_family_for(cls, family: str) -> str | None:
        """The real QFontDatabase family a registered custom face loaded under
        (its own name-table family, e.g. 'Cousine' for a bank 'ScanFont-NNNNN'),
        so the on-screen editor can build a QFont Qt actually resolves. None when
        the face is not a registered custom face."""
        return cls._custom_qt_family.get(family)

    @classmethod
    def display_name_for(cls, family: str) -> str:
        """The HUMAN-READABLE name to SHOW for ``family`` in the formatting bar /
        picker. A scan-matched bank face is stored under an opaque alias
        ('ScanFont-NNNNN') because that alias maps back to the exact bank TTF for
        embedding -- but the user must see the real typeface (e.g. 'Courier New',
        'Solway Medium') the OTF actually carries in its name table, which Qt
        recovered at registration. Returns ``family`` unchanged for ordinary
        families. Hidden system names ('.SF Numeric') are de-dotted."""
        real = cls._custom_qt_family.get(family)
        if real and real != family:
            real = real.lstrip(".").strip()
            return real or family
        return family

    @classmethod
    def is_scan_alias(cls, family: str) -> bool:
        """True when ``family`` is an opaque scan-bank alias whose real name
        differs (so the picker hides the alias and shows the real name instead)."""
        real = cls._custom_qt_family.get(family)
        return bool(real) and real != family

    @classmethod
    def system_face_for(cls, family: str, bold: bool, italic: bool) -> str | None:
        """Resolve a base family + style to an absolute font FILE path on disk
        for Tier-2 embedding into the saved PDF. Looks the matched _FaceRecord
        up in the system index; returns its path (writing the specific .ttc
        face bytes is save_as's job via ``face_bytes``)."""
        rec = cls.system_record_for(family, bold, italic)
        return rec.path if rec else None

    @classmethod
    def system_record_for(cls, family: str, bold: bool,
                          italic: bool) -> _FaceRecord | None:
        """Return the best ``_FaceRecord`` for a family + style, or None.

        Used by save_as: a matched .ttc path needs the specific FACE INDEX so
        face_bytes(path, index) extracts the right standalone face.
        """
        index = cls._build_system_index()
        fam_norm = cls.normalize(family)
        matches = [r for r in index if cls.normalize(r.family) == fam_norm]
        if not matches:
            # Fall back to matching on the full or PostScript name's family part.
            matches = [r for r in index
                       if cls._family_norm(r.postscript) == cls._family_norm(family)]
        if not matches:
            return None

        def score(r: _FaceRecord) -> int:
            s = (2 if r.is_bold == bold else -2) + (2 if r.is_italic == italic else -2)
            # Tie-break unstyled requests toward the PLAIN face, else Regular and
            # Medium/Light/Book tie at 4 and disk order embeds the wrong weight.
            if not bold and not italic and (r.subfamily or "").lower() in (
                    "regular", "roman", "book", "normal", ""):
                s += 1
            return s

        matches.sort(key=score, reverse=True)
        return matches[0]

    # =====================================================================
    # (e) user-PICKED family support (BUILD_SPEC §2.1 / §2.2 / §2.3)
    # =====================================================================
    @classmethod
    def available_families(cls) -> list[str]:
        """Sorted, de-duplicated family names for the inspector's picker.

        The union of the three base-14 display families (Helvetica, Times New
        Roman, Courier New) and every distinct ``family`` in the scanned system
        index. Hidden/system families whose name starts with '.' (e.g.
        ``.AppleSystemUIFont``) are filtered out. Cached on the class after the
        first build. EVERY family in this list resolves to an embeddable face
        via ``resolve_family`` (BUILD_SPEC §0.3), so the user can never pick a
        family that would save as un-embeddable tofu."""
        if cls._available_families is not None:
            return cls._available_families
        index = cls._build_system_index()
        names: set[str] = set(cls._BASE14_DISPLAY_FAMILIES)
        for rec in index:
            fam = (rec.family or "").strip()
            if not fam or fam.startswith("."):
                continue
            # An opaque scan-bank alias ('ScanFont-NNNNN') must NOT appear as a
            # pickable family -- it is an internal handle to a bank TTF. The face's
            # real typeface name shows on the matched box itself (display_name_for);
            # the picker lists only genuinely pickable families.
            if cls.is_scan_alias(fam):
                continue
            names.add(fam)
        cls._available_families = sorted(names, key=str.casefold)
        return cls._available_families

    @classmethod
    def available_families_now(cls) -> "list[str] | None":
        """``available_families`` WITHOUT blocking on the system scan: the
        cached list, or a cheap derivation when the index is already
        published, or ``None`` while the prewarm scan is still in flight.
        Lets the Inspector populate its family combo lazily instead of
        serializing first-window construction on the very scan the prewarm
        thread runs (perf M4a)."""
        if cls._available_families is not None:
            return cls._available_families
        if cls._system_index is None:
            return None
        return cls.available_families()

    def resolve_family(self, family: str, bold: bool, italic: bool,
                       text: str) -> ResolvedFont:
        """Resolve a USER-PICKED family to an embeddable face (BUILD_SPEC §2.2).

        NEVER returns TIER_EMBEDDED: there is no original embedded buffer to
        honor because the user chose a fresh family, and the doc's own embedded
        fonts are usually Identity-H subsets that cannot supply new glyphs. The
        two tiers this can return (SYSTEM, BASE14) are BOTH embeddable, so the
        saved output always renders.

        Algorithm:
          1. TIER_SYSTEM: if the family + style exists in the system index AND
             the on-screen ``QFont(family){bold,italic}`` exactMatch()es it (so
             the preview face matches what we embed), return a SYSTEM
             ResolvedFont whose face bytes save_as embeds via ``face_bytes``.
          2. TIER_BASE14: otherwise classify the family name (serif/mono/sans)
             + the bold/italic bits into a base-14 code -- always glyph-safe.

        Pure + deterministic + cached on (family, bold, italic, glyph_key), like
        ``resolve``, so the live editor and save_as agree."""
        glyph_key = tuple(sorted(set(text)))
        cache_key = ("FAMILY", family, bold, italic, glyph_key)
        cached = self._resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        # --- Tier 2: a concrete system face of the picked family -----------
        # Gated on THREE conditions, all required for an embeddable, faithful
        # save: (1) a face record exists in the system index; (2) the on-screen
        # QFont exactMatch()es it so the preview face == the embedded face; and
        # (3) the face's standalone bytes actually LOAD in fitz AND COVER the
        # text. (3) is the embeddability/coverage gate that mirrors how the
        # 3-tier resolve() gates Tier 1 on font_covers: a non-Latin family
        # (Arabic/Hebrew/Braille/Emoji) or a face whose round-tripped bytes
        # fitz rejects must NOT be emitted as SYSTEM tofu -- it falls to the
        # base-14 floor below, which is always glyph-safe and embeddable
        # (BUILD_SPEC §0.3).
        rec = self.system_record_for(family, bold, italic)
        if rec is not None:
            qf = QFont(family)
            qf.setBold(bold)
            qf.setItalic(italic)
            if QFontInfo(qf).exactMatch() and self._system_face_usable(
                rec, text
            ):
                rf = ResolvedFont(
                    tier=TIER_SYSTEM,
                    exact=True,
                    pdf_fontname="SYS",
                    pdf_fontbuffer=None,
                    base14_code=None,
                    qt_family=family,
                    qt_bold=bold,
                    qt_italic=italic,
                    source_name=family,
                )
                self._resolve_cache[cache_key] = rf
                return rf

        # --- Tier 3: base-14 floor, classified from the family name --------
        code = base14_code_for_family(family, bold, italic)
        qt_family = self._base14_qt_family(code)
        rf = ResolvedFont(
            tier=TIER_BASE14,
            exact=False,
            pdf_fontname=code,
            pdf_fontbuffer=None,
            base14_code=code,
            qt_family=qt_family,
            qt_bold=bold,
            qt_italic=italic,
            source_name=f"{family} ({qt_family} substitute)"
            if self.normalize(qt_family) != self.normalize(family)
            else qt_family,
        )
        self._resolve_cache[cache_key] = rf
        return rf

    # Cache keyed by (path, face_index, glyph_key) -> bool, shared across
    # instances: face bytes are immutable on disk, so usability for a given
    # glyph set never changes within a process.
    _face_usable_cache: dict[tuple, bool] = {}

    @classmethod
    def _system_face_usable(cls, rec: "_FaceRecord", text: str) -> bool:
        """True iff ``rec``'s standalone face bytes LOAD in fitz AND cover every
        glyph of ``text``. The embeddability + coverage gate for a user-PICKED
        SYSTEM family (BUILD_SPEC §0.3): a non-Latin family or a face whose
        round-tripped bytes fitz cannot parse fails here so the picker falls to
        the base-14 floor rather than saving tofu / an unloadable font."""
        glyph_key = tuple(sorted(set(text)))
        key = (rec.path, rec.face_index, glyph_key)
        cached = cls._face_usable_cache.get(key)
        if cached is not None:
            return cached
        ok = False
        try:
            buffer = face_bytes(rec.path, rec.face_index)
            font = fitz.Font(fontbuffer=buffer)
            ok = cls.font_covers(font, text)
        except Exception:
            ok = False
        cls._face_usable_cache[key] = ok
        return ok

    def fitz_font_for(self, rf: "ResolvedFont") -> "fitz.Font":
        """The exact ``fitz.Font`` the save path draws with for ``rf``
        (BUILD_SPEC §2.3). Promotes the helper both test files inlined so the
        model can build the metrics font for ``add_box`` / ``resize_box``
        (BUILD_SPEC §1.6) without re-implementing it, keeping metrics ==
        saved-glyph metrics.

          EMBEDDED -> fitz.Font(fontbuffer=rf.pdf_fontbuffer)
          SYSTEM   -> fitz.Font(fontbuffer=face_bytes(rec.path, rec.face_index))
          BASE14   -> fitz.Font(rf.base14_code)
        """
        if rf.tier == TIER_EMBEDDED:
            try:
                return fitz.Font(fontbuffer=rf.pdf_fontbuffer)
            except Exception:
                # Damaged embedded buffer: fall to the base-14 floor so
                # metrics/bake stay glyph-safe.
                return fitz.Font(rf.base14_code or "helv")
        if rf.tier == TIER_SYSTEM:
            rec = self.system_record_for(rf.qt_family, rf.qt_bold, rf.qt_italic)
            if rec is not None:
                try:
                    return fitz.Font(fontbuffer=face_bytes(rec.path, rec.face_index))
                except Exception:
                    # Unparseable system face: fall to the base-14 floor so
                    # metrics/bake stay glyph-safe.
                    return fitz.Font(rf.base14_code or "helv")
            # No concrete face despite a SYSTEM tier (defensive): fall to the
            # base-14 floor so metrics stay glyph-safe.
            return fitz.Font(rf.base14_code or "helv")
        return fitz.Font(rf.base14_code or rf.pdf_fontname or "helv")

    # =====================================================================
    # internal helpers
    # =====================================================================
    @staticmethod
    @lru_cache(maxsize=None)
    def normalize(name: str) -> str:
        """Strip a subset tag, drop [space - _ ,], lowercase. Cached: callers
        (system_record_for, embedded_xref) re-normalize the same ~800 font names
        every save; the key space is the doc's + system's font names (bounded)."""
        name = re.sub(r"^[A-Z]{6}\+", "", name or "")
        return re.sub(r"[\s\-_,]", "", name).lower()

    @classmethod
    @lru_cache(maxsize=None)
    def _family_norm(cls, name: str) -> str:
        """Normalize to a BARE family: strip subset tag, PostScript artifacts
        (PSMT/MT/PS) and trailing style/weight words, lowercase, punctuation
        removed. Symmetric for span names and basefonts, so e.g. both
        'TimesNewRomanPSMT' and 'Times New Roman Regular' collapse to the same
        key (verified across every fixture)."""
        s = cls.normalize(name)
        # Strip trailing style/weightless words AND PostScript artifacts (PSMT/MT/PS)
        # in ONE loop. The artifact strip must run AFTER each style strip: a bold PS
        # span (TimesNewRomanPS-BoldMT) needs "Bold" removed first to expose the "PS"
        # tail, else it keeps a "ps" a plain basefont (TimesNewRomanPSMT) does not, and
        # the two fail to converge. "Roman" comes off both sides via the shared loop.
        changed = True
        while changed:
            changed = False
            s2 = _PS_ARTIFACT_RE.sub("", s)
            if s2 != s:
                s, changed = s2, True
                continue
            for w in _STYLE_SUFFIXES + _WEIGHTLESS_SUFFIXES:
                if s.endswith(w) and len(s) > len(w):
                    s = s[: -len(w)]
                    changed = True
                    break
        return s

    @classmethod
    def _style_agreement(cls, basefont: str, flags: int) -> int:
        """Score how well a basefont's weight/style tokens agree with flags."""
        s = cls.normalize(basefont)
        want_bold = bool(flags & FLAG_BOLD)
        want_italic = bool(flags & FLAG_ITALIC)
        has_bold = "bold" in s or "heavy" in s or "black" in s
        has_italic = "italic" in s or "oblique" in s
        score = 0
        score += 1 if has_bold == want_bold else -1
        score += 1 if has_italic == want_italic else -1
        return score

    @staticmethod
    def _strip_name(span_font: str) -> str:
        """Human-friendly display name: drop the subset tag, keep the rest."""
        return re.sub(r"^[A-Z]{6}\+", "", span_font or "")

    @staticmethod
    def _base14_qt_family(code: str) -> str:
        """Map a base-14 code to a Qt family for on-screen preview."""
        if code.startswith(("ti",)):       # tiro/tibo/tiit/tibi
            return "Times New Roman"
        if code.startswith(("co",)):       # cour/cobo/coit/cobi
            return "Courier New"
        return "Helvetica"                  # helv/hebo/heit/hebi

    @classmethod
    def prewarm_system_index(cls) -> "threading.Thread":
        """Kick the one-time system-index build onto a daemon thread so the
        first ``MainWindow`` paints without paying the ~150 ms scan on the GUI
        thread (perf foundation M4a). The scan is pure fontTools + os -- no Qt
        objects cross threads (``load_qt_family`` / QFontDatabase paths stay on
        the GUI thread). A caller that needs the index before the prewarm
        finishes (``available_families``, ``system_record_for``) simply blocks
        on ``_index_lock`` until the in-flight build publishes -- the same
        wait it would have paid building synchronously. Returns the thread so
        callers/tests can join it."""
        t = threading.Thread(target=cls._build_system_index,
                             name="font-index-prewarm", daemon=True)
        t.start()
        return t

    @classmethod
    def _build_system_index(cls) -> list[_FaceRecord]:
        """The cached, thread-safe entry to the system font index. Built once
        per process: double-checked locking around ``_scan_system_faces`` so
        the startup prewarm thread and any synchronous caller race safely to
        exactly ONE scan (perf foundation M4a)."""
        if cls._system_index is not None:
            return cls._system_index
        with cls._index_lock:
            if cls._system_index is None:
                cls._system_index = cls._scan_system_faces()
        return cls._system_index

    @classmethod
    def _scan_system_faces(cls) -> list[_FaceRecord]:
        """Enumerate EVERY face of every .ttc/.ttf/.otf under the system font
        dirs, reading name IDs 6, 16/1, 17/2, 4 with fontTools (lazy). Pure
        fontTools + os (thread-safe; no Qt). fitz.Font sees only TTC face 0,
        so fontTools is mandatory. Callers go through ``_build_system_index``,
        which owns the once-only caching."""
        from fontTools.ttLib import TTCollection, TTFont

        records: list[_FaceRecord] = []

        def read_face(tt: "TTFont", path: str, index: int) -> None:
            postscript, family, subfamily, full_name, bold, italic = \
                _style_from_ttfont(tt, path)
            # No usable name table -> skip (matches the old early-return).
            if not (postscript or family or subfamily or full_name):
                return
            records.append(_FaceRecord(
                path=path, face_index=index, postscript=postscript,
                family=family, subfamily=subfamily, full_name=full_name,
                is_bold=bold, is_italic=italic,
            ))

        for directory in _SYSTEM_FONT_DIRS:
            if not os.path.isdir(directory):
                continue
            for fn in sorted(os.listdir(directory)):
                low = fn.lower()
                path = os.path.join(directory, fn)
                try:
                    if low.endswith(".ttc"):
                        coll = TTCollection(path, lazy=True)
                        for i, tt in enumerate(coll.fonts):
                            read_face(tt, path, i)
                    elif low.endswith((".ttf", ".otf")):
                        tt = TTFont(path, lazy=True, fontNumber=0)
                        read_face(tt, path, 0)
                except Exception:
                    # Skip unreadable / non-font files silently.
                    continue

        return records


# --------------------------------------------------------------------------
# Standalone module helper (used by save_as)
# --------------------------------------------------------------------------
# face_bytes memo: a .ttc extraction is a full TTCollection parse + re-save
# (tens of ms) and was re-run on EVERY SYSTEM-tier fitz_font_for /
# _register_resolved call. System font files are immutable for the process
# lifetime, so the memo lives at module level alongside the class-level font
# caches (perf foundation M1c).
_face_bytes_cache: dict[tuple[str, int], bytes] = {}


def face_bytes(path: str, face_index: int) -> bytes:
    """Return the standalone bytes of ONE face from a .ttc/.ttf/.otf so a
    specific .ttc face (e.g. Helvetica-Bold = Helvetica.ttc#1) can be embedded.

    fitz.Font / insert_font take no face index, so we re-emit the single face
    with fontTools. Memoized per (path, face_index) for the process lifetime
    (system font files do not change underneath a running editor).
    """
    key = (path, face_index)
    cached = _face_bytes_cache.get(key)
    if cached is not None:
        return cached

    from fontTools.ttLib import TTCollection, TTFont

    if path.lower().endswith(".ttc"):
        tt = TTCollection(path, lazy=False).fonts[face_index]
    else:
        tt = TTFont(path, fontNumber=face_index)
    buf = io.BytesIO()
    tt.save(buf)
    _face_bytes_cache[key] = buf.getvalue()
    return _face_bytes_cache[key]
