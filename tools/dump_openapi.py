"""Dump the FastAPI OpenAPI schema to a versioned artifact.

Used by CI to detect drift: if the running app's schema differs from
docs/openapi.json, regenerate by running this script and committing
the change. The PR diff makes schema changes intentional rather than
accidental.

Usage:
    uv run python -m tools.dump_openapi          # writes to docs/openapi.json
    uv run python -m tools.dump_openapi --check  # exits non-zero on drift
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "docs" / "openapi.json"


def render() -> str:
    # The lifespan needs an api_token; supply a deterministic value so
    # the rendered schema doesn't drift on the auth secret.
    os.environ.setdefault("SANDBOX_API_TOKEN", "schema-dump-placeholder")
    os.environ.setdefault("SANDBOX_DEV_MODE", "1")

    from api.config import Settings
    from api.server import create_app

    s = Settings()
    app = create_app(s, start_reaper=False)
    schema = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the artifact differs from current schema",
    )
    args = p.parse_args()

    current = render()
    if args.check:
        if not ARTIFACT.exists():
            print(f"ERROR: {ARTIFACT} missing — run without --check to create.", file=sys.stderr)
            return 1
        existing = ARTIFACT.read_text()
        if existing != current:
            print(
                "ERROR: OpenAPI schema drift detected.\n"
                f"  Run:  uv run python -m tools.dump_openapi\n"
                f"  Then: git add {ARTIFACT.relative_to(REPO_ROOT)} && git commit",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {ARTIFACT.relative_to(REPO_ROOT)} is up to date.")
        return 0

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(current)
    print(f"wrote {ARTIFACT.relative_to(REPO_ROOT)} ({len(current)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
