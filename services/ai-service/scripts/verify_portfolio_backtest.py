import argparse
import asyncio
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from shared.market_calendar import MARKET_TZ

_AI_SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(_AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE_DIR))

TRADING_DAYS = 252
MONEY_TOLERANCE = 0.01
RATIO_TOLERANCE = 1e-6


@dataclass
class Txn:
    day: date
    moment: datetime
    kind: str
    ticker: str
    quantity: Decimal
    price: Decimal | None
    dividend_per_share: Decimal | None
    split_ratio: Decimal | None


_SNAPSHOT_TIME_ET = time(17, 30)


def ledger_day(moment: datetime) -> date:
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    local = aware.astimezone(MARKET_TZ)
    if local.timetz().replace(tzinfo=None) <= _SNAPSHOT_TIME_ET:
        return local.date()
    return local.date() + timedelta(days=1)


def cash_delta(txn: Txn) -> Decimal:
    q = txn.quantity
    if txn.kind == "deposit":
        return q
    if txn.kind == "withdrawal":
        return -q
    if txn.kind == "dividend" and txn.dividend_per_share is not None:
        return q * txn.dividend_per_share
    if txn.kind == "buy" and txn.price is not None:
        return -(q * txn.price)
    if txn.kind == "sell" and txn.price is not None:
        return q * txn.price
    return Decimal(0)


def position_as_of(txns: list[Txn],
                   day: date) -> tuple[Decimal, dict[str, Decimal]]:
    cash = Decimal(0)
    shares: dict[str, Decimal] = defaultdict(Decimal)
    for txn in sorted(txns, key=lambda t: t.moment):
        if txn.day > day:
            break
        cash += cash_delta(txn)
        if txn.kind in ("buy", "adjustment"):
            shares[txn.ticker] += txn.quantity
        elif txn.kind == "sell":
            shares[txn.ticker] -= txn.quantity
        elif txn.kind == "stock_split" and txn.split_ratio is not None:
            shares[txn.ticker] *= txn.split_ratio
    return cash, {t: q for t, q in shares.items() if q != 0}


def close_on_or_before(closes: dict[date, float], day: date) -> float | None:
    earlier = [d for d in closes if d <= day]
    return closes[max(earlier)] if earlier else None


def external_flow(txn: Txn, closes: dict[str, dict[date, float]]) -> float:
    if txn.kind == "deposit":
        return float(txn.quantity)
    if txn.kind == "withdrawal":
        return -float(txn.quantity)
    if txn.kind == "adjustment":
        close = close_on_or_before(closes.get(txn.ticker, {}), txn.day)
        if close is None:
            close = float(txn.price or 0)
        return float(txn.quantity) * close
    return 0.0


@dataclass
class Metrics:
    total_return: float
    ending_capital: float
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    beta: float | None
    alpha: float | None
    benchmark_return: float


def independent_metrics(
        days: list[date], values: list[float], benchmark: list[float],
        flows: dict[date, float]) -> Metrics:
    returns: list[float] = []
    for i in range(1, len(days)):
        flow = sum(a for d, a in flows.items() if days[i - 1] < d <= days[i])
        returns.append((values[i] - flow - values[i - 1]) / values[i - 1])
    curve = [values[0]]
    for r in returns:
        curve.append(curve[-1] * (1 + r))
    peak, worst = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak)
    bench_returns = [
        benchmark[i] / benchmark[i - 1] - 1 for i in range(1, len(benchmark))
    ]
    sharpe = sortino = beta = alpha = None
    if len(returns) >= 2:
        mean = statistics.fmean(returns)
        sd = statistics.stdev(returns)
        sharpe = mean / sd * math.sqrt(TRADING_DAYS) if sd else None
        downside = math.sqrt(
            statistics.fmean([min(r, 0.0)**2 for r in returns]))
        sortino = (
            mean / downside * math.sqrt(TRADING_DAYS) if downside else None)
        var_b = statistics.variance(bench_returns)
        if var_b:
            beta = statistics.covariance(returns, bench_returns) / var_b
            alpha = (
                mean - beta * statistics.fmean(bench_returns)) * TRADING_DAYS
    return Metrics(
        total_return=curve[-1] / curve[0] - 1, ending_capital=curve[-1],
        max_drawdown=worst, sharpe=sharpe, sortino=sortino, beta=beta,
        alpha=alpha, benchmark_return=benchmark[-1] / benchmark[0] - 1)


def same(a: float | None, b: float | None, *, money: bool = False) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    if money:
        return abs(a - b) <= MONEY_TOLERANCE
    return abs(a - b) <= RATIO_TOLERANCE * max(1.0, abs(a), abs(b))


def show(
        label: str, mine: float | None, theirs: float | None, *,
        money: bool = False) -> bool:
    ok = same(mine, theirs, money=money)

    def fmt(v: float | None) -> str:
        return "None" if v is None else f"{v:,.6f}"

    print(
        f"  {label:<18} independent={fmt(mine):>18}  "
        f"production={fmt(theirs):>18}  "
        f"{'OK' if ok else 'DIFFERENT'}")
    return ok


async def list_portfolios() -> None:
    from database import SessionLocal
    from sqlalchemy import text

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.id, p.name, "
                    "(SELECT count(*) FROM core.transactions t "
                    "WHERE t.portfolio_id = p.id), "
                    "(SELECT count(*) FROM core.transactions t "
                    "WHERE t.portfolio_id = p.id "
                    " AND t.transaction_type IN ('buy','sell')), "
                    "(SELECT count(*) FROM core.portfolio_snapshots s "
                    "WHERE s.portfolio_id = p.id), "
                    "(SELECT min(snapshot_date) FROM core.portfolio_snapshots "
                    "s "
                    " WHERE s.portfolio_id = p.id), "
                    "(SELECT max(snapshot_date) FROM core.portfolio_snapshots "
                    "s "
                    " WHERE s.portfolio_id = p.id) "
                    "FROM core.portfolios p ORDER BY 5 DESC"))).all()
    print(
        "portfolio id                          txns trades "
        "snaps  first      last        name")
    for pid, name, txns, trades, snaps, first, last in rows:
        print(
            f"{pid}  {txns:>4} {trades:>6} {snaps:>5}  {first}  {last}  {name}"
        )


async def load(
        portfolio: str) -> tuple[list[Txn], dict[date, tuple[float, float]]]:
    from database import SessionLocal
    from sqlalchemy import text

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT transaction_date, transaction_type, "
                    "ticker, quantity, price, "
                    "dividend_per_share, split_ratio FROM core.transactions "
                    "WHERE portfolio_id = :p ORDER BY transaction_date"),
                {"p": portfolio})).all()
        snaps = (
            await session.execute(
                text(
                    "SELECT snapshot_date, total_value, cash_balance "
                    "FROM core.portfolio_snapshots "
                    "WHERE portfolio_id = :p ORDER BY snapshot_date"),
                {"p": portfolio})).all()
    txns = [
        Txn(
            day=ledger_day(r[0]), moment=r[0], kind=str(r[1]).lower(),
            ticker=str(r[2] or "").upper(), quantity=Decimal(r[3] or 0),
            price=r[4], dividend_per_share=r[5], split_ratio=r[6])
        for r in rows
    ]
    return txns, {r[0]: (float(r[1]), float(r[2])) for r in snaps}


async def closes_for(tickers: set[str], start: date,
                     end: date) -> dict[str, dict[date, float]]:
    from ai.providers.registry import get_market_data_provider

    provider = get_market_data_provider()
    out: dict[str, dict[date, float]] = {}
    for ticker in sorted(tickers):
        bars = await provider.get_price_history_range(
            ticker, start=start - timedelta(days=10),
            end=end + timedelta(days=1))
        out[ticker] = {
            b.timestamp.date(): float(b.close)
            for b in bars if math.isfinite(float(b.close))
        }
    return out


async def run_production(
        portfolio: str, start: date, end: date, benchmark: str, initial: float,
        keep: bool) -> dict[str, Any]:
    import ai.workflows.backtest_workflow as workflow
    from database import SessionLocal
    from sqlalchemy import text
    from domains.backtest.repository import BacktestRepository
    from domains.backtest.schema import PORTFOLIO_SNAPSHOT_STRATEGY

    async def _no_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    setattr(workflow, "publish_backtest_finished", _no_event)
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).create(
            user_id=None, strategy_name=PORTFOLIO_SNAPSHOT_STRATEGY,
            benchmark_symbol=benchmark, start_date=start, end_date=end,
            initial_capital=initial)
        await session.commit()
        backtest_id = backtest.id
    await workflow.run_portfolio_backtest_workflow(
        str(backtest_id), portfolio_id=portfolio)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT b.status, b.ending_capital, "
                    "b.strategy_return, b.benchmark_return, "
                    "m.sharpe_ratio, m.sortino_ratio, m.drawdown, r.alpha, "
                    "r.beta "
                    "FROM ai.backtests b LEFT JOIN "
                    "ai.backtest_metrics m ON m.backtest_id = b.id "
                    "LEFT JOIN ai.benchmark_results r ON "
                    "r.backtest_id = b.id WHERE b.id = :b"),
                {"b": backtest_id})).one()
        if not keep:
            for table in ("benchmark_results", "backtest_metrics"):
                await session.execute(
                    text(f"DELETE FROM ai.{table} WHERE backtest_id = :b"),
                    {"b": backtest_id})
            await session.execute(
                text("DELETE FROM ai.backtests WHERE id = :b"),
                {"b": backtest_id})
            await session.commit()
    keys = (
        "status", "ending_capital", "strategy_return", "benchmark_return",
        "sharpe", "sortino", "drawdown", "alpha", "beta")
    result: dict[str, Any] = {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in zip(keys, row, strict=True)
    }
    result["id"] = str(backtest_id)
    result["kept"] = keep
    return result


async def verify(args: argparse.Namespace) -> None:
    txns, snapshots = await load(args.portfolio)
    if len(snapshots) < 2:
        print(f"Only {len(snapshots)} snapshot(s); a backtest needs 2.")
        return
    start = args.start or min(snapshots)
    end = args.end or max(snapshots)
    window = {d: v for d, v in snapshots.items() if start <= d <= end}
    tickers = {
        t.ticker
        for t in txns if t.ticker and t.kind in (
            "buy", "sell", "adjustment", "stock_split", "dividend")
    }
    closes = await closes_for(tickers | {args.benchmark}, start, end)
    kinds = sorted({t.kind for t in txns})
    print(
        f"Portfolio {args.portfolio}: {len(txns)} ledger rows {kinds}, "
        f"{len(window)} snapshots {start}..{end}, tickers {sorted(tickers)}.\n"
    )

    print("1. Snapshots vs ledger rebuilt at each day's close")
    worst = 0.0
    for day in sorted(window):
        total, cash = window[day]
        my_cash, shares = position_as_of(txns, day)
        holdings = 0.0
        missing = []
        for ticker, qty in shares.items():
            close = close_on_or_before(closes.get(ticker, {}), day)
            if close is None:
                missing.append(ticker)
                continue
            holdings += float(qty) * close
        mine = float(my_cash) + holdings
        diff = total - mine
        worst = max(worst, abs(diff))
        flag = (
            "" if abs(diff) <= 1.0
            and abs(float(my_cash) - cash) <= MONEY_TOLERANCE else "  <--")
        print(
            f"  {day}  snapshot={total:>12,.2f}  "
            f"rebuilt={mine:>12,.2f}  diff={diff:>9,.2f}"
            f"  cash snapshot={cash:>11,.2f} ledger={float(my_cash):>11,.2f}"
            f"{'  no price: ' + ','.join(missing) if missing else ''}{flag}")
    print(
        f"  largest total difference: {worst:,.2f} (a few dollars can come "
        f"from a "
        "price taken minutes before the official close)\n")

    bench = closes.get(args.benchmark, {})
    days = sorted(d for d in window if d in bench)
    if len(days) < 2:
        print(
            f"Fewer than 2 days with both a snapshot and a {args.benchmark} "
            f"close.")
        return
    flows: dict[date, float] = defaultdict(float)
    for txn in txns:
        if start <= txn.day <= end:
            amount = external_flow(txn, closes)
            if amount:
                flows[txn.day] += amount
    mine_m = independent_metrics(
        days, [window[d][0] for d in days], [bench[d] for d in days], flows)
    shown_flows = {d.isoformat(): v for d, v in sorted(flows.items())}
    print(
        f"2. Independent time-weighted metrics over {len(days)} aligned days "
        f"({days[0]}..{days[-1]}), external flows {shown_flows}")
    for label, value in vars(mine_m).items():
        print(f"  {label:<18} {value}")

    if not args.run:
        print("\n3. Production backtest: skipped (add --run to compare).")
        return
    prod = await run_production(
        args.portfolio, start, end, args.benchmark, window[min(window)][0],
        args.keep)
    print(
        f"\n3. Production backtest {prod['id']} status={prod['status']} "
        f"({'kept' if prod['kept'] else 'deleted after reading'})")
    checks = [
        show("total return", mine_m.total_return, prod["strategy_return"]),
        show(
            "ending capital", mine_m.ending_capital, prod["ending_capital"],
            money=True),
        show("max drawdown", mine_m.max_drawdown, prod["drawdown"]),
        show("sharpe", mine_m.sharpe, prod["sharpe"]),
        show("sortino", mine_m.sortino, prod["sortino"]),
        show("beta", mine_m.beta, prod["beta"]),
        show("alpha", mine_m.alpha, prod["alpha"]),
        show(
            "benchmark return", mine_m.benchmark_return,
            prod["benchmark_return"])
    ]
    print(
        f"\n{sum(checks)}/{len(checks)} production numbers "
        f"match. Send this output to Claude.")


DESCRIPTION = "Owner-run check: is a portfolio backtest's output right?"


def main() -> None:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--portfolio")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.list or not args.portfolio:
        asyncio.run(list_portfolios())
        return
    UUID(args.portfolio)
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
