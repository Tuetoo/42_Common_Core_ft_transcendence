import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
import structlog
from shared.config import BaseServiceSettings
from shared.redis import get_events_client

logger = structlog.get_logger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class LocalEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._subscribers[event_name].append(handler)

    async def publish(self, event_name: str, payload: dict[str, Any]) -> None:
        handlers = self._subscribers.get(event_name, [])
        if handlers:
            await asyncio.gather(*(handler(payload) for handler in handlers))


async def publish_cross_service(
          settings: BaseServiceSettings, event_name: str,
          payload: dict[str, Any]) -> None:
    logger.debug("cross_service_event_published", event=event_name,
                 payload=payload)
    client = get_events_client(settings)
    await client.publish(f"events:{event_name}", json.dumps(payload))


class CrossServiceSubscriber:
    def __init__(self, settings: BaseServiceSettings) -> None:
        self._settings = settings
        self._handlers: dict[str, EventHandler] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name] = handler

    async def run(self) -> None:
        client = get_events_client(self._settings)
        pubsub = client.pubsub()
        channels = [f"events:{name}" for name in self._handlers]
        if not channels:
            return
        await pubsub.subscribe(*channels)
        logger.info("cross_service_subscriber_started", channels=channels)
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            channel = message["channel"]
            event_name = channel.removeprefix("events:")
            handler = self._handlers.get(event_name)
            if handler is None:
                continue
            try:
                payload = json.loads(message["data"])
                await handler(payload)
            except Exception:
                logger.exception("cross_service_event_handler_failed",
                                 event=event_name)
