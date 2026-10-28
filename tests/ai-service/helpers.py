import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
import ai.llm.google_provider as google_module
import ai.rag.embedding as embedding_module
import config
import pytest
from ai.agents.filing import FilingRecordsCache
from ai.agents.loop import ScratchpadEntry
from ai.agents.risk import _CapturingRiskEventsValidator
from ai.graph.state import (AgentOutput, AnalysisState, DebateArgument,
                            JudgeVerdict)
from ai.knowledge import filing_records as frec
from ai.llm.base import (LLMConfigError, LLMMessage, LLMProviderAdapter,
                         LLMProviderTimeoutError, LLMRequestTimeoutError,
                         LLMResponse, LLMServerOverloadedError, LLMUsage,
                         request_pacer)
from ai.providers.base import PriceQuote, ProviderRequestError
from google.genai import errors as genai_errors
from domains.analysis.repository import (AnalysisPersistenceRepository,
                                         AnalysisRepository)
from domains.decision.repository import (DecisionOutcomeRepository,
                                         InvestmentDecisionRepository)
from shared.enums import (AgentRole, AgentStatus, AnalysisType,
                          RecommendationAction, TierSide, TierStatus)
from tests.auth_tokens import bearer_headers


class _ScriptedReviewRouter:

    def __init__(self, content: str):
        self._content = content
        self.calls: list = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(
            content=self._content, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


async def _make_decision_with_outcome(
        session, *, outperformed: bool | None = True):
    analysis = await AnalysisRepository(session).create(
        user_id=None, portfolio_id=None, ticker="AAPL")
    rec = await AnalysisPersistenceRepository(session).save_recommendation(
        analysis_id=analysis.id, recommendation="buy", confidence=0.75,
        reasoning="strong earnings momentum", confidence_adjustment_note=None,
        debate_converged=True)
    decision = await InvestmentDecisionRepository(session).create(
        user_id=uuid4(), portfolio_id=None, recommendation_id=rec.id,
        ticker="AAPL", action_taken="followed", notes="went with it")
    await DecisionOutcomeRepository(session).create_if_absent(
        decision_id=decision.id, evaluation_days=30, strategy_return=0.12,
        benchmark_return=0.05, alpha=0.07, outperformed=outperformed)
    return decision


def _finish(
        answer: str, reported_metrics: Any = None,
        thought: str = "done") -> str:
    return json.dumps({
        "thought": thought,
        "action": None,
        "action_input": None,
        "finish_answer": answer,
        "reported_metrics": reported_metrics
    })


def _action(
        action: str, action_input: dict[str, Any] | None,
        thought: str = "checking") -> str:
    return json.dumps({
        "thought": thought,
        "action": action,
        "action_input": action_input,
        "finish_answer": None
    })


class _Router:

    def __init__(
            self, script: list[str], finish_reason: str | None = None) -> None:
        self._script = list(script)
        self._finish_reason = finish_reason
        self.calls: list[Any] = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        content = (
            self._script.pop(0) if len(self._script) > 1 else self._script[0])
        return LLMResponse(
            content=content, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1),
            finish_reason=self._finish_reason)


class _AlwaysFailsRouter:

    async def complete(self, tier, messages, **kwargs):
        raise LLMConfigError("no key configured")


class _RaisingRouter:

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    async def complete(self, tier, messages, **kwargs):
        self.calls += 1
        raise self.exc


class _ScriptedAgentRouter:

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[Any] = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(
            content=item, provider="fake", model="fake-model", usage=LLMUsage(
                prompt_tokens=11, completion_tokens=7, thinking_tokens=3),
            finish_reason="STOP")


def _state(ticker: str = "AAPL", **overrides: Any) -> AnalysisState:
    values: dict[str, Any] = {
        "analysis_id": "a1",
        "user_id": "u1",
        "ticker": ticker,
        "analysis_type": AnalysisType.FULL
    }
    values.update(overrides)
    return AnalysisState(**values)


def _news_output(
        *, status=AgentStatus.COMPLETED, observation: str | None = None,
        summary: str = "News summary.") -> AgentOutput:
    pad = ([] if observation is None else [{
        "thought": "t",
        "action": "search_news",
        "action_input": {},
        "observation": observation,
        "is_tool_result": True,
        "tool_error": False
    }])
    return AgentOutput(
        role=AgentRole.NEWS, status=status, summary=summary,
        score=0.9 if status == AgentStatus.COMPLETED else 0.0,
        raw_output={"scratchpad": pad})


@dataclass
class _Row:
    filename: str
    form: str
    filing_date: date
    report_date: date | None
    records: dict[str, Any] = field(default_factory=dict)


_TSLA_ROWS = [
    _Row(
        "TSLA_10-Q_2026-07-23.txt", "10-Q", date(2026, 7, 23),
        date(2026, 6, 30), {
            "legal_matters": [{
                "name": "Certain Derivative Lawsuits in Delaware",
                "status": "appeal",
                "status_date": "2026-05",
                "detail": None,
                "quote": (
                    "In May 2026, plaintiffs appealed to the Delaware "
                    "Supreme "
                    "Court the decision granting Tesla's motion to "
                    "dismiss.")
            }]
        }),
    _Row(
        "TSLA_10-Q_2026-04-23.txt", "10-Q", date(2026, 4, 23),
        date(2026, 3, 31), {
            "legal_matters": [{
                "name": "Benavides v. Tesla, Inc.",
                "status": "appeal",
                "status_date": "2026-03-16",
                "detail": None,
                "quote": (
                    "On March 16, 2026, the Company filed a notice of "
                    "appeal.")
            }]
        }),
    _Row(
        "TSLA_10-K_2026-01-29.txt", "10-K", date(2026, 1, 29),
        date(2025, 12, 31), {
            "legal_matters": [{
                "name": "Benavides v. Tesla, Inc.",
                "status": "pending",
                "status_date": "2025-09-15",
                "detail": None,
                "quote": (
                    "On September 15, 2025, the Company filed a "
                    "post-trial "
                    "motion for judgment as a matter of law.")
            }]
        })
]

_TSLA_FILINGS = frec.render_filing_records(_TSLA_ROWS)


def _pad(observation: str) -> list[ScratchpadEntry]:
    return [ScratchpadEntry("t", "search_filings", {}, observation)]


def _events(*items: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "risk_events": [{
            "entity": entity,
            "evidence_tool": "search_filings",
            "status": status,
            "source_id": source
        } for entity, status, source in items]
    }


def _validator(rows: list[_Row]) -> _CapturingRiskEventsValidator:
    validator = _CapturingRiskEventsValidator(FilingRecordsCache())
    validator.records.rows, validator.records.shown = list(rows), True
    return validator


_ALL_ROLES = [
    AgentRole.MARKET, AgentRole.NEWS, AgentRole.SENTIMENT, AgentRole.FILING,
    AgentRole.FUNDAMENTAL, AgentRole.TECHNICAL, AgentRole.RISK,
    AgentRole.MACRO, AgentRole.PORTFOLIO
]


def _tool_entry(
        observation: str, *, error: bool = False, tool: bool = True) -> dict:
    return {
        "thought": "t",
        "action": "get_price_history",
        "action_input": {},
        "observation": observation,
        "is_tool_result": tool,
        "tool_error": error
    }


def _ok(role: AgentRole, summary: str = "fine", **kw: Any) -> AgentOutput:
    raw = kw.pop("raw_output", {"scratchpad": [_tool_entry("Close 100.0")]})
    return AgentOutput(
        role=role, summary=summary, score=0.9, raw_output=raw, **kw)


def _verdict(
        action: str = "buy", confidence: float = 0.85,
        **kw: Any) -> JudgeVerdict:
    return JudgeVerdict(
        round_number=1, converged=True, leaning=RecommendationAction(action),
        confidence=confidence, rationale="r", **kw)


def _args(bull_ok: bool = True, bear_ok: bool = True,
          round_number: int = 1) -> list[DebateArgument]:
    return [
        DebateArgument(
            round_number=round_number, side="bull",
            argument="bull case" if bull_ok else "[bull argument unavailable]",
            available=bull_ok,
            unavailable_kind=None if bull_ok else "server_overload"),
        DebateArgument(
            round_number=round_number, side="bear",
            argument="bear case" if bear_ok else "[bear argument unavailable]",
            available=bear_ok,
            unavailable_kind=None if bear_ok else "server_overload")
    ]


_P = TierStatus.PENDING_ACCEPTANCE.value
_BUY = TierSide.BUY.value


class _StubGraph:

    def __init__(self, result: dict[str, Any]):
        self.result = result

    async def ainvoke(self, state, config=None):
        return {**self.result}


def _msgs() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="hi")]


class _PerModelAdapter(LLMProviderAdapter):
    name = "google"

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {m: list(v) for m, v in script.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete(
            self, messages, *, model, temperature=0.2, max_tokens=1024,
            response_format=None, timeout_seconds=None, response_schema=None,
            **extra: Any) -> LLMResponse:
        if timeout_seconds is not None:
            extra = {**extra, "timeout_seconds": timeout_seconds}
        self.calls.append((model, extra))
        outcomes = self.script[model]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "503":
            raise LLMServerOverloadedError(f"{model} 503")
        if outcome == "timeout":
            cooldown = config.settings.LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS
            if cooldown > 0:
                await request_pacer.mark_unavailable_until(
                    ("google", "timeout", model),
                    time.monotonic() + cooldown)
            raise LLMRequestTimeoutError(f"{model} timed out")
        if outcome == "provider_timeout":
            raise LLMProviderTimeoutError(
                f"{model} timed out after {timeout_seconds:g}s")
        return LLMResponse(
            content=f"answer from {model}", provider="google", model=model,
            usage=LLMUsage(prompt_tokens=1,
                           completion_tokens=1), finish_reason="stop")

    async def stream(
            self, messages, *, model, temperature=0.2, max_tokens=1024):
        yield "x"

    @property
    def models(self) -> list[str]:
        return [m for m, _ in self.calls]


async def _no_sleep(seconds: float) -> None:
    return None


async def _run_steps(
        *steps: Callable[[pytest.MonkeyPatch], Awaitable[Any]],
        setup: Callable[[pytest.MonkeyPatch], Any] | None = None,
        reset: Callable[[], Any] | None = None) -> None:
    for step in steps:
        if reset is not None:
            reset()
        with pytest.MonkeyPatch.context() as mp:
            if setup is not None:
                setup(mp)
            await step(mp)


def _chain(monkeypatch, primary: str = "m1", fallback: str = "m2") -> None:
    monkeypatch.setattr(
        config.settings, "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY", primary)
    monkeypatch.setattr(
        config.settings, "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK",
        fallback)
    monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)


class _FlakyMarket:

    def __init__(self, failing: set[str] | None = None) -> None:
        self.prices = {"MSFT": Decimal("498.62"), "AMD": Decimal("533.60")}
        self.failing = set(failing or ())
        self.calls: list[str] = []

    async def get_current_price(self, ticker: str) -> PriceQuote:
        self.calls.append(ticker)
        if ticker in self.failing:
            raise ProviderRequestError(f"price lookup failed for {ticker}")
        return PriceQuote(
            ticker=ticker, price=self.prices[ticker], as_of=datetime.now(UTC),
            source="fake")


async def _msft_amd_holdings(portfolio_id: str) -> list[dict[str, Any]]:
    return [{
        "ticker": "MSFT",
        "quantity": 20.0,
        "average_cost": 498.6
    }, {
        "ticker": "AMD",
        "quantity": 5.0,
        "average_cost": 465.7
    }]


async def _cash(portfolio_id: str) -> float:
    return 7698.95


class _RecordingSleep:

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class _FakeEmbedModels:

    def __init__(self, script: list[Any], dims: int) -> None:
        self.script = list(script)
        self.dims = dims
        self.calls: list[dict[str, Any]] = []

    async def embed_content(self, *, model, contents, config):
        self.calls.append({
            "model": model,
            "contents": contents,
            "config": config
        })
        outcome = (
            self.script.pop(0) if len(self.script) > 1 else self.script[0])
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(values=[3.0, 4.0] + [0.0] * (self.dims - 2))
                for _ in contents
            ])


def _install_google_pool(monkeypatch, per_label: dict[str, _FakeEmbedModels]):
    monkeypatch.setattr(
        google_module, "google_pool_labels", lambda: list(per_label))
    monkeypatch.setattr(
        google_module, "google_client_for", lambda label: SimpleNamespace(
            aio=SimpleNamespace(models=per_label[label])))
    monkeypatch.setattr(
        embedding_module, "_google_key_cursor", iter(range(0, 10**6)))
    sleep = _RecordingSleep()
    monkeypatch.setattr("ai.rag.embedding.asyncio.sleep", sleep)
    return sleep


class _FakeGoogleGenerateContent:

    def __init__(
            self, *, fail_times: int = 0, fail_with: Exception | None = None):
        self.fail_times = fail_times
        self.fail_with = fail_with
        self.calls = 0

    async def __call__(self, *, model, contents, config):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.fail_with
        return _FakeGeminiResponse()


class _FakeGeminiResponse:

    class _Usage:
        prompt_token_count = 10
        candidates_token_count = 5
        thoughts_token_count = None

    class _Candidate:
        finish_reason = "STOP"

    text = "ok"
    usage_metadata = _Usage()
    candidates = [_Candidate()]


class _FakeGoogleClient:

    def __init__(self, generate_content: _FakeGoogleGenerateContent):
        self.aio = _SimpleNamespaceModels(generate_content)


class _SimpleNamespaceModels:

    def __init__(self, generate_content):
        self.models = _SimpleNamespaceGenerate(generate_content)


class _SimpleNamespaceGenerate:

    def __init__(self, generate_content):
        self.generate_content = generate_content


class _FakeGooglePool:

    def __init__(self, clients: dict[str, _FakeGoogleClient]):
        self._clients = clients
        self.used: list[str] = []

    def key_count(self) -> int:
        return len(self._clients)

    def available_labels_excluding(self, excluded: set[str]) -> list[str]:
        return [lbl for lbl in self._clients if lbl not in excluded]

    def client_for(self, label: str) -> _FakeGoogleClient:
        self.used.append(label)
        return self._clients[label]


def _single_client_pool(
        generate: Any,
        labels: Sequence[str] = ("primary", )) -> _FakeGooglePool:
    client = _FakeGoogleClient(generate)
    return _FakeGooglePool({label: client for label in labels})


def _server_503() -> genai_errors.ServerError:
    return genai_errors.ServerError(
        503,
        {"error": {
            "code": 503,
            "status": "UNAVAILABLE",
            "message": "busy"
        }})


def _scripted_pool(labels: list[str], script: list[str]) -> _FakeGooglePool:
    outcomes = list(script)

    async def generate_content(*, model, contents, config):
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "503":
            raise _server_503()
        if outcome == "hang":
            await asyncio.sleep(3600)
        return _FakeGeminiResponse()

    return _single_client_pool(generate_content, labels)


def _headers(user_id: UUID) -> dict[str, str]:
    return bearer_headers(user_id)
