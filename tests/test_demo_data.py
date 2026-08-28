"""The demo warehouse resolves to a real file, or fails for a legible reason.

This is the only code path in the project that runs for the first time in
production. Everything else has been exercised locally long before it deploys;
the download has not, because locally the file is already there. So the tests
that matter here are the failure ones - a truncated transfer, a replaced asset,
a manifest written before the upload - and they run against `file://` URLs so
the real `_download` executes without a network.

    python -m pytest tests/test_demo_data.py
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

import pytest

from serving.demo_data import (
    LOCAL,
    MANIFEST,
    DemoDataError,
    _download,
    cache_dir,
    load_manifest,
    resolve,
    sha256_of,
)

SIZE_LIMIT_BYTES = 80 * 1_048_576


@pytest.fixture
def payload(tmp_path):
    """A stand-in for the published asset, served over file://."""
    source = tmp_path / "source.duckdb"
    source.write_bytes(b"duckdb-ish bytes" * 4096)
    return source, sha256_of(source), source.stat().st_size


# --------------------------------------------------------------- the manifest
def test_manifest_is_committed_and_complete():
    """The repository knows which build the app is supposed to read."""
    manifest = load_manifest()
    assert manifest["filename"].endswith(".duckdb")
    assert manifest["url"].startswith("https://github.com/")
    assert len(manifest["sha256"]) == 64
    assert manifest["bytes"] > 0


def test_manifest_stays_under_the_deployment_gate():
    """G2's 80 MB limit applies to what is published, not just what is built."""
    assert load_manifest()["bytes"] < SIZE_LIMIT_BYTES


def test_manifest_url_matches_its_tag_and_filename():
    """A hand-edited URL that no longer points at the pinned asset is a 404 at boot."""
    manifest = load_manifest()
    assert manifest["url"].endswith(f"/{manifest['tag']}/{manifest['filename']}")


@pytest.mark.parametrize("dropped", ["url", "sha256", "bytes", "filename"])
def test_incomplete_manifest_names_the_missing_key(tmp_path, dropped):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    del manifest[dropped]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DemoDataError, match=dropped):
        load_manifest(path)


def test_missing_manifest_says_what_to_run(tmp_path):
    with pytest.raises(DemoDataError, match="publish-demo"):
        load_manifest(tmp_path / "absent.json")


def test_unparseable_manifest_is_not_reported_as_missing(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(DemoDataError, match="not valid JSON"):
        load_manifest(path)


# --------------------------------------------------------------- the download
def test_download_writes_the_file_when_it_verifies(tmp_path, payload):
    source, digest, size = payload
    target = tmp_path / "out" / "demo.duckdb"

    _download(source.as_uri(), target, digest, size)

    assert target.read_bytes() == source.read_bytes()


def test_download_rejects_a_hash_that_does_not_match(tmp_path, payload):
    """A replaced asset must not be opened as though it were the pinned one."""
    source, _, size = payload
    target = tmp_path / "demo.duckdb"
    wrong = hashlib.sha256(b"a different build").hexdigest()

    with pytest.raises(DemoDataError, match="does not match the manifest"):
        _download(source.as_uri(), target, wrong, size)


def test_download_rejects_a_truncated_transfer(tmp_path, payload):
    source, digest, size = payload
    target = tmp_path / "demo.duckdb"

    with pytest.raises(DemoDataError, match="does not match the manifest"):
        _download(source.as_uri(), target, digest, size + 1)


def test_a_failed_download_leaves_nothing_behind(tmp_path, payload):
    """The next boot must not find a half-file and trust it."""
    source, digest, size = payload
    target = tmp_path / "demo.duckdb"

    with pytest.raises(DemoDataError):
        _download(source.as_uri(), target, digest, size + 1)

    assert not target.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_a_missing_asset_points_at_the_publish_step(tmp_path, monkeypatch):
    """The 404 you get from a repo whose release was never created."""

    def not_found(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://github.com/o/r/releases/download/demo-data/x.duckdb",
            404,
            "Not Found",
            {},
            None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", not_found)

    with pytest.raises(DemoDataError, match="publish-demo"):
        _download("https://github.com/o/r/x.duckdb", tmp_path / "out.duckdb", "0" * 64, 1)


def test_an_unreachable_host_is_reported_as_a_download_failure(tmp_path):
    """A local path that does not exist stands in for any transport-level failure."""
    absent = (tmp_path / "never-uploaded.duckdb").as_uri()
    with pytest.raises(DemoDataError, match="could not download"):
        _download(absent, tmp_path / "out.duckdb", "0" * 64, 1)


# --------------------------------------------------------------- resolution
def test_cache_dir_honours_the_environment(tmp_path, monkeypatch):
    """Streamlit Cloud and CI both need somewhere other than the checkout."""
    monkeypatch.setenv("FRESHFLOW_DEMO_CACHE", str(tmp_path / "elsewhere"))
    assert cache_dir() == tmp_path / "elsewhere"


def test_a_local_build_wins_over_the_published_one():
    """If you just ran demo-slice, that is the file you meant to test against."""
    if not LOCAL.exists():
        pytest.skip(f"no local slice at {LOCAL} - run `python tasks.py demo-slice`")
    assert resolve(allow_download=False) == LOCAL


def test_resolve_refuses_rather_than_guessing_when_nothing_is_available(tmp_path, monkeypatch):
    """No file and no network is an error with instructions, not a silent empty database."""
    if LOCAL.exists():
        pytest.skip("a local slice is present, so this path is unreachable here")
    monkeypatch.setenv("FRESHFLOW_DEMO_CACHE", str(tmp_path / "empty"))

    with pytest.raises(DemoDataError, match="demo-slice"):
        resolve(allow_download=False)
