from collections import defaultdict
from fastapi import WebSocket
from shared.schemas.websocket import WebSocketEnvelope


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, websocket: WebSocket, channel: str) -> None:
        await websocket.accept()
        self._connections[channel].add(websocket)

    def disconnect(self, websocket: WebSocket, channel: str) -> None:
        self._connections[channel].discard(websocket)

    async def broadcast(self, channel: str, envelope: WebSocketEnvelope
                        ) -> None:
        dead: list[WebSocket] = []
        for connection in self._connections.get(channel, set()):
            try:
                await connection.send_text(envelope.model_dump_json())
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection, channel)


manager = WebSocketManager()
