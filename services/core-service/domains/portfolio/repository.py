from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.portfolio.model import (Holding, Portfolio, PortfolioSnapshot,
                                     Transaction, Watchlist, WatchlistItem)
from shared.database import count_query
from shared.schemas.base import PaginationParams


class PortfolioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, portfolio_id: UUID) -> Portfolio | None:
        result = await self._db.execute(select(Portfolio)
                                        .where(Portfolio.id == portfolio_id))
        return result.scalar_one_or_none()

    async def create(self, *, owner_id: UUID, name: str, currency: str,
                     organization_id: UUID | None, visibility: str
                     ) -> Portfolio:
        portfolio = Portfolio(owner_id=owner_id, name=name, currency=currency,
                              organization_id=organization_id,
                              visibility=visibility)
        self._db.add(portfolio)
        await self._db.flush()
        return portfolio

    async def delete(self, portfolio: Portfolio) -> None:
        await self._db.delete(portfolio)
        await self._db.flush()

    async def list_for_user(self, user_id: UUID, organization_ids: list[UUID],
                            *, pagination: PaginationParams
                            ) -> tuple[list[Portfolio], int]:
        conditions = [Portfolio.owner_id == user_id]
        if organization_ids:
            conditions.append(Portfolio.organization_id.in_(organization_ids))
        stmt = select(Portfolio).where(or_(*conditions))
        total = await count_query(self._db, stmt)
        result = await self._db.execute(stmt.order_by(
            Portfolio.created_at.desc()).offset(pagination.offset)
            .limit(pagination.limit))
        return list(result.scalars().all()), total

    async def list_all_with_auto_analyze(self) -> list[Portfolio]:
        result = await self._db.execute(select(Portfolio).where(
            Portfolio.auto_analyze_at_open.is_(True)))
        return list(result.scalars().all())

    async def list_all(self) -> list[Portfolio]:
        result = await self._db.execute(select(Portfolio))
        return list(result.scalars().all())


class HoldingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def list_all_distinct_tickers(self) -> list[str]:
        result = await self._db.execute(select(Holding.ticker).distinct())
        return list(result.scalars().all())

    async def get_by_id(self, holding_id: UUID) -> Holding | None:
        result = await self._db.execute(select(Holding)
                                        .where(Holding.id == holding_id))
        return result.scalar_one_or_none()

    async def get_by_ticker(self, portfolio_id: UUID, ticker: str, *,
                            for_update: bool = False) -> Holding | None:
        stmt = select(Holding).where(Holding.portfolio_id == portfolio_id,
                                     Holding.ticker == ticker.upper())
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_portfolio(self, portfolio_id: UUID) -> list[Holding]:
        result = await self._db.execute(select(Holding).where(
            Holding.portfolio_id == portfolio_id))
        return list(result.scalars().all())

    async def create(self, *, portfolio_id: UUID, ticker: str,
                     quantity: Decimal, average_cost: Decimal) -> Holding:
        holding = Holding(portfolio_id=portfolio_id, ticker=ticker.upper(),
                          quantity=quantity, average_cost=average_cost)
        self._db.add(holding)
        await self._db.flush()
        return holding

    async def delete(self, holding: Holding) -> None:
        await self._db.delete(holding)
        await self._db.flush()


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def create(
            self, *, portfolio_id: UUID, ticker: str, transaction_type: str,
            quantity: Decimal | None, price: Decimal | None,
            dividend_per_share: Decimal | None,
            transaction_date: datetime) -> Transaction:
        txn = Transaction(portfolio_id=portfolio_id, ticker=ticker.upper(),
                          transaction_type=transaction_type, quantity=quantity,
                          price=price, dividend_per_share=dividend_per_share,
                          transaction_date=transaction_date)
        self._db.add(txn)
        await self._db.flush()
        return txn

    async def list_for_portfolio(self, portfolio_id: UUID, *,
                                 pagination: PaginationParams
                                 ) -> tuple[list[Transaction], int]:
        stmt = select(Transaction).where(
            Transaction.portfolio_id == portfolio_id)
        total = await count_query(self._db, stmt)
        result = await self._db.execute(stmt.order_by(
            Transaction.transaction_date.desc()).offset(pagination.offset)
            .limit(pagination.limit))
        return list(result.scalars().all()), total

    async def list_all_for_portfolio(self, portfolio_id: UUID
                                     ) -> list[Transaction]:
        result = await self._db.execute(select(Transaction).where(
            Transaction.portfolio_id == portfolio_id)
            .order_by(Transaction.transaction_date.asc()))
        return list(result.scalars().all())


class PortfolioSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def upsert(self, *, portfolio_id: UUID, snapshot_date: date,
                     total_value: Decimal, cash_balance: Decimal
                     ) -> PortfolioSnapshot:
        stmt = pg_insert(PortfolioSnapshot).values(
            portfolio_id=portfolio_id, snapshot_date=snapshot_date,
            total_value=total_value, cash_balance=cash_balance)
        stmt = stmt.on_conflict_do_update(
            index_elements=["portfolio_id", "snapshot_date"],
            set_={"total_value": total_value, "cash_balance": cash_balance})
        await self._db.execute(stmt)
        await self._db.flush()
        result = await self._db.execute(select(PortfolioSnapshot).where(
            PortfolioSnapshot.portfolio_id == portfolio_id,
            PortfolioSnapshot.snapshot_date == snapshot_date))
        return result.scalar_one()


class WatchlistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, watchlist_id: UUID) -> Watchlist | None:
        result = await self._db.execute(select(Watchlist)
                                        .where(Watchlist.id == watchlist_id))
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id: UUID) -> list[Watchlist]:
        result = await self._db.execute(select(Watchlist)
                                        .where(Watchlist.owner_id == user_id))
        return list(result.scalars().all())

    async def create(self, *, owner_id: UUID, name: str) -> Watchlist:
        watchlist = Watchlist(owner_id=owner_id, name=name)
        self._db.add(watchlist)
        await self._db.flush()
        return watchlist

    async def list_items(self, watchlist_id: UUID) -> list[WatchlistItem]:
        result = await self._db.execute(select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id))
        return list(result.scalars().all())

    async def get_item(self, watchlist_id: UUID, ticker: str
                       ) -> WatchlistItem | None:
        result = await self._db.execute(select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.ticker == ticker.upper()))
        return result.scalar_one_or_none()

    async def list_all_distinct_tickers(self) -> list[str]:
        result = await self._db.execute(
            select(WatchlistItem.ticker).distinct())
        return list(result.scalars().all())

    async def add_item(self, *, watchlist_id: UUID, ticker: str
                       ) -> WatchlistItem:
        item = WatchlistItem(watchlist_id=watchlist_id, ticker=ticker.upper())
        self._db.add(item)
        await self._db.flush()
        return item

    async def remove_item(self, item: WatchlistItem) -> None:
        await self._db.delete(item)
        await self._db.flush()
