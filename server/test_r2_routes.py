"""Route-level checks for the R2 offload: 302 to a signed URL when the object is
in R2, fall back to serving the volume otherwise, plus the path/platform guards.
Uses a fake R2 (no network, no creds).

Run from the ``server`` directory:  python test_r2_routes.py   (or python -m pytest)
"""
import os
import tempfile

os.environ.setdefault("PDFTE_DATA_DIR", tempfile.mkdtemp(prefix="pdfte-r2-"))
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "x-long-enough-secret-for-tests-1234567890")
os.environ.setdefault("PDFTE_CHANNEL", "stable")

from fastapi.testclient import TestClient  # noqa: E402

from app import r2  # noqa: E402
from app.config import INSTALLERS_DIR, UPDATES_DIR  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, follow_redirects=False)


def _seed_volume():
    INSTALLERS_DIR.mkdir(parents=True, exist_ok=True)
    (INSTALLERS_DIR / "PDFTextEditor-1.0.0.dmg").write_bytes(b"DMG")
    td = UPDATES_DIR / "mac" / "targets"
    td.mkdir(parents=True, exist_ok=True)
    (td / "PDFTextEditor-1.0.0.tar.gz").write_bytes(b"TGZ")


def _fake_r2(has_object: bool):
    """Pretend R2 is configured; `has_object` decides whether the file is there."""
    r2.enabled = lambda: True
    r2.exists = lambda key: has_object
    r2.presigned_get = lambda key, ttl=r2.PRESIGN_TTL: f"https://signed.example/{key}?sig=x"


def test_installer_redirects_to_r2_when_present():
    _fake_r2(True)
    resp = client.get("/download/PDFTextEditor-1.0.0.dmg")
    assert resp.status_code == 302
    assert resp.headers["location"] == \
        "https://signed.example/stable/installers/PDFTextEditor-1.0.0.dmg?sig=x"
    assert resp.headers.get("cache-control") == "no-store"


def test_target_redirects_to_r2_when_present():
    _fake_r2(True)
    resp = client.get("/updates/mac/targets/PDFTextEditor-1.0.0.tar.gz")
    assert resp.status_code == 302
    assert "stable/updates/mac/targets/PDFTextEditor-1.0.0.tar.gz" in \
        resp.headers["location"]


def test_falls_back_to_volume_when_not_in_r2():
    _seed_volume()
    _fake_r2(False)  # configured, but the object is not in R2 -> serve volume
    assert client.get("/download/PDFTextEditor-1.0.0.dmg").status_code == 200
    assert client.get("/updates/mac/targets/PDFTextEditor-1.0.0.tar.gz").status_code == 200


def test_missing_everywhere_is_404():
    _fake_r2(False)
    assert client.get("/download/nope-9.9.9.dmg").status_code == 404
    assert client.get("/updates/mac/targets/nope-9.9.9.tar.gz").status_code == 404


def test_unknown_platform_is_404():
    _fake_r2(True)
    assert client.get("/updates/linux/targets/x-1.0.0.tar.gz").status_code == 404


if __name__ == "__main__":
    test_installer_redirects_to_r2_when_present()
    test_target_redirects_to_r2_when_present()
    test_falls_back_to_volume_when_not_in_r2()
    test_missing_everywhere_is_404()
    test_unknown_platform_is_404()
    print("r2 route checks ok")
