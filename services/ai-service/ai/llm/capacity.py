from ai.llm.base import LLMTier
from ai.llm.google_provider import (google_model_pool_exhausted,
                                    google_model_timeout_wait,
                                    google_pool_cooldown,
                                    google_server_error_wait)
from ai.llm.router import tier_model_chain
from config import settings


async def chain_model_wait(model: str) -> tuple[float, str]:
    wait, scale = await google_pool_cooldown(model)
    if wait > 0:
        return wait, scale
    server_wait = await google_server_error_wait(model)
    if server_wait > 0:
        return server_wait, "server_error"
    timeout_wait = await google_model_timeout_wait(model)
    return (timeout_wait, "timeout") if timeout_wait > 0 else (0.0, "")


async def chain_capacity(chain: list[str]) -> tuple[float, str, str] | None:
    waits: list[tuple[float, str, str]] = []
    for model in chain:
        if await google_model_pool_exhausted(model):
            continue
        wait, scale = await chain_model_wait(model)
        if wait <= 0:
            return None
        waits.append((wait, scale, model))
    if not waits:
        return 0.0, "day", chain[0]
    return min(waits)


async def free_chain_model(tier: LLMTier) -> str | None:
    if settings.LLM_DEFAULT_PROVIDER != "google":
        return None
    for model in tier_model_chain(tier):
        if await google_model_pool_exhausted(model):
            continue
        wait, _ = await chain_model_wait(model)
        if wait <= 0:
            return model
    return None


async def capacity_aware_delay(tier: LLMTier,
                               delay: float) -> tuple[float, str | None]:
    if delay <= 0:
        return 0.0, None
    free = await free_chain_model(tier)
    return (0.0, free) if free is not None else (delay, None)
