"""Tenant + token bootstrap CLI for the sandbox service.

Used by operators to create new tenants and issue their first bearer
token. Token rotation for an existing tenant goes through the API
endpoint `POST /v1/tenants/me/tokens/rotate` instead — this CLI is
just for the boot-from-zero case.

Usage:
    uv run python -m tools.sandbox_tenants create alice "Alice's team"
    uv run python -m tools.sandbox_tenants list
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from api.auth import TokenAuthenticator, generate_token_plaintext
from api.config import Settings
from api.registry import Registry


async def cmd_create(args: argparse.Namespace) -> int:
    settings = Settings()
    registry = Registry(settings.db_path)
    await registry.init()
    authn = TokenAuthenticator(settings=settings, registry=registry)

    await registry.create_tenant(args.tenant_id, args.display_name or args.tenant_id)
    plaintext = generate_token_plaintext()
    await authn.issue_initial_token(args.tenant_id, plaintext)

    print(f"tenant '{args.tenant_id}' created.")
    print()
    print("Bearer token (save this — it won't be shown again):")
    print(f"    {plaintext}")
    print()
    print("Usage from a client:")
    print(f"    curl -H 'Authorization: Bearer {plaintext}' \\")
    print("        http://127.0.0.1:8000/v1/sessions")
    return 0


async def cmd_list(args: argparse.Namespace) -> int:
    settings = Settings()
    registry = Registry(settings.db_path)
    await registry.init()

    tenants = await registry.list_tenants()
    if not tenants:
        print("(no tenants)", file=sys.stderr)
        return 0

    print(f"{'TENANT_ID':<30}  {'DISPLAY_NAME':<30}  {'CREATED'}")
    for tid, name, created_ms in tenants:
        ts = datetime.fromtimestamp(created_ms / 1000, tz=UTC).isoformat()
        active = await registry.list_active_tokens(tid)
        print(f"{tid:<30}  {name:<30}  {ts}  ({len(active)} active token(s))")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="sandbox-tenants")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create a tenant and issue a token.")
    p_create.add_argument("tenant_id")
    p_create.add_argument("display_name", nargs="?", default=None)
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="List all tenants.")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
