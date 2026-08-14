from uuid import UUID
import clients.auth_client as auth_client
import yjs_relay
from config import settings
from database import SessionLocal
from domains.research.repository import ResearchNoteRepository
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from shared.security import decode_token

router = APIRouter(tags=["yjs"])


async def _can_edit_note(note_id: UUID, user_id: str) -> bool:
    async with SessionLocal() as session:
        repo = ResearchNoteRepository(session)
        note = await repo.get_by_id(note_id)
        if note is None:
            return False
        if str(note.author_id) == user_id:
            return True
        if note.organization_id is not None:
            org_ids = await auth_client.list_user_organization_ids(user_id)
            return note.organization_id in org_ids
        return False


@router.websocket("/ws/yjs/{note_id}")
async def yjs_websocket_endpoint(
    websocket: WebSocket, note_id: str, token: str | None = Query(default=None)
) -> None:
    try:
        claims = decode_token(token or "", settings)
        if claims.get("type") != "access":
            raise ValueError("wrong token type")
        note_uuid = UUID(note_id)
    except Exception:
        await websocket.close(code=4401, reason="invalid token or note id")
        return
    if not await _can_edit_note(note_uuid, claims["sub"]):
        await websocket.close(code=4403, reason="not authorized for this note")
        return
    room = await yjs_relay.relay.connect(websocket, note_id)
    try:
        while True:
            update = await websocket.receive_bytes()
            await yjs_relay.relay.handle_local_update(
                room, update, sender=websocket)
    except WebSocketDisconnect:
        yjs_relay.relay.disconnect(websocket, room)
