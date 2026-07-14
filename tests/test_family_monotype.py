"""Regression: _family must not map 'Monotype Corsiva' (a script face) to
Courier just because the bare 'mono' hint substring-matches 'monotype'.

Real mono faces (DejaVu Sans Mono) and Courier still resolve to 'cour'.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pdftexteditor.fonts import _family


def test_monotype_not_courier():
    assert _family("Monotype Corsiva", 0) != "cour"
    assert _family("DejaVu Sans Mono", 0) == "cour"
    assert _family("Courier New", 0) == "cour"


if __name__ == "__main__":
    test_monotype_not_courier()
    print("ok")
