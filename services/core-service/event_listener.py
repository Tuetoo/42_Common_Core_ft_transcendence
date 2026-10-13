import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID
import structlog
from api.websocket import recheck_user_sessions
from config import settings
from database import SessionLocal
from sqlalchemy import select
from websocket_manager import WebSocketEnvelope, manager
import domains.notification.service as notifications
from domains.research.repository import DocumentRepository
from shared.enums import NotificationType
from shared.events import (CrossServiceSubscriber, DurableEventSubscriber,
                           EventName)
from shared.redis import get_events_client
from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID

logger = structlog.get_logger(__name__)

_EVENT_CLAIM_TTL_SECONDS = 300


async def _claim_event(event: str, payload: dict[str, Any]) -> bool:
    event_id = payload.get("event_id")
    digest = (
        str(event_id) if event_id else hashlib.sha256(
            json.dumps(payload, sort_keys=True,
                       default=str).encode()).hexdigest())
    try:
        claimed = await get_events_client(settings).set(
            f"core:event_claim:{event}:{digest}", "1", nx=True,
            ex=_EVENT_CLAIM_TTL_SECONDS)
    except Exception:
        logger.warning("event_claim_failed", event_name=event, exc_info=True)
        return True
    return bool(claimed)


async def _notify_field(
        event: str, payload: dict[str, Any], key: str, *,
        type: NotificationType, title: str, content: str) -> None:
    recipient = payload.get(key)
    if not recipient:
        logger.warning(
            f"event_missing_{key}", event_name=str(event),
            analysis_id=payload.get("analysis_id"))
        return
    if not await _claim_event(event, payload):
        return
    await notifications.notify_user(
        user_id=recipient, type=type, title=title, content=content)


async def _notify_payload_user(
        event: str, payload: dict[str, Any], *, type: NotificationType,
        title: str, content: str) -> None:
    await _notify_field(
        event, payload, "user_id", type=type, title=title, content=content)


async def _notify_actor(
        event: str, payload: dict[str, Any], *, type: NotificationType,
        title: str, content: str) -> None:
    await _notify_field(
        event, payload, "actor_id", type=type, title=title, content=content)


async def _notify_pair(
        event: str, payload: dict[str, Any], *, type: NotificationType,
        user_title: str, user_content: str, actor_title: str,
        actor_content: str) -> None:
    user_id = payload.get("user_id") or None
    actor_id = payload.get("actor_id") or None
    if not user_id and not actor_id:
        logger.warning("event_missing_recipient", event_name=str(event))
        return
    if not await _claim_event(event, payload):
        return
    if user_id:
        await notifications.notify_user(
            user_id=user_id, type=type, title=user_title,
            content=user_content)
    if actor_id and str(actor_id) != str(user_id):
        await notifications.notify_user(
            user_id=actor_id, type=type, title=actor_title,
            content=actor_content)


async def _handle_analysis_completed(payload: dict[str, Any]) -> None:
    await _broadcast_analysis(EventName.ANALYSIS_COMPLETED, payload)
    await _notify_payload_user(
        EventName.ANALYSIS_COMPLETED, payload,
        type=NotificationType.ANALYSIS_COMPLETED,
        title="Your analysis is ready",
        content=f"Analysis {payload.get('analysis_id')} has completed.")


async def _handle_recommendation_ready(payload: dict[str, Any]) -> None:
    await _broadcast_analysis(EventName.RECOMMENDATION_READY, payload)
    await _notify_payload_user(
        EventName.RECOMMENDATION_READY, payload,
        type=NotificationType.RECOMMENDATION_READY,
        title="A new recommendation is ready",
        content=f"Analysis {payload.get('analysis_id')} produced a "
        "recommendation.")


async def _persist_document_status(payload: dict[str, Any]) -> None:
    document_id = payload.get("document_id")
    if document_id is None:
        logger.warning(
            "document_status_changed_missing_document_id", payload=payload)
        return

    try:
        document_uuid = UUID(document_id)
    except ValueError:
        logger.warning(
            "document_status_changed_invalid_document_id",
            document_id=document_id)
        return

    status = payload.get("status")
    if not isinstance(status, str):
        logger.warning(
            "document_status_changed_missing_status", document_id=document_id)
        return

    async with SessionLocal() as session:
        updated, changed = await DocumentRepository(session).transition_status(
            document_id=document_uuid, status=status)
        if updated is None:
            logger.warning(
                "document_status_changed_unknown_document",
                document_id=document_id)
        await session.commit()

    if (updated is None or not changed
            or updated.status not in ("ready", "failed")
            or updated.owner_id == SYSTEM_DOCUMENT_OWNER_ID):
        return
    name = updated.title or updated.filename
    if updated.status == "ready":
        await notifications.notify_user(
            user_id=updated.owner_id, type=NotificationType.DOCUMENT_READY,
            title="Document ready", content=(
                f'Your document "{name}" has been processed and is searchable.'
            ))
    else:
        await notifications.notify_user(
            user_id=updated.owner_id, type=NotificationType.DOCUMENT_FAILED,
            title="Document processing failed",
            content=f'Your document "{name}" could not be processed.')


async def _broadcast_document_status_changed(payload: dict[str, Any]) -> None:
    document_id = payload.get("document_id")
    await manager.broadcast(
        f"document:{document_id}",
        WebSocketEnvelope(
            event=EventName.DOCUMENT_STATUS_CHANGED, payload=payload))


def _org_name(payload: dict[str, Any]) -> str:
    name = payload.get("organization_name") or payload.get("organization_id")
    return f'"{name}"' if name else "an organization"


def _member_ids(payload: dict[str, Any]) -> list[Any]:
    ids = payload.get("member_ids")
    return list(ids) if isinstance(ids, list) else []


async def _notify_organization(
        payload: dict[str, Any], event: str, *, type: NotificationType,
        member_title: str, member_content: str, user_title: str | None = None,
        user_content: str | None = None) -> None:
    if not await _claim_event(event, payload):
        return
    affected = payload.get("user_id") or None
    if affected and user_title is not None:
        await notifications.notify_user(
            user_id=affected, type=type, title=user_title, content=user_content
            or member_content)
        others = [m for m in _member_ids(payload) if str(m) != str(affected)]
    else:
        others = [*_member_ids(payload), *([affected] if affected else [])]
    await notifications.notify_users(
        others, type=type, title=member_title, content=member_content)


async def _recheck_sessions(
        user_ids: list[Any], *, reason: str,
        deactivated: bool = False) -> None:
    for user_id in dict.fromkeys(str(u) for u in user_ids if u):
        try:
            await recheck_user_sessions(
                user_id, reason=reason, deactivated=deactivated)
        except Exception:
            logger.warning(
                "yjs_session_recheck_failed", user_id=user_id, reason=reason,
                exc_info=True)


async def _handle_organization_created(payload: dict[str, Any]) -> None:
    org = _org_name(payload)
    await _notify_organization(
        payload, EventName.ORGANIZATION_CREATED,
        type=NotificationType.ORGANIZATION_CREATED,
        member_title="Organization created",
        member_content=f"The organization {org} was created.")


async def _handle_organization_updated(payload: dict[str, Any]) -> None:
    org = _org_name(payload)
    await _notify_organization(
        payload, EventName.ORGANIZATION_UPDATED,
        type=NotificationType.ORGANIZATION_UPDATED,
        member_title="Organization updated",
        member_content=f"The organization {org} was updated.")


async def _handle_organization_deleted(payload: dict[str, Any]) -> None:
    await _recheck_sessions(
        _member_ids(payload), reason=EventName.ORGANIZATION_DELETED)
    org = _org_name(payload)
    await _notify_organization(
        payload, EventName.ORGANIZATION_DELETED,
        type=NotificationType.ORGANIZATION_DELETED,
        member_title="Organization deleted",
        member_content=f"The organization {org} was deleted.")


async def _handle_organization_member_joined(payload: dict[str, Any]) -> None:
    user_id = payload.get("user_id")
    if not user_id:
        logger.warning("organization_member_joined_missing_user_id")
        return
    org = _org_name(payload)
    await _notify_organization(
        payload, EventName.ORGANIZATION_MEMBER_JOINED,
        type=NotificationType.ORGANIZATION_MEMBER_JOINED,
        member_title="New organization member",
        member_content=f"A new member joined {org}.",
        user_title="Welcome to the organization",
        user_content=f"You joined the organization {org}.")


async def _handle_organization_member_left(payload: dict[str, Any]) -> None:
    await _recheck_sessions([payload.get("user_id")],
                            reason=EventName.ORGANIZATION_MEMBER_LEFT)
    org = _org_name(payload)
    await _notify_organization(
        payload, EventName.ORGANIZATION_MEMBER_LEFT,
        type=NotificationType.ORGANIZATION_MEMBER_LEFT,
        member_title="Member left the organization",
        member_content=f"A member left {org}.",
        user_title="You left the organization",
        user_content=f"You left the organization {org}.")


async def _handle_organization_member_removed(payload: dict[str, Any]) -> None:
    await _recheck_sessions([payload.get("user_id")],
                            reason=EventName.ORGANIZATION_MEMBER_REMOVED)
    org = _org_name(payload)
    await _notify_organization(
        payload, EventName.ORGANIZATION_MEMBER_REMOVED,
        type=NotificationType.ORGANIZATION_MEMBER_REMOVED,
        member_title="Member removed from the organization",
        member_content=f"A member was removed from {org}.",
        user_title="Removed from an organization",
        user_content=f"You were removed from the organization {org}.")


async def _handle_organization_role_changed(payload: dict[str, Any]) -> None:
    await _recheck_sessions([payload.get("user_id")],
                            reason=EventName.ORGANIZATION_ROLE_CHANGED)
    org = _org_name(payload)
    role = payload.get("role") or "a new role"
    await _notify_organization(
        payload, EventName.ORGANIZATION_ROLE_CHANGED,
        type=NotificationType.ORGANIZATION_ROLE_CHANGED,
        member_title="Organization role changed",
        member_content=f"A member of {org} now has the role {role}.",
        user_title="Your organization role changed",
        user_content=f"Your role in {org} is now {role}.")


async def _handle_organization_invitation_created(
        payload: dict[str, Any]) -> None:
    org = _org_name(payload)
    role = payload.get("role")
    as_role = f" (role {role})" if role else ""
    await _notify_pair(
        EventName.ORGANIZATION_INVITATION_CREATED, payload,
        type=NotificationType.ORGANIZATION_INVITE,
        user_title="Organization invitation",
        user_content=f"You were invited to join the organization {org}.",
        actor_title="Invitation sent",
        actor_content=(
            f"You invited a user to join the organization {org}{as_role}."))


async def _handle_organization_invitation_revoked(
        payload: dict[str, Any]) -> None:
    org = _org_name(payload)
    role = payload.get("role")
    as_role = f" (role {role})" if role else ""
    await _notify_pair(
        EventName.ORGANIZATION_INVITATION_REVOKED, payload,
        type=NotificationType.ORGANIZATION_INVITATION_REVOKED,
        user_title="Invitation withdrawn",
        user_content=f"Your invitation to the organization {org} was "
        "withdrawn.", actor_title="Invitation revoked", actor_content=(
            f"You revoked an invitation to the organization {org}{as_role}."))


def _actor(payload: dict[str, Any]) -> str:
    return str(payload.get("actor_username") or "Someone")


def _other(payload: dict[str, Any]) -> str:
    return str(payload.get("other_username") or "a user")


async def _handle_friend_requested(payload: dict[str, Any]) -> None:
    await _notify_pair(
        EventName.FRIEND_REQUESTED, payload,
        type=NotificationType.FRIEND_REQUEST, user_title="New friend request",
        user_content=f"{_actor(payload)} sent you a friend request.",
        actor_title="Friend request sent",
        actor_content=f"You sent a friend request to {_other(payload)}.")


async def _handle_friend_accepted(payload: dict[str, Any]) -> None:
    await _notify_pair(
        EventName.FRIEND_ACCEPTED, payload,
        type=NotificationType.FRIEND_ACCEPTED,
        user_title="Friend request accepted",
        user_content=f"{_actor(payload)} accepted your friend request.",
        actor_title="Friend request accepted",
        actor_content=f"You are now friends with {_other(payload)}.")


_CONNECTION_REMOVAL_TEXT = {
    "cancelled": ("Friend request cancelled",
                  "You cancelled your friend request to {name}."),
    "declined": ("Friend request declined",
                 "You declined the friend request from {name}."),
    "unblocked": ("User unblocked", "You unblocked {name}.")
}


async def _handle_friend_removed(payload: dict[str, Any]) -> None:
    await _recheck_sessions(
        [payload.get("actor_id"), payload.get("user_id")],
        reason=EventName.FRIEND_REMOVED)
    name = _other(payload)
    action = str(payload.get("action") or "removed")
    if action != "removed":
        title, text = _CONNECTION_REMOVAL_TEXT.get(
            action, ("Connection updated", "Your connection with {name} "
                     "changed."))
        await _notify_actor(
            EventName.FRIEND_REMOVED, payload,
            type=NotificationType.FRIEND_REMOVED, title=title,
            content=text.format(name=name))
        return
    await _notify_pair(
        EventName.FRIEND_REMOVED, payload,
        type=NotificationType.FRIEND_REMOVED, user_title="Friend removed",
        user_content=f"{_actor(payload)} removed you from their friends.",
        actor_title="Friend removed",
        actor_content=f"You removed {name} from your friends.")


async def _handle_connection_blocked(payload: dict[str, Any]) -> None:
    await _recheck_sessions(
        [payload.get("actor_id"), payload.get("blocked_user_id")],
        reason=EventName.CONNECTION_BLOCKED)
    name = payload.get("blocked_username") or "a user"
    await _notify_actor(
        EventName.CONNECTION_BLOCKED, payload,
        type=NotificationType.CONNECTION_BLOCKED, title="User blocked",
        content=f"You blocked {name}. They are not notified.")


def _target(payload: dict[str, Any]) -> str:
    return str(payload.get("username") or "a user")


async def _handle_user_updated(payload: dict[str, Any]) -> None:
    changed = payload.get("changed")
    fields = (
        ", ".join(str(f)
                  for f in changed) if isinstance(changed, list) else "")
    suffix = f" ({fields})" if fields else ""
    await _notify_pair(
        EventName.USER_UPDATED, payload, type=NotificationType.ACCOUNT_UPDATED,
        user_title="Account updated",
        user_content=f"Your account was updated{suffix}.",
        actor_title="Account updated",
        actor_content=f"You updated the account of {_target(payload)}"
        f"{suffix}.")


async def _handle_user_role_changed(payload: dict[str, Any]) -> None:
    await _recheck_sessions([payload.get("user_id")],
                            reason=EventName.USER_ROLE_CHANGED)
    role = payload.get("role") or "a new role"
    await _notify_pair(
        EventName.USER_ROLE_CHANGED, payload,
        type=NotificationType.ACCOUNT_ROLE_CHANGED,
        user_title="Your role changed",
        user_content=f"Your account role is now {role}.",
        actor_title="Account role changed",
        actor_content=f"You changed the role of {_target(payload)} to {role}.")


async def _handle_user_deactivated(payload: dict[str, Any]) -> None:
    await _recheck_sessions([payload.get("user_id")],
                            reason=EventName.USER_DEACTIVATED,
                            deactivated=True)
    await _notify_pair(
        EventName.USER_DEACTIVATED, payload,
        type=NotificationType.ACCOUNT_UPDATED,
        user_title="Account deactivated",
        user_content="Your account was deactivated by an administrator.",
        actor_title="Account deactivated",
        actor_content=f"You deactivated the account of {_target(payload)}.")


async def _handle_user_reactivated(payload: dict[str, Any]) -> None:
    await _notify_pair(
        EventName.USER_REACTIVATED, payload,
        type=NotificationType.ACCOUNT_UPDATED,
        user_title="Account reactivated",
        user_content="Your account was reactivated.",
        actor_title="Account reactivated",
        actor_content=f"You reactivated the account of {_target(payload)}.")


async def _handle_user_deleted(payload: dict[str, Any]) -> None:
    await _recheck_sessions([payload.get("user_id")],
                            reason=EventName.USER_DELETED,
                            deactivated=True)
    logger.info("user_deleted_received", user_id=payload.get("user_id"))
    if str(payload.get("actor_id")) == str(payload.get("user_id")):
        return
    await _notify_actor(
        EventName.USER_DELETED, payload, type=NotificationType.ACCOUNT_UPDATED,
        title="Account deleted",
        content=f"You deleted the account of {_target(payload)}.")


async def _broadcast_analysis(event: str, payload: dict[str, Any]) -> None:
    analysis_id = payload.get("analysis_id")
    if not analysis_id:
        return
    sent = await manager.broadcast(
        f"analysis:{analysis_id}",
        WebSocketEnvelope(event=event, payload=payload))
    logger.info(
        "websocket_analysis_broadcast", event_name=str(event),
        analysis_id=str(analysis_id), sockets=sent)


async def _handle_analysis_started(payload: dict[str, Any]) -> None:
    await _broadcast_analysis(EventName.ANALYSIS_STARTED, payload)
    ticker = payload.get("ticker") or "your ticker"
    await _notify_payload_user(
        EventName.ANALYSIS_STARTED, payload,
        type=NotificationType.ANALYSIS_STARTED,
        title="Analysis started", content=(
            f"The analysis of {ticker} ({payload.get('analysis_id')}) started."
        ))


async def _handle_analysis_failed(payload: dict[str, Any]) -> None:
    await _broadcast_analysis(EventName.ANALYSIS_FAILED, payload)
    ticker = payload.get("ticker") or "your ticker"
    reason = payload.get("failure_reason")
    await _notify_payload_user(
        EventName.ANALYSIS_FAILED, payload,
        type=NotificationType.ANALYSIS_FAILED,
        title="Analysis failed", content=(
            f"The analysis of {ticker} failed: {reason}."
            if reason else f"The analysis of {ticker} failed."))


async def _handle_decision_recorded(payload: dict[str, Any]) -> None:
    await _broadcast_analysis(EventName.DECISION_RECORDED, payload)
    ticker = payload.get("ticker") or "the ticker"
    decision = payload.get("decision") or "recorded"
    await _notify_payload_user(
        EventName.DECISION_RECORDED, payload,
        type=NotificationType.DECISION_RECORDED,
        title="Decision recorded",
        content=f"Your decision on {ticker} was recorded ({decision}).")


async def _handle_decision_reviewed(payload: dict[str, Any]) -> None:
    ticker = payload.get("ticker") or "the ticker"
    await _notify_payload_user(
        EventName.DECISION_REVIEWED, payload,
        type=NotificationType.DECISION_REVIEWED, title="Decision reviewed",
        content=f"A new review of your decision on {ticker} is ready.")


async def _handle_decision_outcome_evaluated(payload: dict[str, Any]) -> None:
    ticker = payload.get("ticker") or "the ticker"
    days = payload.get("evaluation_days")
    after = f" after {days} days" if days else ""
    alpha = payload.get("alpha")
    if isinstance(alpha, int | float):
        verdict = (
            "outperformed"
            if payload.get("outperformed") else "did not outperform")
        result = (
            f": it {verdict} the benchmark "
            f"({alpha * 100:+.1f} percentage points)")
    else:
        result = " (no price data to compare with the benchmark)"
    await _notify_payload_user(
        EventName.DECISION_OUTCOME_EVALUATED, payload,
        type=NotificationType.DECISION_EVALUATED,
        title="Decision evaluated",
        content=f"Your decision on {ticker} was evaluated{after}{result}.")


async def _portfolio_owner(portfolio_id: Any) -> UUID | None:
    from domains.portfolio.model import Portfolio

    try:
        portfolio_uuid = UUID(str(portfolio_id))
    except ValueError:
        return None
    async with SessionLocal() as session:
        owner: UUID | None = (
            await session.execute(
                select(
                    Portfolio.owner_id).where(Portfolio.id == portfolio_uuid)
            )).scalar_one_or_none()
    return owner


async def _handle_tier_event(
        event: str, payload: dict[str, Any], *, type: NotificationType,
        title: str, describe: Callable[[dict[str, Any]], str]) -> None:
    await _broadcast_analysis(event, payload)
    if not await _claim_event(event, payload):
        return
    user_id: Any = payload.get("user_id") or None
    if not user_id and payload.get("portfolio_id"):
        user_id = await _portfolio_owner(payload["portfolio_id"])
    if not user_id:
        logger.warning(
            "tier_event_without_recipient", event_name=event,
            tier_id=payload.get("tier_id"))
        return
    await notifications.notify_user(
        user_id=user_id, type=type, title=title, content=describe(payload))


def _tier_text(payload: dict[str, Any]) -> str:
    action = str(payload.get("action") or "trade").lower()
    quantity = payload.get("quantity") or "?"
    ticker = payload.get("ticker") or "?"
    price = payload.get("price")
    at = f" at {price}" if price else ""
    return f"{action} {quantity} {ticker}{at}"


async def _handle_tier_executed(payload: dict[str, Any]) -> None:
    await _handle_tier_event(
        EventName.TIER_EXECUTED, payload, type=NotificationType.TIER_EXECUTED,
        title="Tier executed",
        describe=lambda p: f"A planned tier was executed: {_tier_text(p)}.")


async def _handle_tier_execution_failed(payload: dict[str, Any]) -> None:

    def _describe(p: dict[str, Any]) -> str:
        reason = p.get("reason")
        suffix = f": {reason}" if reason else ""
        return (
            f"A planned tier could not be executed ({_tier_text(p)}){suffix}.")

    await _handle_tier_event(
        EventName.TIER_EXECUTION_FAILED, payload,
        type=NotificationType.TIER_EXECUTION_FAILED,
        title="Tier execution failed", describe=_describe)


async def _handle_tier_cancelled(payload: dict[str, Any]) -> None:

    def _describe(p: dict[str, Any]) -> str:
        reason = p.get("reason")
        suffix = f": {reason}" if reason else ""
        return f"A planned tier was cancelled ({_tier_text(p)}){suffix}."

    await _handle_tier_event(
        EventName.TIER_CANCELLED, payload,
        type=NotificationType.TIER_CANCELLED, title="Tier cancelled",
        describe=_describe)


async def _handle_backtest_completed(payload: dict[str, Any]) -> None:
    kind = payload.get("kind") or "backtest"
    await _notify_payload_user(
        EventName.BACKTEST_COMPLETED, payload,
        type=NotificationType.BACKTEST_COMPLETED,
        title="Backtest completed", content=(
            f"Your {kind} backtest ({payload.get('backtest_id')}) completed."))


async def _handle_backtest_failed(payload: dict[str, Any]) -> None:
    kind = payload.get("kind") or "backtest"
    error = payload.get("error")
    await _notify_payload_user(
        EventName.BACKTEST_FAILED, payload,
        type=NotificationType.BACKTEST_FAILED,
        title="Backtest failed", content=(
            f"Your {kind} backtest failed: {error}."
            if error else f"Your {kind} backtest failed."))


async def _handle_backtest_deleted(payload: dict[str, Any]) -> None:
    kind = payload.get("kind") or "backtest"
    await _notify_payload_user(
        EventName.BACKTEST_DELETED, payload,
        type=NotificationType.BACKTEST_DELETED,
        title="Backtest deleted", content=(
            f"Your {kind} backtest ({payload.get('backtest_id')}) was "
            "deleted."))


async def _handle_user_presence_changed(payload: dict[str, Any]) -> None:
    user_id = payload.get("user_id")
    if user_id is None:
        logger.warning("user_presence_changed_missing_user_id")
        return
    await manager.broadcast(
        f"presence:{user_id}",
        WebSocketEnvelope(
            event=EventName.USER_PRESENCE_CHANGED, payload=payload))


_PUBSUB_HANDLERS: dict[EventName, Callable[[
    dict[str, Any]
], Awaitable[None]]] = {
    EventName.ANALYSIS_STARTED: _handle_analysis_started,
    EventName.ANALYSIS_COMPLETED: _handle_analysis_completed,
    EventName.ANALYSIS_FAILED: _handle_analysis_failed,
    EventName.RECOMMENDATION_READY: _handle_recommendation_ready,
    EventName.DECISION_RECORDED: _handle_decision_recorded,
    EventName.DECISION_REVIEWED: _handle_decision_reviewed,
    EventName.DECISION_OUTCOME_EVALUATED: _handle_decision_outcome_evaluated,
    EventName.TIER_EXECUTED: _handle_tier_executed,
    EventName.TIER_EXECUTION_FAILED: _handle_tier_execution_failed,
    EventName.TIER_CANCELLED: _handle_tier_cancelled,
    EventName.BACKTEST_COMPLETED: _handle_backtest_completed,
    EventName.BACKTEST_FAILED: _handle_backtest_failed,
    EventName.BACKTEST_DELETED: _handle_backtest_deleted,
    EventName.DOCUMENT_STATUS_CHANGED: _broadcast_document_status_changed,
    EventName.ORGANIZATION_CREATED: _handle_organization_created,
    EventName.ORGANIZATION_UPDATED: _handle_organization_updated,
    EventName.ORGANIZATION_DELETED: _handle_organization_deleted,
    EventName.ORGANIZATION_MEMBER_JOINED: _handle_organization_member_joined,
    EventName.ORGANIZATION_MEMBER_LEFT: _handle_organization_member_left,
    EventName.ORGANIZATION_MEMBER_REMOVED: _handle_organization_member_removed,
    EventName.ORGANIZATION_ROLE_CHANGED: _handle_organization_role_changed,
    EventName.ORGANIZATION_INVITATION_CREATED: (
        _handle_organization_invitation_created),
    EventName.ORGANIZATION_INVITATION_REVOKED: (
        _handle_organization_invitation_revoked),
    EventName.FRIEND_REQUESTED: _handle_friend_requested,
    EventName.FRIEND_ACCEPTED: _handle_friend_accepted,
    EventName.FRIEND_REMOVED: _handle_friend_removed,
    EventName.CONNECTION_BLOCKED: _handle_connection_blocked,
    EventName.USER_UPDATED: _handle_user_updated,
    EventName.USER_ROLE_CHANGED: _handle_user_role_changed,
    EventName.USER_DEACTIVATED: _handle_user_deactivated,
    EventName.USER_REACTIVATED: _handle_user_reactivated,
    EventName.USER_DELETED: _handle_user_deleted,
    EventName.USER_PRESENCE_CHANGED: _handle_user_presence_changed
}


def build_subscriber() -> CrossServiceSubscriber:
    subscriber = CrossServiceSubscriber(settings)
    for event_name, handler in _PUBSUB_HANDLERS.items():
        subscriber.on(event_name, handler)
    return subscriber


def build_durable_subscriber() -> DurableEventSubscriber:
    subscriber = DurableEventSubscriber(
        settings, consumer_name=settings.SERVICE_NAME)
    subscriber.on(EventName.DOCUMENT_STATUS_CHANGED, _persist_document_status)
    return subscriber
