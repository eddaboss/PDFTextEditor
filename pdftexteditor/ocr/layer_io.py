"""Persist the editable OCR layer inside the saved PDF, so reopening an OCR'd
file restores the EXACT fresh-OCR edit state (scan-preserving covers, original
``ocr_text``, per-glyph font map) instead of falling back to the generic
existing-text editor (which has no cover and overlaps the scanned pixels).

The layer is stored as one JSON embedded file (``pdfte_ocr_layer``) inside the
PDF, so it travels with the document. Scan fonts are NOT stored in the blob: an
OCR box's font is a ``ScanFont-NNNNN`` bank alias that maps back to the shipped
font bank on any install (``fontbank.font_file_for``), so restore re-registers
each one from the bank -- the bytes are already on disk.

On save: ``serialize_layer`` -> ``out.embfile_add``.  On open:
``restore_layer`` reads the embedded blob, re-registers the fonts, and rebuilds
``document._new_boxes`` + ``document._pfm_cache`` exactly as a fresh OCR left
them. The caller then ``mark_clean()``s (a restored layer matches disk, so it
must NOT read as unsaved).
"""
from __future__ import annotations

import base64
import dataclasses
import json

EMB_NAME = "pdfte_ocr_layer"
_VERSION = 1

# NewBox fields that are tuples (JSON round-trips them as lists -> back to tuple).
_TUPLE_FIELDS = {"origin", "bbox", "color", "dir", "cover",
                 "edit_image_rect", "line_covers", "runs"}


def _to_tuple(v):
    return tuple(_to_tuple(x) for x in v) if isinstance(v, list) else v


def _is_ocr_box(box) -> bool:
    return bool((getattr(box, "ocr_text", "") or "").strip()
                or getattr(box, "render_mode", 0) == 3)


def serialize_layer(document) -> "bytes | None":
    """JSON bytes capturing every OCR NewBox + the per-page glyph font map, or
    None when the document carries no OCR layer."""
    boxes = []
    for box in document.new_boxes_all():
        if not _is_ocr_box(box):
            continue
        d = {}
        for f in dataclasses.fields(box):
            v = getattr(box, f.name)
            if isinstance(v, (bytes, bytearray)):
                v = {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
            d[f.name] = v
        boxes.append(d)
    if not boxes:
        return None
    pfm: dict = {}
    cache = getattr(document, "_pfm_cache", None) or {}
    for pi, fm in cache.items():
        if fm is None:
            continue
        try:
            centers = fm.centers.tolist() if hasattr(fm.centers, "tolist") \
                else [list(c) for c in fm.centers]
            pfm[str(pi)] = {
                "centers": centers,
                "groups": [None if g is None else int(g) for g in fm.groups],
                # store only the face name; the ttf path is re-derived on restore
                "group_path": {str(gid): (val[1] if val else None)
                               for gid, val in fm.group_path.items()},
            }
        except Exception:
            continue
    blob = {"version": _VERSION, "boxes": boxes, "pfm": pfm}
    return json.dumps(blob).encode("utf-8")


def read_blob(working) -> "bytes | None":
    """The embedded OCR-layer blob from a fitz document, or None."""
    try:
        if EMB_NAME in working.embfile_names():
            return working.embfile_get(EMB_NAME)
    except Exception:
        return None
    return None


def restore_layer(document) -> int:
    """Rebuild the OCR layer in ``document`` from its embedded blob (re-register
    scan fonts, repopulate ``_new_boxes`` + ``_pfm_cache``). Returns the number of
    OCR boxes restored (0 when there is no embedded layer)."""
    blob = read_blob(document.working)
    if not blob:
        return 0
    try:
        data = json.loads(bytes(blob).decode("utf-8"))
    except Exception:
        return 0
    if data.get("version") != _VERSION:
        return 0

    from ..document import NewBox
    from ..font_engine import FontEngine
    from . import fontbank, supermatch

    # Re-register every ScanFont family the boxes / font map reference, from the
    # shipped bank (the alias -> bank TTF map is stable across installs).
    fams: set = set()
    for d in data.get("boxes", []):
        fam = d.get("font_family")
        if isinstance(fam, str) and fam.startswith("ScanFont-"):
            fams.add(fam)
    for p in data.get("pfm", {}).values():
        for fam in p.get("group_path", {}).values():
            if isinstance(fam, str) and fam.startswith("ScanFont-"):
                fams.add(fam)
    fam_path: dict = {}
    for fam in fams:
        try:
            path = fontbank.font_file_for(fam)
            if path:
                with open(path, "rb") as fh:
                    FontEngine.register_custom_face(fam, fh.read())
                fam_path[fam] = path
        except Exception:
            continue

    valid = {f.name for f in dataclasses.fields(NewBox)}
    restored = 0
    for d in data.get("boxes", []):
        kw = {}
        for k, v in d.items():
            if k not in valid:
                continue
            if isinstance(v, dict) and set(v) == {"__b64__"}:
                kw[k] = base64.b64decode(v["__b64__"])
            elif k in _TUPLE_FIELDS:
                kw[k] = _to_tuple(v)
            else:
                kw[k] = v
        try:
            box = NewBox(**kw)
        except Exception:
            continue
        document._new_boxes[box.edit_key] = box
        restored += 1

    cache = getattr(document, "_pfm_cache", None)
    if cache is None:
        cache = document._pfm_cache = {}
    for pis, p in data.get("pfm", {}).items():
        try:
            gp = {}
            for gid, fam in p.get("group_path", {}).items():
                path = fam_path.get(fam)
                gp[int(gid)] = (path, fam) if path else None
            groups = [None if g is None else int(g) for g in p.get("groups", [])]
            cache[int(pis)] = supermatch.PageFontMap(
                p.get("centers", []), groups, gp)
        except Exception:
            continue
    return restored
