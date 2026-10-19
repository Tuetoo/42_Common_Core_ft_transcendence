import argparse
import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID
from config import settings
from database import engine
from event_listener import delete_organization_content
from shared.deleted_users import missing_organization_ids
from shared.redis import close_redis_connections

DESCRIPTION = (
    "Operator script: clean the core content of organizations that auth\n"
    "no longer has, the same way the nightly organization sweep does.\n"
    "Use it when the sweep's safety brake refuses a real deletion.\n"
    "An id that auth still knows is never touched.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--id", dest="ids", action="append", required=True, type=UUID,
        help="organization id; repeat for several")
    parser.add_argument(
        "--confirm", action="store_true",
        help="apply the cleanup (default: preview only)")
    return parser


async def clean(ids: list[UUID], *, confirm: bool) -> int:
    missing = await missing_organization_ids(settings, ids)
    for organization_id in ids:
        state = "missing in auth" if organization_id in missing else (
            "still exists in auth, skipped")
        print(f"{organization_id}: {state}")
    if not confirm:
        print("\nPreview only. Re-run with --confirm to clean.")
        return 0
    failed = 0
    for organization_id in sorted(missing, key=str):
        try:
            removed = await delete_organization_content(
                organization_id, strict=True)
        except Exception as exc:
            failed += 1
            print(f"{organization_id}: failed ({exc})")
            continue
        print(f"{organization_id}: cleaned {removed}")
    return 1 if failed else 0


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return await clean(list(dict.fromkeys(args.ids)), confirm=args.confirm)
    finally:
        await close_redis_connections()
        await engine.dispose()


def _run() -> Any:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _run()
