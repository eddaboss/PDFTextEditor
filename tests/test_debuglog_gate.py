"""The action debug log must write NOTHING when disabled (stable builds), so a
potentially PHI-handling build never puts document text on disk. main() enables
it only for dev builds (or an explicit PDFTE_DEBUG_LOG path)."""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pdftexteditor import debuglog


def test_disabled_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        debuglog._PATH = os.path.join(d, "trace.log")

        debuglog.set_enabled(False)
        debuglog.new_session()
        debuglog.log("EDITOR", "begin_edit", text="secret patient text")
        assert not os.path.exists(debuglog._PATH), "disabled log must not touch disk"

        debuglog.set_enabled(True)
        debuglog.new_session()
        debuglog.log("EDITOR", "begin_edit", text="ok")
        assert os.path.exists(debuglog._PATH), "enabled log should write"
        body = open(debuglog._PATH).read()
        assert "begin_edit" in body

        debuglog.set_enabled(False)   # leave the module as a stable build would


if __name__ == "__main__":
    test_disabled_writes_nothing()
    print("ok: debug log is silent when disabled, writes when enabled")
