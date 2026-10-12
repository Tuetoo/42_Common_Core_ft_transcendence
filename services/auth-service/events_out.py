from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID
import structlog
from config import settings
from fastapi import BackgroundTasks
from shared.events import EventName, publish_cross_service
from shared.user_state import invalidate_membership_cache

logger = structlog.get_logger(__name__)

PendingEvent = tuple[EventName, dict[str, Any]]

MEMBERSHIP_EVENTS = frozenset({
    EventName.ORGANIZATION_CREATED, EventName.ORGANIZATION_DELETED,
    EventName.ORGANIZATION_MEMBER_JOINED, EventName.ORGANIZATION_MEMBER_LEFT,
    EventName.ORGANIZATION_MEMBER_REMOVED, EventName.ORGANIZATION_ROLE_CHANGED
})


async def publish_events(events: Sequence[PendingEvent]) -> None:
    affected: set[str] = set()
    for name, payload in events:
        if name in MEMBERSHIP_EVENTS:
            if payload.get("user_id"):
                affected.add(str(payload["user_id"]))
            if name == EventName.ORGANIZATION_DELETED:
                affected.update(
                    str(m) for m in payload.get("member_ids") or [])
    if affected:
        await invalidate_membership_cache(settings, affected)
    for name, payload in events:
        try:
            await publish_cross_service(settings, name, payload)
        except Exception as exc:
            logger.warning(
                "event_publish_failed", event_name=str(name), error=str(exc))


def schedule_events(
        background_tasks: BackgroundTasks,
        events: Iterable[PendingEvent]) -> None:
    pending = list(events)
    if pending:
        background_tasks.add_task(publish_events, pending)


def user_event_payload(
        *, user_id: UUID, actor_id: UUID, role: str, is_active: bool,
        changed: Iterable[str]) -> dict[str, Any]:
    return {
        "user_id": str(user_id),
        "actor_id": str(actor_id),
        "role": role,
        "is_active": is_active,
        "changed": sorted(set(changed))
    }


def organization_event_payload(
        *, organization_id: UUID, organization_name: str, actor_id: UUID,
        user_id: UUID | None, role: str | None,
        member_ids: Iterable[UUID]) -> dict[str, Any]:
    return {
        "organization_id": str(organization_id),
        "organization_name": organization_name,
        "actor_id": str(actor_id),
        "user_id": str(user_id) if user_id is not None else None,
        "role": role,
        "member_ids": sorted({str(member_id)
                              for member_id in member_ids})
    }


def friend_event_payload(
        *, actor_id: UUID, user_id: UUID,
        actor_username: str) -> dict[str, Any]:
    return {
        "actor_id": str(actor_id),
        "user_id": str(user_id),
        "actor_username": actor_username
    }
