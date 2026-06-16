"""Release-pipeline publisher for ONE platform.

Restores the TUF signing keys from the PDFTE_TUFUP_KEYS env (base64 tar), adds
the freshly-built app bundle to this platform's tufup repo (fetched from the
channel), signs, and uploads the new signed repo + installer onto the channel
volume (a MERGE, so the other platform is untouched). Also best-effort updates
the friendly release.json the download page reads.

Usage:
  python publish_release.py <api_base> <token> <platform> <app_dir> <installer>
"""
import base64
import io
import json
import pathlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))  # so pdftexteditor.__version__ resolves

from tufup.repo import Repository  # noqa: E402

import repo_config as cfg  # noqa: E402
from pdftexteditor import __version__ as VERSION  # noqa: E402


def _restore_keys() -> None:
    blob = os.environ.get("PDFTE_TUFUP_KEYS", "")
    if not blob:
        sys.exit("PDFTE_TUFUP_KEYS not set")
    cfg.KEYS_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob)), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name.startswith("keys/"):
                m.name = m.name[len("keys/"):]
                if m.name:
                    tar.extract(m, str(cfg.KEYS_DIR), filter="data")


def _release_json(staging: pathlib.Path, api: str, platform: str, fname: str) -> None:
    info: dict = {}
    try:
        with urllib.request.urlopen(f"{api.rstrip('/')}/api/version", timeout=20) as r:
            cur = json.loads(r.read())
            if isinstance(cur, dict):
                info = cur
    except Exception:
        pass
    info["version"] = VERSION
    info["mac" if platform == "mac" else "windows"] = fname
    out = staging / "updates" / "release.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(info))


def main() -> None:
    api, token, platform, app_dir, installer = sys.argv[1:6]
    _restore_keys()
    subprocess.run(
        [sys.executable, str(_HERE / "fetch_state.py"), api, token, platform],
        check=True)
    repo = Repository(
        app_name=cfg.APP_NAME, app_version_attr=cfg.APP_VERSION_ATTR,
        repo_dir=cfg.REPO_DIR, keys_dir=cfg.KEYS_DIR, key_map=cfg.KEY_MAP,
        expiration_days=cfg.EXPIRATION_DAYS, encrypted_keys=cfg.ENCRYPTED_KEYS,
        thresholds=cfg.THRESHOLDS)
    repo.save_config()
    # An existing volume has signed metadata to load; a fresh/empty one (the very
    # first publish, or right after a reset) must be bootstrapped. from_config()
    # would otherwise reach tufup's interactive "create directory?" prompt and die
    # with EOFError in CI. initialize() creates the metadata dir itself. BOTH
    # paths sign with the SAME restored keys, so the bundled root.json keeps
    # trusting every update across a reset.
    if (cfg.REPO_DIR / "metadata" / "root.json").exists():
        repo = Repository.from_config()
    else:
        repo.initialize()
    # Idempotent: if this version is already published for this platform, skip
    # add_bundle (it would error on the duplicate) and just refresh the installer.
    targets_dir = cfg.REPO_DIR / "targets"
    already = targets_dir.is_dir() and any(
        targets_dir.glob(f"{cfg.APP_NAME}-{VERSION}*"))
    if already:
        print(f"version {VERSION} already published here; re-uploading installer only")
    else:
        repo.add_bundle(new_bundle_dir=app_dir, custom_metadata={"version": VERSION})
        repo.publish_changes(private_key_dirs=[cfg.KEYS_DIR])

    staging = _HERE / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "updates" / platform).mkdir(parents=True)
    (staging / "installers").mkdir(parents=True)
    shutil.copytree(cfg.REPO_DIR, staging / "updates" / platform, dirs_exist_ok=True)
    fname = pathlib.Path(installer).name
    shutil.copy(installer, staging / "installers" / fname)
    _release_json(staging, api, platform, fname)

    subprocess.run(
        [sys.executable, str(_HERE / "upload.py"), api, token, str(staging)],
        check=True)
    print(f"published {platform} {VERSION} + {fname} to {api}")


if __name__ == "__main__":
    main()
