"""Clipboard wire format for styled text runs (text-editing UX §4.1).

The app's clipboard payload, ``application/x-pdfte-runs``, is UTF-8 JSON:

    {"v": 1,
     "text":  str,                      # the plain text (joined run text)
     "runs":  [[text, bold, italic]],   # per-run weight/slant flags
     "style": {"family": str, "size": float, "color": [r, g, b],
               "bold": bool, "italic": bool}}

It travels NEXT TO ``text/plain`` on the system clipboard, so foreign apps
get clean plain text while a paste back into this app keeps bold/italic runs
(editor paste, §4.3) or the full uniform style (canvas paste -> styled
NewBox, §4.4).

Invariants enforced HERE so every consumer inherits them:

* ``decode_runs_mime`` never trusts external data: anything malformed yields
  ``None``; run/text content is sanitized (line breaks and tabs collapse to
  single spaces -- the model has no hard breaks, wrap owns line breaks; C0
  control characters are stripped); style fields are validated and clamped,
  falling back to Helvetica 12pt black. The decoded ``text`` is ALWAYS the
  joined run text, so the plain and rich views of one payload cannot
  disagree.
* The style-sanity rule (§4.3) -- foreign fonts/sizes/colors never enter an
  existing box -- is the CALLER's job: the editor paste applies bold/italic
  only; the decoded ``style`` exists for the canvas paste, which creates a
  brand-new box.

Module placement: under ``pdftexteditor/`` so PDFTextEditor.spec's
``collect_submodules`` auto-bundles it (packaging invariant). Stdlib only.
"""

from __future__ import annotations

import json
import math
import re

# The custom clipboard format carried beside text/plain (§4.1).
RUNS_MIME = "application/x-pdfte-runs"

# Decoded style sizes are clamped to a sane text range.
_SIZE_MIN, _SIZE_MAX = 1.0, 400.0

# A run of line/tab breaks (plus the spaces hugging it) becomes ONE space:
# the model is single-line/soft-wrapped, so pasted hard breaks are noise.
# U+2028/U+2029 are Qt's line/paragraph separators (QTextCursor.selectedText
# uses U+2029 between blocks).
_BREAK_RUN = re.compile("[ ]*[\\t\\r\\n\\f\\v\\u2028\\u2029]"
                        "[ \\t\\r\\n\\f\\v\\u2028\\u2029]*")
# Remaining C0 control chars (and DEL) are stripped outright. \t \n \v \f \r
# were already folded to spaces by _BREAK_RUN.
_C0_CTRL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


def sanitize_pasted_text(text: str) -> str:
    """Normalize foreign text for insertion into a box (§4.3b): every run of
    line breaks / tabs (with any surrounding spaces) collapses to a single
    space, and control characters are dropped. Printable content is otherwise
    untouched."""
    text = _BREAK_RUN.sub(" ", str(text))
    return _C0_CTRL.sub("", text)


def encode_runs_mime(text, runs, style) -> bytes:
    """Encode (text, runs, style) as the ``RUNS_MIME`` payload (§4.1).

    ``runs`` is an iterable of ``(text, bold, italic)``; ``style`` accepts
    either the wire key ``family`` or the model key ``font_family`` (the
    callers hold ``effective_style`` dicts). Output is UTF-8 JSON bytes ready
    for ``QMimeData.setData``."""
    style = dict(style or {})
    color = tuple(style.get("color") or (0.0, 0.0, 0.0))
    payload = {
        "v": 1,
        "text": str(text),
        "runs": [[str(t), bool(b), bool(i)] for t, b, i in (runs or ())],
        "style": {
            "family": str(style.get("family")
                          or style.get("font_family") or "Helvetica"),
            "size": float(style.get("size") or 12.0),
            "color": [float(c) for c in color[:3]],
            "bold": bool(style.get("bold", False)),
            "italic": bool(style.get("italic", False)),
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_runs_mime(data) -> dict | None:
    """Decode + SANITIZE a ``RUNS_MIME`` payload from the system clipboard.

    Returns ``{"text": str, "runs": ((text, bold, italic), ...),
    "style": {"family", "size", "color", "bold", "italic"}}`` or ``None``
    when the payload is malformed or carries no visible text. External data
    is never trusted: see the module docstring for the exact rules. When the
    payload has valid runs, ``text`` is recomputed as their join (one source
    of truth); when it has text but no usable runs, a single uniform run is
    synthesized from the style's bold/italic."""
    try:
        if isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data).decode("utf-8")
        else:
            raw = str(data)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    text = sanitize_pasted_text(text)
    style = _decode_style(payload.get("style"))
    runs = _decode_runs(payload.get("runs"))
    if runs:
        text = "".join(t for t, _, _ in runs)
    elif text:
        runs = ((text, style["bold"], style["italic"]),)
    if not text.strip():
        return None
    return {"text": text, "runs": runs, "style": style}


def _decode_runs(raw) -> tuple:
    """Validate the ``runs`` field: a list of ``[str, bool, bool]`` triples.
    Invalid items are dropped (never a crash); run text is sanitized."""
    if not isinstance(raw, (list, tuple)):
        return ()
    runs = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        t, b, i = item
        if not isinstance(t, str):
            continue
        t = sanitize_pasted_text(t)
        if not t:
            continue
        runs.append((t, bool(b), bool(i)))
    return tuple(runs)


def _is_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _decode_style(raw) -> dict:
    """Validate + clamp the ``style`` field; every invalid member falls back
    to the Helvetica 12pt black default independently."""
    out = {"family": "Helvetica", "size": 12.0,
           "color": (0.0, 0.0, 0.0), "bold": False, "italic": False}
    if not isinstance(raw, dict):
        return out
    family = raw.get("family")
    if isinstance(family, str) and family.strip():
        out["family"] = family.strip()[:128]
    size = raw.get("size")
    if _is_number(size):
        out["size"] = min(max(float(size), _SIZE_MIN), _SIZE_MAX)
    color = raw.get("color")
    if (isinstance(color, (list, tuple)) and len(color) == 3
            and all(_is_number(c) for c in color)):
        out["color"] = tuple(min(max(float(c), 0.0), 1.0) for c in color)
    out["bold"] = bool(raw.get("bold", False))
    out["italic"] = bool(raw.get("italic", False))
    return out
