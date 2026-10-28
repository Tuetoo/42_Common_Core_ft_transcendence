import csv
import io
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID
from database import get_db
from deps import check_date_range, get_current_user
from fastapi import (APIRouter, BackgroundTasks, Depends, Query, Response,
                     status)
from outbox import publish_now
from sqlalchemy.ext.asyncio import AsyncSession
from domains.notification.service import notify_after_commit
from domains.portfolio.schema import (HoldingAdjustmentRead, HoldingCreate,
                                      HoldingRead, HoldingUpdate,
                                      PortfolioCreate, PortfolioRead,
                                      PortfolioSnapshotRead, PortfolioUpdate,
                                      TransactionCreate, TransactionRead,
                                      WatchlistCreate, WatchlistItemCreate,
                                      WatchlistItemRead, WatchlistRead,
                                      WatchlistUpdate)
from domains.portfolio.service import PortfolioService
from shared.enums import NotificationType, TransactionType
from shared.permissions import Permission
from shared.schemas import (MAX_PAGE, APIResponse, PaginatedResponse,
                            PaginationParams)
from shared.security import CurrentUser, require_permission


def describe_quantity(quantity: object) -> str:
    text = format(quantity, "f") if quantity is not None else "0"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text.startswith("-") else f"+{text}"


def notify_transactions(
        background_tasks: BackgroundTasks, user_id: UUID, portfolio_name: str,
        ticker: str, changes: Sequence[tuple[str, Decimal | None]], *,
        source: str = "") -> None:
    if not changes:
        return
    recorded = ", ".join(
        f"{kind} {describe_quantity(qty) if kind == 'adjustment' else qty}"
        for kind, qty in changes)
    notify_after_commit(
        background_tasks, [user_id], type=NotificationType.TRANSACTION_CREATED,
        title="Transaction recorded", content=(
            f"{ticker} in portfolio '{portfolio_name}'{source}: {recorded}."))


def notify_holding_adjustment(
        background_tasks: BackgroundTasks, user_id: UUID, portfolio_name: str,
        ticker: str, quantities: Sequence[Decimal], *,
        source: str = "") -> None:
    notify_transactions(
        background_tasks, user_id, portfolio_name, ticker,
        [("adjustment", q) for q in quantities], source=source)


def notify_watchlist_item(
        background_tasks: BackgroundTasks, user_id: UUID, watchlist_name: str,
        ticker: str, *, added: bool) -> None:
    notify_after_commit(
        background_tasks, [user_id], type=NotificationType.WATCHLIST_UPDATED,
        title="Watchlist updated", content=(
            f"{ticker} was {'added to' if added else 'removed from'} "
            f"watchlist '{watchlist_name}'."))


router = APIRouter(tags=["portfolios"])
watchlist_router = APIRouter(tags=["watchlists"])

portfolio_writer = require_permission(
    Permission.PORTFOLIO_WRITE)(get_current_user)


@router.post(
    "", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[PortfolioRead])
async def create_portfolio(
    data: PortfolioCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[PortfolioRead]:
    result = await PortfolioService(db).create(current_user.user_id, data)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.PORTFOLIO_CREATED, title="Portfolio created",
        content=f"Portfolio '{result.name}' was created.")
    return APIResponse(data=result)


@router.get("", response_model=PaginatedResponse[PortfolioRead])
async def list_portfolios(
        page: int = Query(default=1, ge=1, le=MAX_PAGE),
        page_size: int = Query(default=20, ge=1, le=100),
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[PortfolioRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await PortfolioService(db).list_mine(
        current_user.user_id, pagination=pagination)
    return PaginatedResponse(
        items=items, total=total, page=pagination.page,
        page_size=pagination.page_size)


@router.get("/{portfolio_id}", response_model=APIResponse[PortfolioRead])
async def get_portfolio(
    portfolio_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[PortfolioRead]:
    result = await PortfolioService(db).get(portfolio_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch("/{portfolio_id}", response_model=APIResponse[PortfolioRead])
async def update_portfolio(
    portfolio_id: UUID, data: PortfolioUpdate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[PortfolioRead]:
    result = await PortfolioService(db).update(
        portfolio_id, current_user.user_id, data)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.PORTFOLIO_UPDATED, title="Portfolio updated",
        content=f"Portfolio '{result.name}' was updated.")
    return APIResponse(data=result)


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    portfolio_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    portfolio, entry = await PortfolioService(db).delete(
        portfolio_id, current_user.user_id)
    background_tasks.add_task(publish_now, [entry])
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.PORTFOLIO_DELETED, title="Portfolio deleted",
        content=f"Portfolio '{portfolio.name}' was deleted.")


@router.get(
    "/{portfolio_id}/snapshots",
    response_model=APIResponse[list[PortfolioSnapshotRead]], description=(
        "Daily NAV history of a portfolio the caller may read. Values are in "
        "USD;"
        " for a non-USD portfolio each row also carries a display-only "
        "conversion"
        " at the current FX rate (omitted when the rate cannot be fetched)."))
async def get_portfolio_snapshots(
    portfolio_id: UUID, start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[PortfolioSnapshotRead]]:
    result = await PortfolioService(db).get_snapshots(
        portfolio_id, current_user.user_id, start_date=start_date,
        end_date=end_date)
    return APIResponse(data=result)


@router.post(
    "/{portfolio_id}/holdings", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[HoldingAdjustmentRead])
async def add_holding(
    portfolio_id: UUID, data: HoldingCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[HoldingAdjustmentRead]:
    service = PortfolioService(db)
    result = await service.add_holding(
        portfolio_id, current_user.user_id, data)
    notify_holding_adjustment(
        background_tasks, current_user.user_id, result.portfolio_name,
        result.read.ticker, result.quantities)
    return APIResponse(data=result.read)


@router.get(
    "/{portfolio_id}/holdings", response_model=APIResponse[list[HoldingRead]])
async def list_holdings(
    portfolio_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[HoldingRead]]:
    result, _ = await PortfolioService(db).list_holdings(
        portfolio_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch(
    "/{portfolio_id}/holdings/{holding_id}",
    response_model=APIResponse[HoldingAdjustmentRead])
async def update_holding(
    portfolio_id: UUID, holding_id: UUID, data: HoldingUpdate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[HoldingAdjustmentRead]:
    service = PortfolioService(db)
    result = await service.update_holding(
        portfolio_id, current_user.user_id, holding_id, data)
    notify_holding_adjustment(
        background_tasks, current_user.user_id, result.portfolio_name,
        result.read.ticker, result.quantities)
    return APIResponse(data=result.read)


@router.delete(
    "/{portfolio_id}/holdings/{holding_id}",
    response_model=APIResponse[HoldingAdjustmentRead])
async def delete_holding(
    portfolio_id: UUID, holding_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[HoldingAdjustmentRead]:
    service = PortfolioService(db)
    result = await service.delete_holding(
        portfolio_id, current_user.user_id, holding_id)
    notify_holding_adjustment(
        background_tasks, current_user.user_id, result.portfolio_name,
        result.read.ticker, result.quantities)
    return APIResponse(data=result.read)


@router.post(
    "/{portfolio_id}/transactions", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[TransactionRead])
async def record_transaction(
    portfolio_id: UUID, data: TransactionCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[TransactionRead]:
    service = PortfolioService(db)
    result = await service.record_transaction(
        portfolio_id, current_user.user_id, data)
    notify_transactions(
        background_tasks, current_user.user_id, await
        service.portfolio_name(portfolio_id), result.ticker,
        [(result.transaction_type.value, result.quantity)])
    return APIResponse(data=result)


@router.get(
    "/{portfolio_id}/transactions",
    response_model=PaginatedResponse[TransactionRead])
async def list_transactions(
    portfolio_id: UUID, page: int = Query(default=1, ge=1, le=MAX_PAGE),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[TransactionRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await PortfolioService(db).list_transactions(
        portfolio_id, current_user.user_id, pagination=pagination)
    return PaginatedResponse(
        items=items, total=total, page=pagination.page,
        page_size=pagination.page_size)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _csv_response(
        filename: str, header: Sequence[str],
        rows: Iterable[Sequence[object]]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    return Response(
        content=buffer.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


_RANGE_MESSAGE = "'from' must be on or before 'to'."


@router.get(
    "/{portfolio_id}/history.csv", response_class=Response, description=(
        "Daily NAV history as CSV (`from`/`to` are inclusive ISO dates). "
        "Columns:"
        " date, total_value, cash_balance, holdings_value, daily_return "
        "(flow-adjusted; empty when there is no previous day)."))
async def export_history_csv(
    portfolio_id: UUID,
    start_date: date | None = Query(default=None, alias="from"),
    end_date: date | None = Query(default=None, alias="to"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> Response:
    check_date_range(start_date, end_date, _RANGE_MESSAGE)
    rows = await PortfolioService(db).export_history(
        portfolio_id, current_user.user_id, start_date=start_date,
        end_date=end_date)
    return _csv_response(
        f"portfolio-{portfolio_id}-history.csv", [
            "date", "total_value", "cash_balance", "holdings_value",
            "daily_return"
        ], ([
            r.snapshot_date.isoformat(), r.total_value, r.cash_balance,
            r.holdings_value, r.daily_return
        ] for r in rows))


@router.get(
    "/{portfolio_id}/transactions.csv", response_class=Response, description=(
        "Every ledger field as CSV, oldest first. `from`/`to` are inclusive "
        "ISO "
        "dates (UTC days); `type` is one TransactionType."))
async def export_transactions_csv(
    portfolio_id: UUID,
    start_date: date | None = Query(default=None, alias="from"),
    end_date: date | None = Query(default=None, alias="to"),
    transaction_type: TransactionType | None = Query(
        default=None,
        alias="type"), current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> Response:
    check_date_range(start_date, end_date, _RANGE_MESSAGE)
    txns = await PortfolioService(db).export_transactions(
        portfolio_id, current_user.user_id, start_date=start_date,
        end_date=end_date, transaction_type=transaction_type)
    return _csv_response(
        f"portfolio-{portfolio_id}-transactions.csv", [
            "id", "transaction_date", "transaction_type", "ticker", "quantity",
            "price", "dividend_per_share", "split_ratio", "idempotency_key"
        ], ([
            str(t.id),
            t.transaction_date.isoformat(), t.transaction_type, t.ticker,
            t.quantity, t.price, t.dividend_per_share, t.split_ratio,
            t.idempotency_key
        ] for t in txns))


@watchlist_router.post(
    "", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[WatchlistRead])
async def create_watchlist(
    data: WatchlistCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[WatchlistRead]:
    result = await PortfolioService(db).create_watchlist(
        current_user.user_id, data)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.WATCHLIST_CREATED, title="Watchlist created",
        content=f"Watchlist '{result.name}' was created.")
    return APIResponse(data=result)


@watchlist_router.patch(
    "/{watchlist_id}", response_model=APIResponse[WatchlistRead],
    description="Rename a watchlist (owner only).")
async def update_watchlist(
    watchlist_id: UUID, data: WatchlistUpdate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[WatchlistRead]:
    result = await PortfolioService(db).update_watchlist(
        watchlist_id, current_user.user_id, data)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.WATCHLIST_UPDATED, title="Watchlist updated",
        content=f"Watchlist renamed to '{result.name}'.")
    return APIResponse(data=result)


@watchlist_router.delete(
    "/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT,
    description="Delete a watchlist and its items (owner only).")
async def delete_watchlist(
    watchlist_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    watchlist = await PortfolioService(db).delete_watchlist(
        watchlist_id, current_user.user_id)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.WATCHLIST_DELETED, title="Watchlist deleted",
        content=f"Watchlist '{watchlist.name}' was deleted.")


@watchlist_router.get("", response_model=APIResponse[list[WatchlistRead]])
async def list_watchlists(
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[WatchlistRead]]:
    result = await PortfolioService(db).list_my_watchlists(
        current_user.user_id)
    return APIResponse(data=result)


@watchlist_router.post(
    "/{watchlist_id}/items", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[WatchlistItemRead])
async def add_watchlist_item(
    watchlist_id: UUID, data: WatchlistItemCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[WatchlistItemRead]:
    service = PortfolioService(db)
    result = await service.add_watchlist_item(
        watchlist_id, current_user.user_id, data)
    notify_watchlist_item(
        background_tasks, current_user.user_id, await
        service.get_watchlist_name(watchlist_id), result.ticker, added=True)
    return APIResponse(data=result)


@watchlist_router.delete(
    "/{watchlist_id}/items/{ticker}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_watchlist_item(
    watchlist_id: UUID, ticker: str, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(portfolio_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = PortfolioService(db)
    await service.remove_watchlist_item(
        watchlist_id, current_user.user_id, ticker)
    notify_watchlist_item(
        background_tasks, current_user.user_id, await
        service.get_watchlist_name(watchlist_id), ticker.upper(), added=False)
