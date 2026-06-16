"""The optional, downloadable OCR pack: path/version, availability, the download
+ install round trip, and the import-safety guarantee that importing the OCR
package never eagerly pulls in the heavy (cv2 / onnxruntime / rapidocr / vtracer)
deps. The base build can therefore ship without them."""
import importlib.util
import io
import subprocess
import sys
import zipfile

from pdftexteditor.ocr import pack


def test_download_url_carries_platform_and_version():
    url = pack.download_url()
    assert "/download/ocr-pack-" in url
    assert pack.appconfig.PLATFORM in url
    assert pack.PACK_VERSION in url
    assert url.endswith(".zip")


def test_is_available_matches_importability():
    # CI installs the OCR deps, so the marker is importable and is_available True.
    assert pack.is_available() == (
        importlib.util.find_spec(pack._MARKER) is not None)


def test_download_and_install_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "pack_root", lambda: tmp_path)
    assert not pack.is_downloaded()

    # A fake pack zip whose root holds the marker package.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{pack._MARKER}/__init__.py", "")
    blob = buf.getvalue()

    class _Resp:
        headers = {"Content-Length": str(len(blob))}

        def __init__(self):
            self._b = io.BytesIO(blob)

        def read(self, n=-1):
            return self._b.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pack.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    seen = []
    pack.download_and_install(progress=lambda d, t: seen.append((d, t)))

    assert pack.is_downloaded()
    assert (pack.pack_dir() / pack._MARKER).is_dir()
    assert seen and seen[-1][0] == len(blob)  # progress ran to completion
    assert str(pack.pack_dir()) in sys.path   # put on the import path


def test_importing_ocr_does_not_eager_load_heavy_deps():
    """`import pdftexteditor.ocr` must stay cheap: the cv2 / onnxruntime /
    rapidocr / vtracer imports are deferred to the moment OCR actually runs, so
    a base build with none of them installed still imports the app."""
    code = (
        "import sys; import pdftexteditor.ocr; "
        "heavy=('cv2','onnxruntime','rapidocr_onnxruntime','vtracer'); "
        "loaded=[m for m in heavy if m in sys.modules]; "
        "print('LOADED', loaded); "
        "sys.exit(1 if loaded else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True)
    assert r.returncode == 0, f"OCR import pulled in heavy deps: {r.stdout}\n{r.stderr}"
