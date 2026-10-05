import asyncio
import contextlib
import random
import time
from collections.abc import AsyncIterator
from typing import Any
import structlog
from ai.agents.evidence_grounding import non_english_reason_in_output
from ai.llm.anthropic_provider import AnthropicAdapter
from ai.llm.base import (ConcurrencyGate, LLMConfigError, LLMError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMNonEnglishOutputError, LLMProviderAdapter,
                         LLMProviderTimeoutError, LLMRateLimitError,
                         LLMRequestError, LLMRequestTimeoutError, LLMResponse,
                         LLMServerOverloadedError, LLMTier,
                         rate_limit_backoff_seconds, request_pacer)
from ai.llm.google_provider import (GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS,
                                    GoogleAdapter, google_is_lite_model,
                                    google_model_confirmed_overloaded,
                                    google_model_last_success_at,
                                    google_model_pool_exhausted,
                                    google_model_recently_timed_out,
                                    google_model_server_error_streak,
                                    google_pool_cooldown)
from ai.llm.mistral_provider import MistralAdapter, mistral_key_count
from ai.llm.openai_provider import OpenAIAdapter
from config import settings
from observability import record_llm_error

logger = structlog.get_logger(__name__)

_RETRY_BACKOFF_SECONDS = 0.5

_IGNORE_COOLDOWN_HONOUR_MIN_STREAK = 2

_GOOGLE_LITE_TIMEOUT_RETRIES = 1
_GOOGLE_LITE_COOLDOWN_JITTER_SECONDS = 3.0


def router_retry_limit(
        exc: LLMRequestError, *, provider_name: str, model: str,
        retries: int) -> int:
    if isinstance(exc, LLMInvalidRequestError):
        return 0
    if provider_name == "google" and google_is_lite_model(model):
        if isinstance(exc, LLMRateLimitError):
            return 0
        if isinstance(exc, LLMProviderTimeoutError):
            return min(retries, _GOOGLE_LITE_TIMEOUT_RETRIES)
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


async def _google_confirmed_overloaded_skippable(
        provider_name: str, model: str) -> bool:
    return (
        provider_name == "google" and not google_is_lite_model(model)
        and await google_model_confirmed_overloaded(model))


def _google_call_kwargs(
        provider_name: str, candidate: str, *, terminal: bool,
        extra_503_keys: int) -> dict[str, Any]:
    if provider_name != "google":
        return {}
    kwargs: dict[str, Any] = {}
    if terminal and not google_is_lite_model(candidate):
        kwargs["terminal_chain_model"] = True
    if extra_503_keys > 0:
        kwargs["extra_server_error_retries"] = extra_503_keys
    return kwargs


async def _highest_rescue_candidate(
        chain: list[str], exclude: str | None) -> str | None:
    ranked: list[tuple[bool, float, int, str]] = []
    for index, candidate in enumerate(chain):
        if candidate == exclude or google_is_lite_model(candidate):
            continue
        if await google_model_pool_exhausted(candidate):
            continue
        if await google_model_recently_timed_out(candidate):
            continue
        last_success = google_model_last_success_at(candidate)
        ranked.append(
            (last_success is None, -(last_success or 0.0), index, candidate))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][3]


async def any_chain_model_with_quota(models: list[str]) -> bool:
    for candidate in models:
        if not await google_model_pool_exhausted(candidate):
            return True
    return False


def _skip_reason(*, quota_exhausted: bool, timed_out: bool) -> str:
    if quota_exhausted:
        return (
            "every pooled Google project for this model is already confirmed "
            "rate-limited or quota-exhausted from an earlier real call this "
            "run -- see google_model_pool_exhausted")
    if timed_out:
        return (
            "this model sent no response within the request timeout on an "
            "earlier call and is in its timeout cooldown -- see "
            "google_model_recently_timed_out")
    return (
        "this model was just confirmed overloaded by real repeated 503s from "
        "an earlier real call this run (Google-side 'high demand', "
        "short-lived signal) -- see google_model_confirmed_overloaded")


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


def _tier_fallback_model_map() -> dict[str, dict[LLMTier, list[str]]]:
    highest_accuracy_fallback = (
        settings.LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK)
    cheap_fallback = settings.LLM_MISTRAL_MODEL_CHEAP_FALLBACK
    mistral_map: dict[LLMTier, list[str]] = {}
    if highest_accuracy_fallback:
        mistral_map[LLMTier.HIGHEST_ACCURACY] = [highest_accuracy_fallback]
    if cheap_fallback:
        mistral_map[LLMTier.CHEAP] = [cheap_fallback]

    google_highest_accuracy_chain = [
        m for m in (
            settings.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK,
            settings.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK_2,
            settings.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK_3) if m
    ]
    google_cheap_fallback = settings.LLM_GOOGLE_MODEL_CHEAP_FALLBACK
    google_map: dict[LLMTier, list[str]] = {}
    if google_highest_accuracy_chain:
        google_map[LLMTier.HIGHEST_ACCURACY] = google_highest_accuracy_chain
    if google_cheap_fallback:
        google_map[LLMTier.CHEAP] = [google_cheap_fallback]

    result: dict[str, dict[LLMTier, list[str]]] = {}
    if mistral_map:
        result["mistral"] = mistral_map
    if google_map:
        result["google"] = google_map
    return result


def tier_model_chain(tier: LLMTier, provider: str | None = None) -> list[str]:
    provider_name = provider or settings.LLM_DEFAULT_PROVIDER
    primary = _tier_model_map().get(provider_name, {}).get(tier)
    if not primary:
        return []
    fallbacks = _tier_fallback_model_map().get(provider_name, {}).get(tier)
    return [primary, *(fallbacks or [])]


class LLMRouter:

    def __init__(
            self,
            providers: dict[str, LLMProviderAdapter] | None = None) -> None:
        self._providers = (
            providers if providers is not None else _DEFAULT_PROVIDERS)

    def _resolve(self, tier: LLMTier,
                 provider: str | None) -> tuple[LLMProviderAdapter, str]:
        provider_name = provider or settings.LLM_DEFAULT_PROVIDER
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
            provider: str | None = None, temperature: float = 0.2,
            max_tokens: int = 1024, retries: int = 1,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None,
            terminal_server_error_retry_delay: float | None = None,
            ignore_server_error_cooldown: bool = False,
            primary_extra_server_error_keys: int = 0) -> LLMResponse:
        provider_name = provider or settings.LLM_DEFAULT_PROVIDER
        adapter, model = self._resolve(tier, provider)

        async def _skip_timed_out(candidate: str) -> bool:
            return (
                provider_name == "google"
                and await google_model_recently_timed_out(candidate))

        async def _skip_overloaded(candidate: str) -> bool:
            if not await _google_confirmed_overloaded_skippable(provider_name,
                                                                candidate):
                return False
            if not ignore_server_error_cooldown:
                return True
            return (
                google_model_server_error_streak(candidate)
                >= _IGNORE_COOLDOWN_HONOUR_MIN_STREAK)

        async with _concurrency_gate(provider_name, tier) as gate:
            min_interval = _min_interval_seconds(provider_name, tier)

            fallback_chain = [
                m
                for m in _tier_fallback_model_map().get(provider_name, {}).get(
                    tier, []) if m and m != model
            ]

            last_exc: LLMRequestError | None = None
            primary_extra = _google_call_kwargs(
                provider_name, model, terminal=not fallback_chain,
                extra_503_keys=primary_extra_server_error_keys)

            async def _call(
                    candidate: str, extra: dict[str, Any], *,
                    rate_limited: bool, with_schema: bool) -> LLMResponse:
                if with_schema:
                    extra = {"response_schema": response_schema, **extra}
                response = await adapter.complete(
                    messages, model=candidate, temperature=temperature,
                    max_tokens=max_tokens, response_format=response_format,
                    timeout_seconds=timeout_seconds, **extra)
                if rate_limited:
                    response.rate_limited = True
                _reject_non_english(
                    response, provider_name=provider_name, tier=tier)
                return response

            async def _lite_cooldown_retry(
                    primary_exc: LLMRequestError | None) -> LLMResponse | None:
                if not (provider_name == "google" and retries > 0
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
                        model, primary_extra, rate_limited=True,
                        with_schema=True)
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

            async def _highest_rescue(
                failure: LLMRequestError, failed_model: str | None
            ) -> tuple[LLMResponse | None, LLMRequestError]:
                if not (provider_name == "google"
                        and not google_is_lite_model(model) and fallback_chain
                        and isinstance(failure, LLMRateLimitError)):
                    return None, failure
                chain = [model, *fallback_chain]
                attempts = (
                    2 if terminal_server_error_retry_delay is not None else 1)
                last_failure: LLMRequestError = failure
                exclude = failed_model
                for attempt in range(attempts):
                    if attempt:
                        if (terminal_server_error_retry_delay is None
                                or not isinstance(last_failure,
                                                  LLMServerOverloadedError)
                                or isinstance(last_failure,
                                              LLMRequestTimeoutError)):
                            break
                        logger.warning(
                            "llm_router.terminal_server_error_retry",
                            provider=provider_name, tier=tier, model=exclude,
                            delay_seconds=terminal_server_error_retry_delay)
                        async with gate.released():
                            await asyncio.sleep(
                                terminal_server_error_retry_delay)
                    candidate = await _highest_rescue_candidate(chain, exclude)
                    if candidate is None:
                        logger.warning(
                            "llm_router.highest_rescue_no_candidate",
                            provider=provider_name, tier=tier,
                            after_model=exclude)
                        break
                    last_success = google_model_last_success_at(candidate)
                    logger.warning(
                        "llm_router.highest_rescue_attempt",
                        provider=provider_name, tier=tier, model=candidate,
                        attempt=attempt + 1, after_model=exclude,
                        last_success_seconds_ago=(
                            None if last_success is None else round(
                                time.monotonic() - last_success, 1)))
                    try:
                        response = await _call(
                            candidate,
                            _google_call_kwargs(
                                provider_name, candidate, terminal=True,
                                extra_503_keys=0), rate_limited=True,
                            with_schema=False)
                    except LLMRequestError as rescue_exc:
                        record_llm_error(provider_name, rescue_exc)
                        logger.warning(
                            "llm_router.highest_rescue_failed",
                            provider=provider_name, model=candidate,
                            error_type=type(rescue_exc).__name__)
                        last_failure, exclude = rescue_exc, candidate
                        continue
                    logger.info(
                        "llm_router.highest_rescue_succeeded",
                        provider=provider_name, model=candidate)
                    return response, last_failure
                return None, last_failure

            _quota_exhausted = (
                provider_name == "google" and fallback_chain
                and (await google_model_pool_exhausted(model)))
            _confirmed_overloaded = (
                not _quota_exhausted and bool(fallback_chain)
                and await _skip_overloaded(model))
            _timed_out = (
                not _quota_exhausted and not _confirmed_overloaded
                and bool(fallback_chain) and await _skip_timed_out(model))
            if _quota_exhausted or _confirmed_overloaded or _timed_out:
                reason = _skip_reason(
                    quota_exhausted=bool(_quota_exhausted),
                    timed_out=_timed_out)
                logger.warning(
                    "llm_router.skipping_known_exhausted_model",
                    provider=provider_name, tier=tier, model=model,
                    reason=reason)
                last_exc = (
                    LLMRateLimitError(f"skipped {model!r}: {reason}")
                    if _quota_exhausted else
                    LLMServerOverloadedError(f"skipped {model!r}: {reason}"))
            else:
                rate_limited_recovery = False
                for attempt in range(retries + 1):
                    pace_wait = await request_pacer.reserve_slot(
                        (provider_name, model), min_interval)
                    if pace_wait > 0:
                        async with gate.released():
                            await asyncio.sleep(pace_wait)
                    try:
                        return await _call(
                            model, primary_extra,
                            rate_limited=rate_limited_recovery,
                            with_schema=True)
                    except LLMRequestError as exc:
                        last_exc = exc
                        record_llm_error(provider_name, exc)
                        retry_limit = router_retry_limit(
                            exc, provider_name=provider_name, model=model,
                            retries=retries)
                        if isinstance(exc, LLMRateLimitError):
                            rate_limited_recovery = True
                            if (attempt < retry_limit
                                    and provider_name == "google"
                                    and fallback_chain and
                                (_sent_nothing(exc)
                                 or await google_model_pool_exhausted(model)
                                 or await _skip_overloaded(model)
                                 or await _skip_timed_out(model) or
                                 (ignore_server_error_cooldown and isinstance(
                                     exc, LLMServerOverloadedError)))):
                                logger.warning(
                                    "llm_router.confirmed_exhausted_"
                                    "mid_retry_loop_"
                                    "skipping_remaining_attempts",
                                    provider=provider_name, model=model,
                                    attempt=attempt + 1, retries=retries,
                                    sent_nothing=_sent_nothing(exc))
                                break
                        if attempt < retry_limit:
                            delay = (
                                rate_limit_backoff_seconds(attempt)
                                if isinstance(exc, LLMRateLimitError) else
                                _RETRY_BACKOFF_SECONDS * (attempt + 1))
                            logger.warning(
                                "llm_router.retrying", provider=adapter.name,
                                model=model, attempt=attempt + 1,
                                delay_seconds=delay, error=str(exc))
                            async with gate.released():
                                await asyncio.sleep(delay)
                            continue
                        break

            if isinstance(last_exc, LLMRateLimitError) and fallback_chain:
                original_exc = last_exc
                last_failed_model: str | None = (
                    None if
                    (_quota_exhausted or _confirmed_overloaded
                     or _timed_out) else model)
                for step, fallback_model in enumerate(fallback_chain, start=1):
                    _chain_step_quota_exhausted = (
                        provider_name == "google"
                        and await google_model_pool_exhausted(fallback_model))
                    _chain_step_confirmed_overloaded = (
                        not _chain_step_quota_exhausted
                        and await _skip_overloaded(fallback_model))
                    _chain_step_timed_out = (
                        not _chain_step_quota_exhausted
                        and not _chain_step_confirmed_overloaded
                        and await _skip_timed_out(fallback_model))
                    is_last_step = step == len(fallback_chain)
                    if (_chain_step_quota_exhausted and is_last_step
                            and not await any_chain_model_with_quota(
                                [model, *fallback_chain[:-1]])):
                        _chain_step_quota_exhausted = False
                    if _chain_step_quota_exhausted or (
                            not is_last_step
                            and (_chain_step_confirmed_overloaded
                                 or _chain_step_timed_out)):
                        reason = _skip_reason(
                            quota_exhausted=_chain_step_quota_exhausted,
                            timed_out=_chain_step_timed_out)
                        logger.warning(
                            "llm_router.skipping_known_exhausted_model",
                            provider=provider_name, tier=tier,
                            model=fallback_model, chain_step=step,
                            chain_length=len(fallback_chain), reason=reason)
                        last_exc = (
                            LLMRateLimitError(
                                f"skipped {fallback_model!r}: {reason}")
                            if _chain_step_quota_exhausted else
                            LLMServerOverloadedError(
                                f"skipped {fallback_model!r}: {reason}"))
                        continue
                    logger.warning(
                        "llm_router.falling_back_to_alternate_model",
                        provider=provider_name, tier=tier, primary_model=model,
                        fallback_model=fallback_model, chain_step=step,
                        chain_length=len(fallback_chain))
                    try:
                        return await _call(
                            fallback_model,
                            _google_call_kwargs(
                                provider_name, fallback_model,
                                terminal=is_last_step, extra_503_keys=0),
                            rate_limited=True, with_schema=False)
                    except LLMRequestError as fallback_exc:
                        last_exc = fallback_exc
                        last_failed_model = fallback_model
                        record_llm_error(provider_name, fallback_exc)
                    continue
                rescued, last_exc = await _highest_rescue(
                    last_exc, last_failed_model)
                if rescued is not None:
                    return rescued
                retried = await _lite_cooldown_retry(original_exc)
                if retried is not None:
                    return retried
                raise last_exc from original_exc

            assert last_exc is not None
            retried = await _lite_cooldown_retry(last_exc)
            if retried is not None:
                return retried
            raise last_exc

    async def stream(
            self, tier: LLMTier, messages: list[LLMMessage], *,
            provider: str | None = None, temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        adapter, model = self._resolve(tier, provider)
        stream = adapter.stream(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens)
        try:
            async for chunk in stream:
                yield chunk
        except LLMError:
            raise
        except Exception as exc:
            adapter_name = getattr(adapter, "name", provider)
            raise LLMRequestError(
                f"{adapter_name} streaming failed for model {model!r}: "
                f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    def resolve_model(self, tier: LLMTier, provider: str | None = None) -> str:
        return self._resolve(tier, provider)[1]
