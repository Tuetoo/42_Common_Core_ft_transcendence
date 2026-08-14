import asyncio
from collections.abc import Awaitable, Iterable
from typing import TypeVar

T = TypeVar("T")


async def gather_bounded(coros: Iterable[Awaitable[T]], limit: int = 10
                         ) -> list[T]:
    semaphore = asyncio.Semaphore(limit)

    async def _run(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro
    return list(await asyncio.gather(*(_run(coro) for coro in coros)))
