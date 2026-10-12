import asyncio
import importlib
import json
import logging
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
import ai.llm.google_provider as google_module
import ai.llm.router as router_module
import ai.rag.embedding as embedding_module
import ai.rag.retriever as retriever_module
import ai.recommendation.position_sizing as sizing_module
import ai.tools.portfolio_tool as portfolio_tool_module
import config
import observability
import pytest
import pytest_asyncio
import structlog
from ai.agents.reflection import review_skip_reason_for
from ai.graph.state import ReflectionVerdict
from ai.llm.anthropic_provider import _split_system_and_turns
from ai.llm.base import (ConcurrencyGate, LLMConfigError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMNonEnglishOutputError, LLMProviderAdapter,
                         LLMProviderTimeoutError, LLMRateLimitError,
                         LLMRequestError, LLMRequestTimeoutError, LLMResponse,
                         LLMServerOverloadedError, LLMTier, LLMUsage,
                         RequestPacer, released_while_waiting, request_pacer)
from ai.llm.router import LLMRouter, router_retry_limit
from ai.providers.base import PriceQuote
from ai.recommendation.position_sizing import (BudgetBlockedNote,
                                               _validated_sizing,
                                               generate_position_sizing)
from google.genai import errors as genai_errors
from helpers import (_chain, _FakeEmbedModels, _FakeGeminiResponse,
                     _FakeGoogleClient, _FakeGoogleGenerateContent,
                     _FakeGooglePool, _install_google_pool, _msgs, _no_sleep,
                     _PerModelAdapter, _RecordingSleep, _run_steps,
                     _scripted_pool, _server_503, _single_client_pool, _state)
from mistralai.client.errors.sdkerror import SDKError
from prometheus_client import REGISTRY
from structlog.testing import capture_logs
from shared.enums import AnalysisType, RecommendationAction

_FAR_FUTURE_DAY_SCALE_SECONDS = 6.0 * 3600.0
_LITE = "gemini-3.5-flash-lite"
_LITE_POOL_GATE = ("google", "pool", "lite")
_LITE_PRIMARY_KEY = ("google", "gemini-3.5-flash-lite", "primary")
_PACING_INTERVAL_SETTINGS = (
    "LLM_MISTRAL_CHEAP_MIN_INTERVAL_SECONDS",
    "LLM_MISTRAL_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS",
    "LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS",
    "LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS",
    "LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS")
_RUSSIAN = (
    "\u041f\u0440\u0438\u0431\u044b\u043b\u044c \u043a\u043e\u043c\u043f"
    "\u0430\u043d\u0438\u0438 \u0432\u044b\u0440\u043e\u0441\u043b\u0430 "
    "\u043d\u0430 \u0434\u0435\u0441\u044f\u0442\u044c \u043f\u0440\u043e"
    "\u0446\u0435\u043d\u0442\u043e\u0432 \u0432 \u044d\u0442\u043e\u043c "
    "\u043a\u0432\u0430\u0440\u0442\u0430\u043b\u0435.")
_CHINESE = (
    "\u516c\u53f8\u5229\u6da6\u672c\u5b63\u5ea6\u589e\u957f\u4e86\u767e"
    "\u5206\u4e4b\u5341\uff0c\u524d\u666f\u826f\u597d\u3002")
_OVERLOADED_TEXT = (
    "Gemini completion for model 'gemini-3.6-flash' hit a real 503 3 times in "
    "a row within this call -- giving up early instead of walking the rest of "
    "the pool, since a 503 reflects Google's shared infrastructure being "
    "overloaded for this model, not a per-project issue.")


@pytest.fixture
def fresh_pacer():
    request_pacer.reset()
    yield
    request_pacer.reset()


@pytest.fixture
def unpaced_llm(monkeypatch, fresh_pacer):
    for name in _PACING_INTERVAL_SETTINGS:
        monkeypatch.setattr(config.settings, name, 0.0)


@pytest.fixture
def no_thinking_budget_rejections():
    google_module._THINKING_BUDGET_REJECTED_MODELS.clear()
    yield
    google_module._THINKING_BUDGET_REJECTED_MODELS.clear()


def _reset_llm_state() -> None:
    request_pacer.reset()
    google_module._THINKING_BUDGET_REJECTED_MODELS.clear()


def _ok_response(model: str, content: str = "ok") -> LLMResponse:
    return LLMResponse(
        content=content, provider="google", model=model,
        usage=LLMUsage(prompt_tokens=1,
                       completion_tokens=1), finish_reason="stop")


class _FakeAdapter(LLMProviderAdapter):

    def __init__(
            self, name: str, *, fail_times: int = 0,
            fail_with: type[Exception] = LLMRequestError):
        self.name = name
        self.fail_times = fail_times
        self.fail_with = fail_with
        self.calls = 0

    async def complete(
            self, messages, *, model, temperature=0.2, max_tokens=1024,
            response_format=None, timeout_seconds=None, response_schema=None,
            **_google_kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.fail_with(
                f"{self.name} simulated failure #{self.calls}")
        return LLMResponse(
            content=f"response from {self.name}/{model}", provider=self.name,
            model=model, usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            finish_reason="stop")

    async def stream(
            self, messages, *, model, temperature=0.2, max_tokens=1024):
        yield f"chunk from {self.name}/{model}"


class _FailingModelsAdapter(LLMProviderAdapter):
    name = "google"

    def __init__(
            self, *,
            failing: dict[str, Callable[[str], Exception]] | None = None,
            content: str = "fine") -> None:
        self.failing = failing or {}
        self.content = content
        self.calls: list[str] = []

    async def complete(
            self, messages, *, model, temperature=0.2, max_tokens=1024,
            response_format=None, timeout_seconds=None, response_schema=None,
            **_google_kwargs) -> LLMResponse:
        self.calls.append(model)
        exc_type = self.failing.get(model)
        if exc_type is not None:
            raise exc_type(f"{model} simulated failure")
        return LLMResponse(
            content=self.content, provider="google", model=model,
            usage=LLMUsage(prompt_tokens=10,
                           completion_tokens=5), finish_reason="stop")

    async def stream(
            self, messages, *, model, temperature=0.2, max_tokens=1024):
        yield "unused"


class _ErrorQueueAdapter(LLMProviderAdapter):
    name = "google"

    def __init__(self, script: dict[str, list[Exception]]) -> None:
        self.script = {m: list(errs) for m, errs in script.items()}
        self.calls: list[str] = []

    async def complete(self, messages, *, model, **_kwargs) -> LLMResponse:
        self.calls.append(model)
        errors = self.script.get(model) or []
        if errors:
            raise errors.pop(0)
        return LLMResponse(
            content="fine", provider="google", model=model,
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))

    async def stream(
            self, messages, *, model, temperature=0.2, max_tokens=1024):
        yield "x"


class _FormatRecordingAdapter(LLMProviderAdapter):
    name = "google"

    def __init__(self, script: dict[str, str]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete(
            self, messages, *, model, temperature=0.2, max_tokens=1024,
            response_format=None, timeout_seconds=None, response_schema=None,
            **extra: Any) -> LLMResponse:
        self.calls.append((
            model, {
                "response_format": response_format,
                "response_schema": response_schema
            }))
        outcome = self.script[model]
        if outcome == "503":
            raise LLMServerOverloadedError(f"{model} 503")
        if outcome == "timeout":
            raise LLMRequestTimeoutError(f"{model} timed out")
        return _ok_response(model, content="{}")

    async def stream(
            self, messages, *, model, temperature=0.2, max_tokens=1024):
        yield "x"


def _router_with(**providers: LLMProviderAdapter) -> LLMRouter:
    return LLMRouter(providers=providers)


def _set_settings(monkeypatch, overrides: dict[str, Any]) -> None:
    for name, value in overrides.items():
        monkeypatch.setattr(config.settings, name, value)


def _set_google_cheap_chain(monkeypatch) -> None:
    _set_settings(
        monkeypatch, {
            "LLM_GOOGLE_MODEL_CHEAP": "gemini-3.5-flash-lite",
            "LLM_GOOGLE_MODEL_CHEAP_FALLBACK": "gemini-3.1-flash-lite"
        })


def _set_google_highest_accuracy_chain(monkeypatch) -> None:
    _set_settings(
        monkeypatch, {
            "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY": "gemini-3.8-flash",
            "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK": "gemini-3.6-flash"
        })


def _quota_api_error(kind: str):
    label = "PerDay" if kind == "day" else "PerMinute"
    return genai_errors.APIError(
        429, {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": f"Quota exceeded for quota metric ...{label}..."
            }
        })


def _quota_client_error() -> genai_errors.ClientError:
    return genai_errors.ClientError(
        429, {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "quota"
            }
        })


def _invalid_argument_error(code: int) -> Exception:
    return genai_errors.ClientError(
        code, {
            "error": {
                "code": code,
                "status": "INVALID_ARGUMENT",
                "message": "x"
            }
        })


class _FakeStreamChunk:

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeGoogleStreamModels:

    def __init__(
            self, *, fail_times: int, fail_with: Exception | None) -> None:
        self.fail_times = fail_times
        self.fail_with = fail_with
        self.calls = 0

    async def generate_content_stream(self, *, model, contents, config):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.fail_with

        async def _iter():
            yield _FakeStreamChunk("streamed")

        return _iter()


class _FakeGoogleStreamClient:

    def __init__(self, models: _FakeGoogleStreamModels) -> None:
        self.aio = type("_Aio", (), {"models": models})()


def _gemini_response(
        *, finish: str, thinking: int | None, completion: int, text: str):
    usage = SimpleNamespace(
        prompt_token_count=1500, candidates_token_count=completion,
        thoughts_token_count=thinking)
    candidate = SimpleNamespace(finish_reason=finish, finish_message=None)
    return SimpleNamespace(
        text=text, usage_metadata=usage, candidates=[candidate],
        prompt_feedback=None)


class _ThinkingRecordingGenerate:

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.thinking_configs: list[Any] = []

    async def __call__(self, *, model, contents, config):
        self.thinking_configs.append(config.thinking_config)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _runaway() -> Any:
    return _gemini_response(
        text='{"thought": "cut', finish="FinishReason.MAX_TOKENS",
        thinking=3931, completion=150)


def _clean(text: str = '{"finish_answer": "ok"}') -> Any:
    return _gemini_response(
        finish="FinishReason.STOP", thinking=None, completion=130, text=text)


async def _complete_lite(max_tokens: int = 4096) -> LLMResponse:
    return await google_module.GoogleAdapter().complete(
        _msgs(), model=_LITE, max_tokens=max_tokens)


@pytest.mark.usefixtures("unpaced_llm")
class TestRouterBasics:

    async def _unknown_provider_raises_config_error(self, monkeypatch):
        router = _router_with(anthropic=_FakeAdapter("anthropic"))
        with pytest.raises(LLMConfigError):
            await router.complete(
                LLMTier.CHEAP, _msgs(), provider="not_a_provider")

    async def _stream_yields_chunks_from_resolved_adapter(self, monkeypatch):
        monkeypatch.setattr(config.settings, "LLM_MISTRAL_MODEL_CHEAP", "m")
        router = _router_with(mistral=_FakeAdapter("mistral"))
        chunks = [
            chunk async for chunk in router.stream(
                LLMTier.CHEAP, _msgs(), provider="mistral")
        ]
        assert chunks == ["chunk from mistral/m"]

    async def _concurrent_cheap_calls_get_paced_when_interval_set(
            self, monkeypatch):
        monkeypatch.setattr(
            config.settings, "LLM_MISTRAL_MODEL_CHEAP", "small")
        monkeypatch.setattr(
            config.settings, "LLM_MISTRAL_CHEAP_MIN_INTERVAL_SECONDS", 1.3)
        recorder = _RecordingSleep()
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", recorder)
        router = _router_with(mistral=_FakeAdapter("mistral"))
        await router.complete(LLMTier.CHEAP, _msgs(), provider="mistral")
        await router.complete(LLMTier.CHEAP, _msgs(), provider="mistral")
        assert recorder.delays and recorder.delays[-1] > 0

    async def _rate_limit_falls_back_to_the_alternate_model(self, monkeypatch):
        _set_settings(
            monkeypatch, {
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY": "medium",
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK": "large"
            })
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _FakeAdapter(
            "mistral", fail_times=2, fail_with=LLMRateLimitError)
        resp = await _router_with(mistral=adapter).complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), provider="mistral", retries=1)
        assert resp.content == "response from mistral/large"
        assert adapter.calls == 3

    async def _failure_without_a_fallback_is_raised(self, monkeypatch):
        cases = {
            "non-rate-limit-error": ({
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY": "medium",
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK": "large"
            }, LLMTier.HIGHEST_ACCURACY, LLMRequestError),
            "no-fallback-configured": ({
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY": "medium",
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK": ""
            }, LLMTier.HIGHEST_ACCURACY, LLMRateLimitError),
            "tier-without-fallback": ({
                "LLM_MISTRAL_MODEL_CHEAP": "small",
                "LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK": "large",
                "LLM_MISTRAL_MODEL_CHEAP_FALLBACK": ""
            }, LLMTier.CHEAP, LLMRateLimitError)
        }
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        for case_id, (overrides, tier, fail_with) in cases.items():
            _set_settings(monkeypatch, overrides)
            adapter = _FakeAdapter(
                "mistral", fail_times=5, fail_with=fail_with)
            with pytest.raises(fail_with):
                await _router_with(mistral=adapter).complete(
                    tier, _msgs(), provider="mistral", retries=1)
            assert adapter.calls == 2, case_id

    async def test_router_basics(self):
        await _run_steps(
            self._unknown_provider_raises_config_error,
            self._stream_yields_chunks_from_resolved_adapter,
            self._concurrent_cheap_calls_get_paced_when_interval_set,
            self._rate_limit_falls_back_to_the_alternate_model,
            self._failure_without_a_fallback_is_raised,
            reset=_reset_llm_state)


def test_anthropic_system_messages_join_and_turns_stay_in_order():
    messages = [
        LLMMessage(role="system", content="You are a helpful analyst."),
        LLMMessage(role="system", content="Always cite sources."),
        LLMMessage(role="user", content="What is AAPL's P/E?"),
        LLMMessage(role="assistant", content="Let me check.")
    ]
    system, turns = _split_system_and_turns(messages)
    assert system == "You are a helpful analyst.\n\nAlways cite sources."
    assert turns == [{
        "role": "user",
        "content": "What is AAPL's P/E?"
    }, {
        "role": "assistant",
        "content": "Let me check."
    }]


@pytest.mark.usefixtures("unpaced_llm")
class TestRequestPacer:

    async def _empty_candidate_lists(self, monkeypatch):
        pacer = RequestPacer()
        with pytest.raises(ValueError):
            await pacer.reserve_best_slot_gated([], 1.0)
        assert await pacer.peek_earliest_next_allowed_at([]) == 0.0
        assert await pacer.peek_exhausted_fraction([], 65.0) == 0.0

    async def _fraction_reflects_exact_share_confirmed_exhausted(
            self, monkeypatch):
        pacer = RequestPacer()
        now = time.monotonic()
        candidates = [("google", "m", "primary"), ("google", "m", "key_2"),
                      ("google", "m", "key_3"), ("google", "m", "key_4")]
        for label in candidates[:3]:
            await pacer.mark_unavailable_until(label, now + 200.0)
        fraction = await pacer.peek_exhausted_fraction(candidates, 65.0)
        assert fraction == pytest.approx(0.75)

    async def _reserve_slot_waits_and_books_the_next_slot(self, monkeypatch):
        pacer = RequestPacer()
        key = ("k", )
        until = time.monotonic() + 50.0
        pacer._next_allowed_at[key] = until
        wait = await pacer.reserve_slot(key, 3.0)
        assert wait > 30.0
        assert pacer._next_allowed_at[key] == pytest.approx(until + 3.0)

    async def _the_adapter_wait_frees_the_running_calls_slot(
            self, monkeypatch):
        semaphore = asyncio.Semaphore(1)
        async with ConcurrencyGate(semaphore):
            assert semaphore._value == 0
            async with released_while_waiting():
                assert semaphore._value == 1
            assert semaphore._value == 0
        assert semaphore._value == 1
        async with released_while_waiting():
            pass

    async def test_pacer_slots_and_concurrency_gate(self):
        await _run_steps(
            self._empty_candidate_lists,
            self._fraction_reflects_exact_share_confirmed_exhausted,
            self._reserve_slot_waits_and_books_the_next_slot,
            self._the_adapter_wait_frees_the_running_calls_slot,
            reset=_reset_llm_state)


@pytest.mark.usefixtures("unpaced_llm")
class TestMistralKeys:

    @pytest.fixture(autouse=True)
    def _clear_all_mistral_key_slots(self, monkeypatch):
        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY", "")
        for i in range(2, 8):
            monkeypatch.setattr(config.settings, f"MISTRAL_API_KEY_{i}", "")

    def _rate_limited_keys_are_skipped_but_never_block(self, monkeypatch):
        from ai.llm.mistral_provider import _MistralKeyPool

        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY", "key-A")
        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY_2", "key-B")
        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY_3", "key-C")
        pool = _MistralKeyPool()
        pool.next_client()
        pool.mark_rate_limited("primary")
        labels = [pool.next_client()[0] for _ in range(4)]
        assert "primary" not in labels

        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY_3", "")
        pool = _MistralKeyPool()
        pool.mark_rate_limited("primary")
        pool.mark_rate_limited("key_2")
        label, client = pool.next_client()
        assert label in ("primary", "key_2")
        assert client is not None

    async def _no_keys_raise_config_error_not_a_fabricated_402(self):
        from ai.llm.mistral_provider import MistralAdapter, _MistralKeyPool

        with pytest.raises(LLMConfigError):
            _MistralKeyPool().next_client()

        adapter = MistralAdapter()
        with pytest.raises(LLMConfigError) as exc_info:
            await adapter.complete(_msgs(), model="mistral-small-2603")
        message = str(exc_info.value)
        assert "returned 402" not in message
        assert ": None" not in message
        assert "not configured" in message or "no Mistral API key" in message

        with pytest.raises(LLMConfigError) as exc_info:
            async for _ in adapter.stream(_msgs(), model="mistral-small-2603"):
                pass
        message = str(exc_info.value)
        assert "returned 402" not in message
        assert ": None" not in message

    async def test_key_pool_rotation_and_missing_keys(self, monkeypatch):
        self._rate_limited_keys_are_skipped_but_never_block(monkeypatch)
        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY", "")
        monkeypatch.setattr(config.settings, "MISTRAL_API_KEY_2", "")
        await self._no_keys_raise_config_error_not_a_fabricated_402()


@pytest.mark.usefixtures("unpaced_llm")
class TestGoogleKeys:

    @pytest.fixture(autouse=True)
    def _clear_all_google_key_slots(self, monkeypatch):
        monkeypatch.setattr(config.settings, "GOOGLE_API_KEY", "")
        for i in range(2, 11):
            monkeypatch.setattr(config.settings, f"GOOGLE_API_KEY_{i}", "")

    def _available_labels_follow_keys_not_cooldowns(self, monkeypatch):
        from ai.llm.google_provider import _GoogleKeyPool

        monkeypatch.setattr(config.settings, "GOOGLE_API_KEY", "key-A")
        pool = _GoogleKeyPool()
        assert pool.key_count() == 1
        assert pool.available_labels_excluding(set()) == ["primary"]
        assert pool.client_for("primary") is not None

        monkeypatch.setattr(config.settings, "GOOGLE_API_KEY_2", "key-B")
        monkeypatch.setattr(config.settings, "GOOGLE_API_KEY_3", "key-C")
        pool = _GoogleKeyPool()
        assert not hasattr(pool, "mark_rate_limited")
        assert not hasattr(pool, "_cooldown_until")
        assert set(pool.available_labels_excluding(
            set())) == {"primary", "key_2", "key_3"}
        assert set(pool.available_labels_excluding({"primary"
                                                    })) == {"key_2", "key_3"}

    async def _stream_without_keys_raises_config_error(self):
        adapter = google_module.GoogleAdapter()
        with pytest.raises(LLMConfigError):
            async for _ in adapter.stream(_msgs(), model="gemini-3.8-flash"):
                pass

    async def test_key_pool_labels_and_missing_keys(self, monkeypatch):
        await self._stream_without_keys_raises_config_error()
        self._available_labels_follow_keys_not_cooldowns(monkeypatch)


@pytest.mark.usefixtures("unpaced_llm")
class TestGoogleAdapter:

    def _day_quota_id_classified_as_day(self):
        exc = Exception(
            "429 RESOURCE_EXHAUSTED. quotaId: "
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        assert google_module._classify_quota_violation(exc) == "day"

    async def _day_quota_hit_gets_the_long_cooldown(self, monkeypatch):
        gen_a = _FakeGoogleGenerateContent(
            fail_times=1, fail_with=_quota_api_error("day"))
        fake_pool = _FakeGooglePool({
            "primary": _FakeGoogleClient(gen_a),
            "key_2": _FakeGoogleClient(_FakeGoogleGenerateContent())
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        resp = await google_module.GoogleAdapter().complete(
            _msgs(), model="gemini-3.8-flash")
        assert resp.content == "ok"
        key = ("google", "gemini-3.8-flash", "primary")
        remaining = request_pacer._next_allowed_at[key] - time.monotonic()
        assert remaining == pytest.approx(
            google_module._seconds_until_pacific_midnight(), abs=2.0)

    async def _non_quota_client_error_raises_without_failover(
            self, monkeypatch):
        auth_error = genai_errors.ClientError(
            401, {
                "error": {
                    "code": 401,
                    "status": "UNAUTHENTICATED",
                    "message": "bad key"
                }
            })
        gen_a = _FakeGoogleGenerateContent(fail_times=99, fail_with=auth_error)
        gen_b = _FakeGoogleGenerateContent()
        fake_pool = _FakeGooglePool({
            "primary": _FakeGoogleClient(gen_a),
            "key_2": _FakeGoogleClient(gen_b)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMRequestError):
            await google_module.GoogleAdapter().complete(
                _msgs(), model="gemini-3.8-flash")
        assert gen_a.calls == 1
        assert gen_b.calls == 0

    async def _gives_up_after_max_malformed_retries_with_a_big_pool(
            self, monkeypatch):
        malformed_resp = _FakeGeminiResponse()
        malformed_resp.candidates = [
            type(
                "C", (),
                {"finish_reason": "FinishReason.MALFORMED_RESPONSE"})()
        ]
        malformed_resp.text = ""

        class _AlwaysMalformed:

            def __init__(self):
                self.calls = 0

            async def __call__(self, *, model, contents, config):
                self.calls += 1
                return malformed_resp

        gens = {
            label: _AlwaysMalformed()
            for label in ("primary", "key_2", "key_3", "key_4", "key_5")
        }
        fake_pool = _FakeGooglePool({
            label: _FakeGoogleClient(gen)
            for label, gen in gens.items()
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMRequestError) as exc_info:
            await google_module.GoogleAdapter().complete(_msgs(), model=_LITE)
        assert "MALFORMED_RESPONSE" in str(exc_info.value)
        total_calls = sum(gen.calls for gen in gens.values())
        assert total_calls == google_module._MAX_MALFORMED_RETRIES + 1
        assert any(gen.calls == 0 for gen in gens.values())

    async def _wait_beyond_one_interval_fails_fast_without_sleeping(
            self, monkeypatch):
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS", 5.0)
        recorder = _RecordingSleep()
        monkeypatch.setattr("ai.llm.google_provider.asyncio.sleep", recorder)
        gen = _FakeGoogleGenerateContent()
        fake_pool = _FakeGooglePool({"primary": _FakeGoogleClient(gen)})
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        await request_pacer.mark_unavailable_until(
            _LITE_PRIMARY_KEY,
            time.monotonic() + 65.0)
        with pytest.raises(LLMRateLimitError):
            await google_module.GoogleAdapter().complete(_msgs(), model=_LITE)
        assert gen.calls == 0
        assert recorder.delays == []

    async def _zero_min_interval_pool_is_never_exhausted(self, monkeypatch):
        fake_pool = _FakeGooglePool(
            {"primary": _FakeGoogleClient(_FakeGoogleGenerateContent())})
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        await request_pacer.mark_unavailable_until(
            _LITE_PRIMARY_KEY,
            time.monotonic() + 99999.0)
        assert await google_module.google_model_pool_exhausted(_LITE) is False

    async def _success_clears_the_models_overloaded_marker(self, monkeypatch):

        class _Resp:

            class _Usage:
                prompt_token_count = 1
                candidates_token_count = 1

            class _Candidate:
                finish_reason = "STOP"

            text = "ok"
            usage_metadata = _Usage()
            candidates = (_Candidate(), )

        async def generate_content(*, model, contents, config):
            return _Resp()

        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)))

        class _Pool:

            def key_count(self) -> int:
                return 1

            def available_labels_excluding(self, excluded):
                return [lbl for lbl in ["primary"] if lbl not in excluded]

            def client_for(self, label):
                return client

        monkeypatch.setattr(google_module, "_pool", _Pool())
        key = ("google", "server_error", "gemini-3.8-flash")
        await request_pacer.mark_unavailable_until(
            key,
            time.monotonic() + 20.0)
        resp = await google_module.GoogleAdapter().complete(
            _msgs(), model="gemini-3.8-flash")
        assert resp.content == "ok"
        assert key not in request_pacer._next_allowed_at
        assert not await google_module.google_model_confirmed_overloaded(
            "gemini-3.8-flash")

    async def _fallback_flag_uses_the_fallback_timeout(self, monkeypatch):
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS",
            3600.0)
        monkeypatch.setattr(
            config.settings,
            "LLM_GOOGLE_HIGHEST_FALLBACK_REQUEST_TIMEOUT_SECONDS", 0.05)
        hanging_pool = _scripted_pool(["primary"], ["hang"])
        monkeypatch.setattr(google_module, "_pool", hanging_pool)
        with pytest.raises(LLMRequestTimeoutError):
            await asyncio.wait_for(
                google_module.GoogleAdapter().complete(
                    _msgs(), model="gemini-3.6-flash", fallback=True), 5)

    async def _server_error_wait_escalates_and_success_resets(
            self, monkeypatch):
        model = "gemini-3.8-flash"
        monkeypatch.setattr(
            google_module, "_pool", _scripted_pool(["primary"], ["503"]))
        waits = []
        for _ in range(4):
            with pytest.raises(LLMServerOverloadedError):
                await google_module.GoogleAdapter().complete(
                    _msgs(), model=model)
            waits.append(await google_module.google_server_error_wait(model))
        for got, cap in zip(waits, (20.0, 60.0, 180.0, 300.0), strict=True):
            assert cap - 5.0 < got <= cap
        monkeypatch.setattr(
            google_module, "_pool", _scripted_pool(["primary"], ["ok"]))
        await google_module.GoogleAdapter().complete(_msgs(), model=model)
        assert not await google_module.google_model_confirmed_overloaded(model)
        monkeypatch.setattr(
            google_module, "_pool", _scripted_pool(["primary"], ["503"]))
        with pytest.raises(LLMServerOverloadedError):
            await google_module.GoogleAdapter().complete(_msgs(), model=model)
        assert (
            15.0 < await google_module.google_server_error_wait(model) <= 20.0)

    async def _a_rate_limit_mark_during_the_pacing_wait_still_skips(
            self, monkeypatch):
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS", 5.0)
        gen = _FakeGoogleGenerateContent()
        monkeypatch.setattr(
            google_module, "_pool",
            _FakeGooglePool({"primary": _FakeGoogleClient(gen)}))
        await request_pacer.mark_unavailable_until(
            _LITE_PRIMARY_KEY,
            time.monotonic() + 2.0)

        async def _another_call_gets_a_429(seconds: float) -> None:
            await request_pacer.mark_unavailable_until(
                _LITE_PRIMARY_KEY,
                time.monotonic() + 3.0)

        monkeypatch.setattr(
            "ai.llm.google_provider.asyncio.sleep", _another_call_gets_a_429)
        with pytest.raises(LLMRateLimitError):
            await google_module.GoogleAdapter().complete(_msgs(), model=_LITE)
        assert gen.calls == 0

    async def test_quota_and_error_handling(self):
        self._day_quota_id_classified_as_day()
        await _run_steps(
            self._day_quota_hit_gets_the_long_cooldown,
            self._non_quota_client_error_raises_without_failover,
            self._gives_up_after_max_malformed_retries_with_a_big_pool,
            self._success_clears_the_models_overloaded_marker,
            self._server_error_wait_escalates_and_success_resets,
            reset=_reset_llm_state)

    async def _timeouts_without_a_cooldown_leave_no_marker(
            self, monkeypatch):
        assert google_module._default_request_timeout(
            "gemini-3.8-flash") == 30.0
        assert google_module._default_request_timeout(
            "gemini-3.6-flash", fallback=True) == 60.0
        assert google_module._default_request_timeout(
            "gemini-3.5-flash-lite", fallback=True) == 60.0
        monkeypatch.setattr(
            google_module, "_pool", _scripted_pool(["primary"], ["hang"]))
        with pytest.raises(LLMRequestError) as info:
            await google_module.GoogleAdapter().complete(
                _msgs(), model="gemini-3.8-flash", timeout_seconds=0.05)
        assert not isinstance(info.value, LLMRequestTimeoutError)
        assert not await google_module.google_model_recently_timed_out(
            "gemini-3.8-flash")
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS",
            0.05)
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS", 0.0)
        monkeypatch.setattr(
            google_module, "_pool", _scripted_pool(["primary"], ["hang"]))
        with pytest.raises(LLMRequestTimeoutError):
            await google_module.GoogleAdapter().complete(
                _msgs(), model="gemini-3.8-flash")
        assert not await google_module.google_model_recently_timed_out(
            "gemini-3.8-flash")

    async def test_pacing_waits_and_timeouts(self):
        await _run_steps(
            self._wait_beyond_one_interval_fails_fast_without_sleeping,
            self._zero_min_interval_pool_is_never_exhausted,
            self._fallback_flag_uses_the_fallback_timeout,
            self._timeouts_without_a_cooldown_leave_no_marker,
            self._a_rate_limit_mark_during_the_pacing_wait_still_skips,
            reset=_reset_llm_state)


@pytest.mark.usefixtures("unpaced_llm")
class TestGoogleServerErrors:

    async def _lite_503_gives_up_after_three_keys_without_cooldown(
            self, monkeypatch):
        gens = [
            _FakeGoogleGenerateContent(fail_times=99, fail_with=_server_503())
            for _ in range(4)
        ]
        fake_pool = _FakeGooglePool({
            label: _FakeGoogleClient(gen)
            for label, gen in zip(("primary", "key_2", "key_3",
                                   "key_4"), gens, strict=True)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMServerOverloadedError) as exc_info:
            await google_module.GoogleAdapter().complete(
                _msgs(), model="gemini-3.1-flash-lite")
        assert [g.calls for g in gens] == [1, 1, 1, 0]
        assert "real 503 3 times" in str(exc_info.value)
        assert isinstance(exc_info.value, LLMRateLimitError)
        assert (
            "google", "server_error",
            "gemini-3.1-flash-lite") not in request_pacer._next_allowed_at
        assert not await google_module.google_model_confirmed_overloaded(
            "gemini-3.1-flash-lite")

    async def _lite_pool_smaller_than_budget_raises_server_overloaded(
            self, monkeypatch):
        gen_a = _FakeGoogleGenerateContent(
            fail_times=99, fail_with=_server_503())
        gen_b = _FakeGoogleGenerateContent(
            fail_times=99, fail_with=_server_503())
        fake_pool = _FakeGooglePool({
            "primary": _FakeGoogleClient(gen_a),
            "key_2": _FakeGoogleClient(gen_b)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMServerOverloadedError) as exc_info:
            await google_module.GoogleAdapter().complete(_msgs(), model=_LITE)
        assert "every pooled Google project (2)" in str(exc_info.value)
        assert (gen_a.calls, gen_b.calls) == (1, 1)
        assert not await google_module.google_model_confirmed_overloaded(_LITE)

    async def _503_then_429_pool_exhaustion_is_a_plain_rate_limit(
            self, monkeypatch):
        gen_a = _FakeGoogleGenerateContent(
            fail_times=99, fail_with=_server_503())
        gen_b = _FakeGoogleGenerateContent(
            fail_times=99, fail_with=_quota_api_error("minute"))
        fake_pool = _FakeGooglePool({
            "primary": _FakeGoogleClient(gen_a),
            "key_2": _FakeGoogleClient(gen_b)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMRateLimitError) as exc_info:
            await google_module.GoogleAdapter().complete(_msgs(), model=_LITE)
        assert not isinstance(exc_info.value, LLMServerOverloadedError)

    async def _lite_stream_tries_three_keys_then_raises_no_cooldown(
            self, monkeypatch):
        models = [
            _FakeGoogleStreamModels(fail_times=99, fail_with=_server_503())
            for _ in range(4)
        ]
        fake_pool = _FakeGooglePool({
            label: _FakeGoogleStreamClient(m)
            for label, m in zip(("primary", "key_2", "key_3",
                                 "key_4"), models, strict=True)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMServerOverloadedError):
            async for _ in google_module.GoogleAdapter().stream(_msgs(),
                                                                model=_LITE):
                pass
        assert [m.calls for m in models] == [1, 1, 1, 0]
        assert not await google_module.google_model_confirmed_overloaded(_LITE)

    async def _lite_stream_recovers_on_second_key(self, monkeypatch):
        m_a = _FakeGoogleStreamModels(fail_times=99, fail_with=_server_503())
        m_b = _FakeGoogleStreamModels(fail_times=0, fail_with=None)
        fake_pool = _FakeGooglePool({
            "primary": _FakeGoogleStreamClient(m_a),
            "key_2": _FakeGoogleStreamClient(m_b)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        chunks = [
            c async for c in google_module.GoogleAdapter().stream(
                _msgs(), model=_LITE)
        ]
        assert chunks == ["streamed"]
        assert (m_a.calls, m_b.calls) == (1, 1)

    async def _highest_accuracy_stream_gives_up_on_first_503(
            self, monkeypatch):
        m_a = _FakeGoogleStreamModels(fail_times=99, fail_with=_server_503())
        m_b = _FakeGoogleStreamModels(fail_times=0, fail_with=None)
        fake_pool = _FakeGooglePool({
            "primary": _FakeGoogleStreamClient(m_a),
            "key_2": _FakeGoogleStreamClient(m_b)
        })
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        with pytest.raises(LLMServerOverloadedError):
            async for _ in google_module.GoogleAdapter().stream(
                    _msgs(), model="gemini-3.8-flash"):
                pass
        assert (m_a.calls, m_b.calls) == (1, 0)
        assert await google_module.google_model_confirmed_overloaded(
            "gemini-3.8-flash")

    async def _flash_fallback_tries_three_keys_on_503(self, monkeypatch):
        labels = ["primary", "key_2", "key_3", "key_4"]
        pool = _scripted_pool(labels, ["503", "503", "ok"])
        monkeypatch.setattr(google_module, "_pool", pool)
        response = await google_module.GoogleAdapter().complete(
            _msgs(), model="gemini-3.6-flash", fallback=True)
        assert response.content == "ok"
        assert len(pool.used) == 3
        pool = _scripted_pool(labels, ["503"])
        monkeypatch.setattr(google_module, "_pool", pool)
        with pytest.raises(LLMServerOverloadedError):
            await google_module.GoogleAdapter().complete(
                _msgs(), model="gemini-3.6-flash", fallback=True)
        assert len(pool.used) == 3
        pool = _scripted_pool(labels, ["503"])
        monkeypatch.setattr(google_module, "_pool", pool)
        with pytest.raises(LLMServerOverloadedError):
            await google_module.GoogleAdapter().complete(
                _msgs(), model="gemini-3.8-flash")
        assert len(pool.used) == 1

    async def test_server_overload_handling(self):
        await _run_steps(
            self._flash_fallback_tries_three_keys_on_503,
            self._lite_503_gives_up_after_three_keys_without_cooldown,
            self._lite_pool_smaller_than_budget_raises_server_overloaded,
            self._503_then_429_pool_exhaustion_is_a_plain_rate_limit,
            self._lite_stream_tries_three_keys_then_raises_no_cooldown,
            self._lite_stream_recovers_on_second_key,
            self._highest_accuracy_stream_gives_up_on_first_503,
            reset=_reset_llm_state)


@pytest.mark.usefixtures("unpaced_llm", "no_thinking_budget_rejections")
class TestRunawayThinkingRetry:

    async def _runaway_is_retried_once_with_a_thinking_budget(
            self, monkeypatch):
        gen = _ThinkingRecordingGenerate([_runaway(), _clean()])
        monkeypatch.setattr(google_module, "_pool", _single_client_pool(gen))
        with capture_logs() as logs:
            response = await _complete_lite()
        assert response.content == '{"finish_answer": "ok"}'
        assert response.finish_reason == "FinishReason.STOP"
        first, second = gen.thinking_configs
        assert first.thinking_level is not None
        assert first.thinking_budget is None
        assert second.thinking_budget == 1024
        assert second.thinking_level is None
        events = [entry["event"] for entry in logs]
        assert "google.thinking_runaway_retrying_with_budget" in events
        assert "google.thinking_runaway_retry_done" in events

    async def _a_400_returns_the_original_and_disables_the_model(
            self, monkeypatch):
        gen = _ThinkingRecordingGenerate(
            [_runaway(), _invalid_argument_error(400),
             _runaway()])
        monkeypatch.setattr(google_module, "_pool", _single_client_pool(gen))
        with capture_logs() as logs:
            first = await _complete_lite()
            second = await _complete_lite()
        assert first.content == '{"thought": "cut'
        assert second.finish_reason == "FinishReason.MAX_TOKENS"
        assert len(gen.thinking_configs) == 3
        assert _LITE in google_module._THINKING_BUDGET_REJECTED_MODELS
        assert any(
            e["event"] ==
            "google.thinking_budget_rejected_disabling_runaway_retry"
            for e in logs)

    async def _other_retry_failures_return_the_original(self, monkeypatch):
        original = _runaway()

        class _RetryFails(_ThinkingRecordingGenerate):

            async def __call__(self, *, model, contents, config):
                if self.thinking_configs:
                    self.thinking_configs.append(config.thinking_config)
                    raise LLMRateLimitError("pool cooling down")
                return await super().__call__(
                    model=model, contents=contents, config=config)

        gen = _RetryFails([original])
        monkeypatch.setattr(google_module, "_pool", _single_client_pool(gen))
        response = await _complete_lite()
        assert response.finish_reason == "FinishReason.MAX_TOKENS"
        assert _LITE not in google_module._THINKING_BUDGET_REJECTED_MODELS

    async def _lite_pool_gate_keeps_the_30_second_limit_without_retry(
            self, monkeypatch):
        _set_settings(
            monkeypatch, {
                "LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS": 3.0,
                "LLM_GOOGLE_LITE_POOL_MAX_WAIT_SECONDS": 30.0,
                "LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS": 0.0
            })
        monkeypatch.setattr(
            "ai.llm.google_provider.asyncio.sleep", _RecordingSleep())
        gen = _ThinkingRecordingGenerate([])
        monkeypatch.setattr(google_module, "_pool", _single_client_pool(gen))
        await request_pacer.mark_unavailable_until(
            _LITE_POOL_GATE,
            time.monotonic() + 30.9)
        with pytest.raises(LLMRateLimitError):
            await _complete_lite()
        assert gen.thinking_configs == []

    async def _caller_thinking_budget_on_the_first_call(self, monkeypatch):
        gen = _ThinkingRecordingGenerate(
            [_clean(), _invalid_argument_error(400),
             _clean(), _clean()])
        monkeypatch.setattr(google_module, "_pool", _single_client_pool(gen))
        adapter = google_module.GoogleAdapter()
        await adapter.complete(_msgs(), model=_LITE, thinking_budget=512)
        await adapter.complete(_msgs(), model=_LITE, thinking_budget=512)
        await adapter.complete(_msgs(), model=_LITE, thinking_budget=512)
        budgets = [c.thinking_budget for c in gen.thinking_configs]
        assert budgets == [512, 512, None, None]
        assert _LITE in google_module._THINKING_BUDGET_REJECTED_MODELS

    async def test_runaway_thinking_retry(self):
        await _run_steps(
            self._caller_thinking_budget_on_the_first_call,
            self._runaway_is_retried_once_with_a_thinking_budget,
            self._a_400_returns_the_original_and_disables_the_model,
            self._other_retry_failures_return_the_original,
            self._lite_pool_gate_keeps_the_30_second_limit_without_retry,
            reset=_reset_llm_state)


@pytest.mark.usefixtures("unpaced_llm")
class TestRouterFallback:

    async def _exhausted_primary_goes_straight_to_the_fallback(
            self, monkeypatch):
        _set_google_highest_accuracy_chain(monkeypatch)
        monkeypatch.setattr(
            config.settings,
            "LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS", 12.0)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        fake_pool = _FakeGooglePool(
            {"primary": _FakeGoogleClient(_FakeGoogleGenerateContent())})
        monkeypatch.setattr(google_module, "_pool", fake_pool)
        far_future = time.monotonic() + _FAR_FUTURE_DAY_SCALE_SECONDS
        for exhausted in (("gemini-3.8-flash", ),
                          ("gemini-3.8-flash", "gemini-3.6-flash")):
            request_pacer.reset()
            for model in exhausted:
                await request_pacer.mark_unavailable_until(
                    ("google", model, "primary"), far_future)
            adapter = _FakeAdapter("google")
            resp = await _router_with(google=adapter).complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google",
                retries=1)
            assert resp.content == "response from google/gemini-3.6-flash"
            assert adapter.calls == 1

    async def _overloaded_skip_is_reported_as_server_overloaded(
            self, monkeypatch):
        _set_google_highest_accuracy_chain(monkeypatch)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        await request_pacer.mark_unavailable_until(
            ("google", "server_error", "gemini-3.8-flash"),
            time.monotonic() + 20.0)
        adapter = _FailingModelsAdapter(
            failing={"gemini-3.6-flash": LLMRateLimitError})
        with pytest.raises(LLMRateLimitError) as exc_info:
            await _router_with(google=adapter).complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google",
                retries=1)
        assert adapter.calls == ["gemini-3.6-flash"]
        assert isinstance(exc_info.value.__cause__, LLMServerOverloadedError)
        assert "skipped 'gemini-3.8-flash'" in str(exc_info.value.__cause__)

    async def _non_english_fallback_response_raises(self, monkeypatch):
        _set_google_cheap_chain(monkeypatch)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _FailingModelsAdapter(
            failing={"gemini-3.5-flash-lite": LLMRateLimitError},
            content=_CHINESE)
        with pytest.raises(LLMNonEnglishOutputError):
            await _router_with(google=adapter).complete(
                LLMTier.CHEAP, _msgs(), provider="google", retries=0)
        assert adapter.calls == [
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"
        ]

    async def _json_escaped_non_english_is_rejected(self, monkeypatch):
        _set_google_cheap_chain(monkeypatch)
        content = json.dumps({"action": "finish", "finish_answer": _RUSSIAN})
        assert "\\u" in content
        adapter = _FailingModelsAdapter(content=content)
        with pytest.raises(LLMNonEnglishOutputError):
            await _router_with(google=adapter).complete(
                LLMTier.CHEAP, _msgs(), provider="google",
                response_format="json_object")

    async def _fallback_call_has_the_flag_and_no_schema(self, monkeypatch):
        _chain(monkeypatch)
        adapter = _PerModelAdapter({"m1": ["503"], "m2": ["ok"]})
        schema = {"type": "object"}
        resp = await LLMRouter(providers={
            "google": adapter
        }).complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google", retries=0,
            response_format="json_object", response_schema=schema)
        assert resp.content == "answer from m2"
        assert adapter.calls == [("m1", {}), ("m2", {"fallback": True})]

    async def _thinking_budget_goes_to_both_google_calls(self, monkeypatch):
        _set_google_cheap_chain(monkeypatch)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _PerModelAdapter({
            "gemini-3.5-flash-lite": ["503"],
            "gemini-3.1-flash-lite": ["ok"]
        })
        await LLMRouter(providers={
            "google": adapter
        }).complete(
            LLMTier.CHEAP, _msgs(), provider="google", retries=0,
            thinking_budget=512)
        assert [extra.get("thinking_budget")
                for _, extra in adapter.calls] == [512, 512]

    async def _a_failed_fallback_is_raised_without_more_calls(
            self, monkeypatch):
        _chain(monkeypatch)
        adapter = _FormatRecordingAdapter({"m1": "timeout", "m2": "timeout"})
        with pytest.raises(LLMRequestTimeoutError):
            await LLMRouter(providers={
                "google": adapter
            }).complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google",
                retries=0)
        assert [m for m, _ in adapter.calls] == ["m1", "m2"]

    async def _day_exhausted_fallback_is_skipped(self, monkeypatch):
        _chain(monkeypatch)

        async def _exhausted(model: str) -> bool:
            return model == "m2"

        monkeypatch.setattr(
            router_module, "google_model_pool_exhausted", _exhausted)
        adapter = _PerModelAdapter({"m1": ["503"], "m2": ["ok"]})
        with pytest.raises(LLMRateLimitError) as exc_info:
            await LLMRouter(providers={
                "google": adapter
            }).complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google",
                retries=0)
        assert adapter.models == ["m1"]
        assert "skipped 'm2'" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, LLMServerOverloadedError)

    async def test_skips_and_non_english_answers(self):
        await _run_steps(
            self._exhausted_primary_goes_straight_to_the_fallback,
            self._overloaded_skip_is_reported_as_server_overloaded,
            self._non_english_fallback_response_raises,
            self._json_escaped_non_english_is_rejected,
            reset=_reset_llm_state)

    async def _timed_out_primary_goes_to_the_fallback(self, monkeypatch):
        _chain(monkeypatch)
        adapter = _PerModelAdapter({"m1": ["timeout"], "m2": ["ok"]})
        router = LLMRouter(providers={"google": adapter})
        first = await router.complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google", retries=4)
        second = await router.complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google", retries=4)
        assert first.content == second.content == "answer from m2"
        assert adapter.models == ["m1", "m2", "m2"]

        request_pacer.reset()
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS", 0.0)
        adapter = _PerModelAdapter({"m1": ["timeout"], "m2": ["ok"]})
        await LLMRouter(providers={
            "google": adapter
        }).complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), provider="google", retries=4)
        assert adapter.models == ["m1", "m2"]

    async def test_fallback_calls(self):
        await _run_steps(
            self._fallback_call_has_the_flag_and_no_schema,
            self._thinking_budget_goes_to_both_google_calls,
            self._a_failed_fallback_is_raised_without_more_calls,
            self._day_exhausted_fallback_is_skipped,
            self._timed_out_primary_goes_to_the_fallback,
            reset=_reset_llm_state)


def test_router_retry_limit():
    cases = [(
        "invalid-request-google", LLMInvalidRequestError("400"), "google",
        "gemini-3.8-flash", 4, 0),
             (
                 "invalid-request-mistral", LLMInvalidRequestError("400"),
                 "mistral", "m", 4, 0),
             (
                 "google-lite-timeout-one-retry", LLMProviderTimeoutError("t"),
                 "google", _LITE, 4, 1),
             (
                 "google-highest-explicit-timeout-one-retry",
                 LLMProviderTimeoutError("t"), "google", "gemini-3.8-flash",
                 4, 1),
             (
                 "google-lite-timeout-no-retries-asked",
                 LLMProviderTimeoutError("t"), "google", _LITE, 0, 0),
             (
                 "google-lite-request-error", LLMRequestError("boom"),
                 "google", _LITE, 4, 4),
             (
                 "google-rate-limit", LLMRateLimitError("429"), "google",
                 "gemini-3.8-flash", 4, 4),
             (
                 "google-overloaded", LLMServerOverloadedError("503"),
                 "google", "gemini-3.8-flash", 4, 4),
             (
                 "google-request-timeout", LLMRequestTimeoutError("t"),
                 "google", "gemini-3.8-flash", 4, 4),
             (
                 "mistral-rate-limit", LLMRateLimitError("429"), "mistral",
                 "mistral-small-lite", 4, 4),
             (
                 "mistral-overloaded", LLMServerOverloadedError("503"),
                 "mistral", "mistral-small-lite", 4, 4),
             (
                 "anthropic-provider-timeout", LLMProviderTimeoutError("t"),
                 "anthropic", "claude-lite", 4, 4)]
    for case_id, exc, provider, model, retries, expected in cases:
        got = router_retry_limit(
            exc, provider_name=provider, model=model, retries=retries)
        assert got == expected, case_id


@pytest.mark.usefixtures("unpaced_llm")
class TestRouterLiteCooldownRetry:

    @staticmethod
    def _cooldown(monkeypatch, wait: float, scale: str = "minute") -> None:

        async def _fake(model):
            return wait, scale

        monkeypatch.setattr("ai.llm.router.google_pool_cooldown", _fake)

    @staticmethod
    def _both_429_then_ok() -> _ErrorQueueAdapter:
        return _ErrorQueueAdapter({
            "gemini-3.5-flash-lite": [
                LLMRateLimitError("429", requests_sent=0)
            ],
            "gemini-3.1-flash-lite": [
                LLMRateLimitError("429", requests_sent=0)
            ]
        })

    async def _run(self, adapter, *, retries: int = 4):
        return await _router_with(google=adapter).complete(
            LLMTier.CHEAP, _msgs(), provider="google", retries=retries)

    async def _no_recorded_cooldown_waits_one_quota_window(self, monkeypatch):
        _set_google_cheap_chain(monkeypatch)
        self._cooldown(monkeypatch, 0.0)
        recorder = _RecordingSleep()
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", recorder)
        resp = await self._run(self._both_429_then_ok())
        assert resp.model == "gemini-3.5-flash-lite"
        assert len(recorder.delays) == 1
        assert 60.0 <= recorder.delays[0] <= 63.0

    async def _also_without_a_fallback_model(self, monkeypatch):
        _set_google_cheap_chain(monkeypatch)
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_MODEL_CHEAP_FALLBACK", "")
        self._cooldown(monkeypatch, 20.0)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _ErrorQueueAdapter({
            "gemini-3.5-flash-lite": [
                LLMRateLimitError("429", requests_sent=2)
            ]
        })
        resp = await self._run(adapter)
        assert resp.model == "gemini-3.5-flash-lite"
        assert adapter.calls == ["gemini-3.5-flash-lite"] * 2

    async def _a_second_failure_is_raised(self, monkeypatch):
        _set_google_cheap_chain(monkeypatch)
        self._cooldown(monkeypatch, 5.0)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _ErrorQueueAdapter({
            "gemini-3.5-flash-lite": [LLMRateLimitError("429")] * 2,
            "gemini-3.1-flash-lite": [LLMRateLimitError("429")]
        })
        with pytest.raises(LLMRateLimitError):
            await self._run(adapter)
        assert adapter.calls == [
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
            "gemini-3.5-flash-lite"
        ]

    async def _not_made_when_it_cannot_help_or_is_off(self, monkeypatch):
        cases = {
            "day-scale-cooldown": (3600.0, "day", 70.0, 4),
            "wait-above-maximum": (100.0, "minute", 70.0, 4),
            "retry-switched-off": (10.0, "minute", 0.0, 4),
            "no-retries-asked": (10.0, "minute", 70.0, 0)
        }
        for case_id, (wait, scale, max_wait, retries) in cases.items():
            await self._no_retry_case(
                monkeypatch, case_id, wait, scale, max_wait, retries)

    async def _no_retry_case(
            self, monkeypatch, case_id, wait, scale, max_wait, retries):
        _set_google_cheap_chain(monkeypatch)
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS",
            max_wait)
        self._cooldown(monkeypatch, wait, scale)
        recorder = _RecordingSleep()
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", recorder)
        adapter = self._both_429_then_ok()
        with pytest.raises(LLMRateLimitError):
            await self._run(adapter, retries=retries)
        assert adapter.calls == [
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"
        ], case_id
        assert recorder.delays == [], case_id

    async def test_cooldown_retry_rules(self):
        await _run_steps(
            self._no_recorded_cooldown_waits_one_quota_window,
            self._also_without_a_fallback_model,
            self._a_second_failure_is_raised,
            self._not_made_when_it_cannot_help_or_is_off,
            reset=_reset_llm_state)


class _TierRouter:

    def __init__(self, outcomes: dict[LLMTier, Any]) -> None:
        self.outcomes = outcomes
        self.tiers: list[LLMTier] = []

    async def complete(self, tier, messages, **kwargs):
        self.tiers.append(tier)
        outcome = self.outcomes[tier]
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(
            content=outcome, provider="google", model=f"model-{tier.value}",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _FixedPriceMarket:

    async def get_current_price(self, ticker):
        return PriceQuote(
            ticker=ticker, price=Decimal("100.00"), as_of=datetime.now(UTC),
            source="fake")


@pytest.fixture
def sizing_env(monkeypatch):

    async def _holdings(portfolio_id):
        return []

    async def _cash(portfolio_id):
        return 10000.0

    monkeypatch.setattr(
        sizing_module, "get_market_data_provider", lambda: _FixedPriceMarket())
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _holdings)
    monkeypatch.setattr(sizing_module, "get_portfolio_cash_balance", _cash)
    monkeypatch.setattr(sizing_module.settings, "TIER_MIN_NOTIONAL_USD", 100.0)
    monkeypatch.setattr(
        sizing_module.settings, "TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION", 0.01)
    monkeypatch.setattr(
        sizing_module.settings, "MAX_POSITION_WEIGHT_PCT", 100.0)


def _tiers_json(pairs, window=5) -> str:
    return json.dumps({
        "window_days": window,
        "tiers": [{
            "target_price": p,
            "quantity": q
        } for p, q in pairs]
    })


async def _size(router) -> tuple[Any, str | None]:
    return await generate_position_sizing(
        ticker="AMD", action=RecommendationAction.BUY, confidence=0.8,
        reasoning="r", portfolio_id="p", llm_router=router)


def _raw_tiers(tiers: list[tuple[str, str]]) -> str:
    return json.dumps({
        "window_days": 5,
        "tiers": [{
            "target_price": float(p),
            "quantity": float(q)
        } for p, q in tiers]
    })


def _validate(raw: str, budget: str, *, capped: bool = False):
    return _validated_sizing(
        raw, action=RecommendationAction.BUY, ticker="AMZN",
        budget=Decimal(budget), cash=Decimal("7698.95"),
        committed_buy_cash=Decimal("4976.20"), capped_by_weight=capped,
        min_notional=Decimal("100"), position=None)


class TestPositionSizingLiteRetry:

    async def test_lite_retry_after_a_failed_primary(self, sizing_env):
        router = _TierRouter({
            LLMTier.HIGHEST_ACCURACY: LLMRequestTimeoutError(
                "3.5-flash timed out"),
            LLMTier.CHEAP: _tiers_json([(100, 5), (95, 5)])
        })
        result, note = await _size(router)
        assert router.tiers == [LLMTier.HIGHEST_ACCURACY, LLMTier.CHEAP]
        assert result is not None and len(result.tiers) == 2
        assert note is not None
        assert "computed by the fallback model (model-cheap)" in note
        assert "3.5-flash timed out" in note

        router = _TierRouter({
            LLMTier.HIGHEST_ACCURACY: LLMServerOverloadedError("503"),
            LLMTier.CHEAP: _tiers_json([(100, 500)])
        })
        result, note = await _size(router)
        assert result is None
        assert note is not None
        assert "above the budget" in note and "fallback model" in note

        router = _TierRouter({
            LLMTier.HIGHEST_ACCURACY: LLMServerOverloadedError("primary 503"),
            LLMTier.CHEAP: LLMRequestError("lite down")
        })
        result, note = await _size(router)
        assert result is None
        assert router.tiers == [LLMTier.HIGHEST_ACCURACY, LLMTier.CHEAP]
        assert note is not None
        assert note.startswith("No entry/exit tiers were generated")
        assert "primary 503" in note and "lite down" in note


class TestBudgetTrim:

    def test_trims_a_small_excess_but_not_below_the_minimum(self):
        result, note = _validate(
            _raw_tiers([("250.00", "5.5"), ("240.00", "5.62")]),
            "2722.750486376953")
        assert result is not None
        assert [t.quantity
                for t in result.tiers] == [Decimal("5.5"),
                                           Decimal("5.6156")]
        total = sum(t.target_price * t.quantity for t in result.tiers)
        assert total <= Decimal("2722.750486376953")
        assert note is not None
        assert "5.62 to 5.6156" in note and "0.5% tolerance" in note

        result, note = _validate(
            _raw_tiers([("500", "1"), ("100.5", "1")]), "600")
        assert result is None and isinstance(note, BudgetBlockedNote)


class TestReflectionReviewSkip:

    def test_skip_reasons_and_review_not_run_note(self):
        assert review_skip_reason_for(LLMRateLimitError("429 ...")) == (
            "LLMRateLimitError: LLM rate limit / quota exhausted (429)")
        assert review_skip_reason_for(LLMRequestTimeoutError("timed out")) == (
            "LLMRequestTimeoutError: the LLM provider timed out")
        state = _state(
            "AMD", user_id="", reflection_history=[
                ReflectionVerdict(
                    round_number=1, verified=False, issues=[],
                    llm_review_ran=False,
                    llm_review_skip_reason=review_skip_reason_for(
                        LLMServerOverloadedError(_OVERLOADED_TEXT)),
                    usable_agent_count=9, agent_count=9)
            ])
        note = state.confidence_adjustment_note()
        assert note is not None
        assert "0 unresolved" not in note
        assert "e.g." not in note
        assert (
            "Reflection could not fully verify the debate content in its only "
            "review pass: the LLM citation review not running this pass "
            "(LLMServerOverloadedError: LLM provider overloaded (503))"
        ) in note
        assert "shared infrastructure" not in note


def _text_of(content) -> str:
    return content.parts[0].text


class _HangingEmbedModels(_FakeEmbedModels):

    async def embed_content(self, *, model, contents, config):
        self.calls.append({"model": model})
        if len(self.calls) <= self.hangs:
            await asyncio.Event().wait()
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(values=[3.0, 4.0] + [0.0] * (self.dims - 2))
                for _ in contents
            ])


def _hanging(hangs: int) -> _HangingEmbedModels:
    models = _HangingEmbedModels(["ok"], dims=4)
    models.hangs = hangs  # type: ignore[attr-defined]
    return models


class TestGoogleEmbedBatch:

    async def _embedding_2_uses_prompt_prefixes_not_task_type(
            self, monkeypatch):
        models = _FakeEmbedModels(["ok"], dims=4)
        _install_google_pool(monkeypatch, {"primary": models})
        await embedding_module.google_embed_batch(["q"], "query",
                                                  model="gemini-embedding-2",
                                                  dimensions=4)
        await embedding_module.google_embed_batch(["d"], "document",
                                                  model="gemini-embedding-2",
                                                  dimensions=4)
        assert models.calls[0]["config"].task_type is None
        assert _text_of(models.calls[0]["contents"][0]) == (
            "task: search result | query: q")
        assert _text_of(
            models.calls[1]["contents"][0]) == ("title: none | text: d")

    async def _429_on_every_project_waits_then_gives_up(self, monkeypatch):
        models = _FakeEmbedModels([_quota_client_error()], dims=4)
        sleep = _install_google_pool(monkeypatch, {"primary": models})
        with pytest.raises(embedding_module.EmbeddingError):
            await embedding_module.google_embed_batch(
                ["a"], "document", model="gemini-embedding-001", dimensions=4)
        assert sleep.delays == list(
            embedding_module._GOOGLE_RETRY_WAITS_SECONDS)

    async def _503_waits_and_retries(self, monkeypatch):
        models = _FakeEmbedModels([_server_503(), "ok"], dims=4)
        sleep = _install_google_pool(monkeypatch, {"primary": models})
        vectors = await embedding_module.google_embed_batch(
            ["a"], "document", model="gemini-embedding-001", dimensions=4)
        assert len(vectors) == 1
        assert sleep.delays[0] == (
            embedding_module._GOOGLE_RETRY_WAITS_SECONDS[0])

    async def _wrong_dimension_is_an_error(self, monkeypatch):
        models = _FakeEmbedModels(["ok"], dims=4)
        _install_google_pool(monkeypatch, {"primary": models})
        with pytest.raises(embedding_module.EmbeddingError):
            await embedding_module.google_embed_batch(
                ["a"], "document", model="gemini-embedding-001", dimensions=8)

    async def _an_embedding_that_never_answers_ends_as_a_timeout(
            self, monkeypatch):
        from ai.workflows.rag_ingestion_workflow import \
            is_transient_ingestion_error

        monkeypatch.setattr(
            config.settings, "EMBEDDING_REQUEST_TIMEOUT_SECONDS", 0.02)
        sleep = _install_google_pool(monkeypatch, {"primary": _hanging(99)})
        with pytest.raises(TimeoutError):
            await embedding_module.google_embed_batch(
                ["a"], "document", model="gemini-embedding-001", dimensions=4)
        assert sleep.delays == list(
            embedding_module._GOOGLE_RETRY_WAITS_SECONDS)
        assert is_transient_ingestion_error(TimeoutError("x"))

    async def _embed_texts_and_query_dispatch_to_google(self, monkeypatch):
        models = _FakeEmbedModels(["ok"], dims=4)
        _install_google_pool(monkeypatch, {"primary": models})
        _set_settings(
            monkeypatch, {
                "EMBEDDING_PROVIDER": "google",
                "GOOGLE_EMBEDDING_MODEL": "gemini-embedding-001",
                "GOOGLE_EMBEDDING_DIMENSIONS": 4
            })
        docs = await embedding_module.embed_texts(["a", "b"])
        query = await embedding_module.embed_query("q")
        assert len(docs) == 2 and len(query) == 4
        assert models.calls[0]["config"].task_type == "RETRIEVAL_DOCUMENT"
        assert models.calls[1]["config"].task_type == "RETRIEVAL_QUERY"
        assert embedding_module.embedding_model_name() == (
            "gemini-embedding-001")

    def test_batches_respect_the_count_and_token_caps(self):
        batches = embedding_module.google_batches(["short text"] * 45)
        assert [len(b) for b in batches] == [20, 20, 5]
        filing_chunk = " ".join(["word"] * 700)
        batches = embedding_module.google_batches([filing_chunk] * 30)
        assert all(
            sum(embedding_module.estimated_tokens(t)
                for t in b) <= embedding_module._GOOGLE_BATCH_TOKEN_BUDGET
            for b in batches)
        assert sum(len(b) for b in batches) == 30

    async def test_requests_and_dispatch(self):
        await _run_steps(
            self._embedding_2_uses_prompt_prefixes_not_task_type,
            self._embed_texts_and_query_dispatch_to_google,
            reset=_reset_llm_state)

    async def test_errors_waits_and_timeouts(self):
        await _run_steps(
            self._429_on_every_project_waits_then_gives_up,
            self._503_waits_and_retries, self._wrong_dimension_is_an_error,
            self._an_embedding_that_never_answers_ends_as_a_timeout,
            reset=_reset_llm_state)


class _FakeMistralHttpResponse:

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""


class _FakeMistralItem:

    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _FakeMistralEmbeddings:

    def __init__(self, data: list[_FakeMistralItem]) -> None:
        self.data = data


def _fake_mistral_client(*, fails_first_n_calls: int = 0):
    calls = {"count": 0}

    async def create_async(model, inputs):
        calls["count"] += 1
        if calls["count"] <= fails_first_n_calls:
            raise SDKError("rate limited", _FakeMistralHttpResponse(429))
        return _FakeMistralEmbeddings([
            _FakeMistralItem(i, [0.1, 0.2, 0.3, 0.4])
            for i in range(len(inputs))
        ])

    client = type("FakeClient", (), {})()
    client.embeddings = type(
        "E", (), {"create_async": staticmethod(create_async)})()
    return client, calls


@pytest.fixture
def fake_mistral_pool(monkeypatch):
    clients: dict[str, object] = {}
    call_sequence: list[str] = []
    marked: list[str] = []
    state = {"i": 0}

    def register(label: str, client) -> None:
        clients[label] = client

    def next_client_excluding_pooled(excluded: set[str]):
        candidates = [lbl for lbl in clients if lbl not in excluded]
        if not candidates:
            return None
        not_cooling_down = [lbl for lbl in candidates if lbl not in marked]
        pool = not_cooling_down if not_cooling_down else candidates
        label = pool[state["i"] % len(pool)]
        state["i"] += 1
        call_sequence.append(label)
        return label, clients[label]

    def mark_pooled_client_rate_limited(label: str) -> None:
        marked.append(label)

    monkeypatch.setattr(
        embedding_module, "next_client_excluding_pooled",
        next_client_excluding_pooled)
    monkeypatch.setattr(
        embedding_module, "mark_pooled_client_rate_limited",
        mark_pooled_client_rate_limited)
    return type(
        "FakePool", (), {
            "register": staticmethod(register),
            "call_sequence": call_sequence,
            "marked": marked
        })


class TestMistralEmbedding:

    @pytest.fixture(autouse=True)
    def _mistral_provider(self, monkeypatch):
        monkeypatch.setattr(
            embedding_module.settings, "EMBEDDING_PROVIDER", "mistral")

    async def test_a_429_on_one_key_moves_to_the_next_pooled_key(
            self, fake_mistral_pool):
        primary, _ = _fake_mistral_client(fails_first_n_calls=1)
        key_2, _ = _fake_mistral_client(fails_first_n_calls=0)
        fake_mistral_pool.register("primary", primary)
        fake_mistral_pool.register("key_2", key_2)
        vectors = await embedding_module.embed_texts(["hello world"])
        assert fake_mistral_pool.call_sequence == ["primary", "key_2"]
        assert fake_mistral_pool.marked == ["primary"]
        assert len(vectors) == 1


class TestEmbeddingConfiguration:

    def test_valid_and_invalid_configurations(self, monkeypatch):
        monkeypatch.setattr(config.settings, "EMBEDDING_PROVIDER", "mistral")
        monkeypatch.setattr(config.settings, "QDRANT_VECTOR_SIZE", 1024)
        embedding_module.check_embedding_configuration()
        assert embedding_module.embedding_model_name() == "mistral-embed"

        cases = {
            "google-vector-size-mismatch": ({
                "EMBEDDING_PROVIDER": "google",
                "GOOGLE_EMBEDDING_DIMENSIONS": 1536,
                "QDRANT_VECTOR_SIZE": 1024
            }, "QDRANT_VECTOR_SIZE"),
            "unknown-provider": ({
                "EMBEDDING_PROVIDER": "openai"
            }, "EMBEDDING_PROVIDER")
        }
        for case_id, (overrides, match) in cases.items():
            _set_settings(monkeypatch, overrides)
            with pytest.raises(embedding_module.EmbeddingError) as exc_info:
                embedding_module.check_embedding_configuration()
            assert match in str(exc_info.value), case_id


class _BM25:

    def __init__(self, hits: list[tuple[str, float]]) -> None:
        self.hits = hits

    def search(self, query: str, top_k: int, **scope):
        return self.hits


class _Qdrant:

    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self.payloads = payloads
        self.query_points_calls = 0

    async def query_points(self, **kwargs):
        self.query_points_calls += 1
        return SimpleNamespace(points=[])

    async def retrieve(self, *, collection_name, ids, with_payload):
        return [
            SimpleNamespace(id=i, payload=self.payloads[i]) for i in ids
            if i in self.payloads
        ]


@pytest.fixture
def failing_embedding_backends(monkeypatch):
    chunk_id = str(uuid4())
    bm25 = _BM25([(chunk_id, 3.0)])
    qdrant = _Qdrant({
        chunk_id: {
            "text": "AMD revenue",
            "category": "filing",
            "ticker": "AMD",
            "document_id": "d1"
        }
    })
    monkeypatch.setattr(retriever_module, "get_bm25_index", lambda: bm25)
    monkeypatch.setattr(retriever_module, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(retriever_module, "_CATEGORY_SOURCE_WEIGHTS", {})

    async def failing_embed_query(query: str):
        raise embedding_module.EmbeddingError("backend_out_of_capacity (3505)")

    async def no_expansion(query: str, **kwargs):
        return [query]

    monkeypatch.setattr(retriever_module, "embed_query", failing_embed_query)
    monkeypatch.setattr(retriever_module, "expand_query", no_expansion)
    return chunk_id, qdrant


class TestRetrieverBm25Fallback:

    async def test_default_sources_degrade_and_pinned_ones_raise(
            self, failing_embedding_backends):
        chunk_id, qdrant = failing_embedding_backends
        results = await retriever_module.BaseRetriever().retrieve(
            "AMD revenue")
        assert [r.chunk_id for r in results] == [chunk_id]
        assert qdrant.query_points_calls == 0
        with pytest.raises(embedding_module.EmbeddingError):
            await retriever_module.BaseRetriever().retrieve(
                "AMD revenue", sources=frozenset({"bm25", "vector"}))


class _CollectionClient:

    def __init__(self, existing_size: int | None) -> None:
        self.existing_size = existing_size
        self.created: list[int] = []

    async def collection_exists(self, *, collection_name):
        return self.existing_size is not None

    async def create_collection(self, *, collection_name, vectors_config):
        self.created.append(vectors_config.size)

    async def get_collection(self, *, collection_name):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=self.existing_size))))


class TestQdrantClientAndCollections:

    async def test_client_url_and_collection_checks(self, monkeypatch):
        import qdrant

        seen: dict[str, Any] = {}

        class _FakeClient:

            def __init__(
                    self, *, url: str, api_key: str | None = None) -> None:
                seen["url"] = url

        monkeypatch.setattr(qdrant, "AsyncQdrantClient", _FakeClient)
        monkeypatch.setattr(qdrant, "_client", None)
        monkeypatch.setattr(
            config.settings, "QDRANT_URL", "http://elsewhere:6333")
        qdrant.get_qdrant_client()
        assert seen["url"] == "http://elsewhere:6333"

        monkeypatch.setattr(
            qdrant, "get_qdrant_client", lambda: _CollectionClient(1024))
        with pytest.raises(RuntimeError, match="1024-dim"):
            await qdrant.ensure_collection("research_documents", 1536)

        cases = {
            "matching-collection-is-kept": (1536, []),
            "missing-collection-is-created": (None, [1536])
        }
        for case_id, (existing_size, created) in cases.items():
            client = _CollectionClient(existing_size)
            monkeypatch.setattr(qdrant, "get_qdrant_client", lambda: client)
            await qdrant.ensure_collection("research_documents_gemini", 1536)
            assert client.created == created, case_id


class _ReembedQdrant:

    def __init__(self, collections: dict[str, dict[str, Any]]) -> None:
        self.collections = collections

    async def collection_exists(self, *, collection_name):
        return collection_name in self.collections

    async def get_collection(self, *, collection_name):
        size = self.collections[collection_name]["size"]
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=size))))

    async def scroll(
            self, *, collection_name, limit, offset, with_payload,
            with_vectors):
        items = sorted(self.collections[collection_name]["points"].items())
        start = offset or 0
        page = items[start:start + limit]
        nxt = start + limit if start + limit < len(items) else None
        return [SimpleNamespace(id=i, payload=p) for i, p in page], nxt

    async def create_collection(self, *, collection_name, vectors_config):
        self.collections[collection_name] = {
            "size": vectors_config.size,
            "points": {}
        }

    async def upsert(self, *, collection_name, points):
        for p in points:
            assert len(p.vector) == self.collections[collection_name]["size"]
            self.collections[collection_name]["points"][str(p.id)] = p.payload


@pytest.fixture
def reembed_env(monkeypatch):
    from scripts import reembed_collection

    ids = [str(UUID(int=i)) for i in range(1, 4)]
    fake = _ReembedQdrant({
        "research_documents": {
            "size": 1024,
            "points": {
                ids[0]: {
                    "text": "one"
                },
                ids[1]: {
                    "text": "two"
                },
                ids[2]: {
                    "text": ""
                }
            }
        }
    })
    monkeypatch.setattr(reembed_collection, "get_qdrant_client", lambda: fake)
    embedded: list[str] = []

    async def fake_embed(batch, purpose, *, model, dimensions):
        assert purpose == "document"
        embedded.extend(batch)
        return [[0.5] * dimensions for _ in batch]

    monkeypatch.setattr(reembed_collection, "google_embed_batch", fake_embed)
    return reembed_collection, fake, ids, embedded


class TestReembedCollection:

    async def test_preview_and_unusable_targets_write_nothing(
            self, reembed_env):
        module, fake, _ids, embedded = reembed_env
        written = await module.run(
            source="research_documents", target="rd_gemini",
            model="gemini-embedding-001", dims=8, confirm=False)
        assert written == 0 and embedded == []
        assert "rd_gemini" not in fake.collections
        await self._refuses_an_unusable_target(module, fake, embedded)

    async def test_second_run_copies_only_the_gap(self, reembed_env):
        module, fake, _ids, embedded = reembed_env
        await module.run(
            source="research_documents", target="rd_gemini",
            model="gemini-embedding-001", dims=8, confirm=True)
        new_id = str(UUID(int=9))
        fake.collections["research_documents"]["points"][new_id] = {
            "text": "late"
        }
        embedded.clear()
        written = await module.run(
            source="research_documents", target="rd_gemini",
            model="gemini-embedding-001", dims=8, confirm=True)
        assert written == 1 and embedded == ["late"]

    async def _refuses_an_unusable_target(self, module, fake, embedded):
        cases = {
            "target-of-another-size": ({
                "rd_gemini": {
                    "size": 768,
                    "points": {}
                }
            }, "rd_gemini"),
            "source-equal-to-target": ({}, "research_documents")
        }
        for case_id, (existing_target, target) in cases.items():
            fake.collections.update(existing_target)
            written = await module.run(
                source="research_documents", target=target,
                model="gemini-embedding-001", dims=8, confirm=True)
            assert written == 0 and embedded == [], case_id


class TestIngestionStatusEvent:

    async def test_retries_ignore_live_update_failure_then_raise(
            self, monkeypatch):
        import ai.workflows.rag_ingestion_workflow as workflow

        attempts: list[int] = []
        waits: list[float] = []

        async def _flaky(settings, event_name, payload, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("redis down")

        async def _live_down(*args, **kwargs):
            raise ConnectionError("redis down")

        async def _sleep(seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(workflow, "publish_durable_event", _flaky)
        monkeypatch.setattr(workflow, "publish_cross_service", _live_down)
        monkeypatch.setattr(
            "ai.workflows.rag_ingestion_workflow.asyncio.sleep", _sleep)
        await workflow._publish_status("doc-1", workflow.DocumentStatus.READY)
        assert len(attempts) == 3
        assert waits == list(workflow._STATUS_PUBLISH_RETRY_WAITS_SECONDS[:2])

        async def _down(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(workflow, "publish_durable_event", _down)
        monkeypatch.setattr(
            "ai.workflows.rag_ingestion_workflow.asyncio.sleep", _no_sleep)
        with pytest.raises(ConnectionError):
            await workflow._publish_status(
                "doc-1", workflow.DocumentStatus.FAILED)


def _metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _failed_analyses(reason: str) -> float:
    return _metric(
        "alphamind_analyses_total", status="failed", failure_reason=reason)


@pytest_asyncio.fixture
async def committed_analysis(_schema_ready):
    from database import SessionLocal
    from domains.analysis.model import Analysis

    created: list[UUID] = []

    async def _make(status: str = "running") -> UUID:
        async with SessionLocal() as session:
            row = Analysis(
                user_id=None, portfolio_id=None, ticker="QQQ", status=status)
            session.add(row)
            await session.commit()
            created.append(row.id)
            return row.id

    yield _make

    async with SessionLocal() as session:
        for analysis_id in created:
            row = await session.get(Analysis, analysis_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


class TestMetrics:

    def test_reimport_does_not_register_twice(self) -> None:
        before = observability.ANALYSES_TOTAL
        reloaded = importlib.reload(observability)
        assert reloaded.ANALYSES_TOTAL is before
        assert reloaded.LLM_ERRORS_TOTAL is observability.LLM_ERRORS_TOTAL

    async def test_mark_analysis_failed_counts_only_the_real_transition(
            self, committed_analysis) -> None:
        from ai.workflows.analysis_workflow import mark_analysis_failed

        analysis_id = await committed_analysis("running")
        before = _failed_analyses("timeout")
        assert await mark_analysis_failed(analysis_id, "timeout") is True
        assert await mark_analysis_failed(analysis_id, "timeout") is False
        assert _failed_analyses("timeout") == before + 1

    async def test_queue_gauges_follow_the_queue(self) -> None:
        from domains.analysis.queue import (AnalysisJob, AnalysisQueue,
                                            analysis_queue)

        release = asyncio.Event()
        started = asyncio.Event()

        async def _runner(job: AnalysisJob) -> None:
            started.set()
            await release.wait()

        async def _mark_failed(analysis_id: UUID, reason: str) -> bool:
            return False

        queue = AnalysisQueue(
            runner=_runner, mark_failed=_mark_failed, timeout_seconds=30)
        observability.bind_analysis_queue(queue)
        try:
            assert _metric("alphamind_analysis_queue_pending") == 0
            assert _metric("alphamind_analysis_queue_running") == 0
            for _ in range(3):
                job = AnalysisJob(
                    analysis_id=uuid4(), analysis_type=AnalysisType.TECHNICAL)
                queue.enqueue(job)
            await asyncio.wait_for(started.wait(), timeout=5)
            assert _metric("alphamind_analysis_queue_running") == 1
            assert _metric("alphamind_analysis_queue_pending") == 2
            release.set()
            await asyncio.wait_for(queue.join(), timeout=5)
            assert _metric("alphamind_analysis_queue_running") == 0
            assert _metric("alphamind_analysis_queue_pending") == 0
        finally:
            await queue.stop()
            observability.bind_analysis_queue(analysis_queue)


class TestServiceSetup:

    def test_logging_and_warning_filters(self):
        from shared.logging import configure_logging

        names = ("httpx", "httpcore")
        old_levels = {n: logging.getLogger(n).level for n in names}
        old_structlog = structlog.get_config()
        try:
            for n in names:
                logging.getLogger(n).setLevel(logging.NOTSET)
            configure_logging(
                SimpleNamespace(  # type: ignore[arg-type]
                    LOG_LEVEL="INFO", LOG_JSON=True, SERVICE_NAME="test"
                )
            )
            for n in names:
                assert logging.getLogger(n).getEffectiveLevel() == (
                    logging.WARNING)
            assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        finally:
            structlog.configure(**old_structlog)
            for n, level in old_levels.items():
                logging.getLogger(n).setLevel(level)

        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / "pyproject.toml").read_text())
        filters = data["tool"]["pytest"]["ini_options"]["filterwarnings"]
        assert any("Failed to obtain server version" in f for f in filters)
        assert "ignore::UserWarning" not in filters
