import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from typing import Any
import structlog
from ai.agents.evidence_grounding import non_english_reason_in_output
from ai.llm.anthropic_provider import AnthropicAdapter
from ai.llm.base import (ConcurrencyGate, LLMConfigError, LLMError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMNonEnglishOutputError, LLMOutputExhaustedError,
                         LLMProviderAdapter, LLMProviderTimeoutError,
                         LLMRateLimitError, LLMRequestError,
                         LLMRequestTimeoutError, LLMResponse,
                         LLMServerOverloadedError, LLMTier,
                         rate_limit_backoff_seconds, request_pacer)
from ai.llm.google_provider import (GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS,
                                    GoogleAdapter, google_is_lite_model,
                                    google_model_confirmed_overloaded,
                                    google_model_pool_exhausted,
                                    google_model_recently_timed_out,
                                    google_model_timeout_wait,
                                    google_pool_cooldown,
                                    google_server_error_wait)
from ai.llm.mistral_provider import MistralAdapter, mistral_key_count
from ai.llm.openai_provider import OpenAIAdapter
from config import settings
from observability import record_llm_error

logger = structlog.get_logger(__name__)

_RETRY_BACKOFF_SECONDS = 0.5

_TIMEOUT_RETRIES = 1
_OUTPUT_EXHAUSTED_RETRIES = 1
_GOOGLE_LITE_COOLDOWN_JITTER_SECONDS = 3.0


def router_retry_limit(
        exc: LLMRequestError, *, provider_name: str, model: str,
        retries: int) -> int:
    if isinstance(exc, LLMInvalidRequestError):
        return 0
    if isinstance(exc, LLMOutputExhaustedError):
        return min(retries, _OUTPUT_EXHAUSTED_RETRIES)
    if isinstance(exc, LLMProviderTimeoutError):
        return min(retries, _TIMEOUT_RETRIES)
    if provider_name != "google":
        return retries
    if google_is_lite_model(model) and isinstance(exc, LLMRateLimitError):
        return 0
    return retries


def _is_quota_rate_limit(exc: BaseException | None) -> bool:
    return isinstance(exc, LLMRateLimitError) and not isinstance(
        exc, LLMServerOverloadedError)


async def _google_lite_cooldown_wait(model: str) -> float | None:
    max_wait = settings.LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS
    if max_wait <= 0:
        return None
    wait, scale = await google_pool_cooldown(model)
    if wait > 0 and scale == "day":
        return None
    if wait <= 0:
        wait = GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS
    if wait > max_wait:
        return None
    return wait + random.uniform(0.0, _GOOGLE_LITE_COOLDOWN_JITTER_SECONDS)


ServedCall = tuple[str, bool]
_served_calls: ContextVar[list[ServedCall] | None] = ContextVar(
    "llm_served_calls", default=None)


@contextlib.contextmanager
def recording_served_models() -> Iterator[list[ServedCall]]:
    calls: list[ServedCall] = []
    token = _served_calls.set(calls)
    try:
        yield calls
    finally:
        _served_calls.reset(token)


def fallback_call_count(calls: list[ServedCall]) -> int:
    return sum(1 for _, by_fallback in calls if by_fallback)


def _sent_nothing(exc: LLMRequestError) -> bool:
    return isinstance(exc, LLMRateLimitError) and exc.requests_sent == 0


_DEFAULT_PROVIDERS: dict[str, LLMProviderAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "google": GoogleAdapter(),
    "mistral": MistralAdapter()
}

_google_lite_concurrency_semaphore = asyncio.Semaphore(
    settings.LLM_GOOGLE_LITE_MAX_CONCURRENT_REQUESTS)


def _concurrency_gate(provider_name: str, tier: LLMTier) -> ConcurrencyGate:
    if provider_name == "google" and tier == LLMTier.CHEAP:
        return ConcurrencyGate(_google_lite_concurrency_semaphore)
    return ConcurrencyGate(None)


def _min_interval_seconds(provider_name: str, tier: LLMTier) -> float:
    if provider_name == "mistral":
        key_count = max(mistral_key_count(), 1)
        if tier == LLMTier.CHEAP:
            return settings.LLM_MISTRAL_CHEAP_MIN_INTERVAL_SECONDS / key_count
        if tier == LLMTier.HIGHEST_ACCURACY:
            return (
                settings.LLM_MISTRAL_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS /
                key_count)
        return 0.0
    return 0.0


_QUOTA_EXHAUSTED_REASON = (
    "every pooled Google project is out of quota for this model")
_OVERLOADED_REASON = "the model answered 503 on a recent call"
_TIMED_OUT_REASON = "the model timed out on a recent call"


async def _known_unavailable(
        provider_name: str, model: str) -> LLMRateLimitError | None:
    if provider_name != "google":
        return None
    if await google_model_pool_exhausted(model):
        return LLMRateLimitError(
            f"skipped {model!r}: {_QUOTA_EXHAUSTED_REASON}")
    if google_is_lite_model(model):
        return None
    if await google_model_confirmed_overloaded(model):
        return LLMServerOverloadedError(
            f"skipped {model!r}: {_OVERLOADED_REASON}")
    if await google_model_recently_timed_out(model):
        return LLMServerOverloadedError(
            f"skipped {model!r}: {_TIMED_OUT_REASON}")
    return None


def _reject_non_english(
        response: LLMResponse, *, provider_name: str, tier: LLMTier) -> None:
    reason = non_english_reason_in_output(response.content)
    if reason is None:
        return
    logger.warning(
        "llm_router.non_english_output_rejected", provider=provider_name,
        tier=tier, model=response.model, reason=reason)
    raise LLMNonEnglishOutputError(
        f"LLM output from {provider_name}/{response.model} rejected: {reason}",
        reason=reason, model=response.model, response=response)


def _tier_model_map() -> dict[str, dict[LLMTier, str]]:
    return {
        "openai": {
            LLMTier.CHEAP: settings.LLM_OPENAI_MODEL_CHEAP,
            LLMTier.HIGHEST_ACCURACY: (
                settings.LLM_OPENAI_MODEL_HIGHEST_ACCURACY)
        },
        "anthropic": {
            LLMTier.CHEAP: settings.LLM_ANTHROPIC_MODEL_CHEAP,
            LLMTier.HIGHEST_ACCURACY: (
                settings.LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY)
        },
        "google": {
            LLMTier.CHEAP: settings.LLM_GOOGLE_MODEL_CHEAP,
            LLMTier.HIGHEST_ACCURACY: (
                settings.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY)
        },
        "mistral": {
            LLMTier.CHEAP: settings.LLM_MISTRAL_MODEL_CHEAP,
            LLMTier.HIGHEST_ACCURACY: (
                settings.LLM_MISTRAL_MODEL_HIGHEST_ACCURACY)
        }
    }


def _tier_fallback_model_map() -> dict[str, dict[LLMTier, str]]:
    return {
        "google": {
            LLMTier.CHEAP: settings.LLM_GOOGLE_MODEL_CHEAP_FALLBACK,
            LLMTier.HIGHEST_ACCURACY: (
                settings.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK)
        },
        "mistral": {
            LLMTier.CHEAP: settings.LLM_MISTRAL_MODEL_CHEAP_FALLBACK,
            LLMTier.HIGHEST_ACCURACY: (
                settings.LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK)
        }
    }


def _fallback_model(provider_name: str, tier: LLMTier,
                    primary: str) -> str | None:
    fallback = _tier_fallback_model_map().get(provider_name, {}).get(tier)
    return fallback if fallback and fallback != primary else None


def tier_model_chain(tier: LLMTier) -> list[str]:
    provider_name = settings.LLM_DEFAULT_PROVIDER
    primary = _tier_model_map().get(provider_name, {}).get(tier)
    if not primary:
        return []
    fallback = _fallback_model(provider_name, tier, primary)
    return [primary] if fallback is None else [primary, fallback]


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


class LLMRouter:

    def __init__(
            self,
            providers: dict[str, LLMProviderAdapter] | None = None) -> None:
        self._providers = (
            providers if providers is not None else _DEFAULT_PROVIDERS)

    def _resolve(self, tier: LLMTier) -> tuple[LLMProviderAdapter, str]:
        provider_name = settings.LLM_DEFAULT_PROVIDER
        adapter = self._providers.get(provider_name)
        if adapter is None:
            raise LLMConfigError(
                f"unknown LLM provider {provider_name!r}; "
                f"known: {sorted(self._providers)}")
        try:
            model = _tier_model_map()[provider_name][tier]
        except KeyError as exc:
            raise LLMConfigError(
                f"no model configured for provider={provider_name!r} "
                f"tier={tier!r}") from exc
        return adapter, model

    async def complete(
            self, tier: LLMTier, messages: list[LLMMessage], *,
            temperature: float = 0.2, max_tokens: int = 1024,
            retries: int = 1,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None,
            thinking_budget: int | None = None) -> LLMResponse:
        provider_name = settings.LLM_DEFAULT_PROVIDER
        adapter, model = self._resolve(tier)
        fallback = _fallback_model(provider_name, tier, model)
        is_google = provider_name == "google"

        async with _concurrency_gate(provider_name, tier) as gate:

            async def _call(
                    candidate: str, *, is_fallback: bool,
                    rate_limited: bool) -> LLMResponse:
                extra: dict[str, Any] = {}
                if not is_fallback:
                    extra["response_schema"] = response_schema
                elif is_google:
                    extra["fallback"] = True
                if is_google and thinking_budget:
                    extra["thinking_budget"] = thinking_budget
                response = await adapter.complete(
                    messages, model=candidate, temperature=temperature,
                    max_tokens=max_tokens, response_format=response_format,
                    timeout_seconds=timeout_seconds, **extra)
                if rate_limited:
                    response.rate_limited = True
                _reject_non_english(
                    response, provider_name=provider_name, tier=tier)
                served = _served_calls.get()
                if served is not None:
                    served.append((response.model, is_fallback))
                return response

            async def _lite_cooldown_retry(
                    primary_exc: LLMRequestError) -> LLMResponse | None:
                if not (is_google and retries > 0
                        and google_is_lite_model(model)
                        and _is_quota_rate_limit(primary_exc)):
                    return None
                wait = await _google_lite_cooldown_wait(model)
                if wait is None:
                    return None
                logger.warning(
                    "llm_router.lite_cooldown_retry", provider=provider_name,
                    tier=tier, model=model, delay_seconds=round(wait, 1))
                async with gate.released():
                    await asyncio.sleep(wait)
                try:
                    response = await _call(
                        model, is_fallback=False, rate_limited=True)
                except LLMRequestError as retry_exc:
                    record_llm_error(provider_name, retry_exc)
                    logger.warning(
                        "llm_router.lite_cooldown_retry_failed",
                        provider=provider_name, model=model,
                        error_type=type(retry_exc).__name__)
                    raise retry_exc from primary_exc
                logger.info(
                    "llm_router.lite_cooldown_retry_succeeded",
                    provider=provider_name, model=model)
                return response

            primary_exc: LLMRequestError | None = (
                await _known_unavailable(provider_name, model)
                if fallback else None)
            if primary_exc is not None:
                logger.warning(
                    "llm_router.skipping_known_exhausted_model",
                    provider=provider_name, tier=tier, model=model,
                    reason=str(primary_exc))
            else:
                min_interval = _min_interval_seconds(provider_name, tier)
                rate_limited = False
                for attempt in range(retries + 1):
                    pace_wait = await request_pacer.reserve_slot(
                        (provider_name, model), min_interval)
                    if pace_wait > 0:
                        async with gate.released():
                            await asyncio.sleep(pace_wait)
                    try:
                        return await _call(
                            model, is_fallback=False,
                            rate_limited=rate_limited)
                    except LLMRequestError as exc:
                        failure = exc
                    primary_exc = failure
                    record_llm_error(provider_name, failure)
                    if attempt >= router_retry_limit(
                            failure, provider_name=provider_name, model=model,
                            retries=retries):
                        break
                    if isinstance(failure, LLMRateLimitError):
                        rate_limited = True
                        if fallback and (
                                _sent_nothing(failure)
                                or isinstance(failure, LLMRequestTimeoutError)
                                or await _known_unavailable(
                                    provider_name, model) is not None):
                            logger.warning(
                                "llm_router.primary_unavailable_"
                                "skipping_remaining_attempts",
                                provider=provider_name, model=model,
                                attempt=attempt + 1, retries=retries,
                                sent_nothing=_sent_nothing(failure))
                            break
                        delay = rate_limit_backoff_seconds(attempt)
                    else:
                        delay = _RETRY_BACKOFF_SECONDS * (attempt + 1)
                    logger.warning(
                        "llm_router.retrying", provider=adapter.name,
                        model=model, attempt=attempt + 1,
                        delay_seconds=delay, error=str(failure))
                    async with gate.released():
                        await asyncio.sleep(delay)

            assert primary_exc is not None
            if fallback is None or not isinstance(primary_exc,
                                                  LLMRateLimitError):
                retried = await _lite_cooldown_retry(primary_exc)
                if retried is not None:
                    return retried
                raise primary_exc

            fallback_exc: LLMRequestError
            if (is_google and await google_model_pool_exhausted(fallback)
                    and not await google_model_pool_exhausted(model)):
                logger.warning(
                    "llm_router.skipping_known_exhausted_model",
                    provider=provider_name, tier=tier, model=fallback,
                    reason=_QUOTA_EXHAUSTED_REASON)
                fallback_exc = LLMRateLimitError(
                    f"skipped {fallback!r}: {_QUOTA_EXHAUSTED_REASON}")
            else:
                logger.warning(
                    "llm_router.falling_back_to_alternate_model",
                    provider=provider_name, tier=tier, primary_model=model,
                    fallback_model=fallback)
                try:
                    return await _call(
                        fallback, is_fallback=True, rate_limited=True)
                except LLMRequestError as exc:
                    record_llm_error(provider_name, exc)
                    fallback_exc = exc
            retried = await _lite_cooldown_retry(primary_exc)
            if retried is not None:
                return retried
            raise fallback_exc from primary_exc

    async def stream(
            self, tier: LLMTier, messages: list[LLMMessage], *,
            temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        adapter, model = self._resolve(tier)
        stream = adapter.stream(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens)
        try:
            async for chunk in stream:
                yield chunk
        except LLMError:
            raise
        except Exception as exc:
            adapter_name = getattr(
                adapter, "name", settings.LLM_DEFAULT_PROVIDER)
            raise LLMRequestError(
                f"{adapter_name} streaming failed for model {model!r}: "
                f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    def resolve_model(self, tier: LLMTier) -> str:
        return self._resolve(tier)[1]
