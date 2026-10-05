import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
import structlog
from ai.providers.base import ProviderError
from ai.tools.market_data_cache import get_bars_cache_first
from ai_events import (TierEventInfo, plain_decimal, publish_event,
                       tier_event_payload)
from database import SessionLocal
from domains.analysis.model import RecommendationTier
from domains.analysis.repository import RecommendationTierRepository
from shared.enums import TierSide, TierStatus
from shared.events_registry import EventName
from shared.market_calendar import MARKET_TZ, market_today

logger = structlog.get_logger(__name__)

_OPEN_TIER_STATUSES = (
    TierStatus.PENDING_ACCEPTANCE.value,
    TierStatus.ACCEPTED_AWAITING_TRIGGER.value)

TIER_STATUS_EXECUTION_FAILED = TierStatus.EXECUTION_FAILED.value


def _session_open_utc(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=MARKET_TZ).astimezone(UTC)


def _today_bar_matches(bar_timestamp: datetime, today: date) -> bool:
    if bar_timestamp.tzinfo is None:
        bar_timestamp = bar_timestamp.replace(tzinfo=UTC)
    return bar_timestamp.astimezone(MARKET_TZ).date() == today


async def _check_one_tier(tier: RecommendationTier, *, today: date) -> str:
    recommendation = tier.recommendation
    if recommendation.window_expires_at is None:
        return "pending"
    if recommendation.window_expires_at < datetime.now(UTC):
        tier.status = TierStatus.EXPIRED.value
        return "expired"

    if tier.status != TierStatus.ACCEPTED_AWAITING_TRIGGER.value:
        return "pending"

    ticker = recommendation.analysis.ticker
    try:
        bars = await get_bars_cache_first(
            ticker, interval="1d", lookback_days=1)
    except ProviderError as exc:
        logger.warning(
            "tier_monitoring.price_lookup_failed", ticker=ticker,
            tier_id=str(tier.id), error=str(exc))
        return "price_lookup_failed"

    if not bars or not _today_bar_matches(bars[-1].timestamp, today):
        return "no_fresh_data"

    bar = bars[-1]
    accepted_at = recommendation.accepted_at
    if accepted_at is not None and accepted_at > _session_open_utc(
            bar.timestamp.date()):
        logger.info(
            "tier_monitoring.accepted_after_bar_open", ticker=ticker,
            tier_id=str(tier.id), accepted_at=accepted_at.isoformat())
        return "still_open"
    if not (math.isfinite(bar.low) and math.isfinite(bar.high)
            and math.isfinite(bar.open)):
        logger.warning(
            "tier_monitoring.bar_has_non_finite_price", ticker=ticker,
            tier_id=str(tier.id), low=bar.low, high=bar.high, open=bar.open)
        return "no_fresh_data"
    if tier.side == TierSide.BUY.value and bar.low <= tier.target_price:
        fill_price = min(bar.open, tier.target_price)
        fill_price = Decimal(str(fill_price))
        return await _execute_and_mark_triggered(
            tier, ticker=ticker, price=fill_price, transaction_type="buy")
    if tier.side == TierSide.SELL.value and bar.high >= tier.target_price:
        fill_price = max(bar.open, tier.target_price)
        fill_price = Decimal(str(fill_price))
        return await _execute_and_mark_triggered(
            tier, ticker=ticker, price=fill_price, transaction_type="sell")
    return "still_open"


async def _execute_and_mark_triggered(
        tier: RecommendationTier, *, ticker: str, price: Decimal,
        transaction_type: str) -> str:
    from clients.core_client import (TransactionRejectedError,
                                     record_transaction)

    recommendation = tier.recommendation
    portfolio_id = recommendation.analysis.portfolio_id
    if portfolio_id is None:
        raise ValueError(
            f"Tier {tier.id}'s analysis has no portfolio_id -- cannot execute")

    try:
        await _send_order(
            record_transaction, tier, portfolio_id=str(portfolio_id),
            ticker=ticker, price=price, transaction_type=transaction_type)
    except TransactionRejectedError as exc:
        tier.status = TIER_STATUS_EXECUTION_FAILED
        line = (
            f"Tier {tier.id} ({tier.side} "
            f"{plain_decimal(Decimal(tier.quantity))} @ "
            f"{plain_decimal(Decimal(tier.target_price))}) "
            f"was not executed: core-service refused the order "
            f"({exc.status_code}: {exc.detail}).")
        existing = recommendation.tier_generation_note
        recommendation.tier_generation_note = (
            f"{existing}\n{line}" if existing else line)
        logger.warning(
            "tier_monitoring.execution_rejected", tier_id=str(tier.id),
            ticker=ticker, portfolio_id=str(portfolio_id),
            status_code=exc.status_code, detail=exc.detail)
        return "execution_rejected"
    tier.status = TierStatus.TRIGGERED.value
    tier.triggered_at = datetime.now(UTC)
    tier.triggered_price = price
    return "triggered"


async def _send_order(
        record_transaction: Any, tier: RecommendationTier, *,
        portfolio_id: str, ticker: str, price: Decimal,
        transaction_type: str) -> None:
    await record_transaction(
        portfolio_id, transaction_type=transaction_type, ticker=ticker,
        quantity=float(tier.quantity), price=float(price),
        transaction_date=datetime.now(UTC).isoformat(),
        idempotency_key=f"tier:{tier.id}")


def _tier_outcome_event(
        tier: RecommendationTier,
        outcome: str) -> tuple[EventName, dict[str, Any]] | None:
    if outcome not in ("triggered", "execution_rejected"):
        return None
    recommendation = tier.recommendation
    analysis = recommendation.analysis
    info = TierEventInfo(
        tier_id=tier.id, analysis_id=analysis.id, user_id=analysis.user_id,
        portfolio_id=analysis.portfolio_id, ticker=analysis.ticker,
        side=tier.side, quantity=Decimal(tier.quantity),
        price=Decimal(tier.target_price))
    if outcome == "triggered":
        return EventName.TIER_EXECUTED, tier_event_payload(
            info, price=tier.triggered_price)
    note = recommendation.tier_generation_note or ""
    reason = note.splitlines()[-1] if note else "execution_rejected"
    return EventName.TIER_EXECUTION_FAILED, tier_event_payload(
        info, reason=reason)


async def run_tier_monitoring_workflow() -> dict[str, Any]:
    today = market_today()
    counts = {
        "checked": 0,
        "triggered": 0,
        "expired": 0,
        "no_fresh_data": 0,
        "price_lookup_failed": 0,
        "still_open": 0,
        "pending": 0,
        "execution_failed": 0,
        "closed_meanwhile": 0,
        "execution_rejected": 0
    }
    async with SessionLocal() as session:
        repo = RecommendationTierRepository(session)
        tier_ids = await repo.list_open_tier_ids()
        for open_tier_id in tier_ids:
            counts["checked"] += 1
            tier_id = str(open_tier_id)
            try:
                loaded = await repo.get_tier_with_details(open_tier_id)
                if loaded is None:
                    counts["closed_meanwhile"] += 1
                    await session.commit()
                    continue
                tier = loaded
                await session.refresh(
                    tier, attribute_names=["status"], with_for_update=True)
                if tier.status not in _OPEN_TIER_STATUSES:
                    counts["closed_meanwhile"] += 1
                    await session.commit()
                    continue
                outcome = await _check_one_tier(tier, today=today)
                counts[outcome] += 1
                event = _tier_outcome_event(tier, outcome)
                await session.commit()
                if event is not None:
                    await publish_event(*event)
            except Exception:
                counts["execution_failed"] += 1
                await session.rollback()
                logger.exception(
                    "tier_monitoring.tier_processing_failed", tier_id=tier_id)

    logger.info("tier_monitoring.run_complete", **counts)
    return counts
