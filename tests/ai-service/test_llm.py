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
import ai.llm.anthropic_provider as anthropic_module
import ai.llm.google_provider as google_module
import ai.llm.router as router_module
import ai.rag.embedding as embedding_module
import ai.rag.retriever as retriever_module
import ai.recommendation.position_sizing as sizing_module
import ai.tools.portfolio_tool as portfolio_tool_module
import anthropic
import config
import httpx
import observability
import pytest
import pytest_asyncio
import structlog
from ai.agents import debate_common as dc
from ai.agents.judge import JudgeAgent
from ai.agents.reflection import review_skip_reason_for
from ai.graph.state import ReflectionVerdict
from ai.llm.anthropic_provider import (_split_system_and_turns,
                                       anthropic_output_schema,
                                       reduced_output_schema)
from ai.llm.base import (ConcurrencyGate, LLMConfigError,
                         LLMCreditExhaustedError, LLMError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMNonEnglishOutputError, LLMOutputExhaustedError,
                         LLMProviderAdapter,
                         LLMProviderTimeoutError, LLMRateLimitError,
                         LLMRequestError, LLMRequestTimeoutError, LLMResponse,
                         LLMServerOverloadedError, LLMTier, LLMUsage,
                         RequestPacer, released_while_waiting, request_pacer)
from ai.llm.router import (LLMRouter, fallback_call_count,
                           recording_served_models, router_retry_limit)
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
from pydantic import ValidationError
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
    anthropic_module._schema_levels.clear()
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
        monkeypatch.setattr(
            config.settings, "LLM_DEFAULT_PROVIDER", "not_a_provider")
        router = _router_with(anthropic=_FakeAdapter("anthropic"))
        with pytest.raises(LLMConfigError):
            await router.complete(LLMTier.CHEAP, _msgs())

    async def _stream_yields_chunks_from_resolved_adapter(self, monkeypatch):
        monkeypatch.setattr(config.settings, "LLM_MISTRAL_MODEL_CHEAP", "m")
        router = _router_with(mistral=_FakeAdapter("mistral"))
        chunks = [
            chunk async for chunk in router.stream(LLMTier.CHEAP, _msgs())
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
        await router.complete(LLMTier.CHEAP, _msgs())
        await router.complete(LLMTier.CHEAP, _msgs())
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
            LLMTier.HIGHEST_ACCURACY, _msgs(), retries=1)
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
                    tier, _msgs(), retries=1)
            assert adapter.calls == 2, case_id

    async def test_router_basics(self):
        await _run_steps(
            self._unknown_provider_raises_config_error,
            self._stream_yields_chunks_from_resolved_adapter,
            self._concurrent_cheap_calls_get_paced_when_interval_set,
            self._rate_limit_falls_back_to_the_alternate_model,
            self._failure_without_a_fallback_is_raised,
            setup=lambda mp: mp.setattr(
                config.settings, "LLM_DEFAULT_PROVIDER", "mistral"),
            reset=_reset_llm_state)


async def test_anthropic_message_order_and_caller_timeouts(monkeypatch):
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

    import ai.llm.openai_provider as openai_module
    from openai import NOT_GIVEN as OPENAI_NOT_GIVEN

    openai_create = _RecordingCreate(
        SimpleNamespace(
            model="gpt", usage=None, choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok"))
            ]))
    anthropic_create = _RecordingCreate(_anthropic_message("ok"))
    monkeypatch.setattr(
        openai_module, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=openai_create)))
    monkeypatch.setattr(
        anthropic_module, "_client",
        SimpleNamespace(messages=anthropic_create))
    for adapter, create, default_timeout in (
            (openai_module.OpenAIAdapter(), openai_create, OPENAI_NOT_GIVEN),
            (anthropic_module.AnthropicAdapter(), anthropic_create,
             config.settings.LLM_ANTHROPIC_REQUEST_TIMEOUT_SECONDS)):
        await adapter.complete(_msgs(), model="m")
        await adapter.complete(_msgs(), model="m", timeout_seconds=300)
        assert [k["timeout"] for k in create.kwargs] == [default_timeout, 300]


async def test_openai_errors_map_like_the_anthropic_adapter(monkeypatch):
    import ai.llm.openai_provider as openai_module
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat")

    def status(
            cls: type[openai.APIStatusError], code: int,
            body: dict[str, str] | None = None) -> Exception:
        return cls(
            f"Error code: {code}", response=httpx.Response(
                code, request=request), body=body)

    class _Raising:

        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def create(self, **kwargs: Any) -> Any:
            raise self.exc

    cases = [
        (status(openai.RateLimitError, 429), LLMRateLimitError),
        (status(openai.RateLimitError, 429, {"code": "insufficient_quota"}),
         LLMCreditExhaustedError),
        (status(openai.InternalServerError, 503), LLMServerOverloadedError),
        (openai.APITimeoutError(request=request), LLMProviderTimeoutError),
        (status(openai.BadRequestError, 400), LLMInvalidRequestError),
        (status(openai.AuthenticationError, 401), LLMInvalidRequestError),
        (RuntimeError("boom"), LLMRequestError),
    ]
    for exc, expected in cases:
        monkeypatch.setattr(
            openai_module, "_client",
            SimpleNamespace(chat=SimpleNamespace(completions=_Raising(exc))))
        with pytest.raises(LLMRequestError) as caught:
            await openai_module.OpenAIAdapter().complete(_msgs(), model="m")
        assert type(caught.value) is expected, type(exc).__name__
        assert caught.value.__cause__ is exc


def _anthropic_message(
        text: str, *, thinking_tokens: int | None = None,
        stop_reason: str = "end_turn") -> SimpleNamespace:
    details = (
        None if thinking_tokens is None else
        SimpleNamespace(thinking_tokens=thinking_tokens))
    return SimpleNamespace(
        model="claude-haiku-5-5", stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=40,
            output_tokens_details=details, cache_read_input_tokens=7,
            cache_creation_input_tokens=None),
        content=[
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text=text)
        ])


def _anthropic_status_error(
        error_cls: type[anthropic.APIStatusError], status: int,
        error_type: str, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return error_cls(
        f"Error code: {status} - {body}", response=response, body=body)


def _anthropic_timeout() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com"))


class _AnthropicCreate:

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _AnthropicSequence:

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.kwargs: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _AnthropicStream:

    def __init__(
            self, chunks: list[str], final: Any,
            fail_after: Exception | None = None) -> None:
        self.chunks = chunks
        self.final = final
        self.fail_after = fail_after

    async def texts(self) -> Any:
        for chunk in self.chunks:
            yield chunk
        if self.fail_after is not None:
            raise self.fail_after

    async def get_final_message(self) -> Any:
        return self.final


class _AnthropicStreamMessages:

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _StreamContext(self.outcome)


class _StreamContext:

    def __init__(self, stream: _AnthropicStream) -> None:
        self.stream = stream

    async def __aenter__(self) -> SimpleNamespace:
        return SimpleNamespace(
            text_stream=self.stream.texts(),
            get_final_message=self.stream.get_final_message)

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _too_complex() -> anthropic.APIStatusError:
    return _anthropic_status_error(
        anthropic.BadRequestError, 400, "invalid_request_error",
        "Schema is too complex.")


_TOOL_UNION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string"
        },
        "action_input": {
            "anyOf": [{
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string"
                    }
                },
                "required": ["ticker"]
            }, {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string"
                    },
                    "ticker": {
                        "type": "integer"
                    }
                }
            }, {
                "type": "null"
            }]
        },
        "reported_metrics": {
            "anyOf": [{
                "type": "object",
                "properties": {
                    "x": {
                        "type": "number"
                    }
                },
                "required": ["x"]
            }, {
                "type": "null"
            }]
        }
    },
    "required": ["thought"]
}


def _install_anthropic(monkeypatch, messages: Any) -> None:
    monkeypatch.setattr(
        anthropic_module, "_client", SimpleNamespace(messages=messages))


_SEGMENTS = ("Task: t", "\n\nScratchpad so far:\nThought: a",
             "\nThought: b", "\nThought: c", "\nThought: d")


def _hinted_messages(count: int) -> list[LLMMessage]:
    return [
        LLMMessage(role="system", content="Be brief."),
        LLMMessage(
            role="user", content="".join(_SEGMENTS[:count]),
            cache_segments=_SEGMENTS[:count])
    ]


_EVIDENCE = "- market: price 100\n- fundamental: revenue up 5%"


def _evidence_messages() -> list[LLMMessage]:
    return [
        LLMMessage(role="system", content="You are the Bull."),
        LLMMessage(
            role="user", content=(
                f"Ticker: X\n\nResearch agent evidence:\n{_EVIDENCE}\n\n"
                "Bull argument: a"), shared_evidence=_EVIDENCE)
    ]


def _judge_follow_up() -> list[LLMMessage]:
    return [
        LLMMessage(role="system", content="You are the Judge."),
        LLMMessage(
            role="user", content=(
                "Ticker: X\nRound: 1\n\nResearch agent evidence:\n"
                f"{_EVIDENCE}\n\nBull: a"), shared_evidence=_EVIDENCE),
        LLMMessage(role="assistant", content="{bad"),
        LLMMessage(role="user", content="Return valid JSON.")
    ]


def _breakpoints(kwargs: dict[str, Any]) -> int:
    return json.dumps([
        kwargs["system"] if kwargs["system"] is not anthropic.omit else None,
        kwargs["messages"]
    ]).count('"cache_control"')


class _PrefixCacheClient:

    def __init__(self) -> None:
        self.written: set[tuple[str, ...]] = set()
        self.messages = self

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        system = kwargs["system"]
        blocks = [*system, *kwargs["messages"][-1]["content"]]
        texts = tuple(b["text"] for b in blocks)
        marks = [i for i, b in enumerate(blocks) if "cache_control" in b]
        read = 0
        for end in range(marks[-1] + 1, max(0, marks[-1] - 19), -1):
            if texts[:end] in self.written:
                read = len("".join(texts[:end]))
                break
        cached = len("".join(texts[:marks[-1] + 1]))
        self.written.update(texts[:i + 1] for i in marks)
        message = _anthropic_message("{}")
        message.usage = SimpleNamespace(
            input_tokens=len("".join(texts)) - cached, output_tokens=2,
            output_tokens_details=None, cache_read_input_tokens=read,
            cache_creation_input_tokens=cached - read)
        return message

    async def count_tokens(self, **kwargs: Any) -> SimpleNamespace:
        content = kwargs["messages"][0]["content"]
        text = content if isinstance(content, str) else "".join(
            b["text"] for b in content)
        return SimpleNamespace(input_tokens=len(text))


@pytest.mark.asyncio
async def test_cache_probe_sees_the_previous_turn_read(capsys):
    probe = importlib.import_module("scripts.probe_anthropic_cache")
    client = _PrefixCacheClient()
    spent = await probe._probe_model(client, "claude-haiku-5-5", "n1")
    await probe._same_tokens_as_one_string(client, "claude-haiku-5-5", "n1")
    out = capsys.readouterr().out
    assert out.count(" OK: ") == 2 * probe._TURNS, out
    assert "UNEXPECTED" not in out and "(same)" in out
    assert spent > 0
    messages, schema = probe.probe_request("n1", probe._TURNS)
    assert len(messages[1].cache_segments) == probe._TURNS
    assert schema["type"] == "object"


@pytest.mark.asyncio
async def test_cache_probe_stops_when_the_scratchpad_is_not_cached(
        monkeypatch, capsys):
    probe = importlib.import_module("scripts.probe_anthropic_cache")
    monkeypatch.setattr("sys.argv", ["probe_anthropic_cache.py"])
    for switch in ("LLM_ANTHROPIC_PROMPT_CACHING",
                   "LLM_ANTHROPIC_CACHE_SCRATCHPAD"):
        with monkeypatch.context() as mp:
            mp.setattr(config.settings, switch, False)
            with pytest.raises(SystemExit) as exit_info:
                await probe.main()
        assert exit_info.value.code == 1
        assert f"{switch} is false" in capsys.readouterr().err


def test_cache_segments_are_only_an_anthropic_hint():
    from ai.llm.google_provider import _split_system_and_contents
    from ai.llm.mistral_provider import _to_mistral_messages
    from ai.llm.openai_provider import _to_openai_messages

    for hinted in (_hinted_messages(3), _evidence_messages(),
                   _judge_follow_up()):
        plain = [LLMMessage(role=m.role, content=m.content) for m in hinted]
        for convert in (_split_system_and_contents, _to_mistral_messages,
                        _to_openai_messages):
            assert convert(hinted) == convert(plain)
    with pytest.raises(ValidationError):
        LLMMessage(role="user", content="ab", cache_segments=("a", "c"))
    with pytest.raises(ValidationError):
        LLMMessage(role="user", content="ab", shared_evidence="c")
    with pytest.raises(ValidationError):
        LLMMessage(
            role="user", content="ab", cache_segments=("a", "b"),
            shared_evidence="a")


_REACT_LIKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000
        },
        "score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "multipleOf": 0.01
        },
        "email": {
            "type": "string",
            "format": "email"
        },
        "code": {
            "type": "string",
            "format": "isin",
            "pattern": "^(?=[A-Z])"
        },
        "tags": {
            "type": "array",
            "minItems": 3,
            "maxItems": 9,
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string"
                    }
                }
            }
        },
        "nonempty": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string"
            }
        },
        "metrics": {
            "anyOf": [{
                "type": "object"
            }, {
                "type": "null"
            }]
        }
    },
    "required": ["thought"]
}


@pytest.mark.usefixtures("unpaced_llm")
class TestAnthropicAdapter:

    async def _success_sends_schema_and_logs_usage(self, monkeypatch):
        create = _AnthropicCreate(
            _anthropic_message('{"a": 1}', thinking_tokens=10))
        _install_anthropic(monkeypatch, create)
        messages = [
            LLMMessage(role="system", content="Be brief."),
            LLMMessage(role="user", content="hi")
        ]
        with capture_logs() as logs:
            resp = await anthropic_module.AnthropicAdapter().complete(
                messages, model="claude-haiku-5-5", temperature=0.0,
                response_format="json_object",
                response_schema=_REACT_LIKE_SCHEMA)
        assert resp.content == '{"a": 1}'
        assert resp.provider == "anthropic"
        assert resp.usage == LLMUsage(
            prompt_tokens=107, completion_tokens=30, thinking_tokens=10)
        assert resp.finish_reason == "end_turn"
        sent = create.kwargs[0]
        assert sent["system"] == [{
            "type": "text",
            "text": "Be brief.",
            "cache_control": {
                "type": "ephemeral"
            }
        }]
        assert sent["temperature"] is anthropic.omit
        assert sent["thinking"] == {"type": "adaptive"}
        assert sent["max_tokens"] == 1024 + 2048
        assert sent["output_config"]["effort"] == "low"
        output_format = sent["output_config"]["format"]
        assert output_format["type"] == "json_schema"
        assert output_format["schema"]["additionalProperties"] is False
        success = [e for e in logs
                   if e["event"] == "anthropic.completion_succeeded"]
        assert len(success) == 1
        assert {
            k: success[0][k]
            for k in ("model", "prompt_tokens", "completion_tokens",
                      "thinking_tokens", "finish_reason", "cache_read_tokens",
                      "cache_creation_tokens", "uncached_prompt_tokens",
                      "schema_sent")
        } == {
            "model": "claude-haiku-5-5",
            "prompt_tokens": 107,
            "cache_creation_tokens": 0,
            "uncached_prompt_tokens": 100,
            "schema_sent": True,
            "completion_tokens": 30,
            "thinking_tokens": 10,
            "finish_reason": "end_turn",
            "cache_read_tokens": 7
        }
        assert isinstance(success[0]["latency_ms"], int)

    async def _plain_call_sends_no_format_or_temperature(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("plain"))
        _install_anthropic(monkeypatch, create)
        resp = await anthropic_module.AnthropicAdapter().complete(
            _msgs(), model="m", response_schema=_REACT_LIKE_SCHEMA)
        sent = create.kwargs[0]
        assert sent["system"] is anthropic.omit
        assert sent["output_config"] == {"effort": "low"}
        assert resp.usage.thinking_tokens is None
        assert resp.usage.completion_tokens == 40
        _set_settings(
            monkeypatch, {
                "LLM_ANTHROPIC_EFFORT": "",
                "LLM_ANTHROPIC_SEND_TEMPERATURE": True
            })
        await anthropic_module.AnthropicAdapter().complete(
            _msgs(), model="m", temperature=0.3, response_format="json_object")
        assert create.kwargs[1]["output_config"] is anthropic.omit
        assert create.kwargs[1]["temperature"] == 0.3

    async def _schema_conversion(self, monkeypatch):
        converted = anthropic_output_schema(_REACT_LIKE_SCHEMA)
        assert converted is not None
        props = converted["properties"]
        assert props["thought"] == {"type": "string"}
        assert props["score"] == {"type": "number"}
        assert props["email"] == {"type": "string", "format": "email"}
        assert props["code"] == {"type": "string"}
        assert props["tags"] == {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string"
                    }
                },
                "additionalProperties": False
            }
        }
        assert props["nonempty"]["minItems"] == 1
        assert props["metrics"]["anyOf"][0] == {
            "type": "object",
            "additionalProperties": False
        }
        assert converted["additionalProperties"] is False
        assert converted["required"] == ["thought"]
        assert "minLength" in _REACT_LIKE_SCHEMA["properties"]["thought"]

        recursive = {
            "$defs": {
                "node": {
                    "type": "object",
                    "properties": {
                        "child": {
                            "$ref": "#/$defs/node"
                        }
                    }
                }
            },
            "$ref": "#/$defs/node"
        }
        assert anthropic_output_schema(recursive) is None
        too_many_optional = {
            "type": "object",
            "properties": {f"p{i}": {"type": "string"} for i in range(25)}
        }
        assert anthropic_output_schema(too_many_optional) is None
        too_many_unions = {
            "type": "object",
            "properties": {
                f"p{i}": {"type": ["string", "null"]}
                for i in range(17)
            },
            "required": [f"p{i}" for i in range(17)]
        }
        assert anthropic_output_schema(too_many_unions) is None

        create = _AnthropicCreate(_anthropic_message("{}"))
        _install_anthropic(monkeypatch, create)
        with capture_logs() as logs:
            await anthropic_module.AnthropicAdapter().complete(
                _msgs(), model="m", response_format="json_object",
                response_schema=too_many_optional)
        assert create.kwargs[0]["output_config"] == {"effort": "low"}
        assert any(
            e["event"] == "anthropic.response_schema_not_sent" for e in logs)

    async def _errors_are_mapped(self, monkeypatch):
        credit_message = (
            "Your credit balance is too low to access the Anthropic API. "
            "Please go to Plans & Billing to upgrade or purchase credits.")
        cases = [
            ("rate-limit",
             _anthropic_status_error(
                 anthropic.RateLimitError, 429, "rate_limit_error", "slow"),
             LLMRateLimitError),
            ("overloaded",
             _anthropic_status_error(
                 anthropic.OverloadedError, 529, "overloaded_error",
                 "Overloaded"), LLMServerOverloadedError),
            ("server-error",
             _anthropic_status_error(
                 anthropic.InternalServerError, 500, "api_error", "boom"),
             LLMServerOverloadedError),
            ("timeout", _anthropic_timeout(), LLMProviderTimeoutError),
            ("bad-request",
             _anthropic_status_error(
                 anthropic.BadRequestError, 400, "invalid_request_error",
                 "max_tokens: too large"), LLMInvalidRequestError),
            ("credit-invalid-request",
             _anthropic_status_error(
                 anthropic.BadRequestError, 400, "invalid_request_error",
                 credit_message), LLMCreditExhaustedError),
            ("credit-billing-error",
             _anthropic_status_error(
                 anthropic.BadRequestError, 400, "billing_error",
                 credit_message), LLMCreditExhaustedError),
            ("auth",
             _anthropic_status_error(
                 anthropic.AuthenticationError, 401, "authentication_error",
                 "invalid x-api-key"), LLMConfigError),
            ("connection", RuntimeError("socket closed"), LLMRequestError)
        ]
        for case_id, raised, expected in cases:
            _install_anthropic(monkeypatch, _AnthropicCreate(raised))
            with pytest.raises(LLMError) as info:
                await anthropic_module.AnthropicAdapter().complete(
                    _msgs(), model="claude-haiku-5-5")
            assert type(info.value) is expected, case_id
            assert info.value.__cause__ is raised, case_id
        _install_anthropic(
            monkeypatch,
            _AnthropicCreate(
                _anthropic_status_error(
                    anthropic.BadRequestError, 400, "invalid_request_error",
                    credit_message)))
        with pytest.raises(LLMCreditExhaustedError) as info:
            await anthropic_module.AnthropicAdapter().complete(
                _msgs(), model="claude-haiku-5-5")
        assert isinstance(info.value, LLMInvalidRequestError)
        assert "Anthropic API credit is used up" in str(info.value)
        assert router_retry_limit(
            info.value, provider_name="anthropic", model="claude-haiku-5-5",
            retries=4) == 0

    async def _stream_success_logs_usage(self, monkeypatch):
        messages = _AnthropicStreamMessages(
            _AnthropicStream(["Hel", "lo"],
                             _anthropic_message("Hello", thinking_tokens=4)))
        _install_anthropic(monkeypatch, messages)
        with capture_logs() as logs:
            chunks = [
                c async for c in anthropic_module.AnthropicAdapter().stream(
                    _msgs(), model="claude-haiku-5-5", temperature=0.1,
                    max_tokens=64)
            ]
        assert chunks == ["Hel", "lo"]
        sent = messages.kwargs[0]
        assert sent["max_tokens"] == 64 + 2048
        assert sent["temperature"] is anthropic.omit
        assert sent["output_config"] == {"effort": "low"}
        done = [e for e in logs if e["event"] == "anthropic.stream_completed"]
        assert len(done) == 1
        assert done[0]["prompt_tokens"] == 107
        assert done[0]["completion_tokens"] == 36
        assert done[0]["thinking_tokens"] == 4

    async def _stream_errors_are_mapped(self, monkeypatch):
        overloaded = _anthropic_status_error(
            anthropic.OverloadedError, 529, "overloaded_error", "Overloaded")
        _install_anthropic(monkeypatch, _AnthropicStreamMessages(overloaded))
        with pytest.raises(LLMServerOverloadedError):
            async for _ in anthropic_module.AnthropicAdapter().stream(
                    _msgs(), model="m"):
                pass
        mid_stream = anthropic.APIStatusError(
            "overloaded", response=httpx.Response(
                200, request=httpx.Request("POST", "https://x")), body={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Overloaded"
                    }
                })
        _install_anthropic(
            monkeypatch,
            _AnthropicStreamMessages(
                _AnthropicStream(["partial"], None, fail_after=mid_stream)))
        received: list[str] = []
        with pytest.raises(LLMServerOverloadedError):
            async for chunk in anthropic_module.AnthropicAdapter().stream(
                    _msgs(), model="m"):
                received.append(chunk)
        assert received == ["partial"]

    async def _router_never_retries_credit_exhaustion(self, monkeypatch):
        _set_settings(
            monkeypatch, {
                "LLM_ANTHROPIC_MODEL_CHEAP": "haiku",
                "LLM_DEFAULT_PROVIDER": "anthropic"
            })
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _FakeAdapter(
            "anthropic", fail_times=5, fail_with=LLMCreditExhaustedError)
        with pytest.raises(LLMCreditExhaustedError):
            await _router_with(anthropic=adapter).complete(
                LLMTier.CHEAP, _msgs(), retries=4)
        assert adapter.calls == 1
        limited = _FakeAdapter(
            "anthropic", fail_times=2, fail_with=LLMServerOverloadedError)
        resp = await _router_with(anthropic=limited).complete(
            LLMTier.CHEAP, _msgs(), retries=4)
        assert resp.content == "response from anthropic/haiku"
        assert limited.calls == 3

    async def _thinking_mode_follows_the_tier_model(self, monkeypatch):
        _set_settings(
            monkeypatch, {
                "LLM_ANTHROPIC_MODEL_CHEAP": "haiku",
                "LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY": "sonnet"
            })
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        adapter = anthropic_module.AnthropicAdapter()
        for model in ("haiku", "sonnet", "other"):
            await adapter.complete(_msgs(), model=model, max_tokens=128)
        assert [(k["thinking"]["type"], k["max_tokens"])
                for k in create.kwargs] == [("adaptive", 128 + 2048),
                                            ("between_tools", 128),
                                            ("adaptive", 128 + 2048)]
        _set_settings(
            monkeypatch, {
                "LLM_ANTHROPIC_THINKING_HIGHEST_ACCURACY": "adaptive",
                "LLM_ANTHROPIC_THINKING_HEADROOM_TOKENS": 0
            })
        await adapter.complete(_msgs(), model="sonnet", max_tokens=128)
        assert create.kwargs[-1]["thinking"] == {"type": "adaptive"}
        assert create.kwargs[-1]["max_tokens"] == 128

    async def _rejected_between_tools_is_explained(self, monkeypatch):
        _set_settings(
            monkeypatch, {"LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY": "sonnet"})
        rejection = _anthropic_status_error(
            anthropic.BadRequestError, 400, "invalid_request_error",
            "thinking.type: between_tools is not supported for this model")
        create = _AnthropicCreate(rejection)
        _install_anthropic(monkeypatch, create)
        with pytest.raises(LLMInvalidRequestError) as info:
            await anthropic_module.AnthropicAdapter().complete(
                _msgs(), model="sonnet")
        assert "'between_tools'" in str(info.value)
        assert "LLM_ANTHROPIC_THINKING_" in str(info.value)
        assert len(create.kwargs) == 1

    async def _thinking_that_uses_every_token_is_an_error(self, monkeypatch):
        exhausted = _anthropic_message(
            "", thinking_tokens=40, stop_reason="max_tokens")
        _install_anthropic(monkeypatch, _AnthropicCreate(exhausted))
        with capture_logs() as logs, pytest.raises(
                LLMOutputExhaustedError) as info:
            await anthropic_module.AnthropicAdapter().complete(
                _msgs(), model="m", max_tokens=128)
        assert "before writing any text" in str(info.value)
        assert [
            router_retry_limit(
                info.value, provider_name="anthropic", model="m",
                retries=retries) for retries in (0, 1, 4)
        ] == [0, 1, 1]
        logged = [
            e for e in logs
            if e["event"] == "anthropic.thinking_exhausted_max_tokens"
        ]
        assert logged and logged[0]["thinking_tokens"] == 40
        assert logged[0]["output_tokens"] == 40
        assert logged[0]["max_tokens"] == 128 + 2048
        cut_text = _anthropic_message("partial", stop_reason="max_tokens")
        _install_anthropic(monkeypatch, _AnthropicCreate(cut_text))
        resp = await anthropic_module.AnthropicAdapter().complete(
            _msgs(), model="m", max_tokens=128)
        assert resp.content == "partial"
        _install_anthropic(
            monkeypatch,
            _AnthropicStreamMessages(_AnthropicStream([], exhausted)))
        with pytest.raises(LLMOutputExhaustedError):
            async for _ in anthropic_module.AnthropicAdapter().stream(
                    _msgs(), model="m"):
                pass
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        _set_settings(
            monkeypatch, {
                "LLM_ANTHROPIC_MODEL_CHEAP": "haiku",
                "LLM_DEFAULT_PROVIDER": "anthropic"
            })
        adapter = _FakeAdapter(
            "anthropic", fail_times=5, fail_with=LLMOutputExhaustedError)
        with pytest.raises(LLMOutputExhaustedError):
            await _router_with(anthropic=adapter).complete(
                LLMTier.CHEAP, _msgs(), retries=4)
        assert adapter.calls == 2

    async def _json_answers_are_normalized(self, monkeypatch):
        cases = [
            ("fenced", '```json\n{"a": 1}\n```', '{"a": 1}'),
            ("preamble", 'Here is the JSON:\n{"a": {"b": 2}}',
             '{"a": {"b": 2}}'),
            ("trailing", '{"a": 1}\nHope this helps.', '{"a": 1}'),
            ("list", 'Result: [{"a": 1}, {"b": 2}] done',
             '[{"a": 1}, {"b": 2}]'),
            ("valid", '{"a": 1}', '{"a": 1}'),
            ("invalid", 'Sorry, {"a": 1,', 'Sorry, {"a": 1,'),
            ("no-json", "I cannot answer.", "I cannot answer.")
        ]
        adapter = anthropic_module.AnthropicAdapter()
        for case_id, raw, expected in cases:
            _install_anthropic(
                monkeypatch, _AnthropicCreate(_anthropic_message(raw)))
            with capture_logs() as logs:
                resp = await adapter.complete(
                    _msgs(), model="m", response_format="json_object")
            assert resp.content == expected, case_id
            normalized = [
                e for e in logs if e["event"] == "anthropic.json_normalized"
            ]
            if expected == raw:
                assert not normalized, case_id
            else:
                assert normalized[0]["chars_dropped"] == (
                    len(raw) - len(expected)), case_id
        fenced = '```json\n{"a": 1}\n```'
        _install_anthropic(
            monkeypatch, _AnthropicCreate(_anthropic_message(fenced)))
        plain = await adapter.complete(_msgs(), model="m")
        assert plain.content == fenced
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        with_schema = await adapter.complete(
            _msgs(), model="m", response_format="json_object",
            response_schema=schema)
        assert with_schema.content == fenced

    async def _too_complex_schema_falls_back_to_plain_json(
            self, monkeypatch):
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        fenced = '```json\n{"a": 1}\n```'
        create = _AnthropicSequence([
            _too_complex(),
            _anthropic_message(fenced),
            _anthropic_message('{"a": 2}')
        ])
        _install_anthropic(monkeypatch, create)
        adapter = anthropic_module.AnthropicAdapter()

        async def call(response_schema: Any = schema) -> LLMResponse:
            return await adapter.complete(
                _msgs(), model="m", response_format="json_object",
                response_schema=response_schema)

        with capture_logs() as logs:
            first = await call()
            second = await call()
        assert first.content == '{"a": 1}'
        assert second.content == '{"a": 2}'
        assert "format" in create.kwargs[0]["output_config"]
        assert [k["output_config"] for k in create.kwargs[1:]] == [{
            "effort": "low"
        }] * 2
        assert all(
            k["thinking"] == {"type": "adaptive"} for k in create.kwargs)
        fallbacks = [
            e for e in logs
            if e["event"] == "anthropic.schema_too_complex_fallback"
        ]
        assert len(fallbacks) == 1
        assert fallbacks[0]["model"] == "m"
        assert len(fallbacks[0]["schema_hash"]) == 16
        assert (fallbacks[0]["rejected_level"],
                fallbacks[0]["next_level"]) == ("full", "none")
        skipped = [
            e for e in logs
            if e["event"] == "anthropic.response_schema_skipped"
        ]
        assert [e["schema_hash"] for e in skipped] == [
            fallbacks[0]["schema_hash"]
        ] * 2
        assert [
            e["schema_sent"]
            for e in logs if e["event"] == "anthropic.completion_succeeded"
        ] == [False, False]

        other_schema = {"type": "object", "properties": {"b": {}}}
        cases: list[tuple[Any, Exception]] = [
            (other_schema,
             _anthropic_status_error(
                 anthropic.BadRequestError, 400, "invalid_request_error",
                 "max_tokens: too large")), (None, _too_complex())
        ]
        for response_schema, raised in cases:
            create = _AnthropicSequence([raised, _anthropic_message("{}")])
            _install_anthropic(monkeypatch, create)
            with pytest.raises(LLMInvalidRequestError):
                await call(response_schema)
            assert len(create.kwargs) == 1

    async def _too_complex_schema_tries_the_reduced_schema(self, monkeypatch):
        full = anthropic_output_schema(_TOOL_UNION_SCHEMA)
        assert full is not None
        reduced = reduced_output_schema(full)
        assert reduced["properties"]["action_input"] == {
            "anyOf": [{
                "type": "object",
                "properties": {
                    "ticker": {
                        "anyOf": [{
                            "type": "string"
                        }, {
                            "type": "integer"
                        }]
                    },
                    "query": {
                        "type": "string"
                    }
                },
                "additionalProperties": False
            }, {
                "type": "null"
            }]
        }
        assert reduced["properties"]["reported_metrics"] == (
            full["properties"]["reported_metrics"])
        assert reduced_output_schema(reduced) == reduced

        create = _AnthropicSequence([
            _too_complex(),
            _anthropic_message('{"thought": "a"}'),
            _anthropic_message('{"thought": "b"}')
        ])
        _install_anthropic(monkeypatch, create)
        adapter = anthropic_module.AnthropicAdapter()
        with capture_logs() as logs:
            for _ in range(2):
                await adapter.complete(
                    _msgs(), model="m", response_format="json_object",
                    response_schema=_TOOL_UNION_SCHEMA)
        sent = [k["output_config"]["format"]["schema"] for k in create.kwargs]
        assert sent == [full, reduced, reduced]
        assert [(e["rejected_level"], e["next_level"]) for e in logs
                if e["event"] == "anthropic.schema_too_complex_fallback"
                ] == [("full", "reduced")]
        assert not any(
            e["event"] == "anthropic.response_schema_skipped" for e in logs)

        anthropic_module._schema_levels.clear()
        create = _AnthropicSequence(
            [_too_complex(),
             _too_complex(),
             _anthropic_message("{}")])
        _install_anthropic(monkeypatch, create)
        with capture_logs() as logs:
            await adapter.complete(
                _msgs(), model="m", response_format="json_object",
                response_schema=_TOOL_UNION_SCHEMA)
        assert [
            k["output_config"].get("format", {}).get("schema")
            for k in create.kwargs
        ] == [full, reduced, None]
        assert [e["event"] for e in logs if e["event"].startswith(
            "anthropic.") and e["event"] != "anthropic.completion_succeeded"
                ] == [
                    "anthropic.schema_too_complex_fallback",
                    "anthropic.schema_too_complex_fallback",
                    "anthropic.response_schema_skipped"
                ]

    async def _prompt_caching_can_be_turned_off(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        _set_settings(monkeypatch, {"LLM_ANTHROPIC_PROMPT_CACHING": False})
        await anthropic_module.AnthropicAdapter().complete(
            [LLMMessage(role="system", content="Be brief."), *_msgs()],
            model="m")
        assert create.kwargs[0]["system"] == "Be brief."
        assert "cache_control" not in create.kwargs[0]
        await anthropic_module.AnthropicAdapter().complete(
            _hinted_messages(3), model="m")
        assert create.kwargs[1]["messages"] == [{
            "role": "user",
            "content": "".join(_SEGMENTS[:3])
        }]
        assert "cache_control" not in str(create.kwargs[1])

    async def _hinted_message_is_sent_as_cached_blocks(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        adapter = anthropic_module.AnthropicAdapter()
        for count in (1, 2, len(_SEGMENTS)):
            await adapter.complete(_hinted_messages(count), model="m")
            blocks = create.kwargs[-1]["messages"][0]["content"]
            assert "".join(b["text"] for b in blocks) == "".join(
                _SEGMENTS[:count])
            assert [b["text"] for b in blocks] == list(_SEGMENTS[:count])
            assert [("cache_control" in b) for b in blocks
                    ] == [False] * (count - 1) + [True]
            assert _breakpoints(create.kwargs[-1]) == 2
        await adapter.complete(
            [LLMMessage(
                role="user", content="ab", cache_segments=("", "a", "", "b"))],
            model="m")
        assert create.kwargs[-1]["messages"][0]["content"] == [{
            "type": "text",
            "text": "a"
        }, {
            "type": "text",
            "text": "b",
            "cache_control": {
                "type": "ephemeral"
            }
        }]
        assert create.kwargs[-1]["system"] is anthropic.omit
        await adapter.complete([
            LLMMessage(
                role="user", content="\n a\n\nb \n",
                cache_segments=("\n ", "a", "\n\n", "b", " \n"))
        ], model="m")
        blocks = create.kwargs[-1]["messages"][0]["content"]
        assert [b["text"] for b in blocks] == ["\n a\n\n", "b \n"]
        assert "cache_control" in blocks[-1]
        await adapter.complete(
            [
                LLMMessage(
                    role="user", content=" \n", cache_segments=(" ", "\n"))
            ], model="m")
        assert create.kwargs[-1]["messages"][0]["content"] == " \n"

    async def _unhinted_turns_stay_plain_strings(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        messages = [
            LLMMessage(role="system", content="Judge."),
            LLMMessage(role="user", content="evidence"),
            LLMMessage(role="assistant", content="{bad"),
            LLMMessage(role="user", content="Return valid JSON.")
        ]
        await anthropic_module.AnthropicAdapter().complete(
            messages, model="m")
        assert create.kwargs[0]["messages"] == [{
            "role": m.role,
            "content": m.content
        } for m in messages[1:]]
        assert _breakpoints(create.kwargs[0]) == 1

    async def _never_more_than_four_breakpoints(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        hinted = [
            LLMMessage(
                role=role, content=f"{role} {i}",
                cache_segments=(f"{role} ", f"{i}"))
            for i in range(4) for role in ("user", "assistant")
        ]
        await anthropic_module.AnthropicAdapter().complete(
            [LLMMessage(role="system", content="S"), *hinted], model="m")
        sent = create.kwargs[0]
        assert _breakpoints(sent) == 4
        marked = [
            i for i, turn in enumerate(sent["messages"])
            if "cache_control" in str(turn["content"])
        ]
        assert marked == [5, 6, 7]
        assert [
            "".join(b["text"] for b in turn["content"])
            for turn in sent["messages"]
        ] == [m.content for m in hinted]

    async def _stream_sends_the_same_cached_blocks(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        await anthropic_module.AnthropicAdapter().complete(
            _hinted_messages(3), model="m")
        streamed = _AnthropicStreamMessages(
            _AnthropicStream(["ok"], _anthropic_message("ok")))
        _install_anthropic(monkeypatch, streamed)
        _ = [
            c async for c in anthropic_module.AnthropicAdapter().stream(
                _hinted_messages(3), model="m")
        ]
        assert streamed.kwargs[0]["messages"] == create.kwargs[0]["messages"]
        assert streamed.kwargs[0]["system"] == create.kwargs[0]["system"]

    async def _scratchpad_caching_can_be_turned_off(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        _set_settings(monkeypatch, {"LLM_ANTHROPIC_CACHE_SCRATCHPAD": False})
        await anthropic_module.AnthropicAdapter().complete(
            _hinted_messages(3), model="m")
        sent = create.kwargs[0]
        assert sent["messages"] == [{
            "role": "user",
            "content": "".join(_SEGMENTS[:3])
        }]
        assert sent["system"] == [{
            "type": "text",
            "text": "Be brief.",
            "cache_control": {
                "type": "ephemeral"
            }
        }]
        assert _breakpoints(sent) == 1

    async def _shared_evidence_hint_changes_nothing_when_off(
            self, monkeypatch):
        for caching, first in ((True, False), (False, True), (False, False)):
            create = _AnthropicCreate(_anthropic_message("ok"))
            _install_anthropic(monkeypatch, create)
            _set_settings(
                monkeypatch, {
                    "LLM_ANTHROPIC_PROMPT_CACHING": caching,
                    "LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST": first
                })
            adapter = anthropic_module.AnthropicAdapter()
            for hinted in (_evidence_messages(), _judge_follow_up()):
                plain = [
                    LLMMessage(role=m.role, content=m.content)
                    for m in hinted
                ]
                with capture_logs() as logs:
                    await adapter.complete(hinted, model="m")
                    await adapter.complete(plain, model="m")
                assert create.kwargs[-2] == create.kwargs[-1]
                assert not any("evidence_first" in e for e in logs)

    async def _shared_evidence_goes_first_when_on(self, monkeypatch):
        create = _AnthropicCreate(_anthropic_message("ok"))
        _install_anthropic(monkeypatch, create)
        _set_settings(
            monkeypatch, {"LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST": True})
        adapter = anthropic_module.AnthropicAdapter()
        cached = {"type": "ephemeral"}
        with capture_logs() as logs:
            await adapter.complete(_evidence_messages(), model="m")
        first = create.kwargs[-1]
        assert first["system"] == [{
            "type": "text",
            "text": "Evidence for this analysis:\n" + _EVIDENCE,
            "cache_control": cached
        }, {
            "type": "text",
            "text": "You are the Bull.",
            "cache_control": cached
        }]
        assert first["messages"] == [{
            "role": "user",
            "content": (
                "Ticker: X\n\nResearch agent evidence:\n"
                + anthropic_module._EVIDENCE_POINTER +
                "\n\nBull argument: a")
        }]
        assert _breakpoints(first) == 2
        assert [e.get("evidence_first") for e in logs
                if e["event"] == "anthropic.completion_succeeded"] == [True]

        await adapter.complete(_judge_follow_up(), model="m")
        follow = create.kwargs[-1]
        assert follow["system"][0] == first["system"][0]
        assert [t["content"] for t in follow["messages"]] == [
            "Ticker: X\nRound: 1\n\nResearch agent evidence:\n" +
            anthropic_module._EVIDENCE_POINTER + "\n\nBull: a",
            "{bad", "Return valid JSON."
        ]
        assert _breakpoints(follow) == 2

        await adapter.complete([
            LLMMessage(role="system", content="S"),
            _evidence_messages()[1],
            *[
                LLMMessage(
                    role=role, content=f"{role} {i}",
                    cache_segments=(f"{role} ", f"{i}"))
                for i in range(2) for role in ("assistant", "user")
            ]
        ], model="m")
        mixed = create.kwargs[-1]
        assert _breakpoints(mixed) == 4
        assert [
            i for i, turn in enumerate(mixed["messages"])
            if "cache_control" in str(turn["content"])
        ] == [3, 4]

        await adapter.complete([_evidence_messages()[1]], model="m")
        assert create.kwargs[-1]["system"] == [{
            "type": "text",
            "text": anthropic_module._EVIDENCE_BLOCK_LABEL + _EVIDENCE,
            "cache_control": cached
        }]

        blank = [
            LLMMessage(role="system", content="S"),
            LLMMessage(role="user", content="Ticker: X\n", shared_evidence="")
        ]
        await adapter.complete(blank, model="m")
        assert create.kwargs[-1]["messages"][0]["content"] == "Ticker: X\n"
        assert len(create.kwargs[-1]["system"]) == 1

        streamed = _AnthropicStreamMessages(
            _AnthropicStream(["ok"], _anthropic_message("ok")))
        _install_anthropic(monkeypatch, streamed)
        _ = [
            c async for c in adapter.stream(_evidence_messages(), model="m")
        ]
        assert streamed.kwargs[0]["system"] == first["system"]
        assert streamed.kwargs[0]["messages"] == first["messages"]

    async def test_anthropic_adapter(self):
        await _run_steps(
            self._too_complex_schema_falls_back_to_plain_json,
            self._too_complex_schema_tries_the_reduced_schema,
            self._prompt_caching_can_be_turned_off,
            self._scratchpad_caching_can_be_turned_off,
            self._shared_evidence_hint_changes_nothing_when_off,
            self._shared_evidence_goes_first_when_on,
            self._hinted_message_is_sent_as_cached_blocks,
            self._unhinted_turns_stay_plain_strings,
            self._never_more_than_four_breakpoints,
            self._stream_sends_the_same_cached_blocks,
            self._json_answers_are_normalized,
            self._thinking_mode_follows_the_tier_model,
            self._rejected_between_tools_is_explained,
            self._thinking_that_uses_every_token_is_an_error,
            self._success_sends_schema_and_logs_usage,
            self._plain_call_sends_no_format_or_temperature,
            self._schema_conversion, self._errors_are_mapped,
            self._stream_success_logs_usage, self._stream_errors_are_mapped,
            self._router_never_retries_credit_exhaustion,
            reset=_reset_llm_state)


def test_credit_exhaustion_becomes_the_analysis_failure_reason():
    from ai.graph.nodes import _failure_kind_of
    from ai.graph.state import AgentOutput
    from ai.recommendation.engine import no_verdict_reason
    from shared.enums import AgentRole, AgentStatus, AnalysisFailureReason

    assert _failure_kind_of(LLMCreditExhaustedError("x")) == "credit_exhausted"
    failed = {
        role: AgentOutput(
            role=role, status=AgentStatus.FAILED, summary="failed",
            score=0.0, is_well_supported=False, failure_kind=kind)
        for role, kind in ((AgentRole.MARKET, "credit_exhausted"),
                           (AgentRole.NEWS, "other"))
    }
    assert no_verdict_reason(_state(agent_outputs=failed)) == (
        AnalysisFailureReason.LLM_CREDIT_EXHAUSTED)
    failed[AgentRole.MARKET].failure_kind = "other"
    assert no_verdict_reason(_state(agent_outputs=failed)) == (
        AnalysisFailureReason.NO_EVIDENCE)


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

    async def test_quota_errors_pacing_and_timeouts(self):
        self._day_quota_id_classified_as_day()
        await _run_steps(
            self._day_quota_hit_gets_the_long_cooldown,
            self._non_quota_client_error_raises_without_failover,
            self._gives_up_after_max_malformed_retries_with_a_big_pool,
            self._success_clears_the_models_overloaded_marker,
            self._server_error_wait_escalates_and_success_resets,
            reset=_reset_llm_state)

        await _run_steps(
            self._wait_beyond_one_interval_fails_fast_without_sleeping,
            self._zero_min_interval_pool_is_never_exhausted,
            self._fallback_flag_uses_the_fallback_timeout,
            self._timeouts_without_a_cooldown_leave_no_marker,
            self._a_rate_limit_mark_during_the_pacing_wait_still_skips,
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
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=1)
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
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=1)
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
                LLMTier.CHEAP, _msgs(), retries=0)
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
                LLMTier.CHEAP, _msgs(), response_format="json_object")

    async def _fallback_call_has_the_flag_and_no_schema(self, monkeypatch):
        _chain(monkeypatch)
        adapter = _PerModelAdapter({"m1": ["503", "ok"], "m2": ["ok"]})
        schema = {"type": "object"}
        router = LLMRouter(providers={"google": adapter})
        with recording_served_models() as served:
            resp = await router.complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=0,
                response_format="json_object", response_schema=schema)
            await router.complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=0)
        await router.complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), retries=0)
        assert resp.content == "answer from m2"
        assert adapter.calls[:2] == [("m1", {}), ("m2", {"fallback": True})]
        assert served == [("m2", True), ("m1", False)]
        assert fallback_call_count(served) == 1

    async def _debate_stage_calls_count_fallback_answers(self, monkeypatch):
        _chain(monkeypatch)
        monkeypatch.setattr(config.settings, "LLM_DEFAULT_PROVIDER", "google")
        router = LLMRouter(providers={
            "google": _PerModelAdapter({"m1": ["503"], "m2": ["ok"]})
        })
        verdict = await JudgeAgent(llm_router=router).assess_round(_state())
        assert verdict.fallback_calls == 2
        argument = await dc.generate_debate_argument(
            side="bull", state=_state(), llm_router=router,
            llm_tier=LLMTier.HIGHEST_ACCURACY, system_prompt="s")
        assert argument.fallback_calls == 1

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
            LLMTier.CHEAP, _msgs(), retries=0,
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
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=0)
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
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=0)
        assert adapter.models == ["m1"]
        assert "skipped 'm2'" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, LLMServerOverloadedError)

    async def test_skips_non_english_and_fallback_calls(self):
        await _run_steps(
            self._exhausted_primary_goes_straight_to_the_fallback,
            self._overloaded_skip_is_reported_as_server_overloaded,
            self._non_english_fallback_response_raises,
            self._json_escaped_non_english_is_rejected,
            reset=_reset_llm_state)

        await _run_steps(
            self._fallback_call_has_the_flag_and_no_schema,
            self._debate_stage_calls_count_fallback_answers,
            self._thinking_budget_goes_to_both_google_calls,
            self._a_failed_fallback_is_raised_without_more_calls,
            self._day_exhausted_fallback_is_skipped,
            self._timed_out_primary_goes_to_the_fallback,
            self._caller_timeout_retries_once_without_fallback,
            reset=_reset_llm_state)

    async def _timed_out_primary_goes_to_the_fallback(self, monkeypatch):
        _chain(monkeypatch)
        adapter = _PerModelAdapter({"m1": ["timeout"], "m2": ["ok"]})
        router = LLMRouter(providers={"google": adapter})
        first = await router.complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), retries=4)
        second = await router.complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), retries=4)
        assert first.content == second.content == "answer from m2"
        assert adapter.models == ["m1", "m2", "m2"]

        request_pacer.reset()
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS", 0.0)
        adapter = _PerModelAdapter({"m1": ["timeout"], "m2": ["ok"]})
        await LLMRouter(providers={
            "google": adapter
        }).complete(
            LLMTier.HIGHEST_ACCURACY, _msgs(), retries=4)
        assert adapter.models == ["m1", "m2"]

    async def _caller_timeout_retries_once_without_fallback(
            self, monkeypatch):
        _chain(monkeypatch)
        adapter = _PerModelAdapter({"m1": ["provider_timeout"], "m2": ["ok"]})
        with pytest.raises(LLMProviderTimeoutError):
            await LLMRouter(providers={
                "google": adapter
            }).complete(
                LLMTier.HIGHEST_ACCURACY, _msgs(), retries=4,
                timeout_seconds=300)
        assert adapter.models == ["m1", "m1"]
        assert all(
            extra["timeout_seconds"] == 300 for _, extra in adapter.calls)


class _RecordingCreate:

    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        return self.response


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
                 "google-request-timeout-one-retry",
                 LLMRequestTimeoutError("t"), "google", "gemini-3.8-flash", 4,
                 1),
             (
                 "mistral-rate-limit", LLMRateLimitError("429"), "mistral",
                 "mistral-small-lite", 4, 4),
             (
                 "mistral-overloaded", LLMServerOverloadedError("503"),
                 "mistral", "mistral-small-lite", 4, 4),
             (
                 "anthropic-provider-timeout-one-retry",
                 LLMProviderTimeoutError("t"), "anthropic", "claude-lite", 4,
                 1)]
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
            LLMTier.CHEAP, _msgs(), retries=retries)

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
            LLMTier.HIGHEST_ACCURACY: LLMNonEnglishOutputError("non-English"),
            LLMTier.CHEAP: _tiers_json([(100, 5)])
        })
        result, note = await _size(router)
        assert router.tiers == [LLMTier.HIGHEST_ACCURACY, LLMTier.CHEAP]
        assert result is not None and len(result.tiers) == 1

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

    async def test_batches_requests_errors_and_timeouts(self):
        batches = embedding_module.google_batches(["short text"] * 45)
        assert [len(b) for b in batches] == [20, 20, 5]
        filing_chunk = " ".join(["word"] * 700)
        batches = embedding_module.google_batches([filing_chunk] * 30)
        assert all(
            sum(embedding_module.estimated_tokens(t)
                for t in b) <= embedding_module._GOOGLE_BATCH_TOKEN_BUDGET
            for b in batches)
        assert sum(len(b) for b in batches) == 30

        await _run_steps(
            self._embedding_2_uses_prompt_prefixes_not_task_type,
            self._embed_texts_and_query_dispatch_to_google,
            reset=_reset_llm_state)

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

    def test_llm_tier_and_provider_typos_fail_at_startup(self, monkeypatch):
        for name, value in (("LLM_DEFAULT_PROVIDER", "gogle"),
                            ("FILING_LLM_TIER", "highest"),
                            ("LLM_ANTHROPIC_THINKING_CHEAP", "adaptve"),
                            ("LLM_ANTHROPIC_EFFORT", "lowest")):
            with pytest.raises(ValidationError, match=name):
                config.AIServiceSettings(**{name: value})

        defaults = config.AIServiceSettings()
        for name in (*config._BLANK_IS_DEFAULT, "LLM_ANTHROPIC_EFFORT"):
            with monkeypatch.context() as mp:
                mp.setenv(name, "")
                loaded = getattr(config.AIServiceSettings(), name)
            expected = ("" if name == "LLM_ANTHROPIC_EFFORT" else getattr(
                defaults, name))
            assert loaded == expected, name

        example = Path(__file__).resolve().parents[2] / ".env.example"
        checked = set()
        for line in example.read_text().splitlines():
            name, _, value = line.lstrip("# ").partition("=")
            if name in (*config._BLANK_IS_DEFAULT, "LLM_ANTHROPIC_EFFORT"):
                with monkeypatch.context() as mp:
                    mp.setenv(name, value)
                    assert getattr(config.AIServiceSettings(), name) == value
                checked.add(name)
        assert {"LLM_DEFAULT_PROVIDER", "RAG_ASK_LLM_TIER"} <= checked


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
        retriever = retriever_module.BaseRetriever(visibility=None)
        results = await retriever.retrieve("AMD revenue")
        assert [r.chunk_id for r in results] == [chunk_id]
        assert qdrant.query_points_calls == 0
        with pytest.raises(embedding_module.EmbeddingError):
            await retriever.retrieve(
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
            with pytest.raises(SystemExit, match="ABORT"):
                await module.run(
                    source="research_documents", target=target,
                    model="gemini-embedding-001", dims=8, confirm=True)
            assert embedded == [], case_id


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

    async def test_metrics_register_once_and_queue_gauges_follow(self):
        before = observability.ANALYSES_TOTAL
        reloaded = importlib.reload(observability)
        assert reloaded.ANALYSES_TOTAL is before
        assert reloaded.LLM_ERRORS_TOTAL is observability.LLM_ERRORS_TOTAL

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

    async def test_mark_analysis_failed_counts_only_the_real_transition(
            self, committed_analysis) -> None:
        from ai.workflows.analysis_workflow import mark_analysis_failed

        analysis_id = await committed_analysis("running")
        before = _failed_analyses("timeout")
        assert await mark_analysis_failed(analysis_id, "timeout") is True
        assert await mark_analysis_failed(analysis_id, "timeout") is False
        assert _failed_analyses("timeout") == before + 1


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


_SIZING_PROSE = (
    "Sizing rationale: the budget is $7,698.95, so I scale in.\n\n"
    "- Tier 1: 343.00 x 6 = 2,058.00\n\n"
    '{"window_days": 5, "tiers": [{"target_price": 343.0, '
    '"quantity": 6.0}]}\n\nTotal is within the budget.')


class TestWrappedJsonRecovery:

    def test_object_wrapped_in_prose_is_recovered_and_logged(self):
        from ai.agents.loop import ReActAgentLoop
        from ai.llm.base import parse_json_dict

        with capture_logs() as logs:
            assert parse_json_dict('{"a": 1}') == {"a": 1}
            assert parse_json_dict('```json\n{"a": 2}\n```') == {"a": 2}
        assert not logs
        with capture_logs() as logs:
            parsed = parse_json_dict(_SIZING_PROSE, log_event="x.unparseable")
        assert parsed is not None and parsed["window_days"] == 5
        recovered = [
            e for e in logs
            if e["event"] == "llm_json.recovered_from_wrapped_text"
        ]
        assert recovered[0]["shape"] == "dict"
        assert recovered[0]["chars_dropped"] == (
            len(_SIZING_PROSE) - len(_SIZING_PROSE[_SIZING_PROSE.index("{"):
                                                   _SIZING_PROSE.rindex("}")
                                                   + 1]))
        assert not any(e["event"] == "x.unparseable" for e in logs)
        assert parse_json_dict("[1, 2]") is None
        assert parse_json_dict('Two: {"a": 1} and {"b": 2}') is None
        step = ReActAgentLoop._parse_response(
            '{"thought": "done", "finish_answer": "ok"}\nextra words')
        assert step is not None and step.finish_answer == "ok"

        result, note = _validate(_SIZING_PROSE, "7698.95")
        assert result is not None and note is not None
        assert "could not be parsed" not in note
        assert [(t.target_price, t.quantity) for t in result.tiers] == [
            (Decimal("343.0"), Decimal("6.0"))
        ]

    def test_trailing_comma_is_repaired_only_after_other_recovery(self):
        from ai.agents.loop import ReActAgentLoop
        from ai.llm.base import parse_json_dict

        with capture_logs() as logs:
            assert parse_json_dict('{"a": [1, 2]}') == {"a": [1, 2]}
        assert not logs
        raw = (
            '{"thought": "t", "finish_answer": "no external analyst '
            'commentary is included.",}')
        with capture_logs() as logs:
            step = ReActAgentLoop._parse_response(raw)
        assert step is not None and step.finish_answer == (
            "no external analyst commentary is included.")
        repaired = [
            e for e in logs if e["event"] == "llm_json.repaired_trailing_comma"
        ]
        assert repaired[0]["chars_removed"] == 1
        assert not any(
            e["event"] == "react_loop.unparseable_response" for e in logs)
        assert parse_json_dict('{"a": "x,}", "b": [1, 2, ], }') == {
            "a": "x,}",
            "b": [1, 2]
        }
        assert parse_json_dict('Reply: {"a": 1,} done') == {"a": 1}
        with capture_logs() as logs:
            assert parse_json_dict('{"a": 1,, }', log_event="x.bad") is None
        assert [e["event"] for e in logs] == ["x.bad"]

    def test_unparseable_text_logs_head_and_tail(self):
        from ai.llm.base import parse_json_dict

        raw = "Tiers: {not json} " + "x" * 900 + " end of answer"
        with capture_logs() as logs:
            assert parse_json_dict(raw, log_event="x.unparseable") is None
        failed = [e for e in logs if e["event"] == "x.unparseable"]
        assert failed[0]["raw"] == raw[:500]
        assert failed[0]["raw_tail"] == raw[-300:]
        assert failed[0]["raw_chars"] == len(raw)

    def test_kg_list_wrapped_in_prose_is_recovered(self):
        from ai.knowledge.archival import _parse_response

        triple = {
            "source": "AMZN",
            "source_type": "company",
            "relation": "reported_revenue_of",
            "target": "$200.61B",
            "target_type": "economic_indicator",
            "confidence": 0.9
        }
        raw = json.dumps([triple]) + "\n\nNote: figures are from Q2."
        with capture_logs() as logs:
            triples = _parse_response(raw)
        assert [t.target for t in triples] == ["$200.61B"]
        assert any(
            e["event"] == "llm_json.recovered_from_wrapped_text"
            and e["shape"] == "list" for e in logs)
        with capture_logs() as logs:
            assert _parse_response("[cut off " + "y" * 600) == []
        failed = [
            e for e in logs
            if e["event"] == "kg_extraction.unparseable_response"
        ]
        assert failed[0]["raw_tail"].endswith("y" * 10)
