import asyncio
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any
from uuid import UUID
from config import settings
from fastapi import HTTPException, status
from shared.exceptions import (NotFoundError, PermissionDeniedError,
                               UpstreamServiceError)
from shared.permissions import Permission
from shared.security import (CurrentUser, make_get_current_user,
                             require_permission)

if TYPE_CHECKING:
    from domains.analysis.model import Analysis

get_current_user = make_get_current_user(settings)
get_analysis_writer = require_permission(
    Permission.ANALYSIS_CREATE)(get_current_user)
get_rag_user = require_permission(Permission.RAG_ASK)(get_current_user)


def _shared_org_id(owner: dict[str, Any]) -> str | None:
    org_id = owner.get("organization_id")
    if org_id is None or owner.get("visibility") == "private":
        return None
    return str(org_id)


def _readable(
        owner: dict[str, Any] | None, user_id: str,
        member_org_ids: Collection[str]) -> bool:
    if owner is None:
        return False
    if str(owner.get("owner_id")) == user_id:
        return True
    org_id = _shared_org_id(owner)
    return org_id is not None and org_id in member_org_ids


def _ownership_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Portfolio ownership check is unavailable, try again later")


async def _portfolio_owner(portfolio_id: UUID) -> dict[str, Any] | None:
    from clients import core_client

    try:
        return await core_client.get_portfolio_owner(str(portfolio_id))
    except UpstreamServiceError as exc:
        raise _ownership_unavailable() from exc


async def _member_org_ids(user_id: str) -> set[str]:
    from clients import auth_client

    try:
        return {
            str(org)
            for org in await auth_client.list_user_organization_ids(user_id)
        }
    except UpstreamServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=(
                "Organization membership check is unavailable, try again later"
            )) from exc


async def _ensure_readable(
        user: CurrentUser, owner: dict[str, Any] | None) -> None:
    me = str(user.user_id)
    needs_membership = (
        owner is not None and str(owner.get("owner_id")) != me
        and _shared_org_id(owner) is not None)
    member_org_ids = await _member_org_ids(me) if needs_membership else set()
    if not _readable(owner, me, member_org_ids):
        raise NotFoundError("Portfolio not found.")


async def ensure_portfolio_access(
        user: CurrentUser, portfolio_id: UUID) -> None:
    await _ensure_readable(user, await _portfolio_owner(portfolio_id))


async def ensure_portfolio_owner(
        user: CurrentUser, portfolio_id: UUID) -> None:
    owner = await _portfolio_owner(portfolio_id)
    if owner is not None and str(owner.get("owner_id")) == str(user.user_id):
        return
    await _ensure_readable(user, owner)
    raise PermissionDeniedError(
        "Only the portfolio owner can start analyses or record decisions "
        "that may trade in this portfolio.")


_OWNER_LOOKUP_CONCURRENCY = 8


async def accessible_portfolio_ids(
        user: CurrentUser, portfolio_ids: Iterable[UUID]) -> set[UUID]:
    from clients import core_client

    distinct = list(dict.fromkeys(portfolio_ids))
    if not distinct:
        return set()
    semaphore = asyncio.Semaphore(_OWNER_LOOKUP_CONCURRENCY)

    async def _owner(pid: UUID) -> dict[str, Any] | None:
        async with semaphore:
            return await core_client.get_portfolio_owner(str(pid))

    try:
        owners = await asyncio.gather(*(_owner(pid) for pid in distinct))
    except UpstreamServiceError as exc:
        raise _ownership_unavailable() from exc
    me = str(user.user_id)
    needs_membership = any(
        owner is not None and str(owner.get("owner_id")) != me
        and _shared_org_id(owner) is not None for owner in owners)
    member_org_ids = await _member_org_ids(me) if needs_membership else set()
    return {
        pid
        for pid, owner in zip(distinct, owners, strict=True)
        if _readable(owner, me, member_org_ids)
    }


async def ensure_analysis_access(
        user: CurrentUser, analysis: "Analysis") -> None:
    if analysis.user_id is not None:
        if analysis.user_id == user.user_id:
            return
        raise NotFoundError(f"Analysis {analysis.id} not found")
    if analysis.portfolio_id is None:
        raise NotFoundError(f"Analysis {analysis.id} not found")
    try:
        await ensure_portfolio_access(user, analysis.portfolio_id)
    except NotFoundError:
        raise NotFoundError(f"Analysis {analysis.id} not found") from None
