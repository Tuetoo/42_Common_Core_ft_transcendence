from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from shared.enums import TransactionType
from shared.market_calendar import MARKET_TZ

SNAPSHOT_TIME_ET = time(17, 30)

_TOLERANCE = Decimal("1e-9")


class LedgerRow(Protocol):

    @property
    def ticker(self) -> str:
        ...

    @property
    def transaction_type(self) -> str:
        ...

    @property
    def quantity(self) -> Decimal | None:
        ...

    @property
    def price(self) -> Decimal | None:
        ...

    @property
    def dividend_per_share(self) -> Decimal | None:
        ...

    @property
    def split_ratio(self) -> Decimal | None:
        ...

    @property
    def transaction_date(self) -> datetime:
        ...


def amounts_equal(a: Decimal, b: Decimal) -> bool:
    scale = max(Decimal(1), abs(a), abs(b))
    return abs(a - b) <= _TOLERANCE * scale


def apply_adjustment_math(
        quantity: Decimal, average_cost: Decimal, signed_quantity: Decimal,
        price: Decimal | None) -> tuple[Decimal, Decimal]:
    if signed_quantity > 0:
        unit_cost = price if price is not None else Decimal(0)
        new_quantity = quantity + signed_quantity
        total_cost = quantity * average_cost + signed_quantity * unit_cost
        return new_quantity, total_cost / new_quantity
    return quantity + signed_quantity, average_cost


def plan_adjustments(
        current_quantity: Decimal, current_average_cost: Decimal,
        target_quantity: Decimal,
        target_average_cost: Decimal) -> list[tuple[Decimal, Decimal | None]]:
    if target_quantity <= 0:
        return [(-current_quantity, None)] if current_quantity > 0 else []
    if current_quantity <= 0:
        return [(target_quantity, target_average_cost)]
    if amounts_equal(current_average_cost, target_average_cost):
        delta = target_quantity - current_quantity
        if amounts_equal(delta, Decimal(0)):
            return []
        return [(delta, target_average_cost if delta > 0 else None)]
    return [(-current_quantity, None), (target_quantity, target_average_cost)]


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _phase(row: LedgerRow) -> int:
    kind = row.transaction_type
    if kind == TransactionType.SELL:
        return 2
    if kind == TransactionType.ADJUSTMENT and (row.quantity or 0) < 0:
        return 2
    if kind == TransactionType.STOCK_SPLIT:
        return 1
    return 0


def ledger_order(rows: Iterable[LedgerRow]) -> list[LedgerRow]:
    return sorted(rows, key=lambda r: (_utc(r.transaction_date), _phase(r)))


@dataclass
class Position:
    quantity: Decimal = Decimal(0)
    average_cost: Decimal = Decimal(0)


@dataclass
class Shortfall:
    ticker: str
    missing: Decimal


@dataclass
class ReplayResult:
    positions: dict[str, Position]
    cash: Decimal
    shortfalls: list[Shortfall] = field(default_factory=list)

    def held(self) -> dict[str, Position]:
        return {t: p for t, p in self.positions.items() if p.quantity > 0}


def signed_cash_delta(
        transaction_type: str, quantity: Decimal | None, price: Decimal | None,
        dividend_per_share: Decimal | None) -> Decimal:
    if transaction_type == TransactionType.DEPOSIT and quantity:
        return quantity
    if transaction_type == TransactionType.WITHDRAWAL and quantity:
        return -quantity
    if (transaction_type == TransactionType.DIVIDEND and quantity
            and dividend_per_share):
        return quantity * dividend_per_share
    if transaction_type == TransactionType.BUY and quantity and price:
        return -(quantity * price)
    if transaction_type == TransactionType.SELL and quantity and price:
        return quantity * price
    return Decimal(0)


def cash_delta(row: LedgerRow) -> Decimal:
    return signed_cash_delta(
        row.transaction_type, row.quantity, row.price, row.dividend_per_share)


def snapshot_cutoff(day: date) -> datetime:
    return datetime.combine(day, SNAPSHOT_TIME_ET,
                            tzinfo=MARKET_TZ).astimezone(UTC)


def snapshot_day(moment: datetime) -> date:
    local = _utc(moment).astimezone(MARKET_TZ)
    if local.timetz().replace(tzinfo=None) <= SNAPSHOT_TIME_ET:
        return local.date()
    return local.date() + timedelta(days=1)


def rows_through(rows: Iterable[LedgerRow], day: date) -> list[LedgerRow]:
    cutoff = snapshot_cutoff(day)
    return [r for r in rows if _utc(r.transaction_date) <= cutoff]


def total_shortfall(result: "ReplayResult", ticker: str) -> Decimal:
    return sum((s.missing for s in result.shortfalls if s.ticker == ticker),
               Decimal(0))


def replay_positions(rows: Iterable[LedgerRow]) -> ReplayResult:
    positions: dict[str, Position] = {}
    cash = Decimal(0)
    shortfalls: list[Shortfall] = []
    for row in ledger_order(rows):
        cash += cash_delta(row)
        kind, quantity = row.transaction_type, row.quantity
        pos = positions.setdefault(row.ticker, Position())
        if (kind == TransactionType.BUY and quantity
                and row.price) or (kind == TransactionType.ADJUSTMENT
                                   and quantity and quantity > 0):
            pos.quantity, pos.average_cost = apply_adjustment_math(
                pos.quantity, pos.average_cost, quantity or Decimal(0),
                row.price)
        elif (kind == TransactionType.SELL and quantity
              and row.price) or (kind == TransactionType.ADJUSTMENT
                                 and quantity and quantity < 0):
            out = abs(quantity or Decimal(0))
            if out > pos.quantity and not amounts_equal(out, pos.quantity):
                shortfalls.append(
                    Shortfall(ticker=row.ticker, missing=out - pos.quantity))
                pos.quantity = Decimal(0)
            elif amounts_equal(out, pos.quantity):
                pos.quantity = Decimal(0)
            else:
                pos.quantity -= out
        elif (kind == TransactionType.STOCK_SPLIT and row.split_ratio
              and pos.quantity > 0):
            pos.quantity = pos.quantity * row.split_ratio
            pos.average_cost = pos.average_cost / row.split_ratio
    return ReplayResult(positions=positions, cash=cash, shortfalls=shortfalls)
