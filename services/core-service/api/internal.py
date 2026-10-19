from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
import structlog
from config import settings
from database import get_db
from deps import day_start
from event_listener import sweep_deleted_organizations, sweep_deleted_users
from fastapi import (APIRouter, BackgroundTasks, Depends, Form, Query,
                     UploadFile, status)
from fastapi.responses import JSONResponse
from job_health import (SCHEDULED_JOBS, record_job_success,
                        record_market_close_skipped)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.notification.service import NotificationService
from domains.portfolio.ledger import cash_delta, snapshot_day
from domains.portfolio.model import Portfolio, Transaction, Watchlist
from domains.portfolio.repository import (HoldingRepository,
                                          PortfolioSnapshotRepository,
                                          TransactionRepository,
                                          WatchlistRepository)
from domains.portfolio.schema import InternalTransactionCreate
from domains.portfolio.service import PortfolioService
from domains.research.model import Document, ResearchComment, ResearchNote
from domains.research.repository import DocumentRepository
from domains.research.schema import SystemDocumentCreate
from domains.research.service import ResearchService
from shared.deleted_users import sweep_steps
from shared.enums import DocumentCategory
from shared.exceptions import NotFoundError, PermissionDeniedError
from shared.market_calendar import is_trading_day, market_today
from shared.permissions import Permission, has_permission
from shared.scheduled_jobs import job_response
from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID
from shared.security import require_internal_token
from shared.user_state import get_user_state

logger = structlog.get_logger(__name__)


def _non_trading_reason(day: date) -> str | None:
    if is_trading_day(day):
        return None
    return "weekend" if day.weekday() >= 5 else "market holiday"


def _skipped(job: str, day: date, reason: str,
             **counts: int) -> dict[str, Any]:
    logger.info(
        "scheduled_job_skipped", job=job, date=day.isoformat(), reason=reason)
    return {
        "skipped": True,
        "reason": reason,
        "date": day.isoformat(),
        **counts
    }


router = APIRouter(
    prefix="/internal", tags=["internal"], include_in_schema=False,
    dependencies=[Depends(require_internal_token)])


@router.post("/scheduler/market-close")
async def market_close(db: AsyncSession = Depends(
        get_db, scope="function")) -> dict[str, Any]:
    today = market_today()
    reason = _non_trading_reason(today)
    if reason is not None:
        return _skipped("market-close", today, reason, portfolios_processed=0)
    result = await PortfolioService(db).run_market_close_job(today)
    await record_market_close_skipped(result.skipped)
    return {
        "skipped": False,
        "date": today.isoformat(),
        "portfolios_processed": result.written,
        "portfolios_skipped": result.skipped
    }


@router.get("/portfolios/{portfolio_id}/holdings")
async def get_portfolio_holdings_internal(
    portfolio_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> list[dict[str, Any]]:
    holdings = await HoldingRepository(db).list_for_portfolio(portfolio_id)
    return [{
        "ticker": h.ticker,
        "quantity": float(h.quantity),
        "average_cost": float(h.average_cost)
    } for h in holdings]


@router.get("/portfolios/{portfolio_id}/owner")
async def get_portfolio_owner_internal(
    portfolio_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    portfolio = await PortfolioService(db).portfolios.get_by_id(portfolio_id)
    if portfolio is None:
        raise NotFoundError("Portfolio not found.")
    return {
        "portfolio_id": str(portfolio.id),
        "owner_id": str(portfolio.owner_id),
        "organization_id": (
            str(portfolio.organization_id)
            if portfolio.organization_id is not None else None),
        "visibility": portfolio.visibility
    }


@router.get("/portfolios/{portfolio_id}/cash-balance")
async def get_portfolio_cash_balance_internal(
    portfolio_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    cash_balance = await PortfolioService(db).compute_cash_balance(
        portfolio_id)
    return {
        "portfolio_id": str(portfolio_id),
        "cash_balance": float(cash_balance)
    }


@router.get("/portfolios/{portfolio_id}/snapshots")
async def get_portfolio_snapshots_internal(
    portfolio_id: UUID, start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: AsyncSession = Depends(get_db, scope="function")
) -> list[dict[str, Any]]:
    snapshots = await PortfolioSnapshotRepository(db).list_for_portfolio(
        portfolio_id, start_date=start_date, end_date=end_date)
    return [{
        "snapshot_date": s.snapshot_date.isoformat(),
        "total_value": float(s.total_value),
        "cash_balance": float(s.cash_balance)
    } for s in snapshots]


@router.get("/portfolios/{portfolio_id}/cash-flows")
async def get_portfolio_cash_flows_internal(
    portfolio_id: UUID, start_date: date = Query(...),
    end_date: date = Query(...),
    db: AsyncSession = Depends(get_db, scope="function")
) -> list[dict[str, Any]]:
    flows = await TransactionRepository(db).list_cash_flows_for_portfolio(
        portfolio_id, start_date=start_date, end_date=end_date)
    return [_cash_flow_row(f) for f in flows]


def _cash_flow_row(txn: Transaction) -> dict[str, Any]:
    amount = float(cash_delta(txn))
    return {
        "transaction_date": txn.transaction_date.isoformat(),
        "snapshot_date": snapshot_day(txn.transaction_date).isoformat(),
        "amount": amount,
        "transaction_type": txn.transaction_type,
        "ticker": txn.ticker,
        "quantity": float(txn.quantity) if txn.quantity is not None else None,
        "price": float(txn.price) if txn.price is not None else None
    }


@router.post("/portfolios/{portfolio_id}/transactions")
async def record_transaction_internal(
    portfolio_id: UUID, payload: InternalTransactionCreate,
    db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    service = PortfolioService(db)
    portfolio = await service.portfolios.get_by_id(portfolio_id)
    if portfolio is None:
        raise NotFoundError("Portfolio not found.")
    await _ensure_owner_may_trade(
        service, portfolio.owner_id, payload.idempotency_key)
    result = await service.record_transaction(
        portfolio_id, portfolio.owner_id, payload,
        idempotency_key=payload.idempotency_key)
    return {"id": str(result.id), "ticker": result.ticker}


async def _ensure_owner_may_trade(
        service: PortfolioService, owner_id: UUID,
        idempotency_key: str | None) -> None:
    if not settings.USER_STATE_CHECK_ENABLED:
        return
    if idempotency_key and await service.transactions.get_by_idempotency_key(
            idempotency_key):
        return
    state = await get_user_state(settings, owner_id)
    if state is None:
        logger.warning(
            "internal_transaction.owner_state_unknown", owner_id=str(owner_id))
        return
    may_write = has_permission(state.role, Permission.PORTFOLIO_WRITE)
    if not state.is_active or not may_write:
        raise PermissionDeniedError(
            "The portfolio owner is deactivated or has a read-only role; "
            "the order is refused.")


@router.post("/scheduler/heartbeat/{job}")
async def scheduler_heartbeat(job: str) -> dict[str, Any]:
    if job not in SCHEDULED_JOBS:
        raise NotFoundError(f"Unknown scheduled job {job!r}.")
    moment = await record_job_success(job)
    return {
        "job": job,
        "recorded_at": datetime.fromtimestamp(moment, UTC).isoformat()
    }


@router.post("/scheduler/market-open", response_model=None)
async def market_open(db: AsyncSession = Depends(
        get_db, scope="function")) -> dict[str, Any] | JSONResponse:
    today = market_today()
    reason = _non_trading_reason(today)
    if reason is not None:
        return _skipped("market-open", today, reason, analyses_requested=0)
    result = await PortfolioService(db).run_market_open_job()
    return job_response(
        "market-open", {
            "skipped": False,
            "date": today.isoformat(),
            "analyses_requested": result.requested,
            "analyses_failed": result.failed
        }, steps={"analyses": (result.requested, result.failed)})


@router.post("/scheduler/notification-retention", response_model=None)
async def notification_retention(
        db: AsyncSession = Depends(get_db, scope="function")) -> JSONResponse:
    result = await NotificationService(db).purge_old_read_notifications()
    return job_response(
        "notification-retention", {
            "retention_days": settings.NOTIFICATION_READ_RETENTION_DAYS,
            "deleted": result.deleted,
            "batches": result.batches,
            "failed_batches": result.failed_batches
        }, steps={"batches": (result.batches, result.failed_batches)})


@router.post("/scheduler/deleted-user-sweep", response_model=None)
async def deleted_user_sweep() -> JSONResponse:
    users = await sweep_deleted_users()
    organizations = await sweep_deleted_organizations()
    return job_response(
        "deleted-user-sweep-core", {
            **asdict(users), "organizations": asdict(organizations)
        }, steps={
            **sweep_steps("users", users),
            **sweep_steps("organizations", organizations, brake=True)
        })


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(
        get_db, scope="function")) -> dict[str, int]:
    models: dict[str, Any] = {
        "portfolios": Portfolio,
        "transactions": Transaction,
        "notes": ResearchNote,
        "comments": ResearchComment,
        "documents": Document,
        "watchlists": Watchlist
    }
    counts: dict[str, int] = {}
    for key, model in models.items():
        result = await db.execute(select(func.count()).select_from(model))
        counts[key] = int(result.scalar_one())
    return counts


@router.get("/tracked-tickers")
async def get_tracked_tickers(
        db: AsyncSession = Depends(get_db, scope="function")) -> list[str]:
    pairs = (
        await HoldingRepository(db).list_all_owner_tickers() +
        await WatchlistRepository(db).list_all_owner_tickers())
    owner_active: dict[UUID, bool] = {}
    tickers: set[str] = set()
    for owner_id, ticker in pairs:
        if owner_id not in owner_active:
            state = await get_user_state(settings, owner_id)
            owner_active[owner_id] = state is None or state.is_active
        if owner_active[owner_id]:
            tickers.add(ticker.upper())
    dropped = sum(1 for active in owner_active.values() if not active)
    if dropped:
        logger.info("tracked_tickers_inactive_owners_skipped", owners=dropped)
    return sorted(tickers)


@router.get("/documents/filenames")
async def get_document_filenames(
        ticker: str | None = Query(default=None), category: str = Query(...),
        db: AsyncSession = Depends(get_db, scope="function")) -> list[str]:
    return await DocumentRepository(db).list_filenames(
        ticker=ticker, category=category,
        failed_retry_days=settings.FAILED_DOCUMENT_RETRY_DAYS)


@router.post("/documents")
async def create_system_document(
    payload: SystemDocumentCreate, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    outcome = await ResearchService(db).upload(
        owner_id=SYSTEM_DOCUMENT_OWNER_ID, organization_id=None,
        filename=payload.filename, content=payload.content.encode("utf-8"),
        category=payload.category, ticker=payload.ticker,
        content_date=payload.content_date, background_tasks=background_tasks)
    return {
        "id": str(outcome.document.id),
        "filename": outcome.document.filename
    }


@router.post("/documents/system", status_code=status.HTTP_201_CREATED)
async def upload_system_document(
    background_tasks: BackgroundTasks, file: UploadFile,
    category: DocumentCategory = Form(...), ticker: str | None = Form(
        default=None,
        max_length=10), title: str | None = Form(default=None, max_length=256),
    content_date: date | None = Form(default=None),
    source_url: str | None = Form(default=None, max_length=2048),
    db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    content = await file.read()
    outcome = await ResearchService(db).upload(
        owner_id=SYSTEM_DOCUMENT_OWNER_ID, organization_id=None,
        filename=file.filename or "upload", content=content,
        category=category.value, ticker=(ticker or "").strip() or None,
        content_date=(
            day_start(content_date) if content_date is not None else None),
        title=(title or "").strip() or None,
        source_url=(source_url or "").strip() or None,
        background_tasks=background_tasks)
    return {
        "id": str(outcome.document.id),
        "status": outcome.document.status,
        "duplicate": outcome.duplicate
    }
