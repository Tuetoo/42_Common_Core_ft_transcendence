import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast
from uuid import uuid4
import redis.asyncio as redis
import structlog
from redis.exceptions import ResponseError
from shared.config import BaseServiceSettings
from shared.redis import get_events_client, get_queue_client

logger = structlog.get_logger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


async def publish_cross_service(
        settings: BaseServiceSettings, event_name: str,
        payload: dict[str, Any], *, log: bool = True) -> None:
    if log:
        logger.debug(
            "cross_service_event_published", event_name=event_name,
            payload=payload)
    client = get_events_client(settings)
    await client.publish(
        f"events:{event_name}", json.dumps({
            **payload, "event_id": uuid4().hex
        }))


RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)


def reconnect_delay(attempt: int) -> float:
    index = min(max(attempt, 0), len(RECONNECT_BACKOFF_SECONDS) - 1)
    return RECONNECT_BACKOFF_SECONDS[index]


async def close_pubsub_quietly(pubsub: Any) -> None:
    if pubsub is None:
        return
    try:
        await pubsub.aclose()
    except Exception:
        logger.debug("pubsub_close_failed", exc_info=True)


PubSubHook = Callable[[Any], Awaitable[None]]
MessageHook = Callable[[dict[str, Any]], Awaitable[None]]


async def run_pubsub_listener(
        settings: BaseServiceSettings, *, name: str, subscribe: PubSubHook,
        handle: MessageHook, **log_fields: Any) -> None:
    attempt = 0
    while True:
        pubsub: Any = None
        try:
            pubsub = get_events_client(settings).pubsub()
            await subscribe(pubsub)
            logger.info(f"{name}_started", **log_fields)
            attempt = 0
            async for message in pubsub.listen():
                await handle(message)
            logger.warning(f"{name}_stream_ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                f"{name}_failed", retry_in_seconds=reconnect_delay(attempt))
        finally:
            await close_pubsub_quietly(pubsub)
        await asyncio.sleep(reconnect_delay(attempt))
        attempt += 1


def log_unexpected_task_end(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        logger.error("background_task_ended", task=task.get_name())
        return
    logger.error(
        "background_task_crashed", task=task.get_name(),
        exc_info=(type(error), error, error.__traceback__))


def start_background_task(
        coro: Coroutine[Any, Any, None], name: str) -> "asyncio.Task[None]":
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(log_unexpected_task_end)
    return task


class CrossServiceSubscriber:

    def __init__(self, settings: BaseServiceSettings) -> None:
        self._settings = settings
        self._handlers: dict[str, EventHandler] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name] = handler

    async def run(self) -> None:
        channels = [f"events:{name}" for name in self._handlers]
        if not channels:
            return

        async def _subscribe(pubsub: Any) -> None:
            await pubsub.subscribe(*channels)

        await run_pubsub_listener(
            self._settings, name="cross_service_subscriber",
            subscribe=_subscribe, handle=self._dispatch, channels=channels)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if message["type"] != "message":
            return
        channel = message["channel"]
        event_name = channel.removeprefix("events:")
        handler = self._handlers.get(event_name)
        if handler is None:
            return
        try:
            payload = json.loads(message["data"])
            await handler(payload)
        except Exception:
            logger.exception(
                "cross_service_event_handler_failed", event_name=event_name)


_CONSUMER_GROUP = "workers"
_MAX_DELIVERY_ATTEMPTS = 5
_RECLAIM_EVERY_N_ITERATIONS = 12
_STREAM_MAXLEN = 10_000


async def publish_durable_event(
        settings: BaseServiceSettings, event_name: str,
        payload: dict[str, Any], *, log: bool = True) -> None:
    if log:
        logger.debug(
            "durable_event_published", event_name=event_name, payload=payload)
    client = get_queue_client(settings)
    await client.xadd(
        f"stream:{event_name}", {"payload": json.dumps(payload)},
        maxlen=_STREAM_MAXLEN, approximate=True)


def is_missing_group(exc: BaseException) -> bool:
    return isinstance(exc, ResponseError) and str(exc).startswith("NOGROUP")


class DurableEventSubscriber:

    def __init__(
            self, settings: BaseServiceSettings, consumer_name: str,
            group: str = _CONSUMER_GROUP) -> None:
        self._settings = settings
        self._group = group
        self._consumer_name = consumer_name
        self._handlers: dict[str, EventHandler] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name] = handler

    async def run(self) -> None:
        if not self._handlers:
            return
        streams = [f"stream:{name}" for name in self._handlers]
        attempt = 0
        ready = False
        iterations_since_reclaim = 0
        while True:
            client: redis.Redis = get_queue_client(self._settings)
            try:
                if not ready:
                    await self._start(client, streams)
                    ready = True
                    iterations_since_reclaim = 0
                await self._read_once(client, streams)
                attempt = 0
                iterations_since_reclaim += 1
                if iterations_since_reclaim >= _RECLAIM_EVERY_N_ITERATIONS:
                    await self._reclaim_pending(client, streams)
                    iterations_since_reclaim = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_missing_group(exc):
                    ready = False
                delay = reconnect_delay(attempt)
                logger.exception(
                    "durable_event_subscriber_loop_error",
                    consumer_name=self._consumer_name, groups_ready=ready,
                    retry_in_seconds=delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _start(self, client: redis.Redis, streams: list[str]) -> None:
        for stream_name in streams:
            try:
                await client.xgroup_create(
                    stream_name, self._group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        logger.info(
            "durable_event_subscriber_started", streams=streams,
            consumer_name=self._consumer_name)
        try:
            await self._reclaim_pending(client, streams)
        except Exception:
            logger.exception(
                "durable_event_subscriber_reclaim_failed",
                consumer_name=self._consumer_name)

    async def _read_once(
            self, client: redis.Redis, streams: list[str]) -> None:
        raw_response = await client.xreadgroup(
            self._group, self._consumer_name, dict.fromkeys(streams, ">"),
            count=10, block=5000)
        response = cast(
            "list[tuple[str, list[tuple[str, dict[str, str]]]]] | None",
            raw_response)
        for stream_name, messages in response or []:
            event_name = stream_name.removeprefix("stream:")
            handler = self._handlers.get(event_name)
            for message_id, fields in messages:
                await self._handle_one(
                    client, stream_name, event_name, handler, message_id,
                    fields)

    async def _reclaim_pending(
            self, client: redis.Redis, streams: list[str]) -> None:
        for stream_name in streams:
            event_name = stream_name.removeprefix("stream:")
            handler = self._handlers.get(event_name)
            cursor = "0-0"
            while True:
                cursor, claimed, _ = await client.xautoclaim(
                    stream_name, self._group, self._consumer_name,
                    min_idle_time=60_000, start_id=cursor, count=50)
                for message_id, fields in claimed:
                    await self._handle_one(
                        client, stream_name, event_name, handler, message_id,
                        fields)
                if cursor == "0-0":
                    break

    async def _handle_one(
            self, client: redis.Redis, stream_name: str, event_name: str,
            handler: EventHandler | None, message_id: str,
            fields: dict[str, str]) -> None:
        if handler is None:
            await client.xack(stream_name, self._group, message_id)
            return
        try:
            payload = json.loads(fields["payload"])
            await handler(payload)
        except Exception:
            pending = await client.xpending_range(
                stream_name, self._group, min=message_id, max=message_id,
                count=1)
            delivery_count = (
                int(pending[0]["times_delivered"]) if pending else 1)
            logger.exception(
                "durable_event_handler_failed", event_name=event_name,
                message_id=message_id, delivery_count=delivery_count)
            if delivery_count >= _MAX_DELIVERY_ATTEMPTS:
                logger.error(
                    "durable_event_dead_lettered", event_name=event_name,
                    message_id=message_id)
                await client.xack(stream_name, self._group, message_id)
            return
        await client.xack(stream_name, self._group, message_id)
