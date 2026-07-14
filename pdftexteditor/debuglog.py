"""Full ACTION debug log for the editor.

Every significant action the app takes is appended to ONE file (default
``/tmp/pdfte_debug.log``) as a single structured line, so a bug can be pinpointed from the
trace: point at the log and the sequence of events is right there -- which box was clicked,
when the editor opened/closed, when text or style changed, and -- crucially -- whether the
MAP recomputed for that change. An action that moves characters but logs no following
``MAP remap`` is the bug.

DEV builds only -- ``main()`` calls ``set_enabled(appconfig.IS_DEV or $PDFTE_DEBUG_LOG)``,
so a stable build writes nothing (the trace can carry truncated document text, which must
never hit disk in a potentially PHI-handling build). Truncated at the start of each
session. Local-only: never sent anywhere.

Usage:
    from .debuglog import log
    log("EDITOR", "begin_edit", box=id(box), text=box.ocr_text, rm=box.render_mode)

Categories (keep them short + consistent):
    INPUT   mouse / key events            EDITOR  open/commit/cancel/focus
    TEXT    per-keystroke text changes    STYLE   bold/italic/font/size/colour
    MAP     char/space map recompute      RENDER  compose / bake / materialize
    BOX     add/delete/move/resize        MODE    editor mode changes
    OCR     recognition / reconstruct     DOC     open / save / orientation
"""
from __future__ import annotations

import os
import threading
import time

_PATH = os.environ.get("PDFTE_DEBUG_LOG", "/tmp/pdfte_debug.log")
_lock = threading.Lock()
_seq = 0
_t0 = time.monotonic()
enabled = False   # main() flips this on for DEV builds (see module docstring)


def new_session() -> None:
    """Truncate the log and write a session header (called once at app start).
    No-op when logging is disabled, so a stable build never even touches the file."""
    if not enabled:
        return
    global _seq, _t0
    with _lock:
        _seq = 0
        _t0 = time.monotonic()
        try:
            with open(_PATH, "w") as fh:
                fh.write("=== PDFTE DEBUG SESSION  %s ===\n"
                         % time.strftime("%Y-%m-%d %H:%M:%S"))
                fh.write("  seq    t(s)  CATEGORY ACTION                 fields\n")
        except Exception:
            pass


def set_enabled(on: bool) -> None:
    global enabled
    enabled = bool(on)


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "T" if v else "F"
    if isinstance(v, float):
        return "%.1f" % v
    if isinstance(v, str):
        s = v.replace("\n", "\\n").replace("\t", "\\t")
        return "'%s%s'" % (s[:60], "…" if len(s) > 60 else "")
    return str(v)[:60]


def log(category: str, action: str, **fields) -> None:
    """Append one event line. Never raises (a logging failure must not break the app)."""
    if not enabled:
        return
    try:
        global _seq
        with _lock:
            _seq += 1
            seq = _seq
            dt = time.monotonic() - _t0
        parts = "  ".join("%s=%s" % (k, _fmt(val)) for k, val in fields.items())
        line = "%5d %7.3f  %-8s %-22s %s\n" % (seq, dt, category, action, parts)
        with _lock:
            with open(_PATH, "a") as fh:
                fh.write(line)
    except Exception:
        pass
