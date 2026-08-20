from typing import Any
from datetime import date
from uuid import UUID
from database import get_db
from domains.portfolio.schema import TransactionCreate
from domains.portfolio.service import PortfolioService
from domains.research.schema import SystemDocumentCreate
from domains.portfolio.repository import (
    HoldingRepository, WatchlistRepository, PortfolioSnapshotRepository,
    TransactionRepository)
from domains.research.repository import DocumentRepository
from domains.research.service import ResearchService
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from shared.exceptions import NotFoundError

router = APIRouter(prefix="/internal", tags=["internal"])
SYSTEM_INGESTION_OWNER_ID = UUID("00000000-0000-0000-0000-0000000005ec")


@router.post("/scheduler/market-close")
async def market_close(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    processed = await PortfolioService(db).run_market_close_job()
    return {"portfolios_processed": processed}


@router.get("/portfolios/{portfolio_id}/holdings")
async def get_portfolio_holdings_internal(
    portfolio_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    holdings = await HoldingRepository(db).list_for_portfolio(portfolio_id)
    return [{"ticker": h.ticker, "quantity": float(h.quantity),
             "average_cost": float(h.average_cost)} for h in holdings]


@router.get("/portfolios/{portfolio_id}/cash-balance")
async def get_portfolio_cash_balance_internal(
    portfolio_id: UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    cash_balance = await PortfolioService(db).compute_cash_balance(
        portfolio_id)
    return {"portfolio_id": str(portfolio_id),
            "cash_balance": float(cash_balance)}


@router.get("/portfolios/{portfolio_id}/snapshots")
async def get_portfolio_snapshots_internal(
    portfolio_id: UUID, start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    snapshots = await PortfolioSnapshotRepository(db).list_for_portfolio(
        portfolio_id, start_date=start_date, end_date=end_date)
    return [{"snapshot_date": s.snapshot_date.isoformat(),
             "total_value": float(s.total_value),
             "cash_balance": float(s.cash_balance)} for s in snapshots]


@router.get("/portfolios/{portfolio_id}/cash-flows")
async def get_portfolio_cash_flows_internal(
    portfolio_id: UUID, start_date: date = Query(...),
    end_date: date = Query(...), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    flows = await TransactionRepository(db).list_cash_flows_for_portfolio(
        portfolio_id, start_date=start_date, end_date=end_date)
    return [{"transaction_date": f.transaction_date.isoformat(),
             "amount": float(f.quantity or 0)
             if f.transaction_type == "deposit" else -float(f.quantity or 0)}
            for f in flows]


@router.post("/portfolios/{portfolio_id}/transactions")
async def record_transaction_internal(
    portfolio_id: UUID, payload: TransactionCreate,
    db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    portfolio = await PortfolioService(db).portfolios.get_by_id(portfolio_id)
    if portfolio is None:
        raise NotFoundError("Portfolio not found.")
    result = await PortfolioService(db).record_transaction(
        portfolio_id, portfolio.owner_id, payload)
    return {"id": str(result.id), "ticker": result.ticker}


@router.post("/scheduler/market-open")
async def market_open(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    requested = await PortfolioService(db).run_market_open_job()
    return {"analyses_requested": requested}


@router.get("/tracked-tickers")
async def get_tracked_tickers(db: AsyncSession = Depends(get_db)) -> list[str]:
    holding_tickers = await HoldingRepository(db).list_all_distinct_tickers()
    wl_tickers = await WatchlistRepository(db).list_all_distinct_tickers()
    return sorted({t.upper() for t in holding_tickers + wl_tickers})


@router.get("/documents/filenames")
async def get_document_filenames(
    ticker: str | None = Query(default=None), category: str = Query(...),
    db: AsyncSession = Depends(get_db)
) -> list[str]:
    return await DocumentRepository(db).list_filenames(
        ticker=ticker, category=category)


@router.post("/documents")
async def create_system_document(
    payload: SystemDocumentCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    result = await ResearchService(db).upload_document(
        owner_id=SYSTEM_INGESTION_OWNER_ID,
        organization_id=payload.organization_id, filename=payload.filename,
        content=payload.content.encode("utf-8"), category=payload.category,
        ticker=payload.ticker, content_date=payload.content_date)
    return {"id": str(result.id), "filename": result.filename}
