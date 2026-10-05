import argparse
import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
import clients.ai_client as ai_client
from database import SessionLocal, engine
from sqlalchemy.ext.asyncio import AsyncSession
from domains.portfolio.ledger import LedgerRow, snapshot_day
from domains.portfolio.model import Portfolio
from domains.portfolio.repository import (PortfolioRepository,
                                          PortfolioSnapshotRepository,
                                          TransactionRepository)
from domains.portfolio.schema import CASH_TICKER
from domains.portfolio.service import positions_at_cutoff
from shared.market_calendar import is_trading_day, market_today
from shared.redis import close_redis_connections

CASH_TOLERANCE = Decimal("0.01")
DEFAULT_THRESHOLD = Decimal("1.00")
_MAX_CLOSE_GAP_DAYS = 6


@dataclass
class DayCheck:
    day: date
    stored_total: Decimal | None
    stored_cash: Decimal | None
    cash: Decimal
    holdings: Decimal | None
    missing: list[str] = field(default_factory=list)
    action: str = "ok"

    @property
    def total(self) -> Decimal | None:
        return None if self.holdings is None else self.cash + self.holdings


def session_close(closes: dict[date, Decimal], day: date) -> Decimal | None:
    if is_trading_day(day):
        return closes.get(day)
    earlier = [
        d for d in closes
        if day - timedelta(days=_MAX_CLOSE_GAP_DAYS) <= d <= day
    ]
    return closes[max(earlier)] if earlier else None


def days_to_check(
        snapshot_days: Iterable[date], start: date | None,
        end: date | None) -> list[date]:
    stored = sorted(snapshot_days)
    if not stored and (start is None or end is None):
        return []
    first = start or stored[0]
    last = end or stored[-1]
    days = {d for d in stored if first <= d <= last}
    day = first
    while day <= last:
        if is_trading_day(day):
            days.add(day)
        day += timedelta(days=1)
    return sorted(days)


def check_days(
        txns: Sequence[LedgerRow], stored: dict[date, tuple[Decimal, Decimal]],
        closes: dict[str, dict[date, Decimal]], days: Iterable[date],
        threshold: Decimal = DEFAULT_THRESHOLD) -> list[DayCheck]:
    checks: list[DayCheck] = []
    for day in days:
        positions, cash = positions_at_cutoff(txns, day)
        holdings: Decimal | None = Decimal(0)
        missing: list[str] = []
        for position in positions:
            close = session_close(closes.get(position.ticker, {}), day)
            if close is None:
                missing.append(position.ticker)
            elif holdings is not None:
                holdings += position.quantity * close
        if missing:
            holdings = None
        stored_total, stored_cash = stored.get(day, (None, None))
        check = DayCheck(
            day, stored_total, stored_cash, cash, holdings, missing)
        total = check.total
        if total is None:
            check.action = "no close"
        elif stored_total is None or stored_cash is None:
            check.action = "add"
        elif (abs(stored_total - total) > threshold
              or abs(stored_cash - cash) > CASH_TOLERANCE):
            check.action = "fix"
        checks.append(check)
    return checks


def _money(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


def format_check(check: DayCheck) -> str:
    total = check.total
    diff = (
        None if total is None or check.stored_total is None else
        check.stored_total - total)
    note = check.action.upper() if check.action != "ok" else "ok"
    if check.missing:
        note += ": " + ",".join(check.missing)
    return (
        f"  {check.day}  stored={_money(check.stored_total):>12}  "
        f"recomputed={_money(total):>12}  diff={_money(diff):>9}  "
        f"cash stored={_money(check.stored_cash):>11} "
        f"ledger={_money(check.cash):>11}  {note}")


async def fetch_closes(tickers: Iterable[str],
                       first_day: date) -> dict[str, dict[date, Decimal]]:
    today = market_today()
    lookback = max((today - first_day).days + _MAX_CLOSE_GAP_DAYS + 5, 5)
    out: dict[str, dict[date, Decimal]] = {}
    for ticker in sorted(set(tickers)):
        closes = await ai_client.get_daily_closes(
            ticker, lookback_days=min(lookback, 3650), timeout=60.0)
        out[ticker] = closes or {}
    return out


async def check_portfolio(
        session: AsyncSession, portfolio: Portfolio, *, start: date | None,
        end: date | None, threshold: Decimal) -> list[DayCheck]:
    snapshots = await PortfolioSnapshotRepository(session).list_for_portfolio(
        portfolio.id)
    stored = {
        s.snapshot_date: (s.total_value, s.cash_balance)
        for s in snapshots
    }
    days = days_to_check(stored, start, end)
    if not days:
        return []
    txns = await TransactionRepository(session).list_all_for_portfolio(
        portfolio.id)
    first_day = min((snapshot_day(t.transaction_date) for t in txns),
                    default=None)
    days = [
        d for d in days
        if d in stored or (first_day is not None and d >= first_day)
    ]
    if not days:
        return []
    tickers = {(t.ticker or "").upper()
               for t in txns if t.ticker and t.ticker.upper() != CASH_TICKER}
    closes = await fetch_closes(tickers, days[0])
    return check_days(txns, stored, closes, days, threshold)


async def apply_checks(
        session: AsyncSession, portfolio_id: UUID,
        checks: Sequence[DayCheck]) -> int:
    repo = PortfolioSnapshotRepository(session)
    written = 0
    for check in checks:
        total = check.total
        if check.action not in ("fix", "add") or total is None:
            continue
        await repo.upsert(
            portfolio_id=portfolio_id, snapshot_date=check.day,
            total_value=total, cash_balance=check.cash)
        written += 1
    await session.commit()
    return written


def _date(value: str) -> date:
    return date.fromisoformat(value)


DESCRIPTION = (
    "Operator script: recompute portfolio NAV snapshots from\n"
    "the transaction ledger and the official daily closes, and fix the wrong\n"
    "ones.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--portfolio", action="append", dest="portfolios",
        help="core.portfolios id; repeat for several")
    target.add_argument("--all", action="store_true", help="every portfolio")
    parser.add_argument("--from", dest="start", type=_date, default=None)
    parser.add_argument(
        "--to", dest="end", type=_date, default=None,
        help="last day (default: last stored snapshot); use it "
        "to add a day the market-close job skipped")
    parser.add_argument(
        "--threshold", type=Decimal, default=DEFAULT_THRESHOLD,
        help="flag a total difference above this (default 1.00)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="write the flagged and missing rows (default: preview)")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ids = [UUID(p.strip()) for p in args.portfolios or []]
    except ValueError as exc:
        print(f"Invalid id: {exc}")
        return 2

    pending = 0
    try:
        async with SessionLocal() as session:
            repo = PortfolioRepository(session)
            if args.all:
                portfolios = await repo.list_all()
            else:
                portfolios = []
                for portfolio_id in ids:
                    found = await repo.get_by_id(portfolio_id)
                    if found is None:
                        print(f"Portfolio {portfolio_id}: not found.")
                    else:
                        portfolios.append(found)
            for portfolio in portfolios:
                portfolio_id = portfolio.id
                checks = await check_portfolio(
                    session, portfolio, start=args.start, end=args.end,
                    threshold=args.threshold)
                counts = {
                    a: sum(1 for c in checks if c.action == a)
                    for a in ("ok", "fix", "add", "no close")
                }
                print(
                    f"\nPortfolio {portfolio_id} ({portfolio.name}): "
                    f"{len(checks)} day(s) checked {counts}")
                for check in checks:
                    print(format_check(check))
                to_write = counts["fix"] + counts["add"]
                if not args.confirm:
                    pending += to_write
                    continue
                if to_write:
                    written = await apply_checks(session, portfolio_id, checks)
                    print(f"  written: {written} row(s)")
            if not args.confirm:
                print(
                    f"\nPreview only: {pending} row(s) would be written. "
                    "Re-run with --confirm to write them.")
            return 0
    finally:
        await close_redis_connections()
        await engine.dispose()


def _run() -> Any:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _run()
