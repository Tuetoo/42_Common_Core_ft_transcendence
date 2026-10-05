from uuid import UUID
import clients.ai_client as ai_client
import clients.auth_client as auth_client
import structlog
from api.ws_common import (CLOSE_BAD_REQUEST, CLOSE_FORBIDDEN,
                           CLOSE_INTERNAL_ERROR, CLOSE_TOO_MANY_CONNECTIONS,
                           CLOSE_UNAUTHORIZED, CLOSE_UNSUPPORTED_DATA,
                           accept_and_close, authenticate, close_quietly,
                           resolve_live_identity, serve_until_expiry)
from database import SessionLocal
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from websocket_manager import (ConnectionLimitExceeded, WebSocketEnvelope,
                               manager)
from domains.portfolio.service import PortfolioService
from domains.research.service import ResearchService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])

_CHANNEL_PREFIXES = ("user", "presence", "analysis", "document")


async def _can_read_analysis(requester_id: str, analysis_id: UUID) -> bool:
    ref = await ai_client.get_analysis_ref(str(analysis_id))
    if ref.user_id is not None:
        return str(ref.user_id) == requester_id
    if ref.portfolio_id is None:
        return False
    async with SessionLocal() as session:
        await PortfolioService(session).get(
            ref.portfolio_id, UUID(requester_id))
    return True


async def _can_read_document(requester_id: str, document_id: UUID) -> bool:
    async with SessionLocal() as session:
        await ResearchService(session).get_document(
            document_id, UUID(requester_id))
    return True


ANALYSIS_STATUS_EVENT = "analysis.status"


async def send_analysis_status(websocket: WebSocket, analysis_id: str) -> None:
    try:
        ref = await ai_client.get_analysis_ref(analysis_id)
        envelope = WebSocketEnvelope(
            event=ANALYSIS_STATUS_EVENT, payload={
                "analysis_id": str(ref.id),
                "ticker": ref.ticker,
                "status": str(ref.status),
                "portfolio_id": (
                    str(ref.portfolio_id) if ref.portfolio_id else None)
            })
        await websocket.send_text(envelope.model_dump_json())
    except Exception:
        logger.info(
            "websocket_analysis_status_skipped", analysis_id=analysis_id,
            exc_info=True)


async def authorize_channel(channel: str,
                            requester_id: str) -> tuple[bool, int]:
    prefix, sep, target = channel.partition(":")
    if not sep or prefix not in _CHANNEL_PREFIXES or not target:
        return False, CLOSE_BAD_REQUEST
    try:
        if prefix == "user":
            allowed = target == requester_id
        elif prefix == "presence":
            allowed = (
                target == requester_id
                or await auth_client.are_users_connected(requester_id, target))
        elif prefix == "analysis":
            allowed = await _can_read_analysis(requester_id, UUID(target))
        else:
            allowed = await _can_read_document(requester_id, UUID(target))
    except Exception:
        logger.info(
            "websocket_channel_auth_denied", channel=channel, exc_info=True)
        allowed = False
    return allowed, CLOSE_FORBIDDEN


@router.websocket("/ws/{channel}")
async def websocket_endpoint(
        websocket: WebSocket, channel: str,
        token: str | None = Query(default=None)) -> None:
    token_identity = authenticate(token)
    if token_identity is None:
        await accept_and_close(
            websocket, CLOSE_UNAUTHORIZED, "invalid or missing token")
        return
    identity = await resolve_live_identity(token_identity)
    if identity is None:
        await accept_and_close(
            websocket, CLOSE_UNAUTHORIZED, "account deactivated")
        return
    requester_id = identity.user_id

    allowed, code = await authorize_channel(channel, requester_id)
    if not allowed:
        await accept_and_close(
            websocket, code, "not authorized for this channel")
        return

    try:
        manager.acquire_user(requester_id)
    except ConnectionLimitExceeded:
        await accept_and_close(
            websocket, CLOSE_TOO_MANY_CONNECTIONS, "too many connections")
        return

    traced = False
    try:
        await websocket.accept()
        await manager.connect(websocket, channel)
        prefix, _, target = channel.partition(":")
        traced = prefix in ("analysis", "document")
        if traced:
            logger.info(
                "websocket_opened", channel=channel, user_id=requester_id)
        if prefix == "analysis":
            await send_analysis_status(websocket, target)

        async def _receive_loop() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is None:
                    await close_quietly(
                        websocket, CLOSE_UNSUPPORTED_DATA, "text frames only")
                    return

        await serve_until_expiry(websocket, identity, _receive_loop)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket_connection_failed", channel=channel)
        await close_quietly(websocket, CLOSE_INTERNAL_ERROR)
    finally:
        manager.disconnect(websocket, channel)
        manager.release_user(requester_id)
        if traced:
            logger.info(
                "websocket_closed", channel=channel, user_id=requester_id)
