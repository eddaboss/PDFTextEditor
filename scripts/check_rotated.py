"""Verify the in-place scanned editor on a /Rotate 270 page (the user's case).
Builds a sideways-content + /Rotate=270 scan (renders upright, like a real fax),
OCRs it, and checks scan_edit_context engages + an edit keeps the grain.
Run: PYTHONPATH=. ~/Documents/GitHub/PDFTextEditor/.venv/bin/python scripts/check_rotated.py
"""
import os, sys, tempfile, io
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2, fitz
from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])
from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import engine as E, reconstruct as R, degrade as D
from pdftexteditor.font_engine import FontEngine
import os.path as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = P.join(ROOT, "pdftexteditor", "assets", "fonts")
MONO = P.join(FONTS, "Cousine-Regular.ttf")
LINES = ["ORDER / TELEPHONE ORDERS:",
         "Home Health Physical Therapy frequency and duration 2wk4 1wk2",
         "(x) Orders read back and verified with MD"]


def horiz_grainy():
    f = fitz.Font(fontfile=MONO); EM = 13.0
    W = 60 + max(f.text_length(t, EM) for t in LINES)
    doc = fitz.open(); pg = doc.new_page(width=W, height=170)
    tw = fitz.TextWriter(pg.rect); y = 40
    for t in LINES:
        tw.append((30, y), t, font=f, fontsize=EM); y += 40
    tw.write_text(pg, color=(0.1, 0.1, 0.1))
    pm = pg.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    return D.hard_degrade(rgb.astype(np.float32), np.array([24, 24, 30], np.float32),
                          np.array([247, 244, 240], np.float32), 0.42, np.random.RandomState(3))


def build(path, rgb, k, rot):
    content = np.ascontiguousarray(np.rot90(rgb, k))
    h, w = content.shape[:2]
    buf = io.BytesIO()
    import PIL.Image; PIL.Image.fromarray(content).save(buf, "PNG")
    out = fitz.open(); page = out.new_page(width=w * 72 / 300, height=h * 72 / 300)
    page.insert_image(page.rect, stream=buf.getvalue())
    page.set_rotation(rot)
    out.save(path); out.close()


def main():
    rgb = horiz_grainy()
    with tempfile.TemporaryDirectory() as d:
        path = P.join(d, "rot.pdf")
        # pick the content pre-rotation so the DISPLAY render comes out UPRIGHT,
        # i.e. render_page_image ~= the original horizontal rgb (a real /Rotate scan)
        chosen, best = 3, 1e9
        for k in (0, 1, 2, 3):
            build(path, rgb, k, 270)
            doc = PDFDocument(path)
            disp = doc.render_page_image(0, 300.0)
            doc.close()
            dd = cv2.resize(disp, (rgb.shape[1], rgb.shape[0]))
            diff = float(np.abs(dd.astype(int) - rgb.astype(int)).mean())
            if diff < best:
                best, chosen = diff, k
        print(f"chosen k={chosen} (display matches upright original, diff={best:.1f})")
        build(path, rgb, chosen, 270)
        doc = PDFDocument(path)
        print("page_rotation =", doc.page_rotation(0), "(want 270)  content rot90 k =", chosen)
        rgbd = doc.render_page_image(0, 300.0)
        res = R.reconstruct_page(rgbd, 300.0, E.get_engine("auto").recognize(rgbd),
                                 P.join(FONTS, "Tinos-Regular.ttf"), P.join(FONTS, "Arimo[wght].ttf"))
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        lb = next((l for l in res.lines if "Orders read back" in l.text), None)
        if lb is None:
            print("FAIL: target line not OCR'd"); return
        o, dn = doc.ocr_text_placement(0, lb.origin)
        cov = tuple(doc.ocr_cover_rect(0, lb.cover)) + tuple(lb.bg)
        box = doc.add_box(0, o, lb.text, res.family_name, lb.size, (0, 0, 0),
                          False, False, direction=dn, cover=cov, render_mode=3,
                          box_w=lb.box_w, leading=lb.leading)
        ctx = doc.scan_edit_context(box, box.text)
        print("scan_edit_context on rotated page:", "OK" if ctx else "NONE (still broken)")
        if ctx is None:
            return
        region = ctx["region"]
        # is the cropped region horizontal text? segment should find the words
        from pdftexteditor.ocr.segment import segment_line
        seg = segment_line(region, box.text)
        print("cropped region words found:", len(seg.words) if seg else 0,
              "region shape", region.shape)
        edit = doc.inplace_compose(ctx, box.text.replace("MD", "RN"))
        import fitz as _f; ff = _f.Font(fontfile=ctx["fpath"])
        pre = len(os.path.commonprefix([box.text, box.text.replace("MD", "RN")]))
        xp = max(0, int(ctx["left_px"] + ff.text_length(box.text[:pre], ctx["em"]) * ctx["ppi"]) - 2)
        def grain(a):
            return float(np.abs(a.astype(int) - cv2.medianBlur(a, 3).astype(int)).mean())
        print(f"prefix grain scan={grain(region[:, :xp]):.2f} edit={grain(edit[:, :xp]):.2f} (equal=kept)")
        cv2.imwrite("/tmp/rot_edit.png", cv2.cvtColor(edit, cv2.COLOR_RGB2BGR))
        cv2.imwrite("/tmp/rot_region.png", cv2.cvtColor(region, cv2.COLOR_RGB2BGR))
        print("saved /tmp/rot_edit.png /tmp/rot_region.png")

        # BAKE: commit the edit and render the whole page (display space) to verify
        # the edit lands on the rotated page in the right place + orientation.
        doc.stage_edit(0, box, box.text.replace("MD", "RN"))
        eb = doc._new_boxes[box.edit_key]
        print("baked edit_image built:", bool(eb.edit_image))
        z = 2.0
        pm = doc.render_with_edits(0, z)
        arr = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
        # the line in display space = cover mapped through rotation_matrix
        rot = doc.working[0].rotation_matrix
        x0, y0, x1, y1 = box.cover[:4]
        pts = [fitz.Point(px, py) * rot for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        dx0 = int(min(p.x for p in pts) * z) - 10; dy0 = int(min(p.y for p in pts) * z) - 10
        dx1 = int(max(p.x for p in pts) * z) + 10; dy1 = int(max(p.y for p in pts) * z) + 10
        crop = arr[max(0, dy0):dy1, max(0, dx0):dx1]
        cv2.imwrite("/tmp/rot_baked.png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
        print("saved /tmp/rot_baked.png (the committed page)")
        doc.close()


if __name__ == "__main__":
    main()
