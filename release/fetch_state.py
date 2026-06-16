"""Download a channel's current tufup repo state (/api/repo-state) and extract one
platform's subtree (updates/<platform>/) into REPO_DIR, so the release pipeline
can add an incremental bundle on top of it. A fresh platform yields an empty
REPO_DIR (the pipeline then initializes it).

Usage: python fetch_state.py <api_base_url> <publish_token> <platform>
"""
import io
import pathlib
import sys
import tarfile

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from repo_config import REPO_DIR


def main() -> None:
    api_base = sys.argv[1].rstrip("/")
    token = sys.argv[2]
    platform = sys.argv[3]
    resp = requests.get(
        f"{api_base}/api/repo-state",
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
    )
    resp.raise_for_status()
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"updates/{platform}/"
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name.startswith(prefix):
                m.name = m.name[len(prefix):]
                if m.name:
                    tar.extract(m, str(REPO_DIR), filter="data")
    has_root = (REPO_DIR / "metadata" / "root.json").exists()
    print(f"restored {platform} repo state to {REPO_DIR} (existing: {has_root})")


if __name__ == "__main__":
    main()
