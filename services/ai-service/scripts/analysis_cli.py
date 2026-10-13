import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any
from uuid import UUID

_BASE_URL = os.environ.get("AI_INTERNAL_BASE_URL", "http://localhost:8000")
_TIMEOUT_SECONDS = 30


def _headers(has_body: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json"} if has_body else {}
    token = os.environ.get("INTERNAL_API_TOKEN", "")
    if token:
        headers["X-Internal-Token"] = token
    return headers


def _request(
        method: str, path: str, body: dict[str, Any] | None = None) -> str:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{_BASE_URL}{path}", data=data, method=method,
        headers=_headers(data is not None))
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        text: str = response.read().decode()
        return text


async def _committed(portfolio_id: str) -> str:
    from database import SessionLocal
    from domains.analysis.repository import RecommendationTierRepository

    async with SessionLocal() as session:
        holders = await RecommendationTierRepository(
            session).committed_buy_holders(
                portfolio_id=UUID(portfolio_id), exclude_ticker="")
    lines = [h.describe() for h in holders]
    total = sum((h.amount for h in holders), Decimal(0))
    lines.append(f"total committed BUY cash: {total:.2f}")
    return "\n".join(lines)


def _run(args: argparse.Namespace) -> str:
    if args.command == "committed":
        return asyncio.run(_committed(args.portfolio_id))
    if args.command == "create":
        body: dict[str, Any] = {"ticker": args.ticker}
        if args.portfolio:
            body["portfolio_id"] = args.portfolio
        if args.user:
            body["user_id"] = args.user
        return _request("POST", "/internal/analyses", body)
    if args.command == "status":
        ref = json.loads(
            _request("GET", f"/internal/analyses/{args.analysis_id}"))
        return json.dumps({"data": ref})
    return _request("GET", f"/internal/analyses/{args.analysis_id}/detail")


def _uuid_arg(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a valid UUID: {value!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analysis_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="queue a new analysis")
    create.add_argument("ticker")
    create.add_argument(
        "--portfolio", default=None, type=_uuid_arg,
        help="portfolio_id (UUID)")
    create.add_argument(
        "--user", default=None, type=_uuid_arg, help=(
            "owning user_id (UUID); without it the analysis is a system "
            "analysis"))
    committed = sub.add_parser(
        "committed", help=(
            "BUY cash held by the portfolio's open tiers, per analysis, as "
            "the position-sizing budget counts it"))
    committed.add_argument("portfolio_id", type=_uuid_arg)
    for name in ("status", "get"):
        cmd = sub.add_parser(name)
        cmd.add_argument("analysis_id")
    args = parser.parse_args(argv)

    try:
        print(_run(args))
    except urllib.error.HTTPError as exc:
        print(
            exc.read().decode(errors="replace")
            or json.dumps({"error": {
                "status": exc.code,
                "message": str(exc)
            }}))
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(json.dumps({"error": {"message": str(exc)}}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
