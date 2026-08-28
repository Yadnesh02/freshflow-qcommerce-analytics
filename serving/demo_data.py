"""Turn the demo warehouse's Release URL back into a local file path (task S2.8b).

Streamlit Community Cloud deploys a git clone, so whatever the app reads has to
be reachable from one. The demo warehouse is ~69 MB of DuckDB, and there are
three ways to get it there. Two of them are traps.

**Committing it** is what `analytics/demo_slice.py` originally assumed, and it
works - until you rebuild. DuckDB's storage layout is not byte-stable, so a
rebuild from identical rows still produces a different file, and git stores a
fresh ~69 MB blob for it. History only grows, and the only way to shrink it
later is a rewrite that breaks every existing clone.

**Git LFS** is the usual answer to that and the wrong one here. Streamlit Cloud
resolves LFS pointers unreliably; when it fails the app receives the ~130-byte
pointer *text* in place of the database. DuckDB then reports a corrupt file, at
container startup, in production, with a message that points at the database
rather than at the download - the most expensive place and time to learn that
your deploy pipeline has a footnote. The free LFS tier is also 1 GB of
bandwidth a month, which a 69 MB file spends in fourteen cold starts.

**A Release asset** - what this module reads - is outside git entirely. The
limit is 2 GB per file, fetching is plain HTTPS with no client extension, and
the tag makes the URL stable, so the repository stays small no matter how often
the slice is rebuilt.

**Why the manifest carries a hash.** A truncated download and a corrupt
database produce the same DuckDB error, and that error names the file rather
than the transfer, so the first hour of debugging goes to the wrong layer. The
sha256 moves the failure to the moment it happens and says which one it was.
The download lands on a temporary name and is renamed only after it verifies,
so an interrupted boot leaves no half-file for the next one to trust.

Local builds win over the download: if `python tasks.py demo-slice` has just
produced a slice, that is the one you want to test against, not the last one
published.

    from serving.demo_data import resolve
    con = duckdb.connect(str(resolve()), read_only=True)
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "serving" / "demo"
MANIFEST = DEMO_DIR / "manifest.json"
LOCAL = DEMO_DIR / "freshflow_demo.duckdb"

# 1 MiB: big enough that hashing 69 MB is not syscall-bound, small enough that
# streaming never holds a meaningful fraction of Streamlit Cloud's 1 GB
CHUNK = 1 << 20

REQUIRED_KEYS = ("filename", "url", "sha256", "bytes")


class DemoDataError(RuntimeError):
    """The demo warehouse is unavailable. The message says what to run next."""


def load_manifest(path: Path | None = None) -> dict:
    """Read and validate the published-asset manifest."""
    path = path or MANIFEST
    if not path.exists():
        raise DemoDataError(
            f"no manifest at {path}\n"
            f"  run `python tasks.py publish-demo` to upload the slice and write one"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DemoDataError(f"{path} is not valid JSON: {exc}") from exc

    missing = [k for k in REQUIRED_KEYS if not manifest.get(k)]
    if missing:
        raise DemoDataError(
            f"{path} is missing {', '.join(missing)}\n"
            f"  it was probably written before the asset was published - "
            f"run `python tasks.py publish-demo`"
        )
    return manifest


def sha256_of(path: Path) -> str:
    """Hash a file without reading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def cache_dir() -> Path:
    """Where downloaded warehouses live.

    Defaults under the repository so a developer can find and delete it. On
    Streamlit Cloud the checkout is writable and disposable, which is exactly
    the lifetime the cache should have - one container, one download.
    """
    override = os.environ.get("FRESHFLOW_DEMO_CACHE")
    return Path(override) if override else DEMO_DIR / ".cache"


def _download(url: str, target: Path, expected_sha: str, expected_bytes: int) -> None:
    """Stream `url` to `target`, verifying before it takes its final name."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    written = 0

    try:
        with urllib.request.urlopen(url, timeout=60) as response, part.open("wb") as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise DemoDataError(
            f"GET {url} returned HTTP {exc.code}\n"
            f"  if this is 404 the release asset is missing - run `python tasks.py publish-demo`"
        ) from exc
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise DemoDataError(f"could not download {url}: {exc}") from exc

    actual_sha = digest.hexdigest()
    if written != expected_bytes or actual_sha != expected_sha:
        part.unlink(missing_ok=True)
        raise DemoDataError(
            f"downloaded file does not match the manifest\n"
            f"  expected {expected_bytes} bytes sha256={expected_sha}\n"
            f"  received {written} bytes sha256={actual_sha}\n"
            f"  the asset was replaced without updating serving/demo/manifest.json"
        )
    part.replace(target)


def resolve(*, allow_download: bool = True, verify_local: bool = False) -> Path:
    """Return a path to a readable demo warehouse, downloading it if needed.

    A locally built slice wins, because if you just ran `demo-slice` that is the
    file you meant. It is not hashed by default - it is expected to differ from
    the published one, and treating that as corruption would make the local
    build unusable. Pass `verify_local=True` when you specifically want to know
    whether the working copy is the published one.
    """
    if LOCAL.exists() and not verify_local:
        return LOCAL

    manifest = load_manifest()
    if LOCAL.exists() and sha256_of(LOCAL) == manifest["sha256"]:
        return LOCAL

    cached = cache_dir() / manifest["filename"]
    if cached.exists() and sha256_of(cached) == manifest["sha256"]:
        return cached

    if not allow_download:
        raise DemoDataError(
            f"no demo warehouse at {LOCAL} or {cached}, and downloading is disabled\n"
            f"  run `python tasks.py demo-slice` to build one"
        )

    _download(manifest["url"], cached, manifest["sha256"], int(manifest["bytes"]))
    return cached
