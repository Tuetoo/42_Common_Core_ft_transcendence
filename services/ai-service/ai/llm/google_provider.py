import asyncio
import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
import structlog
from ai.llm.base import (GatedReservation, LLMConfigError, LLMError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMProviderAdapter, LLMProviderTimeoutError,
                         LLMRateLimitError, LLMRequestError,
                         LLMRequestTimeoutError, LLMResponse,
                         LLMServerOverloadedError, LLMUsage,
                         released_while_waiting, request_pacer)
from config import settings
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logger = structlog.get_logger(__name__)

_MINUTE_COOLDOWN_SECONDS = 65.0

_DAY_COOLDOWN_MARGIN_SECONDS = 60.0

_GEMINI_QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")


def _seconds_until_pacific_midnight() -> float:
    now = datetime.now(_GEMINI_QUOTA_RESET_TZ)
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (next_midnight - now).total_seconds() + _DAY_COOLDOWN_MARGIN_SECONDS


REQUEST_TIMEOUT_SECONDS = 60.0

_MAX_MALFORMED_RETRIES = 2

_MAX_QUOTA_EXCEEDED_RETRIES = 2

_MAX_SERVER_ERROR_RETRIES = 0

_MAX_SERVER_ERROR_RETRIES_LITE = 2

_INVALID_REQUEST_CODES = frozenset({400, 404})


def google_is_lite_model(model: str) -> bool:
    return "lite" in model.lower()


def _max_server_error_retries_for(model: str) -> int:
    return (
        _MAX_SERVER_ERROR_RETRIES_LITE
        if google_is_lite_model(model) else _MAX_SERVER_ERROR_RETRIES)


_THINKING_DISABLED = types.ThinkingConfig(thinking_budget=0)

_MINIMAL_THINKING = types.ThinkingConfig(
    thinking_level=types.ThinkingLevel.MINIMAL)

_LITE_MINIMAL_THINKING_MODELS = (
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")

_MODEL_SPECIFIC_THINKING_CONFIG: dict[str, types.ThinkingConfig] = {
    "gemini-3.6-flash": _MINIMAL_THINKING,
    **dict.fromkeys(_LITE_MINIMAL_THINKING_MODELS, _MINIMAL_THINKING)
}


def _thinking_config_for(model: str) -> types.ThinkingConfig | None:
    if model in _MODEL_SPECIFIC_THINKING_CONFIG:
        return _MODEL_SPECIFIC_THINKING_CONFIG[model]
    if google_is_lite_model(model):
        return None
    return _THINKING_DISABLED


def _min_interval_seconds_for(model: str) -> float:
    return (
        settings.LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS
        if google_is_lite_model(model) else
        settings.LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS)


def _classify_quota_violation(exc: Exception) -> str:
    text = str(exc)
    details = getattr(exc, "details", None)
    if details:
        text = f"{text} {details}"
    normalized = text.lower().replace(" ", "").replace("_", "")
    if "perday" in normalized:
        return "day"
    return "minute"


def _quota_cooldown_for(exc: Exception) -> tuple[str, float]:
    kind = _classify_quota_violation(exc)
    cooldown = (
        _seconds_until_pacific_midnight()
        if kind == "day" else _MINUTE_COOLDOWN_SECONDS)
    return kind, cooldown


def _response_headers_for_log(exc: Exception) -> dict[str, str] | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return dict(headers)
    except Exception:
        return None


class _GoogleKeyPool:

    def __init__(self) -> None:
        self._clients: dict[str, genai.Client] = {}

    def _ensure_built(self) -> None:
        if self._clients:
            return
        for label, key in self._configured_keys():
            self._clients[label] = genai.Client(api_key=key)
        if not self._clients:
            raise LLMConfigError(
                "GOOGLE_API_KEY is not configured (GOOGLE_API_KEY / _2 "
                "through _10 are all unset or blank)")

    @staticmethod
    def _configured_keys() -> list[tuple[str, str]]:
        candidates = [("primary", settings.GOOGLE_API_KEY),
                      ("key_2", settings.GOOGLE_API_KEY_2),
                      ("key_3", settings.GOOGLE_API_KEY_3),
                      ("key_4", settings.GOOGLE_API_KEY_4),
                      ("key_5", settings.GOOGLE_API_KEY_5),
                      ("key_6", settings.GOOGLE_API_KEY_6),
                      ("key_7", settings.GOOGLE_API_KEY_7),
                      ("key_8", settings.GOOGLE_API_KEY_8),
                      ("key_9", settings.GOOGLE_API_KEY_9),
                      ("key_10", settings.GOOGLE_API_KEY_10)]
        return [(label, key) for label, key in candidates if key]

    def key_count(self) -> int:
        return len(self._configured_keys())

    def available_labels_excluding(self, excluded: set[str]) -> list[str]:
        self._ensure_built()
        return [lbl for lbl in self._clients if lbl not in excluded]

    def client_for(self, label: str) -> genai.Client:
        return self._clients[label]


_pool = _GoogleKeyPool()


def google_pool_labels() -> list[str]:
    return _pool.available_labels_excluding(set())


def google_client_for(label: str) -> genai.Client:
    return _pool.client_for(label)


async def google_model_pool_exhausted(model: str) -> bool:
    wait, scale = await google_pool_cooldown(model)
    if wait > 0 and scale == "day":
        return True
    fraction = await _pool_exhausted_fraction(model)
    return fraction >= _POOL_EXHAUSTION_FRACTION_THRESHOLD


async def google_model_confirmed_overloaded(model: str) -> bool:
    return await google_server_error_wait(model) > 0.0


async def google_server_error_wait(model: str) -> float:
    earliest = await request_pacer.peek_earliest_next_allowed_at([
        ("google", "server_error", model)
    ])
    return max(0.0, earliest - time.monotonic())


def _server_error_streak_key(model: str) -> tuple[str, str, str]:
    return ("google", "server_error_streak", model)


def google_model_server_error_streak(model: str) -> int:
    return request_pacer.counter(_server_error_streak_key(model))


def _server_error_cooldown_for_streak(streak: int) -> float:
    base = settings.LLM_GOOGLE_SERVER_ERROR_COOLDOWN_SECONDS
    cap = max(base, settings.LLM_GOOGLE_SERVER_ERROR_COOLDOWN_MAX_SECONDS)
    return float(min(base * (3**max(streak - 1, 0)), cap))


async def _record_server_error_give_up(model: str) -> tuple[int, float]:
    streak = request_pacer.increment_counter(_server_error_streak_key(model))
    cooldown = _server_error_cooldown_for_streak(streak)
    await request_pacer.mark_unavailable_until(
        ("google", "server_error", model),
        time.monotonic() + cooldown)
    return streak, cooldown


def _reset_server_error_streak(model: str) -> int:
    return request_pacer.reset_counter(_server_error_streak_key(model))


def _last_success_key(model: str) -> tuple[str, str, str]:
    return ("google", "last_success", model)


def google_model_last_success_at(model: str) -> float | None:
    return request_pacer.stamp(_last_success_key(model))


def _timeout_marker(model: str) -> tuple[str, str, str]:
    return ("google", "timeout", model)


async def _record_timeout_cooldown(model: str, key_label: str) -> float:
    cooldown = settings.LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS
    if cooldown > 0:
        await request_pacer.mark_unavailable_until(
            _timeout_marker(model),
            time.monotonic() + cooldown)
    logger.warning(
        "google.model_timeout_cooldown_recorded", model=model, key=key_label,
        cooldown_seconds=cooldown)
    return cooldown


async def _record_success(model: str, key_label: str) -> None:
    old_streak = _reset_server_error_streak(model)
    if not google_is_lite_model(model):
        request_pacer.set_stamp(_last_success_key(model), time.monotonic())
    if await request_pacer.clear(("google", "server_error", model)):
        logger.info(
            "google.server_error_cooldown_cleared_on_success", model=model,
            key=key_label, previous_streak=old_streak)


async def google_model_timeout_wait(model: str) -> float:
    earliest = await request_pacer.peek_earliest_next_allowed_at(
        [_timeout_marker(model)])
    return max(0.0, earliest - time.monotonic())


async def google_model_recently_timed_out(model: str) -> bool:
    if google_is_lite_model(model):
        return False
    return await google_model_timeout_wait(model) > 0.0


def _default_request_timeout(model: str, *, terminal: bool = False) -> float:
    if google_is_lite_model(model):
        return REQUEST_TIMEOUT_SECONDS
    if terminal:
        return settings.LLM_GOOGLE_HIGHEST_TERMINAL_REQUEST_TIMEOUT_SECONDS
    per_model = settings.LLM_GOOGLE_MODEL_REQUEST_TIMEOUT_SECONDS.get(model)
    if per_model is not None:
        return per_model
    return settings.LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS


GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS = 60.0


def lite_pool_gate_max_wait_seconds(
        *, delayed_retry_follows: bool = True) -> float:
    configured = settings.LLM_GOOGLE_LITE_POOL_MAX_WAIT_SECONDS
    if not delayed_retry_follows:
        return configured
    if settings.LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS >= (
            GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS):
        return max(configured, GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS)
    return configured


async def _reserve_dispatch_slot(
        model: str, pacer_candidates: list[tuple[str, str, str]], *,
        delayed_retry_follows: bool = False) -> GatedReservation:
    min_interval = _min_interval_seconds_for(model)
    lite = google_is_lite_model(model)
    return await request_pacer.reserve_best_slot_gated(
        pacer_candidates, min_interval, max_wait_seconds=min_interval,
        gate_key=("google", "pool", "lite") if lite else None,
        gate_interval_seconds=(
            settings.LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS
            if lite else 0.0), gate_max_wait_seconds=(
                lite_pool_gate_max_wait_seconds(
                    delayed_retry_follows=delayed_retry_follows)
                if lite else None))


async def _reserve_or_fail_fast(
        model: str, pacer_candidates: list[tuple[str, str, str]], *,
        delayed_retry_follows: bool, tried_count: int) -> GatedReservation:
    reservation = await _reserve_dispatch_slot(
        model, pacer_candidates, delayed_retry_follows=delayed_retry_follows)
    if reservation.reserved:
        return reservation
    min_interval = _min_interval_seconds_for(model)
    if reservation.wait > min_interval:
        logger.warning(
            "google.pacing_wait_exceeds_threshold_failing_fast", model=model,
            key=reservation.key[2], wait_seconds=round(reservation.wait, 1),
            min_interval_seconds=min_interval, tried_count=tried_count,
            pool_size=_pool.key_count())
    else:
        logger.warning(
            "google.pool_pacing_wait_exceeds_threshold_failing_fast",
            model=model, key=reservation.key[2],
            wait_seconds=round(reservation.gate_wait, 1),
            max_wait_seconds=lite_pool_gate_max_wait_seconds(
                delayed_retry_follows=delayed_retry_follows),
            tried_count=tried_count, pool_size=_pool.key_count())
    return reservation


async def _wait_for_reserved_key(
        model: str, reservation: GatedReservation,
        tried_labels: set[str]) -> str | None:
    chosen = reservation.key
    key_label = chosen[2]
    effective_wait = max(
        reservation.wait,
        reservation.gate_wait if google_is_lite_model(model) else 0.0)
    marks_before = request_pacer.mark_count(chosen)
    if effective_wait > 0:
        async with released_while_waiting():
            await asyncio.sleep(effective_wait)
    if request_pacer.mark_count(chosen) == marks_before:
        return key_label
    recheck = await request_pacer.peek_earliest_next_allowed_at([chosen])
    logger.warning(
        "google.key_marked_unavailable_during_wait_skipping", model=model,
        key=key_label,
        remaining_wait_seconds=round(max(0.0, recheck - time.monotonic()), 1),
        tried_count=len(tried_labels), pool_size=_pool.key_count())
    tried_labels.add(key_label)
    return None


_DAY_SCALE_WAIT_THRESHOLD_SECONDS = _MINUTE_COOLDOWN_SECONDS + 120.0


async def _pool_wait_and_interval(model: str) -> tuple[float, float]:
    try:
        labels = _pool.available_labels_excluding(set())
    except LLMConfigError:
        return 0.0, 0.0
    if not labels:
        return 0.0, 0.0
    min_interval = _min_interval_seconds_for(model)
    if min_interval <= 0:
        return 0.0, 0.0
    candidates = [("google", model, lbl) for lbl in labels]
    earliest = await request_pacer.peek_earliest_next_allowed_at(candidates)
    wait = max(0.0, earliest - time.monotonic())
    return wait, min_interval


_POOL_EXHAUSTION_FRACTION_THRESHOLD = 0.7


async def _pool_exhausted_fraction(model: str) -> float:
    try:
        labels = _pool.available_labels_excluding(set())
    except LLMConfigError:
        return 0.0
    if not labels:
        return 0.0
    min_interval = _min_interval_seconds_for(model)
    if min_interval <= 0:
        return 0.0
    candidates = [("google", model, lbl) for lbl in labels]
    return await request_pacer.peek_exhausted_fraction(
        candidates, _DAY_SCALE_WAIT_THRESHOLD_SECONDS)


async def google_pool_cooldown(model: str) -> tuple[float, str]:
    wait, min_interval = await _pool_wait_and_interval(model)
    if wait <= min_interval:
        return 0.0, "minute"
    scale = "day" if wait > _DAY_SCALE_WAIT_THRESHOLD_SECONDS else "minute"
    return wait, scale


def _split_system_and_contents(
        messages: list[LLMMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    contents = [{
        "role": "model" if m.role == "assistant" else m.role,
        "parts": [{
            "text": m.content
        }]
    } for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), contents


_RUNAWAY_THINKING_SHARE = 0.5
_THINKING_BUDGET_REJECTED_MODELS: set[str] = set()


def _is_runaway_thinking(
        response: LLMResponse, *, model: str, max_tokens: int) -> bool:
    budget = settings.LLM_GOOGLE_LITE_RUNAWAY_RETRY_THINKING_BUDGET
    thinking = response.usage.thinking_tokens
    return (
        0 < budget < max_tokens and google_is_lite_model(model)
        and model not in _THINKING_BUDGET_REJECTED_MODELS
        and response.finish_reason is not None
        and "MAX_TOKENS" in response.finish_reason and thinking is not None
        and thinking >= max_tokens * _RUNAWAY_THINKING_SHARE)


class GoogleAdapter(LLMProviderAdapter):
    name = "google"

    async def complete(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2, max_tokens: int = 1024,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None,
            extra_server_error_retries: int = 0,
            terminal_chain_model: bool = False) -> LLMResponse:
        response = await self._complete_on_pool(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, response_format=response_format,
            timeout_seconds=timeout_seconds, response_schema=response_schema,
            extra_server_error_retries=extra_server_error_retries,
            terminal_chain_model=terminal_chain_model)
        if not _is_runaway_thinking(response, model=model,
                                    max_tokens=max_tokens):
            return response
        return await self._runaway_thinking_retry(
            response, messages, model=model, temperature=temperature,
            max_tokens=max_tokens, response_format=response_format,
            timeout_seconds=timeout_seconds, response_schema=response_schema,
            terminal_chain_model=terminal_chain_model)

    async def _runaway_thinking_retry(
            self, original: LLMResponse, messages: list[LLMMessage], *,
            model: str, temperature: float, max_tokens: int,
            response_format: str | None, timeout_seconds: float | None,
            response_schema: dict[str, Any] | None,
            terminal_chain_model: bool) -> LLMResponse:
        budget = settings.LLM_GOOGLE_LITE_RUNAWAY_RETRY_THINKING_BUDGET
        logger.warning(
            "google.thinking_runaway_retrying_with_budget", model=model,
            max_tokens=max_tokens,
            thinking_tokens=original.usage.thinking_tokens,
            completion_tokens=original.usage.completion_tokens,
            thinking_budget=budget)
        try:
            retried = await self._complete_on_pool(
                messages, model=model, temperature=temperature,
                max_tokens=max_tokens, response_format=response_format,
                timeout_seconds=timeout_seconds,
                response_schema=response_schema,
                terminal_chain_model=terminal_chain_model,
                thinking_config_override=types.ThinkingConfig(
                    thinking_budget=budget))
        except LLMInvalidRequestError as exc:
            _THINKING_BUDGET_REJECTED_MODELS.add(model)
            logger.warning(
                "google.thinking_budget_rejected_disabling_runaway_retry",
                model=model, thinking_budget=budget, error=str(exc))
            return original
        except LLMError as exc:
            logger.warning(
                "google.thinking_runaway_retry_failed", model=model,
                error_type=type(exc).__name__)
            return original
        logger.info(
            "google.thinking_runaway_retry_done", model=model,
            finish_reason=retried.finish_reason,
            thinking_tokens=retried.usage.thinking_tokens,
            completion_tokens=retried.usage.completion_tokens)
        return retried

    async def _complete_on_pool(
        self, messages: list[LLMMessage], *, model: str,
        temperature: float = 0.2, max_tokens: int = 1024,
        response_format: str | None = None,
        timeout_seconds: float | None = None,
        response_schema: dict[str, Any] | None = None,
        extra_server_error_retries: int = 0,
        terminal_chain_model: bool = False,
        thinking_config_override: types.ThinkingConfig | None = None
    ) -> LLMResponse:
        system, contents = _split_system_and_contents(messages)
        want_json = response_format == "json_object"
        gen_config = types.GenerateContentConfig(
            system_instruction=system or None, temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if want_json else None,
            response_json_schema=(
                response_schema if want_json and response_schema else None),
            thinking_config=(
                thinking_config_override if thinking_config_override
                is not None else _thinking_config_for(model)))
        default_timeout = timeout_seconds is None
        real_timeout = (
            timeout_seconds if timeout_seconds is not None else
            _default_request_timeout(model, terminal=terminal_chain_model))

        tried_labels: set[str] = set()
        last_exc: Exception | None = None
        fast_failed = False
        requests_sent = 0
        malformed_count = 0
        quota_exceeded_count = 0
        server_error_count = 0
        last_failure_server_error = False
        needed_429_recovery = False
        for _ in range(_pool.key_count()):
            candidate_labels = _pool.available_labels_excluding(tried_labels)
            if not candidate_labels:
                break
            pacer_candidates = [("google", model, lbl)
                                for lbl in candidate_labels]
            reservation = await _reserve_or_fail_fast(
                model, pacer_candidates, delayed_retry_follows=True,
                tried_count=len(tried_labels))
            if not reservation.reserved:
                fast_failed = True
                break
            key_label = await _wait_for_reserved_key(
                model, reservation, tried_labels)
            if key_label is None:
                continue
            client = _pool.client_for(key_label)
            tried_labels.add(key_label)
            requests_sent += 1
            request_started = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model, contents=contents, config=gen_config),
                    timeout=real_timeout)
            except TimeoutError as exc:
                logger.warning(
                    "google.request_timed_out", model=model, key=key_label,
                    timeout_seconds=real_timeout,
                    tried_count=len(tried_labels))
                if default_timeout and not google_is_lite_model(model):
                    cooldown = await _record_timeout_cooldown(model, key_label)
                    raise LLMRequestTimeoutError(
                        f"Gemini completion timed out after "
                        f"{real_timeout:.0f}s "
                        f"for model {model!r} (key={key_label}) -- no "
                        f"response "
                        "received; the model is skipped for "
                        f"{cooldown:.0f}s.") from exc
                raise LLMProviderTimeoutError(
                    f"Gemini completion timed out after {real_timeout:.0f}s "
                    f"for model {model!r} (key={key_label}) -- no response "
                    f"received within the bounded wait.") from exc
            except genai_errors.APIError as exc:
                if exc.code == 429:
                    kind, cooldown = _quota_cooldown_for(exc)
                    await request_pacer.mark_unavailable_until(
                        ("google", model, key_label),
                        time.monotonic() + cooldown)
                    last_exc = exc
                    last_failure_server_error = False
                    needed_429_recovery = True
                    quota_exceeded_count += 1
                    logger.warning(
                        "google.quota_exceeded_failing_over", model=model,
                        key=key_label, quota_window=kind,
                        cooldown_seconds=cooldown,
                        quota_exceeded_count=quota_exceeded_count,
                        tried_count=len(tried_labels),
                        pool_size=_pool.key_count(), raw_error=str(exc),
                        response_headers=_response_headers_for_log(exc),
                        details=getattr(exc, "details", None))
                    if quota_exceeded_count > _MAX_QUOTA_EXCEEDED_RETRIES:
                        logger.warning(
                            "google.quota_exceeded_giving_up_early",
                            model=model, key=key_label,
                            quota_exceeded_count=quota_exceeded_count,
                            tried_count=len(tried_labels),
                            pool_size=_pool.key_count())
                        raise LLMRateLimitError(
                            f"Gemini completion for model {model!r} hit a "
                            f"real 429 {quota_exceeded_count} times in a "
                            "row within this call -- giving up early "
                            "instead of walking the rest of the pool, so "
                            "the outer retry schedule (not this same-call "
                            "loop) absorbs the remaining demand.",
                            requests_sent=requests_sent) from exc
                    continue
                if isinstance(exc, genai_errors.ServerError):
                    last_exc = exc
                    last_failure_server_error = True
                    server_error_count += 1
                    logger.warning(
                        "google.server_error_failing_over", model=model,
                        key=key_label, status_code=exc.code,
                        tried_count=len(tried_labels),
                        pool_size=_pool.key_count(),
                        server_error_count=server_error_count,
                        raw_error=str(exc))
                    is_lite = google_is_lite_model(model)
                    if server_error_count > (
                            _max_server_error_retries_for(model) +
                            extra_server_error_retries):
                        streak, cooldown = 0, 0.0
                        if not is_lite:
                            streak, cooldown = (
                                await _record_server_error_give_up(model))
                        logger.warning(
                            "google.server_error_giving_up_early", model=model,
                            key=key_label,
                            server_error_count=server_error_count,
                            tried_count=len(tried_labels),
                            pool_size=_pool.key_count(), lite_model=is_lite,
                            model_cooldown_recorded=not is_lite,
                            overload_streak=streak, cooldown_seconds=cooldown)
                        raise LLMServerOverloadedError(
                            f"Gemini completion for model {model!r} hit a "
                            f"real 503 {server_error_count} times in a row "
                            "within this call -- giving up early instead "
                            "of walking the rest of the pool, since a 503 "
                            "reflects Google's shared infrastructure being "
                            "overloaded for this model, not a per-project "
                            "issue that trying more pooled projects would "
                            "fix.", requests_sent=requests_sent) from exc
                    continue
                logger.warning(
                    "google.request_failed_non_retryable", model=model,
                    key=key_label, status_code=getattr(exc, "code", None),
                    tried_count=len(tried_labels), pool_size=_pool.key_count(),
                    raw_error=str(exc), details=getattr(exc, "details", None),
                    response_headers=_response_headers_for_log(exc))
                error_cls = (
                    LLMInvalidRequestError if getattr(exc, "code", None)
                    in _INVALID_REQUEST_CODES else LLMRequestError)
                raise error_cls(
                    f"Gemini completion failed for model {model!r} "
                    f"(key={key_label}): {exc}") from exc
            except Exception as exc:
                logger.warning(
                    "google.request_failed_unexpected_error", model=model,
                    key=key_label, tried_count=len(tried_labels),
                    pool_size=_pool.key_count(), error_type=type(exc).__name__,
                    raw_error=str(exc))
                raise LLMRequestError(
                    f"Gemini completion failed for model {model!r} "
                    f"(key={key_label}): {exc}") from exc
            else:
                usage = resp.usage_metadata
                finish_reason = None
                if resp.candidates:
                    finish_reason = str(resp.candidates[0].finish_reason)
                prompt_tokens = (
                    usage.prompt_token_count if usage else None) or 0
                completion_tokens = (
                    usage.candidates_token_count if usage else None) or 0
                thinking_tokens = (
                    getattr(usage, "thoughts_token_count", None)
                    if usage else None)

                if finish_reason and "MALFORMED_RESPONSE" in finish_reason:
                    malformed_count += 1
                    last_failure_server_error = False
                    content_fingerprint = hashlib.sha256(
                        str(contents).encode()).hexdigest()[:12]
                    if malformed_count > _MAX_MALFORMED_RETRIES:
                        logger.warning(
                            "google.malformed_response_giving_up_early",
                            model=model, key=key_label,
                            prompt_tokens=prompt_tokens,
                            malformed_count=malformed_count,
                            content_fingerprint=content_fingerprint,
                            tried_count=len(tried_labels),
                            pool_size=_pool.key_count())
                        raise LLMRequestError(
                            f"Gemini completion for model {model!r} returned "
                            f"MALFORMED_RESPONSE {malformed_count} times in a "
                            "row -- giving up early instead of walking the "
                            "rest of the pool (a malformed decode isn't "
                            "specific to any one pooled project).")
                    logger.warning(
                        "google.malformed_response_retrying", model=model,
                        key=key_label, prompt_tokens=prompt_tokens,
                        malformed_count=malformed_count,
                        content_fingerprint=content_fingerprint,
                        tried_count=len(tried_labels),
                        pool_size=_pool.key_count())
                    continue

                if finish_reason and "STOP" not in finish_reason:
                    logger.warning(
                        "google.non_stop_finish_reason", model=model,
                        key=key_label, finish_reason=finish_reason,
                        finish_message=(
                            resp.candidates[0].finish_message
                            if resp.candidates else None), prompt_feedback=(
                                str(resp.prompt_feedback)
                                if resp.prompt_feedback else None),
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        thinking_tokens=thinking_tokens)

                logger.info(
                    "google.completion_succeeded", model=model, key=key_label,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    thinking_tokens=thinking_tokens,
                    finish_reason=finish_reason, latency_ms=round(
                        (time.monotonic() - request_started) * 1000))
                await _record_success(model, key_label)
                return LLMResponse(
                    content=resp.text or "", provider=self.name, model=model,
                    usage=LLMUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        thinking_tokens=thinking_tokens),
                    finish_reason=finish_reason,
                    rate_limited=needed_429_recovery)

        if not tried_labels and not fast_failed:
            raise LLMConfigError(
                f"Gemini completion for model {model!r} failed: no Google "
                "API key is configured (GOOGLE_API_KEY / _2 through _10 are "
                "all unset or blank) -- zero projects were available to "
                "even attempt a real call. This is a configuration "
                "problem, not a rate limit -- no retry will help.")

        logger.warning(
            "google.pool_exhausted_for_model", model=model,
            tried_count=len(tried_labels), pool_size=_pool.key_count(),
            last_error_type=type(last_exc).__name__ if last_exc else None)
        pool_exhausted_error_cls = (
            LLMServerOverloadedError if last_failure_server_error
            and isinstance(last_exc, genai_errors.ServerError) else
            LLMRateLimitError)
        raise pool_exhausted_error_cls(
            f"Gemini completion failed for model {model!r}: every pooled "
            f"Google project ({len(tried_labels)}) is rate-limited, "
            f"quota-exhausted, or erroring for this model",
            requests_sent=requests_sent) from last_exc

    async def stream(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        stream_timeout = _default_request_timeout(model)
        system, contents = _split_system_and_contents(messages)
        gen_config = types.GenerateContentConfig(
            system_instruction=system or None, temperature=temperature,
            max_output_tokens=max_tokens,
            thinking_config=_thinking_config_for(model))

        tried_labels: set[str] = set()
        last_exc: Exception | None = None
        fast_failed = False
        quota_exceeded_count = 0
        server_error_count = 0
        requests_sent = 0
        stream_iter = None
        for _ in range(_pool.key_count()):
            candidate_labels = _pool.available_labels_excluding(tried_labels)
            if not candidate_labels:
                break
            pacer_candidates = [("google", model, lbl)
                                for lbl in candidate_labels]
            reservation = await _reserve_or_fail_fast(
                model, pacer_candidates, delayed_retry_follows=False,
                tried_count=len(tried_labels))
            if not reservation.reserved:
                fast_failed = True
                break
            label = await _wait_for_reserved_key(
                model, reservation, tried_labels)
            if label is None:
                continue
            key_label = label
            client = _pool.client_for(key_label)
            tried_labels.add(key_label)
            requests_sent += 1
            try:
                stream_iter = await asyncio.wait_for(
                    client.aio.models.generate_content_stream(
                        model=model, contents=contents, config=gen_config),
                    timeout=stream_timeout)
                break
            except TimeoutError as exc:
                logger.warning(
                    "google.stream_start_timed_out", model=model,
                    key=key_label, timeout_seconds=stream_timeout,
                    tried_count=len(tried_labels))
                if not google_is_lite_model(model):
                    await _record_timeout_cooldown(model, key_label)
                raise LLMProviderTimeoutError(
                    f"Gemini streaming for model {model!r} (key={key_label}) "
                    f"did not start within {stream_timeout:.0f}s") from exc
            except genai_errors.APIError as exc:
                if exc.code == 429:
                    kind, cooldown = _quota_cooldown_for(exc)
                    await request_pacer.mark_unavailable_until(
                        ("google", model, key_label),
                        time.monotonic() + cooldown)
                    last_exc = exc
                    quota_exceeded_count += 1
                    logger.warning(
                        "google.quota_exceeded_failing_over", model=model,
                        key=key_label, quota_window=kind,
                        cooldown_seconds=cooldown,
                        quota_exceeded_count=quota_exceeded_count,
                        tried_count=len(tried_labels),
                        pool_size=_pool.key_count(), raw_error=str(exc),
                        response_headers=_response_headers_for_log(exc),
                        details=getattr(exc, "details", None))
                    if quota_exceeded_count > _MAX_QUOTA_EXCEEDED_RETRIES:
                        logger.warning(
                            "google.quota_exceeded_giving_up_early",
                            model=model, key=key_label,
                            quota_exceeded_count=quota_exceeded_count,
                            tried_count=len(tried_labels),
                            pool_size=_pool.key_count())
                        raise LLMRateLimitError(
                            f"Gemini streaming for model {model!r} hit a "
                            f"real 429 {quota_exceeded_count} times in a "
                            "row within this call -- giving up early "
                            "instead of walking the rest of the pool.",
                            requests_sent=requests_sent) from exc
                    continue
                if isinstance(exc, genai_errors.ServerError):
                    last_exc = exc
                    server_error_count += 1
                    logger.warning(
                        "google.server_error_failing_over", model=model,
                        key=key_label, status_code=exc.code,
                        tried_count=len(tried_labels),
                        pool_size=_pool.key_count(),
                        server_error_count=server_error_count,
                        raw_error=str(exc))
                    is_lite = google_is_lite_model(model)
                    if server_error_count > _max_server_error_retries_for(
                            model):
                        streak, cooldown = 0, 0.0
                        if not is_lite:
                            streak, cooldown = (
                                await _record_server_error_give_up(model))
                        logger.warning(
                            "google.server_error_giving_up_early", model=model,
                            key=key_label,
                            server_error_count=server_error_count,
                            tried_count=len(tried_labels),
                            pool_size=_pool.key_count(), lite_model=is_lite,
                            model_cooldown_recorded=not is_lite,
                            overload_streak=streak, cooldown_seconds=cooldown)
                        raise LLMServerOverloadedError(
                            f"Gemini streaming failed for model {model!r} "
                            f"hit a real 503 {server_error_count} times in "
                            "a row within this call -- giving up early "
                            "instead of walking the rest of the pool, "
                            "since a 503 reflects Google's shared "
                            "infrastructure being overloaded for this "
                            "model, not a per-project issue that trying "
                            "more pooled projects would fix.",
                            requests_sent=requests_sent) from exc
                    continue
                logger.warning(
                    "google.request_failed_non_retryable", model=model,
                    key=key_label, status_code=getattr(exc, "code", None),
                    tried_count=len(tried_labels), pool_size=_pool.key_count(),
                    raw_error=str(exc), details=getattr(exc, "details", None),
                    response_headers=_response_headers_for_log(exc))
                raise LLMRequestError(
                    f"Gemini streaming failed for model {model!r} "
                    f"(key={key_label}): {exc}") from exc
            except Exception as exc:
                logger.warning(
                    "google.request_failed_unexpected_error", model=model,
                    key=key_label, tried_count=len(tried_labels),
                    pool_size=_pool.key_count(), error_type=type(exc).__name__,
                    raw_error=str(exc))
                raise LLMRequestError(
                    f"Gemini streaming failed for model {model!r} "
                    f"(key={key_label}): {exc}") from exc

        if stream_iter is None:
            if not tried_labels and not fast_failed:
                raise LLMConfigError(
                    f"Gemini streaming for model {model!r} failed: no "
                    "Google API key is configured (GOOGLE_API_KEY / _2 "
                    "through _10 are all unset or blank).")
            logger.warning(
                "google.pool_exhausted_for_model", model=model,
                tried_count=len(tried_labels), pool_size=_pool.key_count(),
                last_error_type=type(last_exc).__name__ if last_exc else None)
            stream_pool_exhausted_error_cls = (
                LLMServerOverloadedError if isinstance(
                    last_exc, genai_errors.ServerError) else LLMRateLimitError)
            raise stream_pool_exhausted_error_cls(
                f"Gemini streaming failed for model {model!r}: every pooled "
                f"Google project ({len(tried_labels)}) is rate-limited, "
                f"quota-exhausted, or erroring for this model",
                requests_sent=requests_sent) from last_exc

        logger.info("google.stream_started", model=model, key=key_label)

        try:
            chunk_iter = stream_iter.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        chunk_iter.__anext__(), timeout=stream_timeout)
                except StopAsyncIteration:
                    break
                if chunk.text:
                    yield chunk.text
            await _record_success(model, key_label)
        except TimeoutError as exc:
            logger.warning(
                "google.stream_chunk_timed_out", model=model, key=key_label,
                timeout_seconds=stream_timeout)
            raise LLMProviderTimeoutError(
                f"Gemini streaming for model {model!r} (key={key_label}) sent "
                f"nothing for {stream_timeout:.0f}s") from exc
        except genai_errors.APIError as exc:
            if exc.code == 429:
                kind, cooldown = _quota_cooldown_for(exc)
                await request_pacer.mark_unavailable_until(
                    ("google", model, key_label),
                    time.monotonic() + cooldown)
                logger.warning(
                    "google.quota_exceeded_mid_stream", model=model,
                    key=key_label, quota_window=kind,
                    cooldown_seconds=cooldown, raw_error=str(exc),
                    response_headers=_response_headers_for_log(exc))
            else:
                logger.warning(
                    "google.request_failed_non_retryable_mid_stream",
                    model=model, key=key_label, status_code=exc.code,
                    raw_error=str(exc), details=getattr(exc, "details", None),
                    response_headers=_response_headers_for_log(exc))
            raise LLMRequestError(
                f"Gemini streaming failed for model {model!r} "
                f"(key={key_label}): {exc}") from exc
        except Exception as exc:
            logger.warning(
                "google.request_failed_unexpected_error_mid_stream",
                model=model, key=key_label, error_type=type(exc).__name__,
                raw_error=str(exc))
            raise LLMRequestError(
                f"Gemini streaming failed for model {model!r} "
                f"(key={key_label}): {exc}") from exc
        finally:
            close = getattr(stream_iter, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
