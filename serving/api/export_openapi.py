"""Write the OpenAPI contract to disk so it is reviewable in a diff (task S3.7).

The plan commits `serving/api/openapi.json` on purpose: it is what a front-end
author reads when the app is not running, and it is what a reviewer sees when an
endpoint changes shape. An uncommitted spec makes an API change invisible in
review; a stale one is worse, because it sends someone to build against
endpoints that have moved.

`test_the_committed_openapi_matches_the_running_app` fails when the two drift,
so this is the command that resolves it.

    python tasks.py openapi
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET = ROOT / "serving" / "api" / "openapi.json"


def main() -> int:
    from serving.api.main import app

    spec = app.openapi()
    TARGET.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths = sorted(spec["paths"])
    print(f"  wrote {TARGET.relative_to(ROOT)}  ({len(paths)} paths)")
    for path in paths:
        print(f"    {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
