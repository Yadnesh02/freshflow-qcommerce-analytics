# ADR-005 — The demo slice ships as a Release asset, not in git

**Status:** Accepted

## Context

Streamlit Community Cloud gives the app **1 GB of RAM**. The full warehouse is roughly 1.5 GB, so
the deployed app has to read a smaller slice: five stores, ninety days, marts only, under 80 MB.

That file has to reach the container somehow. Three options: commit it, use Git LFS, or publish it
as a GitHub Release asset and fetch it at startup.

## Decision

Publish it as a Release asset under a fixed `demo-data` tag, pin its sha256 in
`serving/demo/manifest.json`, and have `serving/demo_data.py` fetch and verify it on boot.

## Consequences

- **Committing it was rejected because DuckDB files are not byte-stable** (ADR-002): even a no-op
  rebuild produces a different file, so every rebuild would add a permanent ~69 MB blob to history.
  Git history only grows.
- **Git LFS was rejected because it resolves unreliably on Streamlit Cloud.** A pointer file
  reaching DuckDB fails as a corrupt database at container startup — the worst possible place to
  discover it.
- The manifest pins the hash, so the deployed app either reads the exact build that was published
  or refuses to start. `serving/demo_data.py` raises on a mismatch rather than serving whatever it
  found.
- **Publishing is two steps that must move together**, and the workflow enforces it: replacing the
  Release asset without pushing the manifest that pins it leaves the container pointed at a file
  whose hash does not match, and the app survives only while its local cache is warm. That is why
  `warehouse.yml`'s publish input defaults to false and its concurrency group is not
  `cancel-in-progress` — a half-finished publish is the exact state that breaks the deployment.
- A test asserts the **published** asset is under 80 MB, not the local build. The two can differ,
  and only one of them is what the app downloads.
- Every number on the slice equals the same number computed on the full warehouse restricted the
  same way, and a test asserts exactly that — otherwise the live demo would be a different project.
