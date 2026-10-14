import asyncio
from collections.abc import Awaitable, Iterable
from typing import TypeVar
import httpx
from shared.config import get_settings

INTERNAL_TOKEN_HEADER = "X-Internal-Token"

_clients: dict[float, httpx.AsyncClient] = {}


def _internal_targets() -> set[tuple[str, int | None]]:
    settings = get_settings()
    targets = set()
    for base in (settings.AUTH_SERVICE_URL, settings.CORE_SERVICE_URL,
                 settings.AI_SERVICE_URL):
        url = httpx.URL(base)
        targets.add((url.host, url.port))
    return targets


async def _add_internal_token(request: httpx.Request) -> None:
    if INTERNAL_TOKEN_HEADER in request.headers:
        return
    if not request.url.path.startswith("/internal/"):
        return
    if (request.url.host, request.url.port) not in _internal_targets():
        return
    request.headers.update(internal_headers())


def build_http_client(
        timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        event_hooks={"request": [_add_internal_token]}, transport=transport)


def get_http_client(timeout: float = 5.0) -> httpx.AsyncClient:
    if timeout not in _clients:
        _clients[timeout] = build_http_client(timeout)
    return _clients[timeout]


def internal_headers() -> dict[str, str]:
    token = get_settings().INTERNAL_API_TOKEN
    return {INTERNAL_TOKEN_HEADER: token} if token else {}


async def close_http_clients() -> None:
    for client in _clients.values():
        await client.aclose()
    _clients.clear()


T = TypeVar("T")


async def gather_bounded(coros: Iterable[Awaitable[T]],
                         limit: int = 10) -> list[T]:
    semaphore = asyncio.Semaphore(limit)

    async def _run(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro

    return list(await asyncio.gather(*(_run(coro) for coro in coros)))
