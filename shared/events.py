import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, cast
import structlog
import redis.asyncio as redis
from redis.exceptions import ResponseError
from shared.config import BaseServiceSettings
from shared.redis import get_events_client, get_queue_client

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
          payload: dict[str, Any], *, log: bool = True) -> None:
    if log:
        logger.debug("cross_service_event_published", event_name=event_name,
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
                                 event_name=event_name)


_CONSUMER_GROUP = "workers"
_MAX_DELIVERY_ATTEMPTS = 5
_RECLAIM_EVERY_N_ITERATIONS = 12


async def publish_durable_event(settings: BaseServiceSettings, event_name: str,
                                payload: dict[str, Any], *, log: bool = True
                                ) -> None:
    if log:
        logger.debug("durable_event_published", event_name=event_name,
                     payload=payload)
    client = get_queue_client(settings)
    await client.xadd(f"stream:{event_name}",
                      {"payload": json.dumps(payload)})


class DurableEventSubscriber:
    def __init__(self, settings: BaseServiceSettings, consumer_name: str,
                 group: str = _CONSUMER_GROUP) -> None:
        self._settings = settings
        self._group = group
        self._consumer_name = consumer_name
        self._handlers: dict[str, EventHandler] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name] = handler

    async def _handle_one(self, client: redis.Redis, stream: str,
                          event_name: str, handler: EventHandler | None,
                          message_id: str, fields: dict[str, str]) -> None:
        if handler is None:
            await client.xack(stream, self._group, message_id)
            return
        try:
            payload = json.loads(fields["payload"])
            await handler(payload)
        except Exception:
            pending = await client.xpending_range(
                stream, self._group, min=message_id, max=message_id, count=1)
            delivery_count = int(pending[0]["times_delivered"]
                                 ) if pending else 1
            logger.exception("durable_event_handler_failed",
                             event_name=event_name, message_id=message_id,
                             delivery_count=delivery_count)
            if delivery_count >= _MAX_DELIVERY_ATTEMPTS:
                logger.error("durable_event_dead_lettered",
                             event_name=event_name, message_id=message_id)
                await client.xack(stream, self._group, message_id)
            return
        await client.xack(stream, self._group, message_id)

    async def _reclaim_pending(self, client: redis.Redis, streams: list[str]
                               ) -> None:
        for stream in streams:
            event_name = stream.removeprefix("stream:")
            handler = self._handlers.get(event_name)
            cursor = "0-0"
            while True:
                cursor, claimed, _ = await client.xautoclaim(
                    stream, self._group, self._consumer_name,
                    min_idle_time=60000, start_id=cursor, count=50)
                for message_id, fields in claimed:
                    await self._handle_one(client, stream, event_name, handler,
                                           message_id, fields)
                if cursor == "0-0":
                    break

    async def run(self) -> None:
        if not self._handlers:
            return
        client: redis.Redis = get_queue_client(self._settings)
        streams = [f"stream:{name}" for name in self._handlers]
        for stream in streams:
            try:
                await client.xgroup_create(stream, self._group, id="0",
                                           mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        logger.info("durable_event_subscriber_started", streams=streams,
                    consumer_name=self._consumer_name)
        try:
            await self._reclaim_pending(client, streams)
        except Exception:
            logger.exception("durable_event_subscriber_reclaim_failed",
                             consumer_name=self._consumer_name)
        read_targets = dict.fromkeys(streams, ">")
        iterations_since_reclaim = 0
        while True:
            try:
                raw_response = await client.xreadgroup(
                    self._group, self._consumer_name,
                    read_targets,  # type: ignore[arg-type]
                    count=10, block=5000)
                response = cast(
                    "list[tuple[str, list[tuple[str, dict[str, str]]]]] "
                    "| None", raw_response)
                for stream, messages in response or []:
                    event_name = stream.removeprefix("stream:")
                    handler = self._handlers.get(event_name)
                    for message_id, fields in messages:
                        await self._handle_one(client, stream, event_name,
                                               handler, message_id, fields)
                iterations_since_reclaim += 1
                if iterations_since_reclaim >= _RECLAIM_EVERY_N_ITERATIONS:
                    await self._reclaim_pending(client, streams)
                    iterations_since_reclaim = 0
            except Exception:
                logger.exception("durable_event_subscriber_loop_error",
                                 consumer_name=self._consumer_name)
                await asyncio.sleep(2.0)
