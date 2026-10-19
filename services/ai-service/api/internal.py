import json
from dataclasses import asdict
from datetime import date
from typing import Any
from uuid import UUID
import structlog
from ai.user_sentiment import SentimentRequest, SentimentResult
from config import settings
from database import get_db
from deps import ensure_portfolio_owner
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.api import create_and_enqueue
from domains.analysis.repository import AnalysisRepository
from domains.analysis.schema import (AnalysisCreate, AnalysisDetailRead,
                                     AnalysisRead)
from domains.analysis.service import AnalysisService
from domains.market.repository import MAX_PRICE_LOOKBACK_DAYS
from domains.market.schema import PriceBarRead
from shared.deleted_users import sweep_steps
from shared.enums import RecommendationAction, WorkflowStatus
from shared.exceptions import NotFoundError
from shared.market_calendar import is_trading_day, market_today
from shared.permissions import Permission, has_permission
from shared.redis import get_cache_client
from shared.scheduled_jobs import job_response
from shared.schemas import AnalysisRef, APIResponse, RecommendationRef
from shared.security import CurrentUser, require_internal_token
from shared.user_state import get_user_state

router = APIRouter(
    prefix="/internal", tags=["internal"], include_in_schema=False,
    dependencies=[Depends(require_internal_token)])
logger = structlog.get_logger(__name__)

_FX_CACHE_TTL_SECONDS = 3600


@router.post(
    "/analyses", response_model=APIResponse[AnalysisRead], status_code=201)
async def create_analysis_internal(
    payload: AnalysisCreate,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AnalysisRead]:
    role = "user"
    if payload.user_id is not None and settings.USER_STATE_CHECK_ENABLED:
        state = await get_user_state(settings, payload.user_id)
        if state is not None:
            if not state.is_active or not has_permission(
                    state.role, Permission.ANALYSIS_CREATE):
                raise HTTPException(
                    status_code=403,
                    detail="The owning user may not create analyses "
                    "(deactivated or read-only role).")
            role = state.role
    if payload.user_id is not None and payload.portfolio_id is not None:
        await ensure_portfolio_owner(
            CurrentUser(user_id=payload.user_id, role=role),
            payload.portfolio_id)
    analysis = await create_and_enqueue(
        db, user_id=payload.user_id, payload=payload)
    return APIResponse(data=analysis)


@router.get(
    "/analyses/{analysis_id}/detail",
    response_model=APIResponse[AnalysisDetailRead])
async def get_analysis_detail_internal(
    analysis_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AnalysisDetailRead]:
    result = await AnalysisService(db).get_analysis_detail(analysis_id)
    return APIResponse(data=result)


@router.get("/analyses/{analysis_id}", response_model=AnalysisRef)
async def get_analysis_ref(
    analysis_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> AnalysisRef:
    analysis = await AnalysisRepository(db).get_by_id(analysis_id)
    return AnalysisRef(
        id=analysis.id, ticker=analysis.ticker,
        status=WorkflowStatus(analysis.status), created_at=analysis.created_at,
        user_id=analysis.user_id, portfolio_id=analysis.portfolio_id)


@router.get("/recommendations/latest", response_model=RecommendationRef)
async def get_latest_recommendation(
        ticker: str = Query(...), portfolio_id: UUID = Query(...),
        db: AsyncSession = Depends(get_db,
                                   scope="function")) -> RecommendationRef:
    found = await AnalysisRepository(db).get_latest_recommendation_for_ticker(
        ticker, portfolio_id)
    if found is None:
        raise NotFoundError(f"No recommendation found for ticker {ticker}")
    rec, analysis = found
    return RecommendationRef(
        id=rec.id, analysis_id=analysis.id, ticker=analysis.ticker,
        action=RecommendationAction(rec.recommendation),
        confidence=rec.confidence, created_at=rec.created_at)


@router.post("/review/run")
async def trigger_review() -> JSONResponse:
    from ai.workflows.review_workflow import run_review_workflow

    counts = await run_review_workflow()
    return job_response(
        "review-run", counts, steps={
            "recommendations": (
                counts["recommendations_attempted"],
                counts["recommendations_failed"]),
            "decisions": (
                counts["decisions_attempted"], counts["decisions_failed"])
        })


def _market_today() -> date:
    return market_today()


_TIER_FAILED_OUTCOMES = (
    "execution_failed", "no_fresh_data", "price_lookup_failed")
_TIER_NOT_ATTEMPTED_OUTCOMES = ("closed_meanwhile", "pending")


@router.post("/scheduler/tier-monitoring")
async def trigger_tier_monitoring() -> JSONResponse:
    today = _market_today()
    if not is_trading_day(today):
        logger.info(
            "tier_monitoring_skipped_non_trading_day", date=today.isoformat())
        return JSONResponse({
            "skipped": True,
            "reason": "non_trading_day",
            "date": today.isoformat()
        })
    from ai.workflows.tier_monitoring_workflow import \
        run_tier_monitoring_workflow

    counts = await run_tier_monitoring_workflow()
    units = int(counts.get("checked", 0)) - sum(
        int(counts.get(key, 0)) for key in _TIER_NOT_ATTEMPTED_OUTCOMES)
    failed = sum(int(counts.get(key, 0)) for key in _TIER_FAILED_OUTCOMES)
    return job_response(
        "tier-monitoring", counts, steps={"tiers": (units, failed)})


@router.post("/scheduler/deleted-user-sweep")
async def trigger_deleted_user_sweep() -> JSONResponse:
    from user_cleanup import sweep_deleted_users

    result = await sweep_deleted_users()
    return job_response(
        "deleted-user-sweep-ai", asdict(result),
        steps=sweep_steps("users", result))


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(
        get_db, scope="function")) -> dict[str, int]:
    from domains.analysis.model import Analysis, Recommendation
    from domains.backtest.model import Backtest

    rows = (
        await db.execute(
            select(Analysis.status,
                   func.count()).group_by(Analysis.status))).all()
    by_status: dict[str, int] = {str(row[0]): int(row[1]) for row in rows}
    recommendations = (
        await db.execute(select(func.count()).select_from(Recommendation)
                         )).scalar_one()
    backtests = (await db.execute(select(func.count()).select_from(Backtest)
                                  )).scalar_one()
    return {
        "analyses_total": int(sum(by_status.values())),
        "analyses_completed": int(
            by_status.get(WorkflowStatus.COMPLETED.value, 0)),
        "analyses_failed": int(by_status.get(WorkflowStatus.FAILED.value, 0)),
        "recommendations": int(recommendations),
        "backtests": int(backtests)
    }


@router.post("/sentiment", response_model=SentimentResult)
async def analyze_sentiment_internal(
        payload: SentimentRequest) -> SentimentResult:
    from ai.user_sentiment import (SentimentUnavailableError,
                                   analyze_user_sentiment)

    try:
        return await analyze_user_sentiment(payload.text, kind=payload.kind)
    except SentimentUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail=str(exc),
            headers={"Retry-After": "60"}) from exc


@router.post("/scheduler/rag-ingest")
async def trigger_rag_ingest() -> JSONResponse:
    from ai.workflows.auto_ingestion_workflow import (
        run_auto_ingestion_workflow, run_macro_auto_ingestion_workflow)

    per_ticker_result = await run_auto_ingestion_workflow()
    macro_result = await run_macro_auto_ingestion_workflow()

    def _step(name: str) -> tuple[int, int]:
        step = per_ticker_result.get(name) or {}
        failed = len(step.get("failed_tickers") or [])
        return int(step.get("attempted", 0)), failed

    macro_units = 1 if macro_result.get("attempted", True) else 0
    return job_response(
        "rag-ingest", {
            "per_ticker": per_ticker_result,
            "macro": macro_result
        }, steps={
            "filings": _step("filings"),
            "news": _step("news"),
            "macro": (macro_units, 1 if macro_result.get("failed") else 0)
        })


@router.get(
    "/market/instruments/{ticker}/prices",
    response_model=APIResponse[list[PriceBarRead]])
async def get_prices_internal(
    ticker: str, interval: str = Query(default="1d"),
    lookback_days: int = Query(default=5, ge=1, le=MAX_PRICE_LOOKBACK_DAYS)
) -> APIResponse[list[PriceBarRead]]:
    from domains.market.api import price_bars_response

    return await price_bars_response(ticker, interval, lookback_days)


@router.get("/fx-rate")
async def get_fx_rate(
        from_currency: str = Query(...),
        to_currency: str = Query(...)) -> dict[str, Any]:
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    cache_key = f"ai:internal:fx_rate:{from_currency}:{to_currency}"
    cache = get_cache_client(settings)
    try:
        cached = await cache.get(cache_key)
    except Exception as exc:
        logger.warning("fx_rate_cache_read_failed", error=str(exc))
        cached = None
    if cached:
        return {"rate": json.loads(cached)}

    from ai.providers.registry import get_market_data_provider

    try:
        quote = await get_market_data_provider().get_current_price(
            f"{from_currency}{to_currency}=X")
    except Exception:
        logger.warning(
            "fx_rate_lookup_failed", from_currency=from_currency,
            to_currency=to_currency)
        raise NotFoundError(
            f"No real FX rate available for {from_currency}/{to_currency}"
        ) from None

    try:
        await cache.set(
            cache_key, json.dumps(float(quote.price)),
            ex=_FX_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning("fx_rate_cache_write_failed", error=str(exc))
    return {"rate": float(quote.price)}
