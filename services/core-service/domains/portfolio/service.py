from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID
import clients.ai_client as ai_client
import clients.auth_client as auth_client
import structlog
from config import settings
from deps import day_start
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from domains.portfolio.ledger import (LedgerRow, apply_adjustment_math,
                                      cash_delta, plan_adjustments,
                                      replay_positions, rows_through,
                                      snapshot_day, total_shortfall)
from domains.portfolio.model import (Holding, Portfolio, PortfolioSnapshot,
                                     Transaction, Watchlist)
from domains.portfolio.repository import (HoldingRepository,
                                          PortfolioRepository,
                                          PortfolioSnapshotRepository,
                                          TransactionRepository,
                                          WatchlistRepository)
from domains.portfolio.schema import (CASH_TICKER, HoldingAdjustmentRead,
                                      HoldingCreate, HoldingRead,
                                      HoldingUpdate, PortfolioCreate,
                                      PortfolioRead, PortfolioSnapshotRead,
                                      PortfolioUpdate, TransactionCreate,
                                      TransactionRead, WatchlistCreate,
                                      WatchlistItemCreate, WatchlistItemRead,
                                      WatchlistRead, WatchlistUpdate)
from shared.concurrency import gather_bounded
from shared.enums import Currency, TransactionType
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError, ValidationAppError)
from shared.market_calendar import market_today
from shared.schemas import PaginationParams
from shared.user_state import get_user_state

logger = structlog.get_logger(__name__)

_SHARE_MOVING_TYPES = frozenset({
    TransactionType.BUY, TransactionType.SELL, TransactionType.STOCK_SPLIT,
    TransactionType.ADJUSTMENT
})


@dataclass(frozen=True)
class MarketCloseResult:
    written: int
    skipped: int


@dataclass(frozen=True)
class MarketOpenResult:
    requested: int
    failed: int


class PositionLike(Protocol):

    @property
    def ticker(self) -> str:
        ...

    @property
    def quantity(self) -> Decimal:
        ...

    @property
    def average_cost(self) -> Decimal:
        ...


@dataclass(frozen=True)
class SnapshotPosition:
    ticker: str
    quantity: Decimal
    average_cost: Decimal


def positions_at_cutoff(txns: Iterable[LedgerRow],
                        day: date) -> tuple[list[SnapshotPosition], Decimal]:
    result = replay_positions(rows_through(txns, day))
    positions = [
        SnapshotPosition(ticker, position.quantity, position.average_cost)
        for ticker, position in sorted(result.held().items())
        if ticker != CASH_TICKER
    ]
    return positions, result.cash


def missing_session_closes(
        holdings: Sequence[PositionLike],
        closes: dict[str, Decimal | None]) -> list[str]:
    return sorted({
        h.ticker
        for h in holdings if h.quantity != 0 and closes.get(h.ticker) is None
    })


def _ledger_day(moment: datetime) -> date:
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(UTC).date()


def lowest_cash_from(
        transactions: list[Transaction], *, new_day: date,
        new_delta: Decimal) -> tuple[Decimal, date]:
    per_day: dict[date, Decimal] = {}
    for txn in transactions:
        day = _ledger_day(txn.transaction_date)
        per_day[day] = per_day.get(day, Decimal(0)) + cash_delta(txn)
    per_day[new_day] = per_day.get(new_day, Decimal(0)) + new_delta

    running = Decimal(0)
    balances: list[tuple[Decimal, date]] = []
    for day in sorted(per_day):
        running += per_day[day]
        if day >= new_day:
            balances.append((running, day))
    return min(balances, key=lambda b: b[0])


@dataclass
class HistoryRow:
    snapshot_date: date
    total_value: Decimal
    cash_balance: Decimal
    holdings_value: Decimal
    daily_return: Decimal | None


def _external_flow(txn: Transaction) -> Decimal:
    if txn.transaction_type in (TransactionType.DEPOSIT,
                                TransactionType.WITHDRAWAL):
        return cash_delta(txn)
    if (txn.transaction_type == TransactionType.ADJUSTMENT and txn.quantity
            and txn.price is not None):
        return txn.quantity * txn.price
    return Decimal(0)


class PortfolioService:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.portfolios = PortfolioRepository(session)
        self.holdings = HoldingRepository(session)
        self.transactions = TransactionRepository(session)
        self.snapshots = PortfolioSnapshotRepository(session)
        self.watchlists = WatchlistRepository(session)

    async def create(
            self, owner_id: UUID, data: PortfolioCreate) -> PortfolioRead:
        if data.organization_id is not None:
            await auth_client.ensure_organization_writer(
                owner_id, data.organization_id)
        portfolio = await self.portfolios.create(
            owner_id=owner_id, name=data.name, currency=data.currency,
            organization_id=data.organization_id, visibility=data.visibility)
        return await self._to_read(portfolio)

    async def get(
            self, portfolio_id: UUID, requester_id: UUID) -> PortfolioRead:
        portfolio = await self._require_readable(portfolio_id, requester_id)
        return await self._to_read(portfolio)

    async def list_mine(
            self, requester_id: UUID, *,
            pagination: PaginationParams) -> tuple[list[PortfolioRead], int]:
        org_ids = await auth_client.list_user_organization_ids(
            str(requester_id))
        portfolios, total = await self.portfolios.list_for_user(
            requester_id, org_ids, pagination=pagination)
        reads = await gather_bounded((self._to_read(p) for p in portfolios),
                                     limit=20)
        return reads, total

    async def update(
            self, portfolio_id: UUID, requester_id: UUID,
            data: PortfolioUpdate) -> PortfolioRead:
        portfolio = await self._require_owner(portfolio_id, requester_id)
        if data.name is not None:
            portfolio.name = data.name
        if data.visibility is not None:
            portfolio.visibility = data.visibility
        if data.auto_analyze_at_open is not None:
            portfolio.auto_analyze_at_open = data.auto_analyze_at_open
        await self._db.flush()
        await self._db.refresh(portfolio)
        return await self._to_read(portfolio)

    async def delete(
            self, portfolio_id: UUID, requester_id: UUID) -> Portfolio:
        portfolio = await self._require_owner(portfolio_id, requester_id)
        await self.portfolios.delete(portfolio)
        return portfolio

    async def list_holdings(self, portfolio_id: UUID,
                            requester_id: UUID) -> list[HoldingRead]:
        await self._require_readable(portfolio_id, requester_id)
        holdings = await self.holdings.list_for_portfolio(portfolio_id)
        return await gather_bounded(
            (self._holding_to_read(h) for h in holdings), limit=20)

    async def add_holding(
            self, portfolio_id: UUID, requester_id: UUID,
            data: HoldingCreate) -> HoldingAdjustmentRead:
        await self._require_owner(portfolio_id, requester_id)
        await self._lock_portfolio_row(portfolio_id)
        existing = await self.holdings.get_by_ticker(
            portfolio_id, data.ticker, for_update=True)
        if existing is not None:
            raise ValidationAppError(
                f"A holding for {data.ticker.upper()} already exists -- use "
                "a transaction to change its quantity instead.")
        return await self._adjust_holding_to(
            portfolio_id, data.ticker, None, data.quantity, data.average_cost)

    async def update_holding(
            self, portfolio_id: UUID, requester_id: UUID, holding_id: UUID,
            data: HoldingUpdate) -> HoldingAdjustmentRead:
        await self._require_owner(portfolio_id, requester_id)
        await self._lock_portfolio_row(portfolio_id)
        holding = await self._require_holding(portfolio_id, holding_id)
        target_quantity = (
            data.quantity if data.quantity is not None else holding.quantity)
        target_cost = (
            data.average_cost
            if data.average_cost is not None else holding.average_cost)
        return await self._adjust_holding_to(
            portfolio_id, holding.ticker, holding, target_quantity,
            target_cost)

    async def delete_holding(
            self, portfolio_id: UUID, requester_id: UUID,
            holding_id: UUID) -> HoldingAdjustmentRead:
        await self._require_owner(portfolio_id, requester_id)
        await self._lock_portfolio_row(portfolio_id)
        holding = await self._require_holding(portfolio_id, holding_id)
        return await self._adjust_holding_to(
            portfolio_id, holding.ticker, holding, Decimal(0),
            holding.average_cost)

    async def _require_holding(
            self, portfolio_id: UUID, holding_id: UUID) -> Holding:
        holding = await self.holdings.get_by_id(holding_id)
        if holding is None or holding.portfolio_id != portfolio_id:
            raise NotFoundError("Holding not found.")
        locked = await self.holdings.get_by_ticker(
            portfolio_id, holding.ticker, for_update=True)
        if locked is None:
            raise NotFoundError("Holding not found.")
        return locked

    async def _adjust_holding_to(
            self, portfolio_id: UUID, ticker: str, holding: Holding | None,
            target_quantity: Decimal,
            target_cost: Decimal) -> HoldingAdjustmentRead:
        quantity = holding.quantity if holding is not None else Decimal(0)
        cost = holding.average_cost if holding is not None else Decimal(0)
        plan = plan_adjustments(quantity, cost, target_quantity, target_cost)
        now = datetime.now(UTC)
        latest = await self.transactions.latest_date_for_ticker(
            portfolio_id, ticker)
        if latest is not None and latest >= now:
            now = latest + timedelta(microseconds=1)
        txn_ids: list[UUID] = []
        for index, (signed_quantity, price) in enumerate(plan):
            txn = await self.transactions.create(
                portfolio_id=portfolio_id, ticker=ticker,
                transaction_type=TransactionType.ADJUSTMENT,
                quantity=signed_quantity, price=price, dividend_per_share=None,
                transaction_date=now + timedelta(microseconds=index))
            txn_ids.append(txn.id)
            quantity, cost = apply_adjustment_math(
                quantity, cost, signed_quantity, price)

        if holding is None:
            if quantity <= 0:
                raise ValidationAppError(
                    "The holding quantity must be positive.")
            try:
                async with self._db.begin_nested():
                    holding = await self.holdings.create(
                        portfolio_id=portfolio_id, ticker=ticker,
                        quantity=quantity, average_cost=cost)
            except IntegrityError as exc:
                raise ConflictError(
                    f"A holding for {ticker.upper()} already exists.") from exc
            read = await self._holding_to_read(holding)
        elif quantity <= 0:
            read = await self._holding_to_read(holding)
            read.quantity = Decimal(0)
            await self.holdings.delete(holding)
        else:
            holding.quantity = quantity
            holding.average_cost = cost
            await self._db.flush()
            read = await self._holding_to_read(holding)
        if txn_ids:
            logger.info(
                "holding_adjusted", portfolio_id=str(portfolio_id),
                ticker=ticker, transactions=len(txn_ids))
        return HoldingAdjustmentRead(
            **read.model_dump(),
            transaction_id=txn_ids[-1] if txn_ids else None,
            transaction_ids=txn_ids)

    async def record_transaction(
            self, portfolio_id: UUID, requester_id: UUID,
            data: TransactionCreate, *,
            idempotency_key: str | None = None) -> TransactionRead:
        await self._require_owner(portfolio_id, requester_id)
        self._validate_required_fields(data)

        if idempotency_key is not None:
            await self._lock_portfolio_row(portfolio_id)
            existing = await self.transactions.get_by_idempotency_key(
                idempotency_key)
            if existing is not None:
                return self._idempotent_replay(
                    existing, portfolio_id, idempotency_key)

        ticker = (
            CASH_TICKER if data.transaction_type
            in (TransactionType.DEPOSIT,
                TransactionType.WITHDRAWAL) else data.ticker.upper())
        moves_shares = data.transaction_type in _SHARE_MOVING_TYPES
        if moves_shares:
            await self._lock_portfolio_row(portfolio_id)
        await self._check_current_holding(portfolio_id, ticker, data)

        if data.transaction_type in (TransactionType.BUY,
                                     TransactionType.WITHDRAWAL):
            await self._ensure_cash_stays_non_negative(portfolio_id, data)

        try:
            async with (self._db.begin_nested()
                        if idempotency_key else nullcontext()):
                txn = await self.transactions.create(
                    portfolio_id=portfolio_id, ticker=ticker,
                    transaction_type=data.transaction_type,
                    quantity=data.quantity, price=data.price,
                    dividend_per_share=data.dividend_per_share,
                    split_ratio=data.split_ratio,
                    transaction_date=data.transaction_date,
                    idempotency_key=idempotency_key)
        except IntegrityError:
            if idempotency_key is None:
                raise
            existing = await self.transactions.get_by_idempotency_key(
                idempotency_key)
            if existing is None:
                raise
            return self._idempotent_replay(
                existing, portfolio_id, idempotency_key)

        if moves_shares:
            await self._sync_holding_with_ledger(portfolio_id, ticker, txn)

        return TransactionRead.model_validate(txn)

    async def _check_current_holding(
            self, portfolio_id: UUID, ticker: str,
            data: TransactionCreate) -> None:
        kind = data.transaction_type
        if kind == TransactionType.DIVIDEND:
            if await self.holdings.get_by_ticker(portfolio_id, ticker) is None:
                raise ValidationAppError(
                    f"Cannot record a dividend for {data.ticker} -- it is not "
                    "held in this portfolio.")
            return
        if kind == TransactionType.STOCK_SPLIT:
            if (await self.holdings.get_by_ticker(portfolio_id, ticker,
                                                  for_update=True) is None):
                raise ValidationAppError(
                    f"Cannot apply a stock split/dividend for {data.ticker} "
                    f"-- "
                    "no existing holding to rescale.")
            return
        out = data.quantity or Decimal(0)
        if kind == TransactionType.ADJUSTMENT:
            out = -out
        elif kind != TransactionType.SELL:
            return
        if out <= 0:
            return
        held = await self.holdings.get_by_ticker(
            portfolio_id, ticker, for_update=True)
        if held is None or held.quantity < out:
            if kind == TransactionType.SELL:
                raise ValidationAppError(
                    f"Cannot sell {data.quantity} of {ticker} -- not enough "
                    f"held.")
            raise ValidationAppError(
                f"Cannot move out {out} of {ticker} -- not enough held.")

    async def _sync_holding_with_ledger(
            self, portfolio_id: UUID, ticker: str,
            new_row: Transaction) -> None:
        rows = [
            t for t in await self.transactions.list_all_for_portfolio(
                portfolio_id) if t.ticker.upper() == ticker
        ]
        after = replay_positions(rows)
        before = replay_positions([t for t in rows if t.id != new_row.id])
        if total_shortfall(after, ticker) > total_shortfall(before, ticker):
            when = _ledger_day(new_row.transaction_date).isoformat()
            raise ValidationAppError(
                f"Cannot record this {new_row.transaction_type} of {ticker} "
                f"dated "
                f"{when}: with it, the ledger would take out more {ticker} "
                "shares than were held at some date.")
        position = after.positions.get(ticker)
        holding = await self.holdings.get_by_ticker(
            portfolio_id, ticker, for_update=True)
        if position is None or position.quantity <= 0:
            if holding is not None:
                await self.holdings.delete(holding)
                await self._db.flush()
            return
        if holding is None:
            await self.holdings.create(
                portfolio_id=portfolio_id, ticker=ticker,
                quantity=position.quantity, average_cost=position.average_cost)
        else:
            holding.quantity = position.quantity
            holding.average_cost = position.average_cost
        await self._db.flush()

    @staticmethod
    def _idempotent_replay(
            existing: Transaction, portfolio_id: UUID,
            idempotency_key: str) -> TransactionRead:
        if existing.portfolio_id != portfolio_id:
            raise ConflictError(
                f"Idempotency key {idempotency_key!r} was already used for "
                "another portfolio.")
        logger.info(
            "transaction_idempotent_replay", portfolio_id=str(portfolio_id),
            transaction_id=str(existing.id))
        return TransactionRead.model_validate(existing)

    @staticmethod
    def _validate_required_fields(data: TransactionCreate) -> None:
        kind = data.transaction_type
        if kind in (TransactionType.BUY, TransactionType.SELL):
            if not data.quantity or not data.price:
                raise ValidationAppError(
                    f"A {kind.value} requires both quantity and price.")
        elif kind in (TransactionType.DEPOSIT, TransactionType.WITHDRAWAL):
            if not data.quantity:
                raise ValidationAppError(
                    f"A {kind.value} requires a quantity (the cash amount).")
        elif kind == TransactionType.DIVIDEND:
            if not data.quantity or not data.dividend_per_share:
                raise ValidationAppError(
                    "A dividend requires both quantity and dividend_per_share."
                )
        elif kind == TransactionType.STOCK_SPLIT:
            if not data.split_ratio:
                raise ValidationAppError(
                    "A stock split requires a split_ratio.")
            if data.split_ratio <= 0:
                raise ValidationAppError(
                    "A stock split/dividend requires a positive split_ratio "
                    "(e.g. 2.0 for a 2-for-1 split, 0.5 for a 1-for-2 reverse "
                    "split, 1.05 for a 5% stock dividend).")
        elif kind == TransactionType.ADJUSTMENT:
            if not data.quantity:
                raise ValidationAppError(
                    "An adjustment requires a non-zero quantity.")
            if data.quantity > 0 and (data.price is None or data.price < 0):
                raise ValidationAppError(
                    "An adjustment that moves shares in requires a price >= 0."
                )

    async def _lock_portfolio_row(self, portfolio_id: UUID) -> None:
        await self._db.execute(
            select(Portfolio.id).where(
                Portfolio.id == portfolio_id).with_for_update())

    async def _ensure_cash_stays_non_negative(
            self, portfolio_id: UUID, data: TransactionCreate) -> None:
        is_buy = data.transaction_type == TransactionType.BUY
        quantity = data.quantity or Decimal(0)
        amount = quantity * (data.price or Decimal(0)) if is_buy else quantity
        await self._lock_portfolio_row(portfolio_id)
        txns = await self.transactions.list_all_for_portfolio(portfolio_id)
        new_day = _ledger_day(data.transaction_date)
        lowest, lowest_day = lowest_cash_from(
            txns, new_day=new_day, new_delta=-amount)
        if lowest >= 0:
            return
        available = lowest + amount
        when = (
            f"on {new_day.isoformat()}" if lowest_day == new_day else
            f"on {new_day.isoformat()} (the balance would go negative "
            f"on {lowest_day.isoformat()})")
        logger.info(
            "transaction_rejected_insufficient_cash",
            portfolio_id=str(portfolio_id),
            transaction_type=str(data.transaction_type),
            ticker=data.ticker.upper() if is_buy else CASH_TICKER,
            required=str(amount), available=str(available),
            transaction_day=new_day.isoformat(),
            shortfall_day=lowest_day.isoformat())
        if is_buy:
            raise ValidationAppError(
                f"Insufficient cash to buy {data.quantity} "
                f"{data.ticker.upper()} "
                f"at {data.price} {when}: required {amount}, available cash "
                f"{available}. Leverage is not allowed -- record a deposit "
                f"first.")
        raise ValidationAppError(
            f"Insufficient cash to withdraw {amount} {when}: required "
            f"{amount}, "
            f"available cash {available}. Cash can never be negative.")

    async def list_transactions(
            self, portfolio_id: UUID, requester_id: UUID, *,
            pagination: PaginationParams) -> tuple[list[TransactionRead], int]:
        await self._require_readable(portfolio_id, requester_id)
        txns, total = await self.transactions.list_for_portfolio(
            portfolio_id, pagination=pagination)
        return [TransactionRead.model_validate(t) for t in txns], total

    async def create_watchlist(
            self, owner_id: UUID, data: WatchlistCreate) -> WatchlistRead:
        watchlist = await self.watchlists.create(
            owner_id=owner_id, name=data.name)
        return WatchlistRead(id=watchlist.id, name=watchlist.name, items=[])

    async def update_watchlist(
            self, watchlist_id: UUID, owner_id: UUID,
            data: WatchlistUpdate) -> WatchlistRead:
        watchlist = await self._require_watchlist_owner(watchlist_id, owner_id)
        watchlist.name = data.name
        await self._db.flush()
        items = await self.watchlists.list_items(watchlist.id)
        return WatchlistRead(
            id=watchlist.id, name=watchlist.name,
            items=[WatchlistItemRead.model_validate(i) for i in items])

    async def delete_watchlist(
            self, watchlist_id: UUID, owner_id: UUID) -> Watchlist:
        watchlist = await self._require_watchlist_owner(watchlist_id, owner_id)
        await self.watchlists.delete(watchlist)
        return watchlist

    async def get_watchlist_name(self, watchlist_id: UUID) -> str:
        watchlist = await self.watchlists.get_by_id(watchlist_id)
        return watchlist.name if watchlist is not None else ""

    async def list_my_watchlists(self, owner_id: UUID) -> list[WatchlistRead]:
        watchlists = await self.watchlists.list_for_user(owner_id)
        results = []
        for wl in watchlists:
            items = await self.watchlists.list_items(wl.id)
            results.append(
                WatchlistRead(
                    id=wl.id, name=wl.name,
                    items=[WatchlistItemRead.model_validate(i)
                           for i in items]))
        return results

    async def add_watchlist_item(
            self, watchlist_id: UUID, owner_id: UUID,
            data: WatchlistItemCreate) -> WatchlistItemRead:
        watchlist = await self._require_watchlist_owner(watchlist_id, owner_id)
        existing = await self.watchlists.get_item(watchlist.id, data.ticker)
        if existing is not None:
            raise ValidationAppError(
                "This ticker is already on the watchlist.")
        try:
            async with self._db.begin_nested():
                item = await self.watchlists.add_item(
                    watchlist_id=watchlist.id, ticker=data.ticker)
        except IntegrityError as exc:
            raise ConflictError(
                "This ticker is already on the watchlist.") from exc
        return WatchlistItemRead.model_validate(item)

    async def remove_watchlist_item(
            self, watchlist_id: UUID, owner_id: UUID, ticker: str) -> None:
        watchlist = await self._require_watchlist_owner(watchlist_id, owner_id)
        item = await self.watchlists.get_item(watchlist.id, ticker)
        if item is None:
            raise NotFoundError("This ticker is not on the watchlist.")
        await self.watchlists.remove_item(item)

    async def run_market_close_job(
            self, session_date: date | None = None) -> MarketCloseResult:
        session = session_date or market_today()
        portfolios = await self.portfolios.list_all()

        holdings_by_portfolio: dict[UUID, list[SnapshotPosition]] = {}
        cash_by_portfolio: dict[UUID, Decimal] = {}
        all_tickers: set[str] = set()
        for portfolio in portfolios:
            txns = await self.transactions.list_all_for_portfolio(portfolio.id)
            holdings, cash = positions_at_cutoff(txns, session)
            holdings_by_portfolio[portfolio.id] = holdings
            cash_by_portfolio[portfolio.id] = cash
            all_tickers.update(h.ticker for h in holdings)

        tickers = sorted(all_tickers)
        fetched = await gather_bounded(
            (ai_client.get_daily_closes(t) for t in tickers), limit=20)
        closes: dict[str, Decimal | None] = {
            ticker: (bars or {}).get(session)
            for ticker, bars in zip(tickers, fetched, strict=True)
        }

        written = skipped = 0
        for portfolio in portfolios:
            holdings = holdings_by_portfolio[portfolio.id]
            missing = missing_session_closes(holdings, closes)
            if missing:
                skipped += 1
                logger.warning(
                    "market_close_snapshot_skipped",
                    portfolio_id=str(portfolio.id),
                    session_date=session.isoformat(),
                    tickers_without_session_close=missing)
                continue
            await self._compute_snapshot(
                portfolio.id, session, holdings, closes,
                cash_balance=cash_by_portfolio[portfolio.id])
            written += 1
        logger.info(
            "market_close_job_completed", session_date=session.isoformat(),
            portfolio_count=len(portfolios), snapshots_written=written,
            portfolios_skipped=skipped)
        return MarketCloseResult(written=written, skipped=skipped)

    async def run_market_open_job(self) -> MarketOpenResult:
        portfolios = await self.portfolios.list_all_with_auto_analyze()
        requests: list[tuple[UUID, UUID, str]] = []
        owner_active: dict[UUID, bool] = {}
        for portfolio in portfolios:
            owner = portfolio.owner_id
            if owner not in owner_active:
                state = await get_user_state(settings, owner)
                owner_active[owner] = state is None or state.is_active
            if not owner_active[owner]:
                logger.info(
                    "market_open_owner_inactive_skipped",
                    portfolio_id=str(portfolio.id), owner_id=str(owner))
                continue
            holdings = await self.holdings.list_for_portfolio(portfolio.id)
            requests.extend((portfolio.id, portfolio.owner_id, holding.ticker)
                            for holding in holdings)

        results = await gather_bounded((
            ai_client.request_analysis(ticker, portfolio_id, user_id=owner_id)
            for portfolio_id, owner_id, ticker in requests), limit=10)
        failed = 0
        for (portfolio_id, _owner_id, ticker), result in zip(requests, results,
                                                             strict=False):
            if result is None:
                failed += 1
                logger.warning(
                    "market_open_analysis_request_failed",
                    portfolio_id=str(portfolio_id), ticker=ticker)
        logger.info(
            "market_open_job_completed", requests_attempted=len(requests),
            requests_failed=failed)
        return MarketOpenResult(requested=len(requests), failed=failed)

    async def _compute_snapshot(
            self, portfolio_id: UUID, snapshot_date: date,
            holdings: Sequence[PositionLike], prices: dict[str,
                                                           Decimal | None], *,
            cash_balance: Decimal | None = None) -> PortfolioSnapshotRead:
        holdings_value = Decimal(0)
        for holding in holdings:
            price = prices.get(holding.ticker)
            unit_value = price if price is not None else holding.average_cost
            holdings_value += holding.quantity * unit_value

        if cash_balance is None:
            cash_balance = await self.compute_cash_balance(portfolio_id)
        total_value = holdings_value + cash_balance
        snapshot = await self.snapshots.upsert(
            portfolio_id=portfolio_id, snapshot_date=snapshot_date,
            total_value=total_value, cash_balance=cash_balance)
        return PortfolioSnapshotRead.model_validate(snapshot)

    async def compute_cash_balance(self, portfolio_id: UUID) -> Decimal:
        txns = await self.transactions.list_all_for_portfolio(portfolio_id)
        return sum((cash_delta(t) for t in txns), Decimal(0))

    async def export_history(
            self, portfolio_id: UUID, requester_id: UUID, *,
            start_date: date | None = None,
            end_date: date | None = None) -> list[HistoryRow]:
        await self._require_readable(portfolio_id, requester_id)
        snapshots = await self.snapshots.list_for_portfolio(
            portfolio_id, end_date=end_date)
        flows: dict[date, Decimal] = {}
        for txn in await self.transactions.list_all_for_portfolio(portfolio_id
                                                                  ):
            flow = _external_flow(txn)
            if flow:
                day = snapshot_day(txn.transaction_date)
                flows[day] = flows.get(day, Decimal(0)) + flow
        rows: list[HistoryRow] = []
        previous: PortfolioSnapshot | None = None
        for snap in snapshots:
            daily_return: Decimal | None = None
            if previous is not None and previous.total_value > 0:
                period_flow = sum((
                    v for d, v in flows.items()
                    if previous.snapshot_date < d <= snap.snapshot_date),
                                  Decimal(0))
                daily_return = (
                    snap.total_value - period_flow) / previous.total_value - 1
            if start_date is None or snap.snapshot_date >= start_date:
                rows.append(
                    HistoryRow(
                        snapshot_date=snap.snapshot_date,
                        total_value=snap.total_value,
                        cash_balance=snap.cash_balance,
                        holdings_value=snap.total_value - snap.cash_balance,
                        daily_return=daily_return))
            previous = snap
        return rows

    async def export_transactions(
            self, portfolio_id: UUID, requester_id: UUID, *,
            start_date: date | None = None, end_date: date | None = None,
            transaction_type: TransactionType | None = None
    ) -> list[Transaction]:
        await self._require_readable(portfolio_id, requester_id)
        return await self.transactions.list_filtered(
            portfolio_id, start=day_start(start_date) if start_date else None,
            end=day_start(end_date + timedelta(days=1)) if end_date else None,
            transaction_type=transaction_type)

    async def get_snapshots(
            self, portfolio_id: UUID, requester_id: UUID, *,
            start_date: date | None = None,
            end_date: date | None = None) -> list[PortfolioSnapshotRead]:
        portfolio = await self._require_readable(portfolio_id, requester_id)
        snapshots = await self.snapshots.list_for_portfolio(
            portfolio_id, start_date=start_date, end_date=end_date)

        fx_rate: Decimal | None = None
        if portfolio.currency != Currency.USD.value:
            try:
                fx_rate = await ai_client.get_fx_rate(
                    "USD", portfolio.currency)
            except Exception:
                logger.warning(
                    "portfolio_snapshot_fx_lookup_failed",
                    portfolio_id=str(portfolio_id),
                    currency=portfolio.currency)
                fx_rate = None

        return [
            PortfolioSnapshotRead(
                id=snapshot.id, portfolio_id=snapshot.portfolio_id,
                snapshot_date=snapshot.snapshot_date,
                total_value=snapshot.total_value,
                cash_balance=snapshot.cash_balance, total_value_converted=(
                    snapshot.total_value *
                    fx_rate if fx_rate is not None else None),
                cash_balance_converted=(
                    snapshot.cash_balance *
                    fx_rate if fx_rate is not None else None), fx_rate=fx_rate,
                display_currency=(
                    Currency(portfolio.currency)
                    if fx_rate is not None else None))
            for snapshot in snapshots
        ]

    async def _require_readable(
            self, portfolio_id: UUID, requester_id: UUID) -> Portfolio:
        portfolio = await self.portfolios.get_by_id(portfolio_id)
        if portfolio is None:
            raise NotFoundError("Portfolio not found.")
        if portfolio.owner_id == requester_id:
            return portfolio
        if (portfolio.organization_id is not None
                and portfolio.visibility != "private"):
            org_ids = await auth_client.list_user_organization_ids(
                str(requester_id))
            if portfolio.organization_id in org_ids:
                return portfolio
        raise NotFoundError("Portfolio not found.")

    async def _require_owner(
            self, portfolio_id: UUID, requester_id: UUID) -> Portfolio:
        portfolio = await self.portfolios.get_by_id(portfolio_id)
        if portfolio is None:
            raise NotFoundError("Portfolio not found.")
        if portfolio.owner_id != requester_id:
            await self._require_readable(portfolio_id, requester_id)
            raise PermissionDeniedError(
                "Only the portfolio owner can perform this action.")
        return portfolio

    async def _require_watchlist_owner(
            self, watchlist_id: UUID, owner_id: UUID) -> Watchlist:
        watchlist = await self.watchlists.get_by_id(watchlist_id)
        if watchlist is None or watchlist.owner_id != owner_id:
            raise NotFoundError("Watchlist not found.")
        return watchlist

    async def transaction_quantities(
            self, transaction_ids: list[UUID]) -> list[Decimal]:
        if not transaction_ids:
            return []
        result = await self._db.execute(
            select(Transaction.id, Transaction.quantity).where(
                Transaction.id.in_(transaction_ids)))
        by_id = {row.id: row.quantity for row in result}
        return [by_id.get(i) or Decimal(0) for i in transaction_ids]

    async def portfolio_name(self, portfolio_id: UUID) -> str:
        portfolio = await self.portfolios.get_by_id(portfolio_id)
        return portfolio.name if portfolio is not None else ""

    async def _to_read(self, portfolio: Portfolio) -> PortfolioRead:
        owner = await auth_client.get_user_ref(str(portfolio.owner_id))
        return PortfolioRead(
            id=portfolio.id, name=portfolio.name,
            currency=Currency(portfolio.currency),
            visibility=portfolio.visibility,
            organization_id=portfolio.organization_id,
            auto_analyze_at_open=portfolio.auto_analyze_at_open, owner=owner,
            created_at=portfolio.created_at, updated_at=portfolio.updated_at)

    async def _holding_to_read(self, holding: Holding) -> HoldingRead:
        recommendation = None
        try:
            recommendation = await ai_client.get_latest_recommendation(
                holding.ticker, holding.portfolio_id)
        except Exception:
            recommendation = None
        return HoldingRead(
            id=holding.id, portfolio_id=holding.portfolio_id,
            ticker=holding.ticker, quantity=holding.quantity,
            average_cost=holding.average_cost,
            latest_recommendation=recommendation)
