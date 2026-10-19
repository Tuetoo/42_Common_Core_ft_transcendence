from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID
import structlog
from config import settings
from domains.backtest.schema import backtest_kind
from shared import events as shared_events
from shared.events import EventName

logger = structlog.get_logger(__name__)

TIER_CANCEL_REASON_DECLINED = "declined"
TIER_CANCEL_REASON_SUPERSEDED = "superseded"
TIER_CANCEL_REASON_PORTFOLIO_DELETED = "portfolio_deleted"


def _id_or_empty(value: Any) -> str:
    return str(value) if value else ""


def analysis_event_payload(
        *, analysis_id: Any, user_id: Any, ticker: str, portfolio_id: Any,
        failure_reason: str | None = None,
        include_failure_reason: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "analysis_id": str(analysis_id),
        "user_id": _id_or_empty(user_id),
        "ticker": ticker,
        "portfolio_id": str(portfolio_id) if portfolio_id else None
    }
    if include_failure_reason:
        payload["failure_reason"] = failure_reason
    return payload


def recommendation_ready_payload(
        *, recommendation_id: Any, analysis_id: Any, user_id: Any, ticker: str,
        action: str, confidence: float) -> dict[str, Any]:
    return {
        "id": str(recommendation_id),
        "analysis_id": str(analysis_id),
        "ticker": ticker,
        "action": action,
        "confidence": confidence,
        "user_id": str(user_id) if user_id else None
    }


@dataclass(frozen=True)
class TierEventInfo:
    tier_id: UUID
    analysis_id: UUID
    user_id: UUID | None
    portfolio_id: UUID | None
    ticker: str
    side: str
    quantity: Decimal
    price: Decimal


def plain_decimal(value: Decimal) -> str:
    return format(value, "f")


def tier_event_payload(
        info: TierEventInfo, *, reason: str | None = None,
        price: Decimal | None = None,
        user_id_fallback: Any = None) -> dict[str, Any]:
    user_id = info.user_id or user_id_fallback
    return {
        "tier_id": str(info.tier_id),
        "analysis_id": str(info.analysis_id),
        "user_id": _id_or_empty(user_id),
        "portfolio_id": _id_or_empty(info.portfolio_id),
        "ticker": info.ticker,
        "action": info.side,
        "quantity": plain_decimal(info.quantity),
        "price": plain_decimal(price if price is not None else info.price),
        "reason": reason
    }


def decision_event_payload(
        *, decision_id: Any, analysis_id: Any, user_id: Any, ticker: str,
        decision: str) -> dict[str, Any]:
    return {
        "decision_id": str(decision_id),
        "analysis_id": str(analysis_id),
        "user_id": _id_or_empty(user_id),
        "ticker": ticker,
        "decision": decision
    }


def decision_outcome_event_payload(
        *, decision_id: Any, analysis_id: Any, user_id: Any, ticker: str,
        evaluation_days: int, alpha: float | None,
        outperformed: bool | None) -> dict[str, Any]:
    return {
        "decision_id": str(decision_id),
        "analysis_id": _id_or_empty(analysis_id),
        "user_id": _id_or_empty(user_id),
        "ticker": ticker,
        "evaluation_days": evaluation_days,
        "alpha": alpha,
        "outperformed": outperformed
    }


def backtest_event_payload(
        *, backtest_id: Any, user_id: Any, strategy_name: str | None,
        completed: bool, error: str | None = None) -> dict[str, Any]:
    return {
        "backtest_id": str(backtest_id),
        "user_id": _id_or_empty(user_id),
        "kind": backtest_kind(strategy_name),
        "status": "completed" if completed else "failed",
        "error": None if completed else (error or "backtest_failed")
    }


def backtest_deleted_payload(
        *, backtest_id: Any, user_id: Any,
        strategy_name: str | None) -> dict[str, Any]:
    return {
        "backtest_id": str(backtest_id),
        "user_id": _id_or_empty(user_id),
        "kind": backtest_kind(strategy_name)
    }


async def publish_event(
        event_name: EventName, payload: dict[str, Any]) -> None:
    try:
        await shared_events.publish_cross_service(
            settings, event_name, payload)
    except Exception as exc:
        logger.warning(
            "ai_event_publish_failed", event_name=str(event_name),
            error=f"{type(exc).__name__}: {str(exc)[:200]}")


async def publish_events(
        events: Iterable[tuple[EventName, dict[str, Any]]]) -> None:
    for event_name, payload in events:
        await publish_event(event_name, payload)


def tier_cancelled_events(
        infos: Iterable[TierEventInfo], *, reason: str,
        user_id_fallback: Any = None
) -> list[tuple[EventName, dict[str, Any]]]:
    return [(
        EventName.TIER_CANCELLED,
        tier_event_payload(
            info, reason=reason, user_id_fallback=user_id_fallback))
            for info in infos]
