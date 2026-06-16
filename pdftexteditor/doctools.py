"""Qt-free helpers for the page & document tools workstream (doc-tools §1).

Pure stdlib + arithmetic so everything here is headless-testable without a
QApplication: aggregation/formatting for the Properties dialog, plus the
stamps milestone's watermark/header-footer geometry (9-grid presets, the
rotation-centering start point) and page-number token substitution. The model
(``document.py``) and the dialogs both import from here, so a value formats
the same in the chrome and in tests.
"""

from __future__ import annotations

import math

_PT_PER_INCH = 72.0

# The 9-grid watermark position presets (doc-tools §2.4), reading order. The
# Watermark dialog's combo shows exactly these names and the model validates
# against them, so chrome and model can never disagree on a preset.
GRID_POSITIONS = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)

# Header/footer slot names (doc-tools §2.5): two bands x three alignments.
# Dict-key contract between the dialog options and ``add_header_footer``.
HF_SLOTS = (
    "header_left", "header_center", "header_right",
    "footer_left", "footer_center", "footer_right",
)


def human_size(n: int) -> str:
    """Format a byte count the way the chrome reports file sizes: bytes below
    1000, else one decimal in the next unit ("482 B", "1.4 KB", "2.0 MB").
    Decimal units, matching what Finder shows for the same file."""
    n = max(0, int(n))
    if n < 1000:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1000.0
        if size < 1000.0 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def unique_page_sizes(sizes) -> list[tuple[float, float]]:
    """Dedupe an iterable of ``(width_pt, height_pt)`` into first-seen order,
    rounded to 0.1 pt so float jitter (rotation math, cropbox arithmetic)
    cannot split one physical size into several Properties rows."""
    seen: list[tuple[float, float]] = []
    for w, h in sizes:
        key = (round(float(w), 1), round(float(h), 1))
        if key not in seen:
            seen.append(key)
    return seen


def format_page_size(w_pt: float, h_pt: float) -> str:
    """One Properties grid row: points plus inches, e.g.
    ``612 x 792 pt (8.50 x 11.00 in)``."""
    return (f"{w_pt:g} x {h_pt:g} pt "
            f"({w_pt / _PT_PER_INCH:.2f} x {h_pt / _PT_PER_INCH:.2f} in)")


def preset_point(width: float, height: float, position: str,
                 margin: float = 36.0) -> tuple[float, float]:
    """Anchor point for a 9-grid position preset (doc-tools §2.4), in the
    DISPLAYED page space: the page is inset by ``margin`` on every side and
    the remaining area split into a 3x3 grid; the anchor is the chosen cell's
    CENTER. ``"center"`` is therefore exactly the page center, and a corner
    preset sits a comfortable distance inside the margins (the watermark is
    centered ON the anchor, so an edge-hugging anchor would clip half the run
    off-page). Raises ``ValueError`` on an unknown preset name so the model
    can reject a bad position before taking its structural snapshot."""
    if position not in GRID_POSITIONS:
        raise ValueError(f"unknown watermark position: {position!r}")
    idx = GRID_POSITIONS.index(position)
    col, row = idx % 3, idx // 3
    usable_w = max(width - 2.0 * margin, 1.0)
    usable_h = max(height - 2.0 * margin, 1.0)
    return (margin + usable_w * (2 * col + 1) / 6.0,
            margin + usable_h * (2 * row + 1) / 6.0)


def substitute_tokens(template: str, *, page_no: int, total: int,
                      date: str) -> str:
    """Header/footer token substitution (doc-tools §2.5): ``{page}`` ->
    ``page_no``, ``{pages}`` -> ``total``, ``{date}`` -> ``date`` (every
    occurrence). Tokens are substituted AT STAMP TIME, so the result is
    static page content -- a later page reorder does not renumber (the
    dialog says so)."""
    return (template.replace("{page}", str(page_no))
                    .replace("{pages}", str(total))
                    .replace("{date}", date))


def stamp_start(anchor: tuple[float, float], run_width: float, voff: float,
                angle_deg: float) -> tuple[float, float]:
    """Baseline START point (displayed page space) so that a text run of
    ``run_width`` pt, whose glyph-box vertical center sits ``voff`` pt above
    the baseline, ends up CENTERED on ``anchor`` after rotating ``angle_deg``
    about that start point (the watermark recipe, doc-tools §2.4/§0).

    The run center, relative to the baseline start, is ``(run_width/2,
    -voff)`` in unrotated y-down display coordinates; this rotates it by the
    screen-sense rotation the morph recipe produces and subtracts. SIGN LOCK
    (probe-verified against rendered ink, both flat and /Rotate 90 pages):
    with ``morph=(p, Matrix(1,1).prerotate(angle + page.rotation))`` a
    positive angle makes the run RISE to the right on screen, and the
    matching rotation here is ``[[cos, sin], [-sin, cos]]`` -- flipping the
    sin sign mis-centers the ink by ~100 pt at 45 degrees (the M2 ink test
    locks this)."""
    rad = math.radians(angle_deg)
    ca, sa = math.cos(rad), -math.sin(rad)
    dx = (run_width / 2.0) * ca + voff * sa
    dy = (run_width / 2.0) * sa - voff * ca
    return (anchor[0] - dx, anchor[1] - dy)
