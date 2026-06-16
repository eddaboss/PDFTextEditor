"""Tar a staging directory -- already laid out as the server expects, with
``updates/`` (per-platform: updates/<mac|win>/{metadata,targets}) and/or
``installers/`` -- and POST it to a channel's /api/publish, which MERGES it onto
the volume. Reused by the release pipeline and the one-time bootstrap.

Usage: python upload.py <api_base_url> <publish_token> <staging_dir>
"""
import io
import os
import sys
import tarfile

import requests


def main() -> None:
    api_base = sys.argv[1].rstrip("/")
    token = sys.argv[2]
    staging = sys.argv[3]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("updates", "installers"):
            path = os.path.join(staging, name)
            if os.path.isdir(path):
                tar.add(path, arcname=name)
    buf.seek(0)
    resp = requests.post(
        f"{api_base}/api/publish",
        headers={"Authorization": f"Bearer {token}"},
        files={"bundle": ("release.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=300,
    )
    print(resp.status_code, resp.text[:300])
    resp.raise_for_status()


if __name__ == "__main__":
    main()
