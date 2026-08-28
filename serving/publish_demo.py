"""Upload the demo warehouse to a GitHub Release and pin it in the manifest.

The counterpart to `serving/demo_data.py`: this writes what that one reads. It
talks to the REST API over stdlib `urllib` rather than shelling out to `gh`,
because the task runner is stdlib-only by design and CI already has a token in
the environment - adding a CLI dependency would buy one line and cost both.

**One tag, replaced in place.** The asset lives under a fixed `demo-data` tag,
and publishing deletes the previous asset before uploading the new one. A
versioned tag per rebuild would leave the app choosing between URLs at startup,
which is a decision it has no way to make correctly; a stable URL plus a hash
in the manifest means there is exactly one live build and the repository says
which. The tag is a container for the file, not a snapshot of the code - the
commit it points at carries no meaning.

Needs a token in `GITHUB_TOKEN` or `GH_TOKEN` with `contents: write`. In GitHub
Actions the default job token already has it.

    python tasks.py publish-demo
    python tasks.py publish-demo --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from serving.demo_data import LOCAL, MANIFEST, DemoDataError, sha256_of  # noqa: E402

TAG = "demo-data"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
API_VERSION = "2022-11-28"

# Release assets cap at 2 GB, far above anything this produces. The check that
# matters is the app's: Streamlit Cloud has 1 GB of RAM for the whole container.
SIZE_WARN_MB = 200

NOTES = (
    "Demo warehouse for the deployed Streamlit app: 5 stores x 90 days, marts only.\n\n"
    "Not a code snapshot - the tag is a stable container for this file. The build "
    "the app trusts is pinned by sha256 in `serving/demo/manifest.json`.\n\n"
    "Rebuild with `python tasks.py demo-slice`, republish with "
    "`python tasks.py publish-demo`."
)


def token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if value := os.environ.get(var):
            return value
    raise DemoDataError(
        "no GitHub token in GITHUB_TOKEN or GH_TOKEN\n"
        "  create a fine-grained token with 'Contents: read and write' on this repo:\n"
        "  https://github.com/settings/personal-access-tokens"
    )


def origin_repo() -> tuple[str, str]:
    """Owner and repo name from the origin remote, ssh or https."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise DemoDataError(f"cannot parse an owner/repo out of the origin remote: {url}")
    return match.group(1), match.group(2)


def call(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    allow_404: bool = False,
) -> dict | None:
    """One authenticated REST call. Returns the parsed body, or None on an allowed 404."""
    headers = {
        "Authorization": f"Bearer {token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "freshflow-publish-demo",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and allow_404:
            return None
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in (401, 403):
            # The read that precedes this one succeeds for anybody, because the
            # repo is public - so a 403 here is the first thing that has
            # actually tested the token, and it is nearly always one of two
            # settings rather than a wrong token.
            raise DemoDataError(
                f"{method} {url} -> HTTP {exc.code}\n"
                f"  {detail}\n\n"
                f"  The token cannot write to this repo. On a fine-grained token,\n"
                f"  at https://github.com/settings/personal-access-tokens, check both:\n"
                f"    1. Repository access -> Only select repositories -> this repo is ticked.\n"
                f"       The default is no repositories, which fails exactly like this.\n"
                f"    2. Repository permissions -> Contents -> Read and write.\n"
                f"       Releases are filed under Contents, which is not obvious.\n"
                f"  A classic token instead needs the 'repo' scope."
            ) from exc
        raise DemoDataError(f"{method} {url} -> HTTP {exc.code}\n  {detail}") from exc


def ensure_release(owner: str, repo: str) -> dict:
    """Fetch the demo-data release, creating it the first time."""
    existing = call(f"{API}/repos/{owner}/{repo}/releases/tags/{TAG}", allow_404=True)
    if existing:
        return existing
    print(f"  creating release {TAG}")
    body = json.dumps({"tag_name": TAG, "name": "Demo warehouse", "body": NOTES}).encode()
    return call(
        f"{API}/repos/{owner}/{repo}/releases",
        method="POST",
        body=body,
        content_type="application/json",
    )


def manifest_for(filename: str, url: str, digest: str, size: int) -> dict:
    built = dt.datetime.fromtimestamp(LOCAL.stat().st_mtime, tz=dt.UTC)
    return {
        "filename": filename,
        "url": url,
        "sha256": digest,
        "bytes": size,
        "built_at": built.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tag": TAG,
    }


def upload_asset(owner: str, repo: str, release_id: int, size: int) -> None:
    """Stream the file into the release. urllib needs the length up front."""
    with LOCAL.open("rb") as fh:
        request = urllib.request.Request(
            f"{UPLOADS}/repos/{owner}/{repo}/releases/{release_id}/assets?name={LOCAL.name}",
            data=fh,
            headers={
                "Authorization": f"Bearer {token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "freshflow-publish-demo",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise DemoDataError(f"upload failed with HTTP {exc.code}\n  {detail}") from exc


def publish(dry_run: bool = False) -> int:
    if not LOCAL.exists():
        raise DemoDataError(
            f"no demo warehouse at {LOCAL}\n  run `python tasks.py demo-slice` first"
        )

    size = LOCAL.stat().st_size
    mb = size / 1_048_576
    print(f"  slice:  {LOCAL.name}  {mb:.1f} MB")
    if mb > SIZE_WARN_MB:
        print(f"  \033[33mwarning: {mb:.0f} MB is large for a 1 GB container\033[0m")

    print("  hashing...", flush=True)
    digest = sha256_of(LOCAL)
    print(f"  sha256: {digest}")

    owner, repo = origin_repo()
    url = f"https://github.com/{owner}/{repo}/releases/download/{TAG}/{LOCAL.name}"
    manifest = manifest_for(LOCAL.name, url, digest, size)

    if dry_run:
        print(f"\n  dry run - would upload to {url}")
        print("  manifest would read:")
        print(json.dumps(manifest, indent=2))
        return 0

    release = ensure_release(owner, repo)
    for asset in release.get("assets", []):
        if asset["name"] == LOCAL.name:
            print(f"  replacing existing asset ({asset['size'] / 1_048_576:.1f} MB)")
            call(f"{API}/repos/{owner}/{repo}/releases/assets/{asset['id']}", method="DELETE")

    print(f"  uploading {mb:.1f} MB...", flush=True)
    upload_asset(owner, repo, release["id"], size)

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\n  published: {url}")
    print(f"  manifest:  {MANIFEST.relative_to(ROOT)}  <- commit this")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish the demo warehouse as a Release asset.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="hash the slice and print the manifest without uploading anything",
    )
    args = parser.parse_args()
    try:
        return publish(dry_run=args.dry_run)
    except DemoDataError as exc:
        print(f"\n\033[31m{exc}\033[0m", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
