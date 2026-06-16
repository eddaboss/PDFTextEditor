"""One-time tufup repository bootstrap: create the signing key + initial signed
metadata (including root.json, the client's trust anchor).

Run once locally. The resulting root.json is committed to the repo and bundled
into the app; the private key goes into CI secrets. Re-running is safe: tufup
only creates keys/metadata that do not already exist.
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))         # repo_config
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # pdftexteditor

from tufup.repo import Repository  # noqa: E402

from repo_config import (APP_NAME, APP_VERSION_ATTR, ENCRYPTED_KEYS,  # noqa: E402
                         EXPIRATION_DAYS, KEY_MAP, KEYS_DIR, REPO_DIR,
                         THRESHOLDS)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    repo = Repository(
        app_name=APP_NAME,
        app_version_attr=APP_VERSION_ATTR,
        repo_dir=REPO_DIR,
        keys_dir=KEYS_DIR,
        key_map=KEY_MAP,
        expiration_days=EXPIRATION_DAYS,
        encrypted_keys=ENCRYPTED_KEYS,
        thresholds=THRESHOLDS,
    )
    repo.save_config()
    repo.initialize()
    root_json = REPO_DIR / "metadata" / "root.json"
    print("initialized:", REPO_DIR)
    print("root.json exists:", root_json.exists())
    print("private key exists:", (KEYS_DIR / "pdfte").exists())
