from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from config import settings
import clients.auth_client as auth_client
from websocket_manager import manager
from shared.security import decode_token

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str,
                             token: str | None = Query(default=None)) -> None:
    """Frontend connect to eg `/ws/analysis:<uuid>`or`/ws/portfolio:<uuid>`"""
    try:
        claims = decode_token(token or "", settings)
        if claims.get("type") != "access":
            raise ValueError("wrong token type")
    except Exception:
        await websocket.close(code=4401, reason="invalid or missing token")
        return
    requester_id = claims.get("sub")
    if not isinstance(requester_id, str):
        await websocket.close(code=4401, reason="invalid or missing token")
        return
    if channel.startswith("user:") and channel.removeprefix("user:"
                                                            ) != requester_id:
        await websocket.close(
            code=4403, reason="not authorized for this channel")
        return
    if channel.startswith("presence:"):
        target_id = channel.removeprefix("presence:")
        if target_id != requester_id:
            connected = await auth_client.are_users_connected(requester_id,
                                                              target_id)
            if not connected:
                await websocket.close(
                    code=4403, reason="not authorized for this channel")
                return
    await manager.connect(websocket, channel)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)
