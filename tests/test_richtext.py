import os, sys
os.environ["QT_QPA_PLATFORM"]="offscreen"; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QTextCursor
import numpy as np
app=QApplication([])
import fitz
from pdftexteditor.ui.main_window import MainWindow
def pump(ms):
    l=QEventLoop(); QTimer.singleShot(ms,l.quit); l.exec()

def wysiwyg(doc, page=0, zoom=2.0):
    pm=doc.render_with_edits(page, zoom)
    import tempfile
    p=tempfile.mktemp(suffix=".pdf"); doc.save_as(p)
    ps=fitz.open(p)[page].get_pixmap(matrix=fitz.Matrix(zoom,zoom))
    a=np.frombuffer(pm.samples,np.uint8).astype(int); b=np.frombuffer(ps.samples,np.uint8).astype(int)
    return (np.abs(a-b).mean()/255 if a.shape==b.shape else 999), p

# ============ 1) SINGLE LINE: bold just one word via the real UI path ============
w=MainWindow(); w._suppress_close_guard=True; w.resize(1100,820)
w.open_path("tests/fixtures/form_like.pdf"); w.show(); pump(250)
v=w.view
box=next(h.span for h in v._hotspots if "Pending" in h.span.text)
hs=next(h for h in v._hotspots if h.span is box)
v.begin_edit(hs)
ed=v._editor
# select the word "review" (text is 'Pending\xa0review'); find its range
t=ed.toPlainText()
start=t.lower().find("review")
cur=ed.textCursor(); cur.setPosition(start); cur.setPosition(start+6, QTextCursor.KeepAnchor)
ed.setTextCursor(cur)
# drive THE INSPECTOR's bold exactly like clicking B
w.inspector.styleEdited.emit({"bold": True})
pump(50)
v.commit_edit(); pump(150)
runs=w.document.staged_runs(0, box)
print("1) staged runs:", runs)
assert runs is not None and any(b for _,b,_ in runs) and any(not b for _,b,_ in runs), "mixed runs expected"
d,saved=wysiwyg(w.document)
fonts={f[3] for f in fitz.open(saved)[0].get_fonts(full=True)}
has_bold=any("bold" in f.lower() for f in fonts)
txt=fitz.open(saved)[0].get_text("text")
print(f"   WYSIWYG diff={d:.5f} | bold face in saved: {has_bold} | text intact: {'review' in txt.lower()}")
assert d < 0.02 and has_bold

# undo/redo round-trip
w.undo_stack.undo(); pump(50)
print("   after undo staged runs:", w.document.staged_runs(0, box))
assert w.document.staged_runs(0, box) is None
w.undo_stack.redo(); pump(50)
assert w.document.staged_runs(0, box) is not None
print("   undo/redo OK")

# re-open the editor: staged bold must round-trip (not flatten)
hs2=next(h for h in v._hotspots if getattr(h.span,'identity',None)==box.identity or h.span.text==box.text)
v.begin_edit(hs2)
rt=v._extract_editor_runs(v._editor)
v.cancel_edit()
mixed=any((b,i)!=(rt[0][1],rt[0][2]) for _,b,i in rt[1:]) if len(rt)>1 else False
print("   editor round-trip mixed styles:", mixed)
assert mixed

# ============ 2) PARAGRAPH: bold one word inside the body paragraph ============
w2=MainWindow(); w2._suppress_close_guard=True; w2.resize(1100,820)
w2.open_path("tests/fixtures/paragraphs.pdf"); w2.show(); pump(250)
v2=w2.view
para=next(h.span for h in v2._hotspots if getattr(h.span,'is_paragraph',False) and "quarterly" in h.span.text.lower())
hs3=next(h for h in v2._hotspots if h.span is para)
v2.begin_edit(hs3)
ed2=v2._editor
t2=ed2.toPlainText()
s2=t2.lower().find("operations")
cur2=ed2.textCursor(); cur2.setPosition(s2); cur2.setPosition(s2+10, QTextCursor.KeepAnchor)
ed2.setTextCursor(cur2)
w2.inspector.styleEdited.emit({"bold": True})
pump(50)
v2.commit_edit(); pump(150)
runs2=w2.document.staged_runs(0, para)
print("2) paragraph staged runs styles:", [(r[1],r[2]) for r in (runs2 or [])])
assert runs2 is not None
d2,saved2=wysiwyg(w2.document)
fonts2={f[3] for f in fitz.open(saved2)[0].get_fonts(full=True)}
print(f"   WYSIWYG diff={d2:.5f} | faces: {sorted(fonts2)}")
assert d2 < 0.02 and any("bold" in f.lower() for f in fonts2)
print("ALL RICH-TEXT CHECKS PASSED")
