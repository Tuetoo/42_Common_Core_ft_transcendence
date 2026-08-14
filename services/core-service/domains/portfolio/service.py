from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID
import clients.ai_client as ai_client
import clients.auth_client as auth_client
import structlog
from config import settings
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from domains.portfolio.model import Holding, Portfolio, Watchlist
from domains.portfolio.repository import (
    HoldingRepository, PortfolioRepository, PortfolioSnapshotRepository,
    TransactionRepository, WatchlistRepository)
from domains.portfolio.schema import (
    HoldingCreate, HoldingRead, HoldingUpdate, PortfolioCreate, PortfolioRead,
    PortfolioSnapshotRead, PortfolioUpdate, TransactionCreate, TransactionRead,
    WatchlistItemCreate, WatchlistCreate, WatchlistItemRead, WatchlistRead)
from shared.concurrency import gather_bounded
from shared.enums.market import TransactionType
from shared.events import publish_cross_service
from shared.events_registry import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError, ValidationAppError)

logger = structlog.get_logger(__name__)
_CASH_TICKER = "CASH"


class PortfolioService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.portfolios = PortfolioRepository(session)
        self.holdings = HoldingRepository(session)
        self.transactions = TransactionRepository(session)
        self.snapshots = PortfolioSnapshotRepository(session)
        self.watchlists = WatchlistRepository(session)

    async def _to_read(self, portfolio: Portfolio) -> PortfolioRead:
        owner = await auth_client.get_user_ref(str(portfolio.owner_id))
        return PortfolioRead(
            id=portfolio.id, name=portfolio.name, currency=portfolio.currency,
            visibility=portfolio.visibility,
            organization_id=portfolio.organization_id,
            auto_analyze_at_open=portfolio.auto_analyze_at_open, owner=owner,
            created_at=portfolio.created_at, updated_at=portfolio.updated_at)

    async def create(self, owner_id: UUID, data: PortfolioCreate
                     ) -> PortfolioRead:
        portfolio = await self.portfolios.create(
            owner_id=owner_id, name=data.name, currency=data.currency,
            organization_id=data.organization_id, visibility=data.visibility)
        await publish_cross_service(
            settings, EventName.PORTFOLIO_CREATED,
            {"portfolio_id": str(portfolio.id), "owner_id": str(owner_id)})
        return await self._to_read(portfolio)

    async def _require_readable(self, portfolio_id: UUID, requester_id: UUID
                                ) -> Portfolio:
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

    async def get(self, portfolio_id: UUID, requester_id: UUID
                  ) -> PortfolioRead:
        portfolio = await self._require_readable(portfolio_id, requester_id)
        return await self._to_read(portfolio)

    async def list_mine(self, requester_id: UUID, *, page: int, page_size: int
                        ) -> tuple[list[PortfolioRead], int]:
        org_ids = await auth_client.list_user_organization_ids(
            str(requester_id))
        portfolios, total = await self.portfolios.list_for_user(
            requester_id, org_ids, offset=(page - 1) * page_size,
            limit=page_size)
        reads = await gather_bounded((self._to_read(p) for p in portfolios),
                                     limit=20)
        return reads, total

    async def _require_owner(self, portfolio_id: UUID, requester_id: UUID
                             ) -> Portfolio:
        portfolio = await self.portfolios.get_by_id(portfolio_id)
        if portfolio is None:
            raise NotFoundError("Portfolio not found.")
        if portfolio.owner_id != requester_id:
            raise PermissionDeniedError(
                "Only the portfolio owner can perform this action.")
        return portfolio

    async def update(self, portfolio_id: UUID, requester_id: UUID,
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

    async def delete(self, portfolio_id: UUID, requester_id: UUID) -> None:
        portfolio = await self._require_owner(portfolio_id, requester_id)
        await self.portfolios.delete(portfolio)

    async def _holding_to_read(self, holding: Holding) -> HoldingRead:
        recommendation = None
        try:
            recommendation = await ai_client.get_latest_recommendation(
                holding.ticker)
        except Exception:
            recommendation = None
        return HoldingRead(id=holding.id, portfolio_id=holding.portfolio_id,
                           ticker=holding.ticker, quantity=holding.quantity,
                           average_cost=holding.average_cost,
                           latest_recommendation=recommendation)

    async def list_holdings(self, portfolio_id: UUID, requester_id: UUID
                            ) -> list[HoldingRead]:
        await self._require_readable(portfolio_id, requester_id)
        holdings = await self.holdings.list_for_portfolio(portfolio_id)
        return await gather_bounded(
            (self._holding_to_read(h) for h in holdings), limit=20)

    async def add_holding(self, portfolio_id: UUID, requester_id: UUID,
                          data: HoldingCreate) -> HoldingRead:
        await self._require_owner(portfolio_id, requester_id)
        existing = await self.holdings.get_by_ticker(portfolio_id, data.ticker)
        if existing is not None:
            raise ValidationAppError(
                f"A holding for {data.ticker.upper()} already exists -- use a "
                "transaction to change its quantity instead.")
        try:
            async with self._db.begin_nested():
                holding = await self.holdings.create(
                    portfolio_id=portfolio_id, ticker=data.ticker,
                    quantity=data.quantity, average_cost=data.average_cost)
        except IntegrityError as exc:
            raise ConflictError(
                f"A holding for {data.ticker.upper()} already exists") from exc
        return await self._holding_to_read(holding)

    async def update_holding(self, portfolio_id: UUID, requester_id: UUID,
                             holding_id: UUID, data: HoldingUpdate
                             ) -> HoldingRead:
        await self._require_owner(portfolio_id, requester_id)
        holding = await self.holdings.get_by_id(holding_id)
        if holding is None or holding.portfolio_id != portfolio_id:
            raise NotFoundError("Holding not found.")
        if data.quantity is not None:
            holding.quantity = data.quantity
        if data.average_cost is not None:
            holding.average_cost = data.average_cost
        await self._db.flush()
        return await self._holding_to_read(holding)

    async def delete_holding(self, portfolio_id: UUID, requester_id: UUID,
                             holding_id: UUID) -> None:
        await self._require_owner(portfolio_id, requester_id)
        holding = await self.holdings.get_by_id(holding_id)
        if holding is None or holding.portfolio_id != portfolio_id:
            raise NotFoundError("Holding not found.")
        await self.holdings.delete(holding)

    async def _apply_buy(self, portfolio_id: UUID, data: TransactionCreate
                         ) -> None:
        if not data.quantity or not data.price:
            raise ValidationAppError("A buy requires both quantity and price.")
        existing = await self.holdings.get_by_ticker(portfolio_id, data.ticker,
                                                     for_update=True)
        if existing is None:
            try:
                async with self._db.begin_nested():
                    await self.holdings.create(
                        portfolio_id=portfolio_id, ticker=data.ticker,
                        quantity=data.quantity, average_cost=data.price)
                    return
            except IntegrityError:
                existing = await self.holdings.get_by_ticker(
                    portfolio_id, data.ticker, for_update=True)
                if existing is None:
                    raise
        total_cost = (existing.quantity * existing.average_cost
                      + data.quantity * data.price)
        new_quantity = existing.quantity + data.quantity
        existing.average_cost = total_cost / new_quantity
        existing.quantity = new_quantity
        await self._db.flush()

    async def _apply_sell(self, portfolio_id: UUID, data: TransactionCreate
                          ) -> None:
        if not data.quantity:
            raise ValidationAppError("A sell requires a quantity.")
        holding = await self.holdings.get_by_ticker(portfolio_id, data.ticker,
                                                    for_update=True)
        if holding is None or holding.quantity < data.quantity:
            raise ValidationAppError(
                f"Cannot sell {data.quantity} of {data.ticker.upper()} "
                "-- not enough held.")
        holding.quantity -= data.quantity
        if holding.quantity == 0:
            await self.holdings.delete(holding)
        await self._db.flush()

    async def record_transaction(self, portfolio_id: UUID, requester_id: UUID,
                                 data: TransactionCreate) -> TransactionRead:
        await self._require_owner(portfolio_id, requester_id)
        ticker = (_CASH_TICKER if data.transaction_type in (
            TransactionType.DEPOSIT, TransactionType.WITHDRAWAL)
            else data.ticker.upper())
        txn = await self.transactions.create(
            portfolio_id=portfolio_id, ticker=ticker,
            transaction_type=data.transaction_type, quantity=data.quantity,
            price=data.price, transaction_date=data.transaction_date)
        if data.transaction_type == TransactionType.BUY:
            await self._apply_buy(portfolio_id, data)
        elif data.transaction_type == TransactionType.SELL:
            await self._apply_sell(portfolio_id, data)
        return TransactionRead.model_validate(txn)

    async def list_transactions(
            self, portfolio_id: UUID, requester_id: UUID, *, page: int,
            page_size: int) -> tuple[list[TransactionRead], int]:
        await self._require_readable(portfolio_id, requester_id)
        txns, total = await self.transactions.list_for_portfolio(
            portfolio_id, offset=(page - 1) * page_size, limit=page_size)
        return [TransactionRead.model_validate(t) for t in txns], total

    async def create_watchlist(self, owner_id: UUID, data: WatchlistCreate
                               ) -> WatchlistRead:
        watchlist = await self.watchlists.create(
            owner_id=owner_id, name=data.name)
        return WatchlistRead(id=watchlist.id, name=watchlist.name, items=[])
    
    async def list_my_watchlists(self, owner_id: UUID) -> list[WatchlistRead]:
        watchlists = await self.watchlists.list_for_user(owner_id)
        results = []
        for wl in watchlists:
            items = await self.watchlists.list_items(wl.id)
            results.append(WatchlistRead(
                id=wl.id, name=wl.name,
                items=[WatchlistItemRead.model_validate(i) for i in items]))
        return results

    async def _require_watchlist_owner(self, watchlist_id: UUID,
                                       owner_id: UUID) -> Watchlist:
        watchlist = await self.watchlists.get_by_id(watchlist_id)
        if watchlist is None:
            raise NotFoundError("Watchlist not found.")
        if watchlist.owner_id != owner_id:
            raise PermissionDeniedError(
                "Only the watchlist owner can perform this action.")
        return watchlist

    async def add_watchlist_item(self, watchlist_id: UUID, owner_id: UUID,
                                 data: WatchlistItemCreate
                                 ) -> WatchlistItemRead:
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

    async def remove_watchlist_item(self, watchlist_id: UUID, owner_id: UUID,
                                    ticker: str) -> None:
        watchlist = await self._require_watchlist_owner(watchlist_id, owner_id)
        item = await self.watchlists.get_item(watchlist.id, ticker)
        if item is None:
            raise NotFoundError("This ticker is not on the watchlist.")
        await self.watchlists.remove_item(item)

    async def compute_cash_balance(self, portfolio_id: UUID) -> Decimal:
        txns = await self.transactions.list_all_for_portfolio(portfolio_id)
        cash = Decimal(0)
        for txn in txns:
            if (txn.transaction_type == TransactionType.DEPOSIT
                    and txn.quantity):
                cash += txn.quantity
            elif (txn.transaction_type == TransactionType.WITHDRAWAL
                  and txn.quantity):
                cash -= txn.quantity
            elif (txn.transaction_type == TransactionType.DIVIDEND
                  and txn.quantity and txn.price):
                cash += txn.quantity * txn.price
            elif (txn.transaction_type == TransactionType.BUY
                  and txn.quantity and txn.price):
                cash -= txn.quantity * txn.price
            elif (txn.transaction_type == TransactionType.SELL
                  and txn.quantity and txn.price):
                cash += txn.quantity * txn.price
        return cash

    async def _compute_snapshot(
            self, portfolio_id: UUID, snapshot_date: date,
            holdings: list[Holding], prices: dict[str, Decimal | None]
            ) -> PortfolioSnapshotRead:
        total_value = Decimal(0)
        for holding in holdings:
            price = prices.get(holding.ticker)
            unit_value = price if price is not None else holding.average_cost
            total_value += holding.quantity * unit_value
        cash_balance = await self.compute_cash_balance(portfolio_id)
        snapshot = await self.snapshots.upsert(
            portfolio_id=portfolio_id, snapshot_date=snapshot_date,
            total_value=total_value, cash_balance=cash_balance)
        return PortfolioSnapshotRead.model_validate(snapshot)

    async def run_market_close_job(self) -> int:
        today = datetime.now(UTC).date()
        portfolios = await self.portfolios.list_all()
        holdings_by_portfolio: dict[UUID, list[Holding]] = {}
        all_tickers: set[str] = set()
        for portfolio in portfolios:
            holdings = await self.holdings.list_for_portfolio(portfolio.id)
            holdings_by_portfolio[portfolio.id] = holdings
            all_tickers.update(h.ticker for h in holdings)
        tickers = list(all_tickers)
        prices = dict(zip(tickers, await gather_bounded(
            (ai_client.get_current_price(t) for t in tickers), limit=20),
            strict=False))
        for portfolio in portfolios:
            await self._compute_snapshot(
                portfolio.id, today, holdings_by_portfolio[portfolio.id],
                prices)
        logger.info("market_close_job_completed",
                    portfolio_count=len(portfolios))
        return len(portfolios)

    async def run_market_open_job(self) -> int:
        portfolios = await self.portfolios.list_all_with_auto_analyze()
        requests: list[tuple[UUID, str]] = []
        for portfolio in portfolios:
            holdings = await self.holdings.list_for_portfolio(portfolio.id)
            requests.extend(
                (portfolio.id, holding.ticker) for holding in holdings)
        results = await gather_bounded((ai_client.request_analysis(
            ticker, portfolio_id) for portfolio_id, ticker in requests),
            limit=10)
        for (portfolio_id, ticker), result in zip(requests, results,
                                                  strict=False):
            if result is None:
                logger.warning("market_open_analysis_request_failed",
                               portfolio_id=str(portfolio_id), ticker=ticker)
        logger.info("market_open_job_completed",
                    requests_attempted=len(requests))
        return len(requests)
