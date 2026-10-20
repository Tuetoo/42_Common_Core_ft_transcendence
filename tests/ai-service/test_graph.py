import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any
from uuid import UUID, uuid4
import ai.agents.debate_common as side_module
import ai.agents.judge as judge_module
import ai.graph.nodes as nodes_module
import ai.llm.router as router_module
import ai.memory.agent_memory as memory_module
import config
import pytest
from ai.agents.reflection import ReflectionAgent
from ai.graph.builder import (build_analysis_graph, route_after_news,
                              route_after_reflection,
                              should_continue_debate_round,
                              should_continue_to_debate)
from ai.graph.nodes import (_run_agent_safely, bear_node, bull_node,
                            debate_join_node, episodic_memory_node, judge_node,
                            recommendation_node, reflection_node,
                            select_agents)
from ai.graph.state import (MAX_DEBATE_ROUNDS, AgentOutput, AnalysisState,
                            DebateArgument, DebateState, EpisodicMemoryHit,
                            JudgeVerdict, ReflectionVerdict,
                            merge_debate_state, replace_reflection_feedback)
from ai.llm.base import (LLMResponse, LLMServerOverloadedError, LLMTier,
                         LLMUsage)
from alembic import command
from helpers import _ALL_ROLES, _BUY, _P, _AlwaysFailsRouter, _RaisingRouter
from helpers import _args, _ok
from helpers import _state as _base_state
from helpers import _StubGraph, _tool_entry, _verdict
from langgraph.graph import END
from sqlalchemy import func, select, text, update
from structlog.testing import capture_logs
from domains.analysis.model import Agent
from domains.analysis.repository import (AnalysisPersistenceRepository,
                                         AnalysisRepository)
from domains.memory.model import AgentMemory, EpisodicMemory
from domains.memory.repository import (AgentMemoryRepository,
                                       EpisodicMemoryRepository)
from shared.enums import (AgentRole, AgentStatus, AnalysisFailureReason,
                          AnalysisType, RecommendationAction, TierStatus)
from tests.alembic_contract import (alembic_config, scratch,  # noqa: F401
                                    upgrade_to)

_S = TierStatus.SUPERSEDED.value
_NINE = list(_ALL_ROLES)
_T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def no_memory_io(monkeypatch):

    async def _empty_episodic_context(ticker, *, user_id, limit=None):
        return []

    async def _no_procedural_hints(role, ticker, *, user_id, limit=2):
        return None

    monkeypatch.setattr(
        memory_module, "retrieve_episodic_context", _empty_episodic_context)
    monkeypatch.setattr(
        memory_module, "retrieve_procedural_hints", _no_procedural_hints)


@pytest.fixture
def alembic_schema() -> str:
    return "ai"


_state = partial(_base_state, ticker="TSLA")
_amd = partial(_base_state, ticker="AMD")


def _nine_state(**overrides: Any) -> AnalysisState:
    values: dict[str, Any] = {"ticker": "MSFT", "selected_agents": list(_NINE)}
    values.update(overrides)
    return _state(**values)


def _priced(
        role: AgentRole, summary: str | None = None,
        completed_min: int | None = None) -> AgentOutput:
    timing: dict[str, Any] = {}
    if completed_min is not None:
        done = _T0 + timedelta(minutes=completed_min)
        timing = {
            "started_at": done - timedelta(seconds=30),
            "completed_at": done
        }
    return AgentOutput(
        role=role, summary=summary or f"{role.value} closed at 106.15",
        score=0.9, raw_output={
            "scratchpad": [{
                "thought": "t",
                "action": "get_price_history",
                "action_input": {},
                "observation": "Close 106.15",
                "is_tool_result": True,
                "tool_error": False
            }]
        }, **timing)


def _failed(
        role: AgentRole, kind: str | None = "server_overload") -> AgentOutput:
    return AgentOutput(
        role=role, status=AgentStatus.FAILED,
        summary=f"{role.value} agent failed: x", score=0.0,
        is_well_supported=False, failure_kind=kind)


def _outputs(usable: int) -> dict[AgentRole, AgentOutput]:
    return {
        role: (_priced(role) if i < usable else _failed(role))
        for i, role in enumerate(_NINE)
    }


def _is_debate(route: Any) -> bool:
    if not isinstance(route, list):
        return False
    return [s.node for s in route] == ["bull_node", "bear_node"]


class _EchoRouter:

    async def complete(self, tier, messages, **kwargs):
        return LLMResponse(
            content=messages[-1].content, provider="fake", model="m",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _ReplyRouter:

    def __init__(self, *contents: str, repeat: bool = False):
        self._contents = list(contents)
        self._repeat = repeat
        self.calls = 0

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        if self._repeat and len(self._contents) == 1:
            content = self._contents[0]
        else:
            content = self._contents.pop(0)
        return LLMResponse(
            content=content, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _JudgeAfterFailures:

    def __init__(self, failures: list[Exception]):
        self.failures = list(failures)
        self.calls = 0

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return LLMResponse(
            content=(
                '{"converged": true, "leaning": "buy", "confidence": 0.8, '
                '"rationale": "ok"}'), provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _Sleeps:

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.mark.usefixtures("no_memory_io")
class TestDebateRouting:

    def test_agent_choice_debate_entry_round_cap_and_judge_failure(self):
        selected, skipped = select_agents(
            requested_agents=[AgentRole.MARKET, AgentRole.RISK],
            analysis_type=AnalysisType.FULL, holdings_count=5)
        assert AgentRole.PORTFOLIO not in selected
        assert any(s.role == AgentRole.PORTFOLIO for s in skipped)

        state = _state(agent_outputs={})
        assert should_continue_to_debate(state) == "reflection_node"

        state = _state(selected_agents=[AgentRole.MARKET, AgentRole.NEWS])
        assert route_after_news(state) == "agent_join_node"

        assert MAX_DEBATE_ROUNDS == 3
        state = _state(
            debate=DebateState(
                round_number=5, mutual_rounds_completed=2, verdicts=[
                    JudgeVerdict(
                        round_number=5, converged=False,
                        leaning=RecommendationAction.HOLD, confidence=0.5,
                        rationale="still unsure")
                ]))
        route = should_continue_debate_round(state)
        assert isinstance(route, list)
        assert {send.node for send in route} == {"bull_node", "bear_node"}

        failed = JudgeVerdict(
            round_number=1, converged=False, rationale="503",
            leaning=RecommendationAction.HOLD, confidence=0.0,
            judge_failed=True, failure_reason="judge_unavailable")
        state = _amd(
            debate=DebateState(round_number=1, verdicts=[failed]),
            pending_retry_agents=[AgentRole.NEWS])
        assert route_after_reflection(state) == "recommendation_node"


def _retry_state(
        market: AgentOutput, pending: list[AgentRole]) -> AnalysisState:
    outputs = _outputs(9)
    outputs[AgentRole.MARKET] = market
    verdict = JudgeVerdict(
        round_number=2, converged=True, leaning=RecommendationAction.HOLD,
        confidence=0.7, rationale="r")
    return _nine_state(
        agent_outputs=outputs, pending_retry_agents=pending,
        debate=DebateState(
            arguments=[
                DebateArgument(
                    round_number=1, side="bull", argument="bull case"),
                DebateArgument(
                    round_number=1, side="bear", argument="bear case")
            ], round_number=2, verdicts=[verdict]))


@pytest.mark.usefixtures("clean_pacer")
class TestRetryRouting:

    def test_retry_outcome_and_usable_agent_gate(self, monkeypatch):
        state = _retry_state(
            _failed(AgentRole.MARKET, "server_overload"), [AgentRole.MARKET])
        assert should_continue_to_debate(state) == "recommendation_node"

        changed = {
            "retried-agent-recovered": (
                _priced(AgentRole.MARKET), [AgentRole.MARKET]),
            "retried-agent-failed-differently": (
                _failed(AgentRole.MARKET, "other"), [AgentRole.MARKET]),
            "one-of-two-retried-agents-recovered": (
                _failed(AgentRole.MARKET,
                        "server_overload"), [AgentRole.MARKET, AgentRole.NEWS])
        }
        for case_id, (market, pending) in changed.items():
            state = _retry_state(market, pending)
            assert _is_debate(should_continue_to_debate(state)), case_id

        state = _nine_state(agent_outputs=_outputs(3))
        assert state.usable_agent_count() == 3
        assert should_continue_to_debate(state) == "reflection_node"
        monkeypatch.setattr(config.settings, "MIN_USABLE_AGENTS_FOR_DEBATE", 0)
        assert _is_debate(
            should_continue_to_debate(_nine_state(agent_outputs=_outputs(2))))


def _timed_outputs(*, news_min: int, sentiment_min: int,
                   market_min: int = 0) -> dict[AgentRole, AgentOutput]:
    outputs = {role: _priced(role, completed_min=0) for role in _NINE}
    outputs[AgentRole.NEWS] = _priced(AgentRole.NEWS, completed_min=news_min)
    outputs[AgentRole.SENTIMENT] = _priced(
        AgentRole.SENTIMENT, completed_min=sentiment_min)
    outputs[AgentRole.MARKET] = _priced(
        AgentRole.MARKET, completed_min=market_min)
    return outputs


class TestNewsAndSentimentRetryOrder:

    def test_sentiment_reruns_after_news_before_the_join(self):
        state = _nine_state(
            pending_retry_agents=[AgentRole.NEWS, AgentRole.MARKET],
            agent_outputs=_timed_outputs(
                news_min=10, sentiment_min=1, market_min=10))
        assert should_continue_to_debate(state) == END
        selected = [r for r in _NINE if r != AgentRole.SENTIMENT]
        outputs = _timed_outputs(news_min=10, sentiment_min=1)
        outputs.pop(AgentRole.SENTIMENT)
        unselected = _nine_state(
            selected_agents=selected,
            pending_retry_agents=[AgentRole.NEWS,
                                  AgentRole.MARKET], agent_outputs=outputs)
        assert _is_debate(should_continue_to_debate(unselected))

        pending = [AgentRole.NEWS, AgentRole.SENTIMENT, AgentRole.MARKET]
        outputs = {role: _priced(role, completed_min=0) for role in _NINE}
        state = _nine_state(
            pending_retry_agents=pending, agent_outputs=outputs)
        sent = route_after_reflection(state)
        assert isinstance(sent, list)
        assert {send.node for send in sent} == {"news_node", "market_node"}
        after_news = route_after_news(state)
        assert isinstance(after_news, list)
        assert [send.node for send in after_news] == ["sentiment_node"]

        outputs[AgentRole.NEWS] = _priced(AgentRole.NEWS, completed_min=10)
        outputs[AgentRole.MARKET] = _priced(AgentRole.MARKET, completed_min=9)
        stale = _nine_state(
            pending_retry_agents=pending, agent_outputs=outputs)
        assert should_continue_to_debate(stale) == END
        outputs[AgentRole.SENTIMENT] = _priced(
            AgentRole.SENTIMENT, completed_min=11)
        fresh = _nine_state(
            pending_retry_agents=pending, agent_outputs=outputs)
        assert _is_debate(should_continue_to_debate(fresh))

        state = _nine_state(pending_retry_agents=[AgentRole.SENTIMENT])
        sent = route_after_reflection(state)
        assert isinstance(sent, list)
        assert [send.node for send in sent] == ["sentiment_node"]


async def _timeout_wait(state: dict[str, Any], model: str) -> float:
    return float(state.get("timeouts", {}).get(model, 0.0))


def _set_free_model(monkeypatch, model: str | None) -> list[str]:
    asked: list[str] = []

    async def _free(tier):
        asked.append(str(tier))
        return model

    monkeypatch.setattr(router_module, "free_chain_model", _free)
    monkeypatch.setattr(nodes_module, "free_chain_model", _free)
    return asked


class TestRetryCapacityGate:

    @pytest.fixture
    def chain(self, monkeypatch):
        state: dict[str, Any] = {"exhausted": set(), "waits": {}}

        async def _exhausted(model):
            return model in state["exhausted"]

        async def _cooldown(model):
            return state["waits"].get(model, (0.0, "minute"))

        async def _server_wait(model):
            return 0.0

        async def _sleep(seconds):
            state.setdefault("slept", []).append(seconds)

        monkeypatch.setattr(config.settings, "LLM_DEFAULT_PROVIDER", "google")
        monkeypatch.setattr(
            nodes_module, "tier_model_chain",
            lambda tier: ["primary", "fallback"])
        monkeypatch.setattr(
            router_module, "google_model_pool_exhausted", _exhausted)
        monkeypatch.setattr(router_module, "google_pool_cooldown", _cooldown)
        monkeypatch.setattr(
            router_module, "google_server_error_wait", _server_wait)
        monkeypatch.setattr(
            router_module, "google_model_timeout_wait",
            lambda model: _timeout_wait(state, model))
        monkeypatch.setattr(nodes_module.asyncio, "sleep", _sleep)
        return state

    @pytest.mark.asyncio
    async def test_day_quota_waits_and_timeouts_gate_the_retry(
            self, chain, monkeypatch):
        chain["exhausted"] = {"primary"}
        retry = {AgentRole.BEAR: "fix figure", AgentRole.NEWS: "fix"}
        gated, notes = await nodes_module._rate_limit_gate_for_retry(retry)
        assert gated == retry and notes == []

        chain["exhausted"] = {"primary", "fallback"}
        gated, notes = await nodes_module._rate_limit_gate_for_retry(
            {AgentRole.BEAR: "fix figure"})
        assert gated == {}
        assert "primary, fallback" in notes[0].text

        chain["exhausted"] = set()
        chain["waits"] = {"primary": (40.0, "minute")}
        gated, notes = await nodes_module._rate_limit_gate_for_retry(
            {AgentRole.NEWS: "fix"})
        assert notes == [] and "slept" not in chain
        chain["waits"]["fallback"] = (5.0, "minute")
        gated, notes = await nodes_module._rate_limit_gate_for_retry(
            {AgentRole.NEWS: "fix"})
        assert gated == {AgentRole.NEWS: "fix"}
        assert chain["slept"] == [5.0] and len(notes) == 1

        chain["waits"] = {}
        chain.pop("slept")
        chain["timeouts"] = {"primary": 200.0, "fallback": 30.0}
        gated, notes = await nodes_module._rate_limit_gate_for_retry(
            {AgentRole.BEAR: "fix"})
        assert gated == {AgentRole.BEAR: "fix"}
        assert chain["slept"] == [20.0]
        assert "fallback sent no response within the request timeout" in (
            notes[0].text)
        monkeypatch.setattr(
            router_module, "tier_model_chain",
            lambda tier: ["primary", "fallback"])
        assert await router_module.free_chain_model(
            LLMTier.HIGHEST_ACCURACY) is None
        assert await router_module.capacity_aware_delay(
            LLMTier.HIGHEST_ACCURACY, 60.0) == (60.0, None)
        chain["timeouts"] = {"primary": 200.0}
        assert await router_module.free_chain_model(
            LLMTier.HIGHEST_ACCURACY) == "fallback"
        assert await router_module.capacity_aware_delay(
            LLMTier.HIGHEST_ACCURACY, 60.0) == (0.0, "fallback")
        monkeypatch.setattr(
            config.settings, "LLM_DEFAULT_PROVIDER", "anthropic")
        assert await router_module.capacity_aware_delay(
            LLMTier.HIGHEST_ACCURACY, 60.0) == (60.0, None)


class TestSupervisorEnrichment:

    @pytest.mark.asyncio
    async def test_enrichment_failures_give_no_context(self, monkeypatch):
        import ai.knowledge.kg_context as kg_module
        import clients.core_client as core_client

        async def _boom(*args, **kwargs):
            raise RuntimeError("down")

        monkeypatch.setattr(memory_module, "retrieve_procedural_hints", _boom)
        monkeypatch.setattr(nodes_module, "ingest_new_filings", _boom)
        monkeypatch.setattr(kg_module, "get_kg_context_for_ticker", _boom)
        monkeypatch.setattr(core_client, "get_portfolio_holdings", _boom)
        state = _nine_state(portfolio_id="p1")
        update = await nodes_module.supervisor_node(state)
        assert update["selected_agents"]
        assert update["procedural_hints"] == {}
        assert update["fresh_filing_context"] is None
        assert update["kg_context"] is None
        assert update["portfolio_kg_context"] is None

    @pytest.mark.asyncio
    async def test_slow_fresh_filing_ingest_times_out_and_keeps_running(
            self, monkeypatch):
        import ai.ingestion.sec_edgar_filings as filings_module

        release = asyncio.Event()
        runs: list[str] = []

        async def _slow_ingest(ticker):
            runs.append(ticker)
            await release.wait()
            return filings_module.FilingIngestResult(new_filings=[])

        monkeypatch.setattr(
            filings_module, "_ingest_new_filings", _slow_ingest)
        monkeypatch.setattr(nodes_module, "FRESH_FILING_BUDGET_SECONDS", 0.01)
        with capture_logs() as logs:
            assert await nodes_module.load_fresh_filing_context("SLOW") is None
        assert "fresh_filing_check.timed_out" in [e["event"] for e in logs]
        task = filings_module._ingests_in_flight["SLOW"]
        assert not task.done()

        joined = asyncio.create_task(filings_module.ingest_new_filings("SLOW"))
        await asyncio.sleep(0)
        release.set()
        assert (await joined).new_filings == []
        assert runs == ["SLOW"]
        assert "SLOW" not in filings_module._ingests_in_flight


def _patch_debate_sides(monkeypatch, bull_router, bear_router) -> _Sleeps:
    routers = {"bull": bull_router, "bear": bear_router}
    monkeypatch.setattr(
        nodes_module, "DebateSideAgent",
        lambda side: side_module.DebateSideAgent(
            side, llm_router=routers[side]))
    sleeps = _Sleeps()
    monkeypatch.setattr(nodes_module.asyncio, "sleep", sleeps)
    return sleeps


class TestDebateNodes:

    @pytest.mark.usefixtures("no_memory_io", "clean_pacer")
    @pytest.mark.asyncio
    async def test_rounds_answer_the_other_side_and_one_side_is_repaired(
            self, monkeypatch):
        monkeypatch.setattr(side_module, "LLMRouter", lambda: _EchoRouter())
        round1 = DebateState(
            round_number=1, arguments=[{
                "round_number": 1,
                "side": "bull",
                "argument": "bull round 1"
            }, {
                "round_number": 1,
                "side": "bear",
                "argument": "bear round 1"
            }])
        state = _state(debate=round1)
        bull_update = await bull_node(state)
        bear_update = await bear_node(state)
        assert "bear round 1" in bull_update["debate"].arguments[0].argument
        assert "bull round 1" in bear_update["debate"].arguments[0].argument

        _patch_debate_sides(
            monkeypatch, _ReplyRouter("Bull case.", repeat=True),
            _RaisingRouter(LLMServerOverloadedError("503")))
        args = _args(bull_ok=False, bear_ok=False)
        state = _state(
            ticker="DIS", agent_outputs={
                AgentRole.MARKET: _priced(
                    AgentRole.MARKET, "DIS closed at 106.15")
            }, debate=DebateState(arguments=args))
        update = await debate_join_node(state)
        assert [a.side for a in update["debate"].arguments] == ["bull"]
        merged = merge_debate_state(state.debate, update["debate"])
        assert merged.unavailable_latest_sides() == ["bear"]

    @pytest.mark.asyncio
    async def test_failed_correction_and_side_repair_wait(self, monkeypatch):
        _patch_debate_sides(
            monkeypatch, _RaisingRouter(LLMServerOverloadedError("503")),
            _ReplyRouter("Bear case.", repeat=True))
        state = _state(debate=DebateState(arguments=_args()))
        correction = state.model_copy(
            update={"pending_retry_agents": [AgentRole.BULL]})
        with capture_logs() as logs:
            assert await bull_node(correction) == {}
        assert "bull_node.correction_failed_kept_previous" in [
            entry["event"] for entry in logs
        ]
        update = await bull_node(state)
        assert update["debate"].arguments[0].available is False

        monkeypatch.setattr(
            config.settings, "DEBATE_SIDE_REPAIR_DELAY_SECONDS", 60.0)
        cases = {
            "no-model-free-keeps-wait": (None, [60.0]),
            "free-model-skips-wait": ("gemini-3.6-flash", [])
        }
        for case_id, (free, slept) in cases.items():
            sleeps = _patch_debate_sides(
                monkeypatch, _ReplyRouter("Bull case.", repeat=True), None)
            _set_free_model(monkeypatch, free)
            state = _amd(debate=DebateState(arguments=_args(bull_ok=False)))
            with capture_logs() as logs:
                update = await debate_join_node(state)
            assert sleeps.delays == slept, case_id
            sides = [a.side for a in update["debate"].arguments]
            assert sides == ["bull"], case_id
            retry = next(
                e for e in logs
                if e["event"] == "debate_join_node.side_repair_retry")
            assert retry["delay_seconds"] == (
                slept[0] if slept else 0.0), case_id
            assert retry["free_model"] == free, case_id

        _patch_debate_sides(
            monkeypatch, _RaisingRouter(LLMServerOverloadedError("503")), None)
        state = _amd(debate=DebateState(arguments=_args(bull_ok=False)))
        update = await debate_join_node(state)
        assert update == {"debate_side_repair_used": True}

        sleeps = _patch_debate_sides(
            monkeypatch, _ReplyRouter("x", repeat=True), None)
        used = _amd(
            debate=DebateState(arguments=_args(bull_ok=False)),
            debate_side_repair_used=True)
        later = _amd(
            debate=DebateState(
                arguments=_args(
                    bull_ok=False), round_number=1, verdicts=[_verdict()]))
        non_english = _args(bull_ok=False)
        non_english[0] = non_english[0].model_copy(
            update={"unavailable_kind": "non_english"})
        lang = _amd(debate=DebateState(arguments=non_english))
        for state in (used, later, lang):
            assert await debate_join_node(state) == {}
        assert sleeps.delays == []


def _judge_with_failures(monkeypatch, failures: list[Exception]):
    router = _JudgeAfterFailures(failures)
    monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
    sleeps = _Sleeps()
    monkeypatch.setattr(nodes_module.asyncio, "sleep", sleeps)
    state = _amd(
        agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
        debate=DebateState(arguments=_args()))
    return router, sleeps, state


class TestJudgeNode:

    @pytest.mark.usefixtures("no_memory_io")
    @pytest.mark.asyncio
    async def test_failed_verdict_stops_and_correction_keeps_mutual_rounds(
            self, monkeypatch):
        monkeypatch.setattr(
            config.settings, "JUDGE_RETRY_BUDGET_SECONDS", 0.0)
        cases = {
            "config-error": (_AlwaysFailsRouter(), _state()),
            "server-overloaded": (
                _RaisingRouter(LLMServerOverloadedError("503")),
                _amd(
                    agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
                    debate=DebateState(arguments=_args())))
        }
        for case_id, (router, state) in cases.items():
            monkeypatch.setattr(
                judge_module, "LLMRouter", lambda router=router: router)
            update = await judge_node(state)
            verdict = update["debate"].verdicts[0]
            assert update["debate"].converged is False, case_id
            assert verdict.confidence == 0.0, case_id
            assert verdict.judge_failed is True, case_id
            assert verdict.failure_reason == "judge_unavailable", case_id
            after = state.model_copy(update={"debate": update["debate"]})
            assert should_continue_debate_round(after) == (
                "reflection_node"), case_id

        monkeypatch.setattr(judge_module, "LLMRouter", lambda: _EchoRouter())
        state = _state(
            debate=DebateState(round_number=1, mutual_rounds_completed=1),
            pending_retry_agents=[AgentRole.BULL])
        update = await judge_node(state)
        assert update["debate"].mutual_rounds_completed == 1
        assert update["debate"].round_number == 2
        assert update["pending_retry_agents"] == []

    @pytest.mark.asyncio
    async def test_retries_wait_for_the_chain_cooldown_within_a_budget(
            self, monkeypatch):
        cap = config.settings.JUDGE_RETRY_MAX_ATTEMPTS
        cases = {
            "waits-for-cooldown": (1, (30.0, "minute", "m"), [30.0], 1, False),
            "model-free-retries-now": (1, None, [], 1, False),
            "budget-ends-retries": (9, (100.0, "minute", "m"),
                                    [100.0, 100.0, 40.0], 3, True),
            "attempt-cap": (9, None, [], cap, True),
            "day-quota-no-retry": (1, (0.0, "day", "m"), [], 0, True)
        }
        for case_id, case in cases.items():
            failures, capacity, waits, attempts, failed = case
            router, sleeps, state = _judge_with_failures(
                monkeypatch,
                [LLMServerOverloadedError("503") for _ in range(failures)])

            async def _capacity(chain, capacity=capacity):
                return capacity

            monkeypatch.setattr(nodes_module, "chain_capacity", _capacity)
            update = await judge_node(state)
            assert update["debate"].verdicts[0].judge_failed is failed, (
                case_id)
            assert sleeps.delays == waits, case_id
            assert router.calls == attempts + 1, case_id
            assert update["judge_retry_attempts"] == attempts, case_id
            assert update["judge_retry_seconds"] == sum(waits), case_id

        router, sleeps, state = _judge_with_failures(
            monkeypatch, [LLMServerOverloadedError("503")])
        spent = config.settings.JUDGE_RETRY_BUDGET_SECONDS
        state = state.model_copy(update={"judge_retry_seconds": spent})
        update = await judge_node(state)
        assert update["debate"].verdicts[0].judge_failed
        assert router.calls == 1 and sleeps.delays == []


_JUDGE_JSON = (
    '{"converged": true, "leaning": "hold", "confidence": 0.7, '
    '"rationale": "ok"}')


class _SeesBothSidesJudge:

    def __init__(self, failures: list[Exception]):
        self.failures = list(failures)
        self.user_texts: list[str] = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        if self.failures:
            raise self.failures.pop(0)
        self.user_texts.append(messages[-1].content)
        return LLMResponse(
            content=_JUDGE_JSON, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


def _missing_bull_after_repair(monkeypatch, judge_failures, side_router):
    judge = _SeesBothSidesJudge(judge_failures)
    monkeypatch.setattr(judge_module, "LLMRouter", lambda: judge)
    sleeps = _patch_debate_sides(monkeypatch, side_router, None)
    state = _amd(
        agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
        debate=DebateState(arguments=_args(bull_ok=False)),
        debate_side_repair_used=True)
    return judge, sleeps, state


class TestLateSideRepair:

    @pytest.mark.asyncio
    async def test_missing_side_regenerated_after_the_judge_wait(
            self, monkeypatch):
        judge, sleeps, state = _missing_bull_after_repair(
            monkeypatch, [LLMServerOverloadedError("503")],
            _ReplyRouter("Late bull case.", repeat=True))
        free = {"model": None}

        async def _free(tier):
            return free["model"]

        async def _capacity(chain):
            return None if free["model"] else (45.0, "minute", "m")

        async def _sleep(seconds):
            sleeps.delays.append(seconds)
            free["model"] = "gemini-3.6-flash"

        monkeypatch.setattr(nodes_module, "free_chain_model", _free)
        monkeypatch.setattr(nodes_module, "chain_capacity", _capacity)
        monkeypatch.setattr(nodes_module.asyncio, "sleep", _sleep)
        with capture_logs() as logs:
            update = await judge_node(state)
        assert sleeps.delays == [45.0]
        assert [a.argument
                for a in update["debate"].arguments] == ["Late bull case."]
        assert update["debate_late_side_repair_used"] is True
        assert "Late bull case." in judge.user_texts[-1]
        assert "Bull argument: (UNAVAILABLE" not in judge.user_texts[-1]
        merged = merge_debate_state(state.debate, update["debate"])
        assert merged.unavailable_latest_sides() == []
        events = [e["event"] for e in logs]
        assert events.count("judge_node.late_side_repair") == 1
        assert "judge_node.late_side_repaired" in events

    @pytest.mark.asyncio
    async def test_late_repair_is_skipped_or_spent_once(self, monkeypatch):
        judge, _, state = _missing_bull_after_repair(
            monkeypatch, [], _ReplyRouter("never used", repeat=True))
        _set_free_model(monkeypatch, None)
        update = await judge_node(state)
        assert update["debate"].arguments == []
        assert update["debate_late_side_repair_used"] is False
        assert "Bull argument: (UNAVAILABLE" in judge.user_texts[-1]

        side_router = _RaisingRouter(LLMServerOverloadedError("503"))
        judge, _, state = _missing_bull_after_repair(
            monkeypatch, [], side_router)
        _set_free_model(monkeypatch, "gemini-3.6-flash")
        update = await judge_node(state)
        assert update["debate"].arguments == []
        assert update["debate_late_side_repair_used"] is True
        calls = side_router.calls
        used = state.model_copy(update={"debate_late_side_repair_used": True})
        update = await judge_node(used)
        assert side_router.calls == calls
        assert update["debate_late_side_repair_used"] is True

        judge, _, state = _missing_bull_after_repair(
            monkeypatch, [], _ReplyRouter("x", repeat=True))
        asked = _set_free_model(monkeypatch, "gemini-3.6-flash")
        state = state.model_copy(
            update={"pending_retry_agents": [AgentRole.BEAR]})
        update = await judge_node(state)
        assert asked == [] and update["debate"].arguments == []


def _bull_correction(round_number: int, issue: str, reason: str):

    async def _fake_check(self, state):
        return ReflectionVerdict(
            round_number=round_number, verified=False, issues=[issue],
            retry_agents={AgentRole.BULL: reason})

    return _fake_check


class TestReflectionNode:

    @pytest.mark.usefixtures("no_memory_io")
    @pytest.mark.asyncio
    async def test_retry_need_correction_per_round_crash_and_feedback(
            self, monkeypatch):
        state = _state(
            agent_outputs={
                AgentRole.MARKET: AgentOutput(
                    role=AgentRole.MARKET, summary="ok", score=0.9,
                    is_well_supported=True)
            })
        update = await reflection_node(state)
        assert update["pending_retry_agents"] == []
        assert (
            route_after_reflection(
                state.model_copy(update=update)) == "recommendation_node")

        monkeypatch.setattr(
            ReflectionAgent, "check",
            _bull_correction(
                1, "Bull cited a figure not in evidence",
                "unsupported figure"))
        state = _state(debate=DebateState(mutual_rounds_completed=1))
        update = await reflection_node(state)
        assert update["pending_retry_agents"] == [AgentRole.BULL]
        assert update["debate"].correction_used_for_round == 1

        monkeypatch.setattr(
            ReflectionAgent, "check",
            _bull_correction(
                2, "Bull cited another unsupported figure",
                "still unsupported"))
        real_gate = nodes_module._rate_limit_gate_for_retry
        gated: list[dict[AgentRole, str]] = []

        async def _spy_gate(retry_now):
            gated.append(dict(retry_now))
            return retry_now, []

        monkeypatch.setattr(
            nodes_module, "_rate_limit_gate_for_retry", _spy_gate)
        state = _state(
            debate=DebateState(
                mutual_rounds_completed=1, correction_used_for_round=1))
        update = await reflection_node(state)
        assert update["pending_retry_agents"] == []
        assert "debate" not in update
        assert gated == []
        monkeypatch.setattr(
            nodes_module, "_rate_limit_gate_for_retry", real_gate)

        async def _boom(self, state):
            raise RuntimeError("bug")

        monkeypatch.setattr(ReflectionAgent, "check", _boom)
        state = _amd()
        update = await reflection_node(state)
        verdict = update["reflection_history"][0]
        assert verdict.verified is False
        assert verdict.usable_agent_count == state.usable_agent_count()

        async def _check(self, state):
            return ReflectionVerdict(
                round_number=2, verified=False,
                retry_agents={AgentRole.NEWS: "fix the $1 billion figure"})

        async def _no_gate(retry_now):
            return retry_now, []

        monkeypatch.setattr(nodes_module.ReflectionAgent, "check", _check)
        monkeypatch.setattr(
            nodes_module, "_rate_limit_gate_for_retry", _no_gate)
        state = _nine_state(
            reflection_feedback={
                AgentRole.SENTIMENT: "old sentiment feedback"
            }, agent_outputs={
                role: _priced(role, completed_min=0)
                for role in _NINE
            })
        update = await reflection_node(state)
        after = replace_reflection_feedback(
            state.reflection_feedback, update["reflection_feedback"])
        assert after == {AgentRole.NEWS: "fix the $1 billion figure"}

        class _Boom:

            async def run(self, state):
                raise RuntimeError("x")

        monkeypatch.setattr(nodes_module, "_FAN_OUT_STAGGER_ORDER", ())
        out = await _run_agent_safely(
            AgentRole.SENTIMENT,
            _Boom(),  # type: ignore[arg-type]
            state.model_copy(update={"reflection_feedback": after}))
        assert out.retry_of_feedback is None


@pytest.mark.usefixtures("no_memory_io")
@pytest.mark.asyncio
async def test_recommendation_node_returns_a_json_serializable_dict():
    debate = DebateState(
        round_number=1, converged=True, verdicts=[
            JudgeVerdict(
                round_number=1, converged=True,
                leaning=RecommendationAction.SELL, confidence=0.4,
                rationale="weak fundamentals")
        ])
    update = await recommendation_node(_state(debate=debate))
    final = update["final_recommendation"]
    assert final["action"] == "sell"
    assert final["confidence"] == pytest.approx(0.4)
    assert update.get("no_verdict_reason") is None


@pytest.mark.usefixtures("no_memory_io")
@pytest.mark.asyncio
async def test_failed_later_judge_pass_keeps_earlier_valid_verdict():
    valid = JudgeVerdict(
        round_number=1, converged=True, leaning=RecommendationAction.SELL,
        confidence=0.62, rationale="weak fundamentals")
    failed = JudgeVerdict(
        round_number=2, converged=False, leaning=RecommendationAction.HOLD,
        confidence=0.0, rationale="Judge unavailable: 529",
        judge_failed=True, failure_reason="judge_unavailable")
    update = await recommendation_node(
        _state(
            debate=DebateState(round_number=2, verdicts=[valid, failed])))
    final = update["final_recommendation"]
    assert update.get("no_verdict_reason") is None
    assert final["action"] == "sell"
    assert final["reasoning"] == "weak fundamentals"
    assert ("last Judge pass failed (judge unavailable)"
            in final["confidence_adjustment_note"])
    assert "earlier valid verdict from round 1" in (
        final["confidence_adjustment_note"])

    only_failed = await recommendation_node(
        _state(debate=DebateState(round_number=2, verdicts=[failed])))
    assert only_failed["final_recommendation"] is None
    assert only_failed["no_verdict_reason"] == "judge_unavailable"


def _hit(
        action: str, confidence: float, days_ago: int,
        outcome: str | None = None,
        outcome_return: float | None = None) -> EpisodicMemoryHit:
    return EpisodicMemoryHit(
        analysis_id=str(uuid4()), ticker="AMD",
        analyzed_at=datetime(2026, 9, 29, tzinfo=UTC) -
        timedelta(days=days_ago), recommendation=RecommendationAction(action),
        confidence=confidence, outcome=outcome, outcome_return=outcome_return)


async def _episode(
        db_session, *, user_id, ticker: str, action: str,
        minutes_ago: int) -> EpisodicMemory:
    analysis = await AnalysisRepository(db_session).create(
        user_id=user_id, portfolio_id=None, ticker=ticker)
    memory = await EpisodicMemoryRepository(db_session).create(
        user_id=user_id, analysis_id=analysis.id, ticker=ticker,
        recommendation=action, confidence=0.6, summary="secret rationale")
    await db_session.execute(
        update(EpisodicMemory).where(EpisodicMemory.id == memory.id).values(
            created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago)))
    return memory


def _fake_session_local(session: Any) -> Any:

    @asynccontextmanager
    async def _factory() -> AsyncIterator[Any]:
        yield session

    return _factory


async def _recommendation_columns(engine: Any, schema: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'recommendations'"),
            {"s": schema})
        return {r[0] for r in rows}


class TestEpisodicMemory:

    @pytest.mark.asyncio
    async def test_node_passes_the_user_or_returns_empty_context(
            self, monkeypatch):
        seen: dict[str, Any] = {}

        async def _fake(ticker, *, user_id, limit=None):
            seen.update(ticker=ticker, user_id=user_id)
            return [_hit("buy", 0.7, 3)]

        monkeypatch.setattr(
            memory_module, "retrieve_episodic_context", _fake)
        state = _amd(user_id=str(uuid4()))
        result = await episodic_memory_node(state)
        assert seen == {"ticker": "AMD", "user_id": state.user_id}
        assert len(result["episodic_context"]) == 1

        cases = {
            "database-down": (
                ConnectionError("database is down"), None, _nine_state()),
            "limit-zero": (
                AssertionError("must not be called"), 0,
                _amd(user_id=str(uuid4())))
        }
        for case_id, (error, limit, state) in cases.items():

            async def _raise(ticker, *, user_id, limit=None, error=error):
                raise error

            monkeypatch.setattr(
                memory_module, "retrieve_episodic_context", _raise)
            if limit is not None:
                monkeypatch.setattr(
                    config.settings, "EPISODIC_CONTEXT_LIMIT", limit)
            assert await episodic_memory_node(state) == {
                "episodic_context": []
            }, case_id

    @pytest.mark.asyncio
    async def test_repositories_keep_owners_apart(self, db_session):
        me, other = uuid4(), uuid4()
        ticker = "EPI" + uuid4().hex[:4].upper()
        mine = await _episode(
            db_session, user_id=me, ticker=ticker, action="buy",
            minutes_ago=10)
        system = await _episode(
            db_session, user_id=None, ticker=ticker, action="hold",
            minutes_ago=5)
        await _episode(
            db_session, user_id=other, ticker=ticker, action="sell",
            minutes_ago=1)
        rows = await EpisodicMemoryRepository(
            db_session).list_recent_for_ticker(ticker, user_id=me, limit=5)
        assert [r.id for r in rows] == [system.id, mine.id]

        agent = Agent(name="proc-" + uuid4().hex[:8])
        db_session.add(agent)
        await db_session.flush()
        repo = AgentMemoryRepository(db_session)

        async def _row(user_id, ticker):
            return await repo.create(
                agent_id=agent.id, user_id=user_id, ticker=ticker, score=0.9,
                content={"steps": []})

        mine = await _row(me, "AAPL")
        mine_other_ticker = await _row(me, "MSFT")
        await _row(other, "AAPL")
        await _row(other, "MSFT")
        ownerless = await _row(None, "AAPL")

        rows = await repo.recent_qualifying_for_agent(
            agent.id, user_id=me, ticker="AAPL", limit=10)
        assert [r.id for r in rows] == [mine.id, mine_other_ticker.id]
        rows = await repo.recent_qualifying_for_agent(
            agent.id, user_id=None, ticker="MSFT", limit=10)
        assert [r.id for r in rows] == [ownerless.id]

        for _ in range(3):
            await _row(other, "AAPL")
        await repo.prune_beyond_retention(agent.id, "AAPL", user_id=other)
        rows = await repo.recent_qualifying_for_agent(
            agent.id, user_id=me, ticker="AAPL", limit=1)
        assert [r.id for r in rows] == [mine.id]
        stored = await db_session.execute(
            select(AgentMemory.user_id, func.count()).where(
                AgentMemory.agent_id == agent.id,
                AgentMemory.ticker == "AAPL").group_by(AgentMemory.user_id))
        counts = {row[0]: row[1] for row in stored}
        assert counts == {me: 1, other: 3, None: 1}
        assert AgentMemory.__table__.c.user_id.nullable

    @pytest.mark.asyncio
    async def test_hits_carry_the_raw_judge_verdict_and_hit_line(
            self, db_session, monkeypatch):
        ticker = "EPG" + uuid4().hex[:4].upper()
        persist = AnalysisPersistenceRepository(db_session)
        episodes = EpisodicMemoryRepository(db_session)
        with_raw = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=None, ticker=ticker)
        await persist.save_recommendation(
            analysis_id=with_raw.id, recommendation="hold", confidence=0.4,
            reasoning="r", confidence_adjustment_note=None,
            debate_converged=True, judge_leaning="buy", judge_confidence=0.8)
        await episodes.create(
            user_id=None, analysis_id=with_raw.id, ticker=ticker,
            recommendation="hold", confidence=0.4, summary="s")
        without = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=None, ticker=ticker)
        await persist.save_recommendation(
            analysis_id=without.id, recommendation="sell", confidence=0.5,
            reasoning="r", confidence_adjustment_note=None,
            debate_converged=True)
        await episodes.create(
            user_id=None, analysis_id=without.id, ticker=ticker,
            recommendation="sell", confidence=0.5, summary="s")
        monkeypatch.setattr(
            memory_module, "SessionLocal", _fake_session_local(db_session))
        hits = await memory_module.retrieve_episodic_context(
            ticker, user_id=None, limit=5)
        by_id = {h.analysis_id: h for h in hits}
        raw = by_id[str(with_raw.id)]
        assert raw.recommendation == RecommendationAction.HOLD
        assert raw.confidence == pytest.approx(0.4)
        assert raw.judge_leaning == RecommendationAction.BUY
        assert raw.judge_confidence == pytest.approx(0.8)
        old = by_id[str(without.id)]
        assert (old.judge_leaning, old.judge_confidence) == (None, None)

        line = _hit("hold", 0.4, 60, "correct").track_record_line(30)
        assert line.endswith("(confidence 0.40), call was correct")
        assert "summary" not in EpisodicMemoryHit.model_fields
        assert "reasoning" not in EpisodicMemoryHit.model_fields
        f = memory_module.stored_judge_verdict
        assert f("buy", 0.7) == (RecommendationAction.BUY, 0.7)
        assert f("buy", None) == (None, None)
        assert f(None, 0.7) == (None, None)
        assert f("strong_buy", 0.7) == (None, None)

    @pytest.mark.asyncio
    async def test_judge_and_owner_columns_migrate_up_and_down(
            self,
            scratch: Any  # noqa: F811
    ) -> None:
        schema, engine = scratch
        judge_columns = {"judge_leaning", "judge_confidence"}

        async def _has_owner() -> bool:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = :s "
                        "AND table_name = 'agent_memories' "
                        "AND column_name = 'user_id'"), {"s": schema})
                return rows.first() is not None

        alembic_cfg = alembic_config()  # type: ignore[no-untyped-call]
        await upgrade_to(  # type: ignore[no-untyped-call]
            engine, schema, "f1230a000001"
        )
        assert not judge_columns & await _recommendation_columns(
            engine, schema)
        await asyncio.to_thread(command.upgrade, alembic_cfg, "f1240a000001")
        assert judge_columns <= await _recommendation_columns(engine, schema)
        assert not await _has_owner()
        await asyncio.to_thread(command.upgrade, alembic_cfg, "f1250a000001")
        assert await _has_owner()
        await asyncio.to_thread(command.downgrade, alembic_cfg, "f1240a000001")
        assert not await _has_owner()
        await asyncio.to_thread(command.downgrade, alembic_cfg, "f1230a000001")
        assert not judge_columns & await _recommendation_columns(
            engine, schema)
        assert "confidence" in await _recommendation_columns(engine, schema)


@pytest.mark.usefixtures("_schema_ready", "no_memory_io")
@pytest.mark.asyncio
async def test_full_analysis_graph_runs_to_completion():
    graph = build_analysis_graph()
    state = _state(analysis_type=AnalysisType.FULL, holdings_count=3)
    result = await graph.ainvoke(state, config={"recursion_limit": 100})
    assert len(result["agent_outputs"]) == 9
    assert result["skipped_agents"] == []
    if result["final_recommendation"] is not None:
        assert result.get("no_verdict_reason") is None
    else:
        assert result["no_verdict_reason"] == "no_evidence"
        assert all(
            out.status.value == "failed"
            for out in result["agent_outputs"].values())


def _final_payload(action: str, confidence: float) -> dict[str, Any]:
    return {
        "ticker": "AMD",
        "action": action,
        "confidence": confidence,
        "reasoning": "r",
        "confidence_adjustment_note": None,
        "debate_converged": True
    }


class TestAnalysisWorkflow:

    @pytest.mark.asyncio
    async def test_prior_runs_are_counted_with_the_live_usable_rule(
            self, rows):
        from ai.workflows.analysis_workflow import _count_usable_persisted_runs
        from database import SessionLocal
        from domains.analysis.repository import AgentCatalogRepository

        ok = {
            "is_well_supported": True,
            "scratchpad": [_tool_entry("Close 1.0")]
        }
        runs = [("market", "completed", ok),
                (
                    "technical", "completed", {
                        "is_well_supported": False,
                        "scratchpad": []
                    }),
                (
                    "portfolio", "completed", {
                        "is_well_supported": True,
                        "scratchpad": []
                    }),
                (
                    "sentiment", "completed", {
                        "is_well_supported": True,
                        "scratchpad": []
                    }), ("macro", "failed", None),
                ("news", "completed", {
                    "scratchpad": []
                })]
        assert _count_usable_persisted_runs(runs) == 2
        analysis_id = await rows.analysis(
            ticker="MSFT", portfolio_id=None, status="completed")
        async with SessionLocal() as s:
            agent = await AgentCatalogRepository(s).get_or_create("market")
            await AnalysisPersistenceRepository(s).save_agent_run(
                analysis_id=analysis_id, agent_id=agent.id, status="completed",
                started_at=None, completed_at=None, duration_ms=None,
                score=0.9, summary="x", output={"is_well_supported": True})
            await s.commit()
            listed = await AnalysisRepository(s).list_agent_run_outputs(
                analysis_id)
        assert listed == [("market", "completed", {"is_well_supported": True})]

    @pytest.mark.asyncio
    async def test_all_failed_marks_failed_without_a_recommendation(
            self, rows, workflow_env, monkeypatch):
        from database import SessionLocal
        from domains.analysis.model import Analysis

        wf, events = workflow_env
        outputs = {r: _failed(r, "server_overload") for r in _ALL_ROLES}
        reflection = ReflectionVerdict(
            round_number=1, verified=False, issues=["x"], agent_count=9)
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": outputs,
                "debate": DebateState(),
                "reflection_history": [reflection],
                "final_recommendation": None,
                "no_verdict_reason": "no_evidence"
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=uuid4(), status="pending")
        await wf.run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.FULL)

        async with SessionLocal() as s:
            row = await s.get(Analysis, analysis_id)
            assert (row.status,
                    row.failure_reason) == ("failed", "no_evidence")
            counts = {}
            for table in ("recommendations", "episodic_memories", "agent_runs",
                          "reflections"):
                result = await s.execute(
                    text(
                        f"SELECT count(*) FROM ai.{table} "
                        "WHERE analysis_id = :a"), {"a": analysis_id})
                counts[table] = result.scalar_one()
            issues = (
                await s.execute(
                    text(
                        "SELECT issues FROM ai.reflections WHERE analysis_id "
                        "= :a"), {"a": analysis_id})).scalar_one()
        assert counts == {
            "recommendations": 0,
            "episodic_memories": 0,
            "agent_runs": 9,
            "reflections": 1
        }
        assert issues[-1].startswith(
            "REVIEW_META: llm_review_ran=false; usable_agents=0/9")
        assert [n
                for n, _ in events] == ["analysis.started", "analysis.failed"]
        assert events[1][1]["failure_reason"] == "no_evidence"

    @pytest.mark.asyncio
    async def test_experiment_isolation_ignores_other_analyses_tiers(
            self, rows, workflow_env, monkeypatch):
        from ai.recommendation.position_sizing import (PositionSizingResult,
                                                       TierSuggestion)
        from ai.tools.portfolio_tool import committed_buy_cash_elsewhere
        from database import SessionLocal
        from domains.analysis.model import Recommendation

        wf, events = workflow_env
        pf = uuid4()
        _, old_amd, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "587.37", "6", _P), (_BUY, "575.5", "7", _P)])
        _, old_nvda, _ = await rows.recommendation(
            ticker="NVDA", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "150", "10", _P)])
        assert await committed_buy_cash_elsewhere(str(pf), "AMD") > 0
        monkeypatch.setattr(config.settings, "ANALYSIS_EXPERIMENT_ISOLATION",
                            True)
        assert await committed_buy_cash_elsewhere(str(pf), "AMD") == 0
        seen: dict[str, Any] = {}

        async def _fake_sizing(**kwargs):
            seen.update(kwargs)
            return (
                PositionSizingResult(
                    window_days=14, tiers=[
                        TierSuggestion(
                            target_price=Decimal("600"),
                            quantity=Decimal("2"))
                    ]), None)

        monkeypatch.setattr(wf, "generate_position_sizing", _fake_sizing)
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": {},
                "debate": DebateState(),
                "reflection_history": [],
                "final_recommendation": _final_payload("buy", 0.7)
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=pf, status="pending")
        await wf.run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.FULL)

        assert seen["committed_buy_cash"] == Decimal(0)
        assert await rows.tier_statuses(old_amd) == [_P, _P]
        assert await rows.tier_statuses(old_nvda) == [_P]
        async with SessionLocal() as s:
            new = (
                await s.execute(
                    select(Recommendation).where(
                        Recommendation.analysis_id == analysis_id)
                )).scalar_one()
        assert await rows.tier_statuses(new.id) == [_P]
        assert "tier.cancelled" not in [n for n, _ in events]

    @pytest.mark.asyncio
    async def test_new_buy_replaces_earlier_tiers_and_records_why(
            self, rows, workflow_env, monkeypatch):
        from ai.recommendation.position_sizing import (PositionSizingResult,
                                                       TierSuggestion)
        from database import SessionLocal
        from domains.analysis.model import Recommendation

        wf, events = workflow_env
        pf = uuid4()
        old_analysis, old_rec, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy", confidence=0.72,
            tiers=[(_BUY, "587.37", "6", _P), (_BUY, "575.5", "7", _P)])

        seen: dict[str, Any] = {}

        async def _fake_sizing(**kwargs):
            seen.update(kwargs)
            return (
                PositionSizingResult(
                    window_days=14, tiers=[
                        TierSuggestion(
                            target_price=Decimal("615.35"),
                            quantity=Decimal("6.25")),
                        TierSuggestion(
                            target_price=Decimal("585.0"),
                            quantity=Decimal("6.5"))
                    ]), None)

        class _CapturingGraph(_StubGraph):

            async def ainvoke(self, state, config=None):
                seen["state"] = state
                return await super().ainvoke(state, config)

        monkeypatch.setattr(wf, "generate_position_sizing", _fake_sizing)
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _CapturingGraph({
                "agent_outputs": {},
                "debate": DebateState(),
                "reflection_history": [],
                "final_recommendation": _final_payload("buy", 0.3)
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=pf, status="pending")
        await wf.run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.FULL)

        state = seen["state"]
        assert state.prior_recommendation.analysis_id == str(old_analysis)
        assert state.prior_recommendation.well_supported_count is None
        assert "old reasoning text" not in (
            state.pending_recommendation_context)
        assert state.held_quantity == 5.0
        assert seen["committed_buy_cash"] == Decimal(0)
        assert await rows.tier_statuses(old_rec) == [_S, _S]
        async with SessionLocal() as s:
            new = (
                await s.execute(
                    select(Recommendation).where(
                        Recommendation.analysis_id == analysis_id)
                )).scalar_one()
            new_statuses = await rows.tier_statuses(new.id)
        assert new_statuses == [_P, _P]
        assert "replaced by a newer BUY recommendation" in (
            new.tier_generation_note)
        assert [n for n, _ in events] == [
            "analysis.started", "analysis.completed", "recommendation.ready",
            "tier.cancelled", "tier.cancelled"
        ]
        cancelled = [p for n, p in events if n == "tier.cancelled"]
        assert {p["reason"] for p in cancelled} == {"superseded"}
        assert {p["action"] for p in cancelled} == {"buy"}

    @pytest.mark.asyncio
    async def test_run_failed_meanwhile_discards_its_completion(
            self, rows, workflow_env, monkeypatch):
        from ai.recommendation.position_sizing import (PositionSizingResult,
                                                       TierSuggestion)
        from database import SessionLocal
        from domains.analysis.model import Analysis

        wf, events = workflow_env
        pf = uuid4()
        _, old_rec, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "587.37", "6", _P)])
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=pf, status="pending")

        async def _fake_sizing(**kwargs):
            return (
                PositionSizingResult(
                    window_days=14, tiers=[
                        TierSuggestion(
                            target_price=Decimal("600"), quantity=Decimal("1"))
                    ]), None)

        class _TimedOutGraph(_StubGraph):

            async def ainvoke(self, state, config=None):
                await wf.mark_analysis_failed(analysis_id, "timeout")
                return await super().ainvoke(state, config)

        monkeypatch.setattr(wf, "generate_position_sizing", _fake_sizing)
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _TimedOutGraph({
                "agent_outputs": {},
                "debate": DebateState(),
                "reflection_history": [
                    ReflectionVerdict(
                        round_number=1, verified=True, llm_review_ran=True)
                ],
                "final_recommendation": _final_payload("buy", 0.7)
            }))
        await wf.run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.FULL)

        async with SessionLocal() as s:
            row = await s.get(Analysis, analysis_id)
            assert (row.status, row.failure_reason) == ("failed", "timeout")
            counts = {}
            for table in ("recommendations", "episodic_memories",
                          "reflections"):
                counts[table] = (
                    await s.execute(
                        text(
                            f"SELECT count(*) FROM ai.{table} "
                            "WHERE analysis_id = :a"),
                        {"a": analysis_id})).scalar_one()
        assert counts == {
            "recommendations": 0,
            "episodic_memories": 0,
            "reflections": 1
        }
        assert await rows.tier_statuses(old_rec) == [_P]
        assert [n
                for n, _ in events] == ["analysis.started", "analysis.failed"]

    @pytest.mark.asyncio
    async def test_only_the_last_reflection_round_blocks_archival(
            self, rows, workflow_env, monkeypatch):
        wf, _events = workflow_env
        calls: dict[str, list[Any]] = {"memory": [], "archive": []}

        async def _remember(**kwargs):
            calls["memory"].append(kwargs["agent_id"])
            return False

        async def _archive(**kwargs):
            calls["archive"].append(kwargs["role"])

        monkeypatch.setattr(wf, "maybe_remember_trajectory", _remember)
        monkeypatch.setattr(wf, "archive_agent_report", _archive)
        outputs = {
            AgentRole.MACRO: _ok(AgentRole.MACRO, summary="rates " * 50),
            AgentRole.NEWS: _ok(AgentRole.NEWS, summary="headline " * 50),
            AgentRole.MARKET: _ok(AgentRole.MARKET, summary="price " * 50)
        }
        history = [
            ReflectionVerdict(
                round_number=1, verified=False, issues=["m"],
                retry_agents={AgentRole.MACRO: "fix macro"}),
            ReflectionVerdict(
                round_number=2, verified=False, issues=["n"],
                retry_agents={AgentRole.NEWS: "fix news"})
        ]
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": outputs,
                "debate": DebateState(),
                "reflection_history": history,
                "final_recommendation": _final_payload("hold", 0.5)
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=None, status="pending")
        await wf.run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.FULL)
        assert AgentRole.MACRO in calls["archive"]
        assert AgentRole.NEWS not in calls["archive"]
        assert len(calls["memory"]) == 1

    @pytest.mark.asyncio
    async def test_experiment_isolation_skips_memory_and_prior_context(
            self, rows, workflow_env, monkeypatch):
        import ai.knowledge.kg_context as kg_module
        from ai.graph.nodes import _procedural_hints
        from database import SessionLocal
        from domains.analysis.model import Analysis

        wf, _events = workflow_env
        calls: list[str] = []

        async def _remember(**kwargs):
            calls.append("memory")
            return True

        async def _archive(**kwargs):
            calls.append("archive")

        # The code under test catches errors from these reads, so they
        # record each call instead of raising.
        reads: list[str] = []

        async def _hints(*args, **kwargs):
            reads.append("procedural_hints")

        async def _episodes(*args, **kwargs):
            reads.append("episodic_context")
            return []

        def _session(*args, **kwargs):
            reads.append("prior_recommendation")
            return SessionLocal()

        monkeypatch.setattr(config.settings, "ANALYSIS_EXPERIMENT_ISOLATION",
                            True)
        monkeypatch.setattr(config.settings, "EPISODIC_CONTEXT_LIMIT", 3)
        monkeypatch.setattr(wf, "maybe_remember_trajectory", _remember)
        monkeypatch.setattr(wf, "archive_agent_report", _archive)
        monkeypatch.setattr(
            memory_module, "retrieve_procedural_hints", _hints)
        monkeypatch.setattr(
            memory_module, "retrieve_episodic_context", _episodes)
        monkeypatch.setattr(wf, "SessionLocal", _session)
        with capture_logs() as logs:
            assert await wf._resolve_prior_recommendation(
                str(uuid4()), "AMD") is None
            assert await _procedural_hints([AgentRole.MACRO], "AMD",
                                           str(uuid4())) == {}
            assert await kg_module.fetch_kg_relations(
                "AMD", "AMD", None) == []
            assert await episodic_memory_node(_amd(user_id=str(
                uuid4()))) == {"episodic_context": []}
        assert reads == []
        assert sorted(e["path"] for e in logs
                      if e["event"] == "experiment_isolation.skipped") == [
                          "episodic_memory_read", "knowledge_graph_read",
                          "prior_recommendation_read",
                          "procedural_memory_read"
                      ]
        monkeypatch.setattr(config.settings, "ANALYSIS_EXPERIMENT_ISOLATION",
                            False)
        await episodic_memory_node(_amd(user_id=str(uuid4())))
        await _procedural_hints([AgentRole.MACRO], "AMD", str(uuid4()))
        assert reads == ["episodic_context", "procedural_hints"]
        monkeypatch.setattr(config.settings, "ANALYSIS_EXPERIMENT_ISOLATION",
                            True)
        monkeypatch.setattr(wf, "SessionLocal", SessionLocal)
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": {
                    AgentRole.MACRO: _ok(
                        AgentRole.MACRO, summary="rates " * 50)
                },
                "debate": DebateState(),
                "reflection_history": [],
                "final_recommendation": _final_payload("hold", 0.5)
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=None, status="pending")
        with capture_logs() as logs:
            await wf.run_analysis_workflow(
                str(analysis_id), analysis_type=AnalysisType.FULL)
        assert calls == []
        assert sorted(e["path"] for e in logs
                      if e["event"] == "experiment_isolation.skipped") == [
                          "episodic_memory_write",
                          "procedural_memory_and_archival_write"
                      ]
        async with SessionLocal() as s:
            assert (await s.get(Analysis, analysis_id)).status == "completed"
            for table, expected in (("recommendations", 1),
                                    ("episodic_memories", 0)):
                count = (
                    await s.execute(
                        text(
                            f"SELECT count(*) FROM ai.{table} "
                            "WHERE analysis_id = :a"),
                        {"a": analysis_id})).scalar_one()
                assert count == expected, table

    @pytest.mark.asyncio
    async def test_learning_still_runs_when_publishing_fails(
            self, rows, workflow_env, monkeypatch):
        import shared.events as shared_events
        from shared.events import EventName

        wf, _events = workflow_env
        archived: list[Any] = []

        async def _archive(**kwargs):
            archived.append(kwargs["role"])

        async def _broken_publish(settings, event_name, payload):
            if event_name == EventName.ANALYSIS_COMPLETED:
                raise RuntimeError("redis down")

        monkeypatch.setattr(wf, "archive_agent_report", _archive)
        monkeypatch.setattr(
            shared_events, "publish_cross_service", _broken_publish)
        outputs = {
            AgentRole.MACRO: _ok(AgentRole.MACRO, summary="rates " * 50)
        }
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": outputs,
                "debate": DebateState(),
                "reflection_history": [],
                "final_recommendation": _final_payload("hold", 0.5)
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=None, status="pending")
        with contextlib.suppress(RuntimeError):
            await wf.run_analysis_workflow(
                str(analysis_id), analysis_type=AnalysisType.FULL)
        assert archived == [AgentRole.MACRO]

    @pytest.mark.asyncio
    async def test_replaced_side_placeholder_is_not_stored(
            self, rows, workflow_env, monkeypatch):
        from database import SessionLocal
        from domains.analysis.model import DebateArgument as ArgumentRow
        from domains.analysis.model import DebateSession

        wf, _events = workflow_env
        repaired = DebateArgument(
            round_number=1, side="bull", argument="repaired bull case")
        debate = DebateState(arguments=[*_args(bull_ok=False), repaired])
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": {
                    AgentRole.MARKET: _ok(AgentRole.MARKET)
                },
                "debate": debate,
                "reflection_history": [],
                "final_recommendation": _final_payload("hold", 0.5)
            }))
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=None, status="pending")
        with capture_logs() as logs:
            await wf.run_analysis_workflow(
                str(analysis_id), analysis_type=AnalysisType.FULL)
        async with SessionLocal() as s:
            stored = (
                await s.execute(
                    select(ArgumentRow.side,
                           ArgumentRow.content).join(DebateSession).where(
                               DebateSession.analysis_id == analysis_id)
                )).all()
        assert sorted(stored) == [("bear", "bear case"),
                                  ("bull", "repaired bull case")]
        dropped = [
            e for e in logs if e["event"] ==
            "analysis_workflow.replaced_side_placeholders_dropped"
        ]
        assert [e["count"] for e in dropped] == [1]


class TestDebateTranscript:

    def test_only_replaced_placeholders_leave_the_transcript(self):
        late = DebateArgument(
            round_number=2, side="bull",
            argument="[bull argument unavailable]", available=False,
            unavailable_kind="server_overload")
        repaired = DebateArgument(
            round_number=1, side="bull", argument="repaired bull case")
        debate = DebateState(arguments=[*_args(bull_ok=False), repaired, late])
        kept = debate.transcript_arguments()
        assert [(a.round_number, a.side, a.available)
                for a in kept] == [(1, "bear", True), (1, "bull", True),
                                   (2, "bull", False)]
        state = _amd(debate=debate)
        text = ReflectionAgent._debate_transcript(state)
        assert text.count("[bull argument unavailable]") == 1
        assert "Round 2 bull (LATEST): [bull argument unavailable]" in text
        assert "Round 1 bull: repaired bull case" in text


class TestAnalysisQueueOwnerCheck:

    async def _run(self, owner_active: Any, user_id: UUID | None):
        from domains.analysis.queue import AnalysisJob, AnalysisQueue

        ran: list[UUID] = []
        failed: list[tuple[UUID, str]] = []

        async def runner(job: AnalysisJob) -> None:
            ran.append(job.analysis_id)

        async def mark_failed(analysis_id: UUID, reason: str) -> bool:
            failed.append((analysis_id, reason))
            return True

        queue = AnalysisQueue(
            runner=runner, mark_failed=mark_failed, timeout_seconds=5,
            owner_active=owner_active)
        job = AnalysisJob(analysis_id=uuid4(), user_id=user_id)
        with capture_logs() as logs:
            try:
                queue.enqueue(job)
                await asyncio.wait_for(queue.join(), timeout=5)
            finally:
                await queue.stop()
        return ran, failed, logs

    @pytest.mark.asyncio
    async def test_only_an_inactive_owner_skips_and_default_check(
            self, monkeypatch):
        async def inactive(_: UUID) -> bool | None:
            return False

        ran, failed, logs = await self._run(inactive, uuid4())
        assert ran == []
        reasons = [reason for _, reason in failed]
        assert reasons == [AnalysisFailureReason.CANCELLED.value]
        assert any(
            e["event"] == "analysis_queue_job_skipped_owner_inactive"
            for e in logs)
        states = {"active": True, "unknown": None, "check-fails": "raise"}
        for case_id, state in states.items():

            async def check(_: UUID, state: Any = state) -> bool | None:
                if state == "raise":
                    raise RuntimeError("redis down")
                return state  # type: ignore[no-any-return]

            ran, failed, _ = await self._run(check, uuid4())
            assert len(ran) == 1 and failed == [], case_id

        import shared.user_state as user_state_module
        from domains.analysis import queue as queue_module
        from shared.user_state import UserState

        seen: list[Any] = []

        async def fake_get_user_state(
                settings: Any, user_id: Any) -> UserState:
            seen.append(user_id)
            return UserState(role="user", is_active=False)

        monkeypatch.setattr(
            user_state_module, "get_user_state", fake_get_user_state)
        monkeypatch.setattr(config.settings, "USER_STATE_CHECK_ENABLED", True)
        owner = uuid4()
        assert await queue_module._default_owner_active(owner) is False
        assert seen == [owner]
        monkeypatch.setattr(config.settings, "USER_STATE_CHECK_ENABLED", False)
        assert await queue_module._default_owner_active(owner) is None


@pytest.mark.asyncio
async def test_knowledge_graph_tool_drops_non_english_rows(monkeypatch):
    import ai.tools.knowledge_graph_tool as kg_tool_module

    now = datetime.now(UTC)
    cyrillic = "Санкт-Петербург"
    rows_out = [(
        "NVIDIA", "competes_with", "outgoing", 1, 0.9, "fundamental", now,
        now), (cyrillic, "partner_of", "outgoing", 1, 0.9, "news", now, now)]

    async def _fetch(
            entity_name, ticker_filter, session, viewer_user_id=None,
            events_expire_before=None):
        return rows_out

    class _Session:

        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(kg_tool_module, "fetch_kg_relations", _fetch)
    monkeypatch.setattr(kg_tool_module, "SessionLocal", lambda: _Session())
    text_out = await kg_tool_module.QueryKnowledgeGraphTool().run("AMD")
    assert "NVIDIA" in text_out
    assert cyrillic not in text_out
