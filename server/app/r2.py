"""Cloudflare R2 offload for the heavy release bytes.

Installers and tufup target archives are handed to users straight from R2 (whose
egress is free) instead of Railway's metered egress. The signed TUF metadata and
everything else keeps being served from the Railway volume. Every path degrades
gracefully: if R2 is not configured, or a specific file is not in R2, callers
fall back to serving the volume exactly as before -- so nothing here can break a
download or an update, it can only make it cheaper.

Config (already set per Railway environment; the server has these):
  R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET

Object layout, namespaced by channel so dev and stable never collide in one
bucket:  <channel>/installers/<file>  and  <channel>/updates/<mac|win>/targets/<file>
"""
import functools
import os
import re

_ACCOUNT = os.environ.get("R2_ACCOUNT_ID", "")
_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
_SECRET = os.environ.get("R2_SECRET_ACCESS_KEY", "")
_BUCKET = os.environ.get("R2_BUCKET", "")

PRESIGN_TTL = 300  # seconds; the download starts the instant we redirect
KEEP_VERSIONS = 3  # per prefix; older release blobs are pruned on publish


def enabled() -> bool:
    return all((_ACCOUNT, _KEY, _SECRET, _BUCKET))


@functools.lru_cache(maxsize=1)
def _client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{_ACCOUNT}.r2.cloudflarestorage.com",
        aws_access_key_id=_KEY,
        aws_secret_access_key=_SECRET,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def exists(key: str) -> bool:
    """True only if the object is really in R2. Used to decide redirect-vs-volume,
    so any error (not configured, missing, network) safely means 'serve volume'."""
    if not enabled():
        return False
    try:
        _client().head_object(Bucket=_BUCKET, Key=key)
        return True
    except Exception:
        return False


def presigned_get(key: str, ttl: int = PRESIGN_TTL) -> str:
    """A short-lived signed URL for a private-bucket GET. Pure local signing, no
    network call, so the bucket never needs public access."""
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": _BUCKET, "Key": key}, ExpiresIn=ttl)


# --- release sync (called from /api/publish) --------------------------------
# Release filenames are "<app>-<version>[...]": the first "-<n.n...>" token is the
# version. tufup archives/patches (<app>-<ver>.tar.gz / .patch) and our installers
# (<app>-<ver>.dmg / <app>-<ver>-setup.exe) all follow it.
_VER_RE = re.compile(r"-(\d+(?:\.\d+)*)")


def version_of(name: str):
    m = _VER_RE.search(name.rsplit("/", 1)[-1])
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def keys_to_prune(keys, keep: int = KEEP_VERSIONS):
    """Which of `keys` to delete so only the newest `keep` versions survive. Keys
    whose version can't be parsed (e.g. stray metadata) are always kept."""
    parsed = [(k, version_of(k)) for k in keys]
    newest = sorted({v for _, v in parsed if v is not None}, reverse=True)[:keep]
    keep_set = set(newest)
    return [k for k, v in parsed if v is not None and v not in keep_set]


def _list(prefix: str):
    keys, token = [], None
    while True:
        kw = {"Bucket": _BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = _client().list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents", [])]
        if not resp.get("IsTruncated"):
            return keys
        token = resp.get("NextContinuationToken")


def _prune(prefix: str, keep: int = KEEP_VERSIONS):
    doomed = keys_to_prune(_list(prefix), keep)
    for i in range(0, len(doomed), 1000):
        batch = [{"Key": k} for k in doomed[i:i + 1000]]
        _client().delete_objects(Bucket=_BUCKET, Delete={"Objects": batch})
    return doomed


def _upload_dir(local_dir: str, key_prefix: str):
    if not os.path.isdir(local_dir):
        return
    c = _client()
    for name in os.listdir(local_dir):
        p = os.path.join(local_dir, name)
        if os.path.isfile(p):
            c.upload_file(p, _BUCKET, f"{key_prefix}{name}")


def sync_release(channel: str, installers_dir: str, updates_dir: str,
                 platforms=("mac", "win"), keep: int = KEEP_VERSIONS):
    """Mirror the channel's installers + tufup targets into R2, then prune each
    prefix to the newest `keep` versions. Idempotent (overwriting puts, deleting
    already-gone keys is a no-op), so the two per-platform publish calls can run
    concurrently without coordination. Never touches TUF metadata -- that stays on
    the volume, so the 'metadata version only ever increases' rule is untouched.
    Best-effort: the caller swallows failures so a publish still succeeds and the
    volume keeps serving."""
    _upload_dir(installers_dir, f"{channel}/installers/")
    _prune(f"{channel}/installers/", keep)
    for plat in platforms:
        tdir = os.path.join(updates_dir, plat, "targets")
        _upload_dir(tdir, f"{channel}/updates/{plat}/targets/")
        _prune(f"{channel}/updates/{plat}/targets/", keep)


if __name__ == "__main__":  # runnable self-check for the prune/version logic
    assert version_of("PDFTextEditor-1.0.0.tar.gz") == (1, 0, 0)
    assert version_of("stable/installers/PDFTextEditor-0.9.2-setup.exe") == (0, 9, 2)
    assert version_of("PDFTextEditor-1.2.0.patch") == (1, 2, 0)
    assert version_of("release.json") is None
    ks = [f"c/installers/PDFTextEditor-0.{n}.0.dmg" for n in (1, 2, 3)] + \
         ["c/installers/PDFTextEditor-1.0.0.dmg"]
    assert keys_to_prune(ks, keep=3) == ["c/installers/PDFTextEditor-0.1.0.dmg"]
    assert keys_to_prune(["c/updates/mac/metadata/root.json"], keep=1) == []
    print("r2 self-check ok")
