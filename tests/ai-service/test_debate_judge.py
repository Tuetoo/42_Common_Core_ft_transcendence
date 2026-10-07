import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4
import ai.agents.debate_side as side_module
import ai.agents.judge as judge_module
import ai.agents.reflection as reflection_module
import ai.llm.google_provider as google_module
import ai.recommendation.position_sizing as sizing_module
import ai.tools.portfolio_tool as portfolio_tool_module
import config
import pytest
from ai.agents import debate_common as dc
from ai.agents.fundamental import _TOOL_NOTE_RE
from ai.agents.judge import JudgeAgent
from ai.agents.loop import ReActAgentLoop
from ai.agents.reflection import ReflectionAgent
from ai.graph.state import (ISSUE_LLM_REVIEW, ISSUE_NO_EVIDENCE_NOT_RETRIED,
                            ISSUE_NOT_RETRIED, AgentOutput, AgentSkip,
                            AnalysisState, DebateArgument, DebateState,
                            EpisodicMemoryHit, JudgeVerdict,
                            PriorRecommendation, ReflectionIssue,
                            ReflectionVerdict)
from ai.llm.base import (LLMConfigError, LLMMessage, LLMProviderAdapter,
                         LLMRequestError, LLMRequestTimeoutError, LLMResponse,
                         LLMServerOverloadedError, LLMTier, LLMUsage,
                         request_pacer)
from ai.llm.json_fence import parse_json_object
from ai.llm.router import LLMRouter
from ai.providers import sec_edgar
from ai.providers.base import PriceQuote, ProviderRequestError
from ai.recommendation.engine import (build_final_recommendation,
                                      no_verdict_reason)
from ai.recommendation.position_sizing import (_validated_sizing,
                                               generate_position_sizing)
from ai.tools.base import Tool
from google.genai import errors as genai_errors
from helpers import _ALL_ROLES, _BUY, _P
from helpers import _amd_state as _state
from helpers import (_args, _make_decision_with_outcome, _ok,
                     _ScriptedReviewRouter, _tool_entry, _verdict)
from structlog.testing import capture_logs
from domains.analysis.repository import (AnalysisPersistenceRepository,
                                         AnalysisRepository)
from domains.decision.repository import (DecisionReviewRepository,
                                         InvestmentDecisionRepository)
from shared.enums import (AgentRole, AgentStatus, RecommendationAction,
                          TierSide, TierStatus)
from shared.exceptions import NotFoundError, ValidationAppError
from tests.auth_tokens import bearer_headers

_A = TierStatus.ACCEPTED_AWAITING_TRIGGER.value
_T = TierStatus.TRIGGERED.value
_S = TierStatus.SUPERSEDED.value
_SELL = TierSide.SELL.value
_NINE = list(_ALL_ROLES)
_T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def clean_pacer(monkeypatch):
    request_pacer.reset()
    for name in ("LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS",
                 "LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS",
                 "LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS"):
        monkeypatch.setattr(config.settings, name, 0.0)
    yield
    request_pacer.reset()


def _final(state: AnalysisState):
    return build_final_recommendation(state, state.debate.latest_verdict)


def _failed(
        role: AgentRole, kind: str | None = "server_overload",
        **kw: Any) -> AgentOutput:
    return AgentOutput(
        role=role, status=AgentStatus.FAILED,
        summary=f"{role.value} agent failed: x", score=0.0,
        is_well_supported=False, failure_kind=kind, **kw)


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


def _plain(role: AgentRole, summary: str) -> AgentOutput:
    return AgentOutput(role=role, summary=summary, score=0.9)


def _completed(
        role: AgentRole, raw: dict[str, Any], summary: str = "summary",
        status: AgentStatus = AgentStatus.COMPLETED) -> AgentOutput:
    return AgentOutput(
        role=role, status=status, summary=summary, score=0.8, raw_output=raw)


def _two_sides() -> DebateState:
    return DebateState(
        round_number=1, arguments=[
            DebateArgument(round_number=1, side="bull", argument="bull"),
            DebateArgument(round_number=1, side="bear", argument="bear")
        ])


class _Router:

    def __init__(self, *contents: str | Exception, repeat: bool = False):
        self._contents = list(contents)
        self._repeat = repeat
        self.calls: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []
        self.tiers: list[Any] = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        self.tiers.append(tier)
        if self._repeat and len(self._contents) == 1:
            content = self._contents[0]
        else:
            content = self._contents.pop(0)
        if isinstance(content, Exception):
            raise content
        return LLMResponse(
            content=content, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1,
                           completion_tokens=1), finish_reason="STOP")


class _RaisingRouter:

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    async def complete(self, tier, messages, **kwargs):
        self.calls += 1
        raise self.exc


class _CountingReviewRouter:

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=json.dumps({
                "issues": [],
                "retry_roles": [],
                "checked_claims": ["c"]
            }), provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _AlwaysFailsRouter:

    async def complete(self, tier, messages, **kwargs):
        raise LLMConfigError("no key configured")


def _judge_json(
        leaning: str = "buy", confidence: float = 0.8,
        rationale: str = "ok") -> str:
    return json.dumps({
        "converged": True,
        "leaning": leaning,
        "confidence": confidence,
        "rationale": rationale
    })


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == name]


async def _judge_logs(
        monkeypatch: pytest.MonkeyPatch, state: AnalysisState,
        **fields: Any) -> list[Any]:
    payload: dict[str, Any] = {
        "converged": True,
        "leaning": "buy",
        "confidence": 0.85,
        "rationale": "Revenue grew.",
        "deciding_factor": "growth"
    }
    payload.update(fields)
    router = _Router(json.dumps(payload))
    monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
    with capture_logs() as logs:
        await judge_module.JudgeAgent().assess_round(state)
    return logs


_CLEAN_REVIEW = json.dumps({
    "checked_claims": ["close 100.0"],
    "issues": [],
    "retry_roles": {}
})


def _reaffirm_state(action: str = "buy", usable: int = 5) -> AnalysisState:
    outputs = {r: _ok(r) for r in _ALL_ROLES}
    for role in _ALL_ROLES[usable:]:
        outputs[role] = _failed(role, "rate_limit")
    prior = PriorRecommendation(
        recommendation_id=str(uuid4()), analysis_id=str(uuid4()),
        action=RecommendationAction.BUY, confidence=0.6,
        issued_at=datetime(2026, 9, 16, 14, 2, tzinfo=UTC), accepted=False,
        well_supported_count=6, agent_count=9)
    return _state(
        ticker="MSFT", agent_outputs=outputs, prior_recommendation=prior,
        debate=DebateState(
            round_number=1, converged=True, arguments=_args(),
            verdicts=[_verdict(action, 0.85)]))


def _hold_verdict_state(
        *, converged: bool, history: list[ReflectionVerdict]) -> AnalysisState:
    verdict = JudgeVerdict(
        round_number=1, converged=converged, leaning=RecommendationAction.HOLD,
        confidence=0.85, rationale="Some rationale")
    return _state(
        ticker="AAPL", reflection_history=history, debate=DebateState(
            round_number=3, verdicts=[verdict], converged=converged))


class TestFinalRecommendation:

    def test_unavailable_bull_and_skipped_agent_shape_the_note(self):
        outputs = {r: _ok(r) for r in _ALL_ROLES}
        outputs[AgentRole.SENTIMENT] = _ok(
            AgentRole.SENTIMENT, is_well_supported=False)
        state = _state(
            agent_outputs=outputs, debate=DebateState(
                round_number=1, converged=True, arguments=_args(bull_ok=False),
                verdicts=[_verdict("buy", 0.85)]))
        rec = _final(state)
        assert rec.confidence == pytest.approx(0.3)
        note = rec.confidence_adjustment_note
        assert "Bull side's argument was unavailable" in note
        assert "overloaded (503)" in note

        state = _state(
            ticker="TSLA", skipped_agents=[
                AgentSkip(
                    role=AgentRole.MACRO, reason="not part of default set")
            ], debate=DebateState(
                round_number=1, converged=True, verdicts=[
                    JudgeVerdict(
                        round_number=1, converged=True,
                        leaning=RecommendationAction.HOLD, confidence=0.5,
                        rationale="ok")
                ]))
        rec = _final(state)
        assert rec.confidence_adjustment_note is not None
        assert "macro" in rec.confidence_adjustment_note

    def test_reaffirmation_cap_rules(self):
        cases = {
            "different-action": ("hold", 5),
            "as-much-evidence-as-before": ("buy", 6)
        }
        for case_id, (action, usable) in cases.items():
            rec = _final(_reaffirm_state(action=action, usable=usable))
            assert rec.confidence == pytest.approx(0.70), case_id

        buy_debate = DebateState(
            round_number=1, converged=True, arguments=[
                DebateArgument(
                    round_number=1, side="bull", argument="bull case"),
                DebateArgument(
                    round_number=1, side="bear", argument="bear case")
            ], verdicts=[
                JudgeVerdict(
                    round_number=1, converged=True,
                    leaning=RecommendationAction.BUY, confidence=0.78,
                    rationale="r")
            ])
        prior = PriorRecommendation(
            recommendation_id="r0", analysis_id="a0",
            action=RecommendationAction.HOLD, confidence=0.6,
            issued_at=_T0 - timedelta(days=2), accepted=False,
            well_supported_count=9, agent_count=9)
        rec = _final(
            _state(
                ticker="MSFT", selected_agents=list(_NINE), debate=buy_debate,
                position_weight_pct=55.0, prior_recommendation=prior,
                agent_outputs={
                    r: _priced(r, completed_min=0)
                    for r in _NINE[:5]
                }))
        assert rec.action == RecommendationAction.HOLD
        assert rec.confidence == pytest.approx(0.6)
        assert rec.confidence_adjustment_note is not None
        assert "reaffirms the prior HOLD" in rec.confidence_adjustment_note

    def test_unverified_reflection_lowers_confidence(self):
        unverified = _hold_verdict_state(
            converged=True, history=[
                ReflectionVerdict(
                    round_number=1, verified=False,
                    issues=["fabricated figure A"]),
                ReflectionVerdict(
                    round_number=2, verified=False, issues=[
                        "fabricated figure A", "fabricated figure B",
                        "fabricated figure C", "fabricated figure D"
                    ])
            ])
        rec = _final(unverified)
        note = unverified.confidence_adjustment_note()
        assert rec.confidence == 0.85 - 0.15
        assert rec.debate_converged is True
        assert note is not None
        assert "Reflection could not fully verify" in note
        assert "4 unresolved issues" in note

        clean = _hold_verdict_state(
            converged=True, history=[
                ReflectionVerdict(round_number=1, verified=True, issues=[])
            ])
        assert _final(clean).confidence == 0.85
        assert clean.confidence_adjustment_note() is None

        stacked = _hold_verdict_state(
            converged=False, history=[
                ReflectionVerdict(
                    round_number=1, verified=False, issues=["x"])
            ])
        assert abs(_final(stacked).confidence - 0.55) < 1e-9

    def test_no_verdict_reason(self):
        assert no_verdict_reason(_state()) == "no_evidence"
        failed_all = {r: _failed(r) for r in _ALL_ROLES}
        assert (
            no_verdict_reason(
                _state(agent_outputs=failed_all)) == "no_evidence")
        judged_failed = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            debate=DebateState(
                verdicts=[
                    _verdict(
                        "hold", 0.0, judge_failed=True,
                        failure_reason="debate_unavailable")
                ]))
        assert no_verdict_reason(judged_failed) == "debate_unavailable"
        ok = _state(debate=DebateState(verdicts=[_verdict()]))
        assert no_verdict_reason(ok) is None

        def nine(usable: int) -> AnalysisState:
            return _state(
                ticker="MSFT", selected_agents=list(_NINE), agent_outputs={
                    role: _priced(role) if i < usable else _failed(role)
                    for i, role in enumerate(_NINE)
                })

        assert no_verdict_reason(nine(2)) == "insufficient_evidence"
        assert no_verdict_reason(nine(5)) == "judge_unavailable"


class TestConfidenceNote:

    def test_retry_and_verified_wording(self):
        state = _state(
            agent_outputs={
                AgentRole.MARKET: _ok(AgentRole.MARKET),
                AgentRole.FILING: _ok(
                    AgentRole.FILING, is_well_supported=False,
                    retry_of_feedback="thin"),
                AgentRole.RISK: _ok(AgentRole.RISK, is_well_supported=False)
            })
        note = state.confidence_adjustment_note()
        assert "Limited evidence despite retry attempts: filing." in note
        assert "Limited evidence, not retried: risk." in note

        history = [
            ReflectionVerdict(round_number=1, verified=False, issues=["x"]),
            ReflectionVerdict(
                round_number=2, verified=True, llm_review_ran=True,
                usable_agent_count=1, agent_count=2)
        ]
        partial = _state(
            agent_outputs={
                AgentRole.MARKET: _ok(AgentRole.MARKET),
                AgentRole.MACRO: _failed(AgentRole.MACRO)
            }, reflection_history=history)
        note = partial.confidence_adjustment_note()
        assert "Confidence is not reduced" not in note
        assert "not every agent's evidence was verified" in note
        full = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            reflection_history=history)
        assert "Confidence is not reduced for this" in (
            full.confidence_adjustment_note())


def _episode(
        action: str, confidence: float, days_ago: int,
        outcome: str | None = None,
        outcome_return: float | None = None) -> EpisodicMemoryHit:
    return EpisodicMemoryHit(
        analysis_id=str(uuid4()), ticker="AMD",
        analyzed_at=datetime(2026, 9, 29, tzinfo=UTC) -
        timedelta(days=days_ago), recommendation=RecommendationAction(action),
        confidence=confidence, outcome=outcome, outcome_return=outcome_return)


def _amzn_judge_state(**overrides: Any) -> AnalysisState:
    values: dict[str, Any] = {
        "ticker": "AMZN",
        "agent_outputs": {
            AgentRole.MARKET: _completed(AgentRole.MARKET, {}, "price $200")
        },
        "debate": _two_sides()
    }
    values.update(overrides)
    return _state(**values)


def _previous(
        action: RecommendationAction, confidence: float,
        judge_leaning: RecommendationAction | None = None,
        judge_confidence: float | None = None) -> EpisodicMemoryHit:
    return EpisodicMemoryHit(
        analysis_id="x", ticker="AMZN", recommendation=action,
        confidence=confidence,
        analyzed_at=datetime(2026, 10, 2, 23, 0, tzinfo=UTC),
        judge_leaning=judge_leaning, judge_confidence=judge_confidence)


class TestJudge:

    @pytest.mark.asyncio
    async def test_unavailable_sides_skip_or_are_stated(self, monkeypatch):
        router = _Router(_judge_json(), repeat=True)
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        state = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            debate=DebateState(arguments=_args(bull_ok=False, bear_ok=False)))
        verdict = await JudgeAgent().assess_round(state)
        assert verdict.judge_failed
        assert verdict.failure_reason == "debate_unavailable"
        assert router.calls == []

        router = _Router(_judge_json(), repeat=True)
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        state = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            debate=DebateState(arguments=_args(bull_ok=False)))
        verdict = await JudgeAgent().assess_round(state)
        assert not verdict.judge_failed
        user = router.calls[0][-1].content
        assert "Bull argument: (UNAVAILABLE" in user
        assert "[bull argument unavailable]" not in user

    @pytest.mark.asyncio
    async def test_prior_is_action_confidence_date_and_holding(
            self, monkeypatch):
        router = _Router(_judge_json(), repeat=True)
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        prior = PriorRecommendation(
            recommendation_id="r", analysis_id="a",
            action=RecommendationAction.BUY, confidence=0.72,
            issued_at=datetime(2026, 9, 21, tzinfo=UTC), accepted=True)
        state = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            debate=DebateState(arguments=_args()), prior_recommendation=prior,
            pending_recommendation_context=prior.judge_context_line("AMD"),
            held_quantity=5.0)
        await JudgeAgent().assess_round(state)
        user = router.calls[0][-1].content
        assert "prior BUY recommendation for AMD" in user
        assert "2026-09-21" in user and "0.72" in user
        assert "Reasoning:" not in user
        assert "Current holding of AMD in this portfolio: 5 shares" in user

    @pytest.mark.asyncio
    async def test_non_english_fails_and_track_record_is_shown(
            self, monkeypatch):
        russian = "Рекомендуем покупать"
        router = _Router(_judge_json(rationale=russian), repeat=True)
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        verdict = await JudgeAgent().assess_round(
            _state(
                agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
                debate=DebateState(arguments=_args())))
        assert verdict.judge_failed
        assert verdict.failure_reason == "judge_unavailable"
        assert russian not in verdict.rationale

        router = _Router(
            '{"converged": true, "leaning": "buy", "confidence": 0.6, '
            '"rationale": "r"}', repeat=True)
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        state = _state(
            user_id=str(uuid4()), agent_outputs={
                AgentRole.MARKET: _ok(AgentRole.MARKET, summary="price fine")
            }, debate=DebateState(arguments=_args()), episodic_context=[
                _episode("buy", 0.72, 40, "incorrect", -0.063),
                _episode("hold", 0.5, 5)
            ])
        await JudgeAgent().assess_round(state)
        system, user = router.calls[0][0].content, router.calls[0][1].content
        assert "calibrate" in system
        assert "Track record" in user
        assert "30-day return -6.3%, call was incorrect" in user
        assert "outcome not known yet" in user

    async def test_trailing_pe_reading_and_skip_log(self, monkeypatch):

        def _fund(pad: list[dict[str, Any]]) -> AgentOutput:
            return AgentOutput(
                role=AgentRole.FUNDAMENTAL, status=AgentStatus.COMPLETED,
                summary="f", score=0.8, raw_output={
                    "key_facts": {
                        "net_margin_pct": 12.5,
                        "operating_margin_pct": 10.7
                    },
                    "scratchpad": pad
                })

        own_old = {
            "action": "get_fundamentals",
            "is_tool_result": True,
            "observation": "P/E=20.00 forward P/E=20.50"
        }
        own_new = {**own_old, "observation": "P/E=10.00 forward P/E=30.00"}
        peer = {**own_old, "peer_ticker": "AMD"}
        assert judge_module._trailing_and_forward_pe(
            _fund([own_old, own_new, peer])) == (10.0, 30.0)
        assert judge_module._trailing_and_forward_pe(_fund([peer])) is None

        state = _amzn_judge_state(
            episodic_context=[_previous(RecommendationAction.BUY, 0.85)])
        fundamental = AgentOutput(
            role=AgentRole.FUNDAMENTAL, status=AgentStatus.COMPLETED,
            summary="f", score=0.8, raw_output={
                "key_facts": {
                    "net_margin_pct": 12.5,
                    "operating_margin_pct": 10.7
                },
                "scratchpad": [{
                    "action": "get_fundamentals",
                    "is_tool_result": True,
                    "observation": "P/E=20.00 forward P/E=20.50"
                }]
            })
        state = state.model_copy(
            update={
                "agent_outputs": {
                    **state.agent_outputs, AgentRole.FUNDAMENTAL: fundamental
                }
            })
        logs = await _judge_logs(monkeypatch, state)
        skipped = _events(logs, "judge.trailing_pe_note_skipped")
        assert len(skipped) == 1

    @pytest.mark.asyncio
    async def test_explained_move_from_raw_verdict_is_not_flagged(
            self, monkeypatch) -> None:
        hit = _previous(
            RecommendationAction.HOLD, 0.45, RecommendationAction.BUY, 0.75)
        logs = await _judge_logs(
            monkeypatch, _amzn_judge_state(episodic_context=[hit]),
            change_since_previous="Q3 revenue was reported",
            change_basis="new_evidence")
        events = [e["event"] for e in logs]
        assert "judge.change_since_previous_missing" not in events
        assert "judge.change_basis_missing" not in events

    async def test_opposing_points_are_kept_and_logged(self, monkeypatch):
        parsed = {
            "opposing_points": [{
                "point": " a  b ",
                "response": "c"
            }, {
                "point": "",
                "response": "x"
            }, "text", {
                "point": "d",
                "response": "e"
            }]
        }
        assert judge_module.opposing_points(parsed) == [{
            "point": "a b",
            "response": "c"
        }, {
            "point": "d",
            "response": "e"
        }]
        assert judge_module.opposing_points({"opposing_points": "x"}) == []

        points = [{
            "point": "Legal claims are large.",
            "response": "Lowers confidence."
        }, {
            "point": "Net loss continues.",
            "response": "Operating income turned."
        }]
        router = _Router(
            json.dumps({
                "converged": True,
                "leaning": "hold",
                "confidence": 0.6,
                "rationale": (
                    "Operating income grew 112% to $604 million year to "
                    "date."),
                "deciding_factor": "profit trend",
                "opposing_points": points
            }))
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        state = _state(
            ticker="BA", debate=_two_sides(), agent_outputs={
                AgentRole.FUNDAMENTAL: AgentOutput(
                    role=AgentRole.FUNDAMENTAL, status=AgentStatus.COMPLETED,
                    summary="Forward P/E is 47.32.", score=0.8,
                    raw_output={"latest_quarter": _BA_QUARTER})
            })
        with capture_logs() as logs:
            await judge_module.JudgeAgent().assess_round(state)
        assert _events(logs, "judge.opposing_points_missing") == []
        logged = _events(logs, "judge.opposing_points")
        assert logged[0]["opposing_points"] == points

    def test_parse_converged(self):
        cases = [(True, True), (False, False), ("true", True), ("TRUE", True),
                 ("false", False), ("False", False), ("yes", False),
                 (1, False), (None, False), ("", False)]
        for value, expected in cases:
            got = judge_module._parse_converged(value)
            assert got is expected, repr(value)


_BA_QUARTER: dict[str, Any] = {
    "text": (
        "Latest quarter (SEC XBRL, 10-Q filed 2026-07-28, quarter 2026-04-01 "
        "to 2026-06-30, compared with the quarter ended 2025-06-30): revenue "
        "$24.56B vs $22.75B (+8%); operating income $156M vs -$176M (swung to "
        "a profit); net income -$444M vs -$611M (loss narrowed 27%)"),
    "period_end": "2026-06-30",
    "revenue": 24.56e9,
    "revenue_prior": 22.75e9,
    "operating_income": 156e6,
    "operating_income_prior": -176e6,
    "net_income": -444e6,
    "net_income_prior": -611e6,
    "year_to_date": {
        "months": 6,
        "period_start": "2026-01-01",
        "period_end": "2026-06-30",
        "revenue": 46.78e9,
        "revenue_prior": 42.24e9,
        "operating_income": 604e6,
        "operating_income_prior": 285e6,
        "net_income": -448e6,
        "net_income_prior": -648e6
    }
}

_GOOGL_QUARTER_TEXT = (
    "Latest quarter (SEC XBRL, 10-Q filed 2026-07-28, quarter 2026-04-01 to "
    "2026-06-30, compared with the quarter ended 2025-06-30): revenue $96.43B "
    "vs $81.73B (+18%); operating income $31.27B vs $28.53B (+10%); net "
    "income $62.65B vs $28.20B (+122%)")
_GOOGL_RECORD = (
    "Gain on equity securities, net: $36,915 million "
    "(3 months ended 2026-03-31)")
_UNIT_SLIP = (
    "Net income rose to $62.65B, including $36.915 million in gains on "
    "equity securities.")
_UNIT_FIXED = (
    "Net income rose to $62.65B, including $36,915 million in gains on "
    "equity securities.")


def _googl_state() -> AnalysisState:
    return _state(
        ticker="GOOGL", debate=_two_sides(), agent_outputs={
            AgentRole.FUNDAMENTAL: _completed(
                AgentRole.FUNDAMENTAL,
                {"latest_quarter": {
                    "text": _GOOGL_QUARTER_TEXT
                }}, "Forward P/E is 22.79."),
            AgentRole.FILING: _completed(
                AgentRole.FILING, {"one_off_items": [_GOOGL_RECORD]},
                "The 10-Q filed 2026-07-28 reports results.")
        })


def _answer(rationale: str, leaning: str = "buy") -> str:
    return json.dumps({
        "converged": True,
        "leaning": leaning,
        "confidence": 0.7,
        "rationale": rationale,
        "deciding_factor": "profit trend",
        "opposing_points": [{
            "point": "a",
            "response": "b"
        }, {
            "point": "c",
            "response": "d"
        }]
    })


async def _run_judge(
        router: _Router,
        monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[dict[str, Any]]]:
    monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
    with capture_logs() as logs:
        verdict = await judge_module.JudgeAgent().assess_round(_googl_state())
    return verdict, logs


class TestJudgeFigureRewrite:

    @pytest.mark.asyncio
    async def test_unit_slip_triggers_one_rewrite_that_is_used(
            self, monkeypatch) -> None:
        router = _Router(
            _answer(_UNIT_SLIP), _answer(_UNIT_FIXED, leaning="hold"))
        verdict, logs = await _run_judge(router, monkeypatch)
        assert len(router.calls) == 2
        first = _events(logs, "judge.ungrounded_figures")
        assert len(first) == 1
        assert first[0]["ungrounded_figures"] == ["$36.915 million"]
        assert not _events(logs, "judge.ungrounded_figures_after_rewrite")
        assert verdict.rationale == _UNIT_FIXED
        assert verdict.leaning.value == "hold"
        assert router.tiers[0] == router.tiers[1]
        assert router.kwargs[0] == router.kwargs[1]
        rewrite = router.calls[1]
        assert rewrite[:2] == router.calls[0]
        assert rewrite[2].role == "assistant"
        assert rewrite[2].content == _answer(_UNIT_SLIP)
        request = rewrite[3].content
        assert rewrite[3].role == "user"
        assert "$36.915 million" in request
        assert "value, unit and period" in request
        assert "Do not add items together" in request

    @pytest.mark.asyncio
    async def test_second_or_failed_rewrite_ends_without_a_loop(
            self, monkeypatch) -> None:
        still = "Net income rose to $62.65B with $99.9 billion of gains."
        router = _Router(_answer(_UNIT_SLIP), _answer(still))
        verdict, logs = await _run_judge(router, monkeypatch)
        assert len(router.calls) == 2
        assert verdict.rationale == still
        after = _events(logs, "judge.ungrounded_figures_after_rewrite")
        assert len(after) == 1
        assert after[0]["ungrounded_figures"] == ["$99.9 billion"]
        assert len(_events(logs, "judge.ungrounded_figures")) == 1

        failed_rewrites = {
            "provider-error": RuntimeError("provider down"),
            "not-json": "not json",
            "incomplete-json": json.dumps({"leaning": "sell"})
        }
        for case_id, second in failed_rewrites.items():
            router = _Router(_answer(_UNIT_SLIP), second)
            verdict, logs = await _run_judge(router, monkeypatch)
            assert len(router.calls) == 2, case_id
            assert not verdict.judge_failed, case_id
            assert verdict.rationale == _UNIT_SLIP, case_id
            assert verdict.leaning.value == "buy", case_id
            failed = _events(logs, "judge.ungrounded_rewrite_failed")
            assert len(failed) == 1, case_id


_BROKEN_JSON = (
    '{"converged": true, "leaning": "buy", "confidence": 0.7, '
    '"rationale": "Revenue said "strong" growth."}')


class TestJudgeJsonRecovery:

    @pytest.mark.asyncio
    async def test_unparseable_answer_is_reasked_once(
            self, monkeypatch) -> None:
        router = _Router(_BROKEN_JSON, _answer(_UNIT_FIXED, leaning="hold"))
        verdict, logs = await _run_judge(router, monkeypatch)
        assert len(router.calls) == 2
        assert not verdict.judge_failed and not verdict.retry_round
        assert verdict.leaning.value == "hold"
        assert router.kwargs[0] == router.kwargs[1]
        reask = router.calls[1]
        assert reask[:2] == router.calls[0]
        assert reask[2].role == "assistant"
        assert reask[2].content == _BROKEN_JSON
        request = reask[3].content
        assert "could not be read as JSON" in request
        failure = _events(logs, "judge.unparseable_response")[0]
        assert failure["raw_chars"] == len(_BROKEN_JSON)
        assert failure["error_pos"] == _BROKEN_JSON.index("strong")
        assert f"character {failure['error_pos']} of " in request
        assert "Revenue said " in failure["error_context"]
        assert failure["finish_reason"] == "STOP"
        assert failure["raw_tail"] == _BROKEN_JSON[-300:]
        assert len(_events(logs, "judge.json_reask")) == 1
        assert len(_events(logs, "judge.json_reask_recovered")) == 1

        failed_reasks = {
            "still-unparseable": _BROKEN_JSON,
            "provider-error": RuntimeError("provider down")
        }
        for case_id, second in failed_reasks.items():
            router = _Router(_BROKEN_JSON, second)
            verdict, logs = await _run_judge(router, monkeypatch)
            assert len(router.calls) == 2, case_id
            assert verdict.judge_failed and verdict.retry_round, case_id
            failed = _events(logs, "judge.json_reask_failed")
            assert len(failed) == 1, case_id

    @pytest.mark.asyncio
    async def test_json_slips_parse_without_a_second_call(
            self, monkeypatch) -> None:
        slips = {
            "trailing-text": (
                _answer(_UNIT_FIXED) + "\nDone.", "trailing_chars_ignored", 5),
            "second-object": (
                _answer(_UNIT_FIXED) + _answer("second"),
                "trailing_chars_ignored", len(_answer("second"))),
            "leading-text": (
                "Verdict: " + _answer(_UNIT_FIXED), "leading_chars_ignored",
                9),
            "line-break-in-string": (
                _answer(_UNIT_FIXED).replace("rose to", "rose\nto", 1),
                "control_characters_in_strings", True)
        }
        for case_id, (raw, field, value) in slips.items():
            router = _Router(raw)
            verdict, logs = await _run_judge(router, monkeypatch)
            assert len(router.calls) == 1, case_id
            assert not verdict.judge_failed, case_id
            assert verdict.leaning.value == "buy", case_id
            lenient = _events(logs, "judge.json_parsed_leniently")
            assert len(lenient) == 1, case_id
            assert lenient[0][field] == value, case_id
            assert not _events(logs, "judge.unparseable_response"), case_id

        fenced = parse_json_object('```json\n{"a": 1}\n```')
        assert fenced.data == {"a": 1} and not fenced.trailing_text
        listed = parse_json_object("[1, 2]")
        assert listed.data is None and listed.error == "not a JSON object"
        broken = parse_json_object('{"a": "x" "b"}')
        assert broken.data is None
        assert (broken.error_line, broken.error_col) == (1, 11)
        assert parse_json_object("no object").data is None


class TestPrompts:

    def test_judge_prompt_rules(self) -> None:
        states = {
            "legal-amount-rule": dc.LEGAL_AMOUNT_RULE,
            "overlap-rule": dc.OVERLAP_RULE,
            "free-cash-flow-rule": dc.FREE_CASH_FLOW_RULE,
            "year-to-date-change": "state the year-to-date change next to the",
            "no-cause": "when the evidence states no cause",
            "year-to-date-same-metric": (
                "year-to-date change of the same metric too"),
            "comparison-period": (
                "unusual items in the comparison period a year earlier"),
            "opposing-points": '"opposing_points": [{"point":',
            "two-strongest": "at least the two strongest",
            "compares-the-action": "leaning differs from its action",
            "confidence-move": "confidence move of 0.05 or more also needs",
            "no-anchoring": "do not anchor to it"
        }
        base = judge_module._build_system_prompt(False)
        for case_id, fragment in states.items():
            assert fragment in base, case_id
        omits = {
            "cited-figures": '"cited_figures"',
            "metric-key": '"metric_key"',
            "confidence-difference": "by 0.05 or more"
        }
        for case_id, fragment in omits.items():
            assert fragment not in base, case_id

        prompt = judge_module._build_system_prompt(True, has_track_record=True)
        assert dc.UNUSUAL_ITEMS_RULE in prompt
        assert dc.SOURCE_WORDING_RULE in prompt
        assert "compare it with the latest actual quarter" in prompt
        assert '"change_basis": "new_evidence" | "reweighting"' in prompt
        assert "leaning differs from its action" in prompt
        assert "confidence differs from its confidence by 0.05" not in prompt
        assert "one-off" not in prompt.lower()

        prompt = judge_module._build_system_prompt(
            True, low_participation=True)
        assert "1-3 sentences" not in prompt
        assert '"rationale": "2-8 sentences: first every statement' in prompt

    def test_debate_side_prompts_and_shared_rule_texts(self) -> None:
        for side in ("bull", "bear"):
            prompt = side_module._SYSTEM_PROMPTS[side]  # type: ignore[index]
            for rule in (dc.UNUSUAL_ITEMS_RULE, dc.SOURCE_WORDING_RULE,
                         dc.LEGAL_AMOUNT_RULE, dc.OVERLAP_RULE,
                         dc.FREE_CASH_FLOW_RULE):
                assert rule in prompt, side
            assert "never put 'system', a record id or a data-source name" in (
                prompt), side
            assert "one-off" not in prompt.lower(), side
            assert "state the year-to-date change next to the" in prompt, side
            assert "when the evidence states no cause" in prompt, side
            assert (
                "unusual items in the comparison period a year earlier"
                in prompt), side
            assert "2-4 sentences" not in prompt, side
            assert "3-6 sentences" in prompt, side

        assert "claimed, sought, assessed or awarded" in dc.LEGAL_AMOUNT_RULE
        assert "accrued, paid or escrowed" in dc.LEGAL_AMOUNT_RULE
        assert "Count each event once" in dc.OVERLAP_RULE
        assert "cite either that amount or its quarterly parts" in (
            dc.OVERLAP_RULE)
        assert "XBRL lines and in a filing record" in dc.OVERLAP_RULE
        assert "free cash flow" in dc.FREE_CASH_FLOW_RULE
        assert "do not call capital spending covered" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "after buybacks and/or dividends is negative" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "'operating cash flow covered capital spending' is" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "only 'free cash flow covered (or funded) capital" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "there is no funding gap" in dc.FREE_CASH_FLOW_RULE
        assert "When free cash flow is positive, do not present debt" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "That line covers only the returns its label names" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "a source covers only its own amount" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "never say cash flow funds or supports those returns" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "Name a funding source only with a figure the evidence" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "Debt repaid is a use of cash, not a source" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "say the funding is not shown -- never guess it" in (
            dc.FREE_CASH_FLOW_RULE)
        assert "(debt issued, cash)" not in dc.FREE_CASH_FLOW_RULE
        assert reflection_module.COMPUTED_VALUES_ARE_EXACT in (
            reflection_module._SYSTEM_PROMPT)
        for marker in ("computed:", "marked *", "unrounded", "inconsistent"):
            assert marker in reflection_module.COMPUTED_VALUES_ARE_EXACT
        assert dc.YEAR_TO_DATE_AND_DRIVERS_RULE in dc.PROFIT_TREND_RULE
        for rule in (dc.PERIOD_LABEL_RULE, dc.PROFIT_TREND_RULE):
            assert "one-off" not in rule.lower()
            assert "code-computed" not in rule
        assert dc.CODE_BUILT_LINES_ARE_EVIDENCE in (
            reflection_module._SYSTEM_PROMPT)
        note = sec_edgar._ITEMS_NOTE
        assert "Items can overlap" in note
        assert "impairment also tagged as restructuring" in note
        assert "never add them; cite a single item or the total" in note


_Q2, _Q2_PRIOR = ("2026-04-01", "2026-06-30"), ("2025-04-01", "2025-06-30")
_YTD, _YTD_PRIOR = ("2026-01-01", "2026-06-30"), ("2025-01-01", "2025-06-30")
_UPWARD = (
    "EquitySecuritiesWithoutReadilyDeterminableFairValueUpwardPriceAdjustment"
    "AnnualAmount")
_DOWNWARD = (
    "EquitySecuritiesWithoutReadilyDeterminableFairValue"
    "DownwardPriceAdjustmentAnnualAmount")


def _xbrl_four(q: float, q_prior: float, ytd: float,
               ytd_prior: float) -> list[dict[str, Any]]:
    return [{
        "start": period[0],
        "end": period[1],
        "val": val,
        "form": "10-Q",
        "filed": "2026-07-31",
        "fp": "Q2"
    } for period, val in ((_Q2, q), (_Q2_PRIOR, q_prior), (_YTD, ytd),
                          (_YTD_PRIOR, ytd_prior))]


class TestFigures:

    def test_figure_reading_and_ungrounded_figures(self) -> None:
        readings = {
            "spaced-amounts": (
                "a $ 1.00 billion gain, write-downs of $ 68 million "
                "and $ 280 cash", ["$ 1.00 billion", "$ 68 million", "$ 280"]),
            "unspaced-amounts": (
                "$1.4 trillion, $17.8B, 14%, $7.46 and $999.99",
                ["$1.4 trillion", "$17.8B", "14%", "$7.46", "$999.99"]),
            "dollar-sign-before-a-word": ("the $ sign and $ amounts", []),
            "parenthesized-amounts": (
                "losses of $(1,180) million, $(1.246) billion, $ (83) and "
                "$(13)M",
                ["$(1,180) million", "$(1.246) billion", "$ (83)", "$(13)M"]),
            "other-readings-next-to-parentheses": (
                "$1.4 trillion, $ 68 million, 14%, $7.46 (up) and ($5.2 "
                "billion)", [
                    "$1.4 trillion", "$ 68 million", "14%", "$7.46",
                    "$5.2 billion"
                ])
        }
        for case_id, (text, expected) in readings.items():
            found = [m.group(0) for m in dc._FIGURE_RE.finditer(text)]
            assert found == expected, case_id
        ungrounded = {
            "plain-dollar-amounts-checked": (
                "Price $151.23, EPS $7.46, target $999.99",
                "close=151.23 on 2026-09-12; diluted EPS $7.46", {"$999.99"}),
            "rounding-tolerated": ("near $228", "close=227.40", set()),
            "both-ends-of-a-dash-range": (
                "total expenses of $169 billion and $165B",
                "expenses of $165-169 billion", set()),
            "range-forms-carry-currency-and-scale": (
                "$169 billion, $145 billion, 7% and $2.05",
                "$165 to $169 billion; $130\u2013$145B; 5-7%; $1.95 to 2.05",
                set()),
            "a-range-claim-reports-the-range": (
                "$170-175 billion and 5-9%",
                "expenses of $165-169 billion, growth 5% and 7%",
                {"$170-175 billion", "5-9%"}),
            "descending-or-signed-change-is-no-range": (
                "$500 billion and 4%",
                "$500 to $1.2 billion; revenue $10,365 -4%", {"$500 billion"})
        }
        for case_id, (claim, evidence, expected) in ungrounded.items():
            got = dc.find_ungrounded_figures(claim, evidence)
            assert got == expected, case_id

    def test_equity_adjustments_shown_inside_the_gains(self):
        facts = {
            "facts": {
                "us-gaap": {
                    t: {
                        "units": {
                            "USD": f
                        }
                    }
                    for t, f in {
                        "Revenues": _xbrl_four(100e9, 90e9, 190e9, 170e9),
                        "OperatingIncomeLoss": _xbrl_four(
                            20e9, 18e9, 38e9, 34e9),
                        "NetIncomeLoss": _xbrl_four(17e9, 15e9, 32e9, 28e9),
                        "GainLossOnInvestments": _xbrl_four(
                            7_770e6, 2_250e6, 23_710e6, 2_070e6),
                        _UPWARD: _xbrl_four(4_900e6, 267e6, 7_500e6, 330e6),
                        _DOWNWARD: _xbrl_four(112e6, 33e6, 134e6, 52e6)
                    }.items()
                }
            }
        }
        q = sec_edgar._quarter_results_from_facts(facts, "X")
        assert q is not None
        text = sec_edgar.quarter_results_payload(q)["text"]
        line = next(
            line for line in text.split("\n")
            if line.startswith("Below operating income (SEC XBRL"))
        assert (
            "gains (losses) on investments +$7.77B vs +$2.25B; of which "
            "upward adjustments of equity securities without a readily "
            "determinable fair value (gain) $4.90B vs $267M (included in the "
            "gains (losses) on investments above); of which downward "
            "adjustments") in line
        ytd = sec_edgar._items_text(
            q.year_to_date.below_operating)  # type: ignore[union-attr]
        assert "of which upward adjustments" in ytd
        payload = sec_edgar.quarter_results_payload(q)
        labels = [i["label"] for i in payload["below_operating"]]
        assert not any(label.startswith("of which") for label in labels)

        text = (
            "AMD | note: operating margin (10.7%) is below net margin (12.5%)"
            " -- check directly\nLatest quarter (SEC XBRL, 10-Q): revenue "
            "$11.54B")
        notes = [m.group(1) for m in _TOOL_NOTE_RE.finditer(text)]
        assert notes == [
            "note: operating margin (10.7%) is below net margin (12.5%) "
            "-- check directly"
        ]


_META_YTD = (
    "Discrete income tax benefit from the One Big Beautiful Bill Act: $8.03 "
    "billion (3 months ended 2026-03-31; line: tax) [10-Q filed 2026-04-30] "
    'Filing text: "As a result, we recognized an $8.03 billion discrete '
    'income tax benefit during the first quarter of 2026"')
_META_PRIOR = (
    "Unrealized loss on marketable equity securities: $511 million (3 months "
    "ended 2025-06-30; line: non_operating) [10-Q filed 2025-07-31]")
_META_RESULT = (
    "net income, 6 months ended 2026-06-30: $42.62 billion vs $34.98 billion "
    "a year earlier [10-Q filed 2026-07-30]")
_KEY_RECORDS = [
    "Android antitrust: judgment; exposure $5.2B; next event: none stated",
    "Trial in the securities class action set for 2026-09-15 (date passed)",
    "Guidance: Q3 2026 net sales $174B to $179B"
]


def _meta_filing(
        status: AgentStatus = AgentStatus.COMPLETED,
        **raw: Any) -> AgentOutput:
    return _completed(
        AgentRole.FILING, raw, "The 10-Q filed 2026-07-30 reports results.",
        status)


def _tsla_fundamental(
        ytd: dict[str, Any] | None, op_yoy: float = -56.9) -> AgentOutput:
    payload: dict[str, Any] = {
        "text": (
            "Latest quarter (SEC XBRL): operating income $398M vs $923M (-57%)"
        ),
        "period_end": "2026-06-30",
        "revenue": 28.24e9,
        "revenue_prior": 22.50e9,
        "revenue_yoy_pct": 25.5,
        "operating_income": 398e6,
        "operating_income_prior": 923e6,
        "operating_income_yoy_pct": op_yoy,
        "net_income": 1.11e9,
        "net_income_prior": 1.17e9,
        "net_income_yoy_pct": -5.1
    }
    if ytd is not None:
        payload["year_to_date"] = ytd
    return _completed(AgentRole.FUNDAMENTAL, {"latest_quarter": payload})


class TestEvidenceBlocks:

    def test_absent_or_bad_filing_period_lists_render_nothing(self) -> None:
        old = _state(
            ticker="META",
            agent_outputs={AgentRole.FILING: _meta_filing(one_off_items=[])})
        assert dc.filing_period_texts(old) == []
        bad = _state(
            ticker="META", agent_outputs={
                AgentRole.FILING: _meta_filing(
                    year_to_date_unusual_items="not a list",
                    prior_year_unusual_items=[None, 3, "  ", "a  b"])
            })
        assert dc.filing_raw_list(bad, dc.YEAR_TO_DATE_UNUSUAL_KEY) == []
        assert dc.filing_raw_list(bad, dc.PRIOR_YEAR_UNUSUAL_KEY) == ["a b"]
        failed = _state(
            ticker="META", agent_outputs={
                AgentRole.FILING: _meta_filing(
                    AgentStatus.FAILED, newest_filing_results=[_META_RESULT])
            })
        assert dc.filing_period_texts(failed) == []

    def test_judge_profit_trend_block_with_and_without_ytd(self) -> None:
        state = _state(
            ticker="META", agent_outputs={
                AgentRole.FILING: _meta_filing(
                    year_to_date_unusual_items=[_META_YTD],
                    prior_year_unusual_items=[_META_PRIOR],
                    newest_filing_results=[_META_RESULT])
            })
        text = judge_module.profit_trend_line(state)
        assert _META_YTD not in text and _META_RESULT not in text
        assert "Unusual items in earlier quarters of this year-to-date" in text
        evidence = dc.format_evidence(state)
        assert _META_YTD in evidence and _META_PRIOR in evidence
        assert _META_RESULT not in evidence
        assert "must say the change includes them and give their amounts" in (
            text)
        assert "do not compute an adjusted figure" in text

        declined = _state(
            ticker="TSLA",
            agent_outputs={AgentRole.FUNDAMENTAL: _tsla_fundamental(None)})
        assert "year-to-date figures" not in judge_module.profit_trend_line(
            declined)
        flat = _state(
            ticker="TSLA", agent_outputs={
                AgentRole.FUNDAMENTAL: _tsla_fundamental(None, op_yoy=30.0)
            })
        assert judge_module.profit_trend_line(flat) == ""
        empty = _state(
            ticker="TSLA", agent_outputs={
                AgentRole.FUNDAMENTAL: _tsla_fundamental({"months": 6},
                                                         op_yoy=30.0)
            })
        assert judge_module.profit_trend_line(empty) == ""

    def test_key_records_and_one_off_items_in_evidence(self) -> None:
        state = _state(
            ticker="XOM", agent_outputs={
                AgentRole.FILING: _completed(
                    AgentRole.FILING, {"key_records": _KEY_RECORDS})
            })
        line = (
            "filing (key records from the newest filing, checked against "
            "quotes): " + "; ".join(_KEY_RECORDS))
        assert f"- {line}" in dc.format_evidence(state)
        assert line in dc.format_grounding_reference(state)
        assert (
            dc.find_ungrounded_figures(
                "a $5.2B exposure and Q3 sales of $174B to $179B",
                dc.format_grounding_reference(state)) == set())
        messy = _state(
            ticker="XOM", agent_outputs={
                AgentRole.FILING: _completed(
                    AgentRole.FILING,
                    {"key_records": [" a  b ", None, 3, "a b", ""]})
            })
        assert dc.key_records(messy) == ["a b"]

        items = [f"item {n}: ${n} million" for n in range(1, 9)]
        state = _state(
            ticker="AMZN", agent_outputs={
                AgentRole.FILING: _completed(
                    AgentRole.FILING, {"one_off_items": items})
            })
        with capture_logs() as logs:
            evidence = dc.format_evidence(state)
        assert "item 6: $6 million" in evidence and "item 7:" not in evidence
        dropped = _events(logs, "debate.one_off_items_dropped")
        assert dropped and dropped[0]["dropped"] == 2
        overlong = _state(
            ticker="AMZN", agent_outputs={
                AgentRole.FILING: _completed(
                    AgentRole.FILING, {"one_off_items": ["x" * 900]})
            })
        with capture_logs() as logs:
            items = dc.one_off_items(overlong)
        assert items == ["x" * 500]
        assert any(e["event"] == "debate.one_off_item_cut" for e in logs)


def _amzn_reflection_verdict() -> ReflectionVerdict:
    issues = [
        ReflectionIssue(
            code=ISSUE_LLM_REVIEW, role="bull", kind="not_engaged",
            text="Bull (not engaged): ignores Bear's point on debt"),
        ReflectionIssue(
            code=ISSUE_LLM_REVIEW, role="bear", kind="unsupported",
            figure="$9B",
            text="Bear (unsupported): $9B buyback not in evidence"),
        ReflectionIssue(
            code=ISSUE_LLM_REVIEW, role="bull", kind="unsupported",
            figure="$14 billion",
            text="Bull (unsupported): $14 billion exposure not in evidence"),
        ReflectionIssue(
            code=ISSUE_NO_EVIDENCE_NOT_RETRIED, role="news",
            text="news agent found no evidence, not retried"),
        ReflectionIssue(
            code=ISSUE_NOT_RETRIED, role="bear", failure_kind="rate_limit",
            text="bear failed, not retried")
    ]
    return ReflectionVerdict(
        round_number=1, verified=False, issues=[i.text for i in issues],
        issue_details=issues)


def _both_sides_flagged_state() -> AnalysisState:
    issues = [
        ReflectionIssue(
            code=ISSUE_LLM_REVIEW, role=side, kind="unsupported", figure="25%",
            text=f"{side} claims 25% growth") for side in ("bull", "bear")
    ]
    return _state(
        ticker="AAPL", agent_outputs={
            AgentRole.MARKET: _plain(AgentRole.MARKET, "close 151.23, up 4%")
        }, reflection_history=[
            ReflectionVerdict(
                round_number=1, verified=False,
                issues=[i.text for i in issues], issue_details=issues)
        ])


class TestDebateSides:

    @pytest.mark.asyncio
    async def test_one_regeneration_with_mechanical_recheck(self, monkeypatch):
        assert dc.DEBATE_MAX_REGENERATIONS == 1
        for side in ("bull", "bear"):
            router = _Router("Growth 25% [market].", "Still 25% [market].")
            monkeypatch.setattr(
                side_module, "LLMRouter", lambda router=router: router)
            argument = await side_module.DebateSideAgent(side).make_argument(
                _both_sides_flagged_state())
            assert argument.argument == "Still 25% [market].", side
            assert len(router.calls) == 2, side
            assert "primary_extra_server_error_keys" in router.kwargs[0], side
            assert "primary_extra_server_error_keys" in router.kwargs[1], side
            assert "25%" in router.calls[1][-1].content, side
            assert argument.citations == ["market"], side

            router = _Router("Growth 25% [market].", "Up 4% [market].")
            monkeypatch.setattr(
                side_module, "LLMRouter", lambda router=router: router)
            argument = await side_module.DebateSideAgent(side).make_argument(
                _both_sides_flagged_state())
            assert argument.argument == "Up 4% [market].", side
            assert len(router.calls) == 2, side

    @pytest.mark.asyncio
    async def test_still_ungrounded_after_the_one_regeneration(self) -> None:
        state = _state(
            ticker="META", agent_outputs={AgentRole.FILING: _meta_filing()})
        router = _Router(
            "Revenue rose to $99.9B [filing].",
            "Revenue rose to $88.8B [filing].")
        with capture_logs() as logs:
            argument = await dc.generate_debate_argument(
                side="bull",
                state=state,
                llm_router=router,  # type: ignore[arg-type]
                llm_tier=None,  # type: ignore[arg-type]
                system_prompt="s")
        assert argument.argument == "Revenue rose to $88.8B [filing]."
        events = _events(logs, "bull.final_draft_still_flagged")
        assert len(events) == 1
        assert events[0]["ungrounded_figures"] == ["$88.8B"]
        assert _events(logs, "debate.regenerated_still_ungrounded") == []
        assert _events(logs, "debate.contradicted_regenerated") == []

    @pytest.mark.asyncio
    async def test_prior_issues_are_split_by_side(self) -> None:
        state = _state(
            ticker="AMZN", reflection_history=[_amzn_reflection_verdict()])
        bull = dc.format_prior_reflection_issues(state, "bull")
        assert bull is not None
        problems, answer = bull.split("did not answer")
        assert "Bull (unsupported): $14 billion exposure" in problems
        assert "ignores Bear's point on debt" not in problems
        assert "ignores Bear's point on debt" in answer

        verdict = ReflectionVerdict(
            round_number=1, verified=False, issues=["x"], issue_details=[
                ReflectionIssue(
                    code=ISSUE_LLM_REVIEW, role="news", kind="inconsistent",
                    text="x")
            ])
        state = _state(ticker="AMZN", reflection_history=[verdict])
        assert dc.format_prior_reflection_issues(state, "bull") is None
        assert (
            dc.format_prior_reflection_issues(_state(ticker="AMZN"), "bear")
            is None)

        state = _state(
            ticker="AMZN", reflection_history=[_amzn_reflection_verdict()])
        router = _Router("Debt rose [filing].")
        await dc.generate_debate_argument(
            side="bear",
            state=state,
            llm_router=router,  # type: ignore[arg-type]
            llm_tier=LLMTier.CHEAP,
            system_prompt="sys")
        user = router.calls[0][1].content
        assert "$9B buyback" in user and "ignores Bear's point" not in user


def _fundamental_debate_state(**overrides: Any) -> AnalysisState:
    return _state(
        ticker="AAPL", agent_outputs={
            AgentRole.FUNDAMENTAL: _plain(
                AgentRole.FUNDAMENTAL,
                "Gross margin 45% and revenue grew 16%.")
        }, debate=DebateState(
            round_number=1, arguments=[
                DebateArgument(
                    round_number=1, side="bull",
                    argument="Margin 45% [fundamental]"),
                DebateArgument(
                    round_number=1, side="bear", argument="Growth slowing")
            ]), **overrides)


async def _review(
        monkeypatch, payload: Any, state: AnalysisState | None = None):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    router = _Router(content)
    monkeypatch.setattr(reflection_module, "LLMRouter", lambda: router)
    return await ReflectionAgent().check(state or _fundamental_debate_state())


def _issue(
        role: str | None, kind: str, description: str, *,
        figure: str | None = None,
        quote: str = "Gross margin 45% and revenue grew 16%"
) -> dict[str, Any]:
    return {
        "role": role,
        "kind": kind,
        "figure": figure,
        "claimed_period": None,
        "evidence_quote": quote,
        "description": description
    }


def _failed_verdict(reason: str) -> JudgeVerdict:
    return JudgeVerdict(
        round_number=1, converged=False, leaning=RecommendationAction.HOLD,
        confidence=0.0, rationale="Judge unavailable", judge_failed=True,
        failure_reason=reason, retry_round=False)


class TestReflection:

    @pytest.mark.usefixtures("clean_pacer")
    @pytest.mark.asyncio
    async def test_skipped_review_is_not_verified(self, monkeypatch):
        monkeypatch.setattr(
            reflection_module.settings,
            "REFLECTION_REVIEW_DELAYED_RETRY_SECONDS", 0.0)
        monkeypatch.setattr(
            reflection_module, "LLMRouter", lambda: _RaisingRouter(
                LLMServerOverloadedError("503 high demand")))
        verdict = await ReflectionAgent().check(
            _state(agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)}))
        assert verdict.retry_agents == {}
        assert verdict.verified is False
        assert verdict.llm_review_ran is False
        assert "LLMServerOverloadedError" in verdict.llm_review_skip_reason
        meta = verdict.review_meta_line()
        assert meta.startswith(
            "REVIEW_META: llm_review_ran=false; usable_agents=1/1")

        for reason in ("judge_unavailable", "debate_unavailable"):
            router = _CountingReviewRouter()
            state = _state(
                ticker="DIS", agent_outputs={
                    AgentRole.MARKET: _priced(
                        AgentRole.MARKET, "DIS closed at 106.15")
                }, debate=DebateState(
                    arguments=_args(), round_number=1,
                    verdicts=[_failed_verdict(reason)]))
            verdict = await ReflectionAgent(llm_router=router).check(state)
            assert router.calls == 0, reason
            assert verdict.llm_review_ran is False, reason
            assert reason in (verdict.llm_review_skip_reason or ""), reason
            assert verdict.verified is False, reason

    @pytest.mark.asyncio
    async def test_retry_candidates_by_evidence_and_failure_kind(
            self, monkeypatch):
        monkeypatch.setattr(
            reflection_module, "LLMRouter",
            lambda: _Router(_CLEAN_REVIEW, repeat=True))
        state = _state(
            agent_outputs={
                AgentRole.MARKET: _ok(
                    AgentRole.MARKET, summary="AMD at $440.50",
                    raw_output={"scratchpad": []}),
                AgentRole.TECHNICAL: _ok(AgentRole.TECHNICAL),
                AgentRole.SENTIMENT: _ok(
                    AgentRole.SENTIMENT, raw_output={"scratchpad": []})
            })
        verdict = await ReflectionAgent().check(state)
        assert "zero successful tool results" in (
            verdict.retry_agents[AgentRole.MARKET])
        assert AgentRole.SENTIMENT not in verdict.retry_agents

        monkeypatch.setattr(
            reflection_module, "LLMRouter",
            lambda: _Router(_CLEAN_REVIEW, repeat=True))
        state = _state(
            agent_outputs={
                AgentRole.MARKET: _ok(AgentRole.MARKET),
                AgentRole.MACRO: _failed(AgentRole.MACRO, "rate_limit"),
                AgentRole.NEWS: _failed(AgentRole.NEWS, "non_english"),
                AgentRole.RISK: _failed(AgentRole.RISK, "other")
            })
        verdict = await ReflectionAgent().check(state)
        assert set(verdict.retry_agents) == {AgentRole.RISK}
        assert "agent failed this run (other)" in (
            verdict.retry_agents[AgentRole.RISK])

        review = json.dumps({
            "checked_claims": ["x"],
            "issues": ["news is thin"],
            "retry_roles": {
                "news": "search again"
            }
        })
        monkeypatch.setattr(
            reflection_module, "LLMRouter",
            lambda: _Router(review, repeat=True))
        news = AgentOutput(
            role=AgentRole.NEWS, summary="fine", score=0.35,
            is_well_supported=False, raw_output={
                "scratchpad": [_tool_entry("No news articles found")],
                "no_evidence": True
            })
        state = _state(
            agent_outputs={
                AgentRole.MARKET: _ok(AgentRole.MARKET),
                AgentRole.NEWS: news
            })
        verdict = await ReflectionAgent().check(state)
        assert AgentRole.NEWS not in verdict.retry_agents
        assert any(
            "found no evidence, not retried" in i for i in verdict.issues)

    @pytest.mark.asyncio
    async def test_bull_regeneration_rules(self, monkeypatch):
        review = json.dumps({
            "checked_claims": ["x"],
            "issues": [{
                "role": "bull",
                "kind": "not_engaged",
                "evidence_quote": "bear case",
                "description": "Bull argument is missing"
            }]
        })
        monkeypatch.setattr(
            reflection_module, "LLMRouter",
            lambda: _Router(review, repeat=True))
        state = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            debate=DebateState(round_number=1, arguments=_args(bull_ok=False)))
        verdict = await ReflectionAgent().check(state)
        assert AgentRole.BULL not in verdict.retry_agents
        assert any(
            "debate side unavailable, not regenerated" in i
            for i in verdict.issues)

        verdict = await _review(
            monkeypatch, {
                "checked_claims": ["margin"],
                "issues": [
                    _issue(
                        "bull", "unsupported", "a 45% gross margin",
                        figure="45%"),
                    _issue(
                        "bull", "unsupported", "a $30B buyback", figure="$30B")
                ]
            })
        assert len(verdict.issues) == 1
        assert "$30B buyback" in verdict.issues[0]
        assert "$30B buyback" in verdict.retry_agents[AgentRole.BULL]

    @pytest.mark.asyncio
    async def test_badly_shaped_review_parts_and_issue_text(self, monkeypatch):
        state = _fundamental_debate_state()
        state.debate.verdicts.append(
            JudgeVerdict(
                round_number=1, converged=True,
                leaning=RecommendationAction.HOLD, confidence=0.5,
                rationale="r"))
        state.agent_outputs[AgentRole.MARKET] = AgentOutput(
            role=AgentRole.MARKET, summary="thin", score=0.35,
            is_well_supported=False, hit_iteration_cap=True)
        verdict = await _review(
            monkeypatch, {
                "checked_claims": [],
                "issues": {
                    "market": "x"
                }
            }, state)
        assert AgentRole.MARKET in verdict.retry_agents
        assert verdict.llm_review_ran is False
        assert "issues" in verdict.llm_review_skip_reason
        verdict = await _review(
            monkeypatch, {
                "checked_claims": [{
                    "a": 1
                }],
                "issues": []
            })
        assert verdict.llm_review_ran is False
        verdict = await _review(
            monkeypatch, {
                "checked_claims": ["x"],
                "issues": [
                    "Bull invented a deal",
                    _issue("bear", "stale", "old figure")
                ]
            })
        assert verdict.llm_review_ran is True
        assert [i.role for i in verdict.issue_details] == ["bear"]

        text = reflection_module._issue_text(
            "bull", "not_engaged", "ignores the debt point",
            evidence_quote="debt rose to ~$100B " + "y" * 300)
        assert "the other side's point: \"debt rose to ~$100B" in text
        assert text.endswith('..."]')
        assert reflection_module._issue_text(None, None,
                                             "general") == ("(issue) general")


class _GeminiReply:

    class _Usage:
        prompt_token_count = 1
        candidates_token_count = 1
        thoughts_token_count = None

    class _Candidate:
        finish_reason = "STOP"

    text = "ok"
    usage_metadata = _Usage()
    candidates = (_Candidate(), )


class _ScriptedKeyPool:

    def __init__(self, labels: list[str], script: list[str]) -> None:
        self.labels = labels
        self.script = list(script)
        self.used: list[str] = []

        async def generate_content(*, model, contents, config):
            if len(self.script) > 1:
                outcome = self.script.pop(0)
            else:
                outcome = self.script[0]
            if outcome == "503":
                raise genai_errors.ServerError(
                    503, {
                        "error": {
                            "code": 503,
                            "status": "UNAVAILABLE",
                            "message": "busy"
                        }
                    })
            if outcome == "hang":
                await asyncio.sleep(3600)
            return _GeminiReply()

        self._client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)))

    def key_count(self) -> int:
        return len(self.labels)

    def available_labels_excluding(self, excluded):
        return [lbl for lbl in self.labels if lbl not in excluded]

    def client_for(self, label):
        self.used.append(label)
        return self._client


class _ChainAdapter(LLMProviderAdapter):
    name = "google"

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {m: list(v) for m, v in script.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete(
            self, messages, *, model, temperature=0.2, max_tokens=1024,
            response_format=None, timeout_seconds=None, response_schema=None,
            **extra: Any) -> LLMResponse:
        self.calls.append((model, extra))
        outcomes = self.script[model]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "503":
            raise LLMServerOverloadedError(f"{model} 503")
        if outcome == "timeout":
            await request_pacer.mark_unavailable_until(
                ("google", "timeout", model),
                time.monotonic() + 300.0)
            raise LLMRequestTimeoutError(f"{model} timed out")
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


def _hi() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="hi")]


@pytest.mark.usefixtures("clean_pacer")
class TestHighestAccuracyModels:

    @pytest.mark.asyncio
    async def test_default_timeouts_and_timeouts_without_cooldown(
            self, monkeypatch):
        defaults = {
            "gemini-3.8-flash": 30.0,
            "gemini-3.7-flash": 60.0,
            "gemini-3.5-flash-lite": 60.0
        }
        for model, seconds in defaults.items():
            got = google_module._default_request_timeout(model)
            assert got == seconds, model
        monkeypatch.setattr(
            google_module, "_pool", _ScriptedKeyPool(["primary"], ["hang"]))
        with pytest.raises(LLMRequestError) as info:
            await google_module.GoogleAdapter().complete(
                _hi(), model="gemini-3.7-flash", timeout_seconds=0.05)
        assert not isinstance(info.value, LLMRequestTimeoutError)
        assert not await google_module.google_model_recently_timed_out(
            "gemini-3.7-flash")
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS",
            0.05)
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS", 0.0)
        monkeypatch.setattr(
            config.settings, "LLM_GOOGLE_MODEL_REQUEST_TIMEOUT_SECONDS", {})
        monkeypatch.setattr(
            google_module, "_pool", _ScriptedKeyPool(["primary"], ["hang"]))
        with pytest.raises(LLMRequestTimeoutError):
            await google_module.GoogleAdapter().complete(
                _hi(), model="gemini-3.7-flash")
        assert not await google_module.google_model_recently_timed_out(
            "gemini-3.7-flash")

    @pytest.mark.asyncio
    async def test_timed_out_model_is_skipped_by_later_calls(
            self, monkeypatch):
        prefix = "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY"
        for suffix, model in (("", "m1"), ("_FALLBACK", "m2"),
                              ("_FALLBACK_2", "m3"), ("_FALLBACK_3", "m4")):
            monkeypatch.setattr(config.settings, prefix + suffix, model)
        monkeypatch.setattr("ai.llm.router.asyncio.sleep", _no_sleep)
        adapter = _ChainAdapter({
            "m1": ["503"],
            "m2": ["timeout"],
            "m3": ["ok"],
            "m4": ["ok"]
        })
        router = LLMRouter(providers={"google": adapter})
        first = await router.complete(
            LLMTier.HIGHEST_ACCURACY, _hi(), provider="google", retries=4,
            ignore_server_error_cooldown=True)
        second = await router.complete(
            LLMTier.HIGHEST_ACCURACY, _hi(), provider="google", retries=4,
            ignore_server_error_cooldown=True)
        assert first.content == second.content == "answer from m3"
        assert adapter.models == ["m1", "m2", "m3", "m1", "m3"]


@pytest.mark.usefixtures("clean_pacer")
class TestFinishAnswerValidation:

    @pytest.mark.asyncio
    async def test_rejection_uses_the_validation_suffix(self):
        default = ReActAgentLoop(
            role=AgentRole.FILING, tools=[], system_prompt="x",
            max_iterations=2,
            llm_router=_CountingReviewRouter())  # type: ignore[arg-type]
        assert default.finish_validation_suffix.startswith(
            "Recompute using the actual figures already shown above")

        class _Echo(Tool):
            name = "get_price_history"
            description = "echo"
            parameters: ClassVar[dict[str, Any]] = {
                "type": "object",
                "properties": {}
            }

            async def run(self, **kwargs: Any) -> str:
                return "Close 106.15 on 2026-09-25"

        router = _Router(
            json.dumps({
                "thought": "t",
                "action": "get_price_history",
                "action_input": {},
                "finish_answer": None
            }),
            json.dumps({
                "thought": "t",
                "action": None,
                "action_input": None,
                "finish_answer": "Close 106.15 on 2026-09-25, bad"
            }),
            json.dumps({
                "thought": "t",
                "action": None,
                "action_input": None,
                "finish_answer": "Close 106.15 on 2026-09-25"
            }))
        loop = ReActAgentLoop(
            role=AgentRole.FILING,
            tools=[_Echo()],
            system_prompt="x",
            max_iterations=4,
            llm_router=router,  # type: ignore[arg-type]
            finish_answer_validator=(
                lambda a, m, p: "bad answer." if "bad" in a else None),
            finish_validation_suffix="CUSTOM SUFFIX.")
        result = await loop.run("task")
        rejections = [
            e.observation for e in result.scratchpad
            if "not accepted" in str(e.observation)
        ]
        assert rejections == [
            "[Your finish_answer was not accepted: bad answer. "
            "CUSTOM SUFFIX.]"
        ]
        assert result.final_answer == "Close 106.15 on 2026-09-25"


class _FakeMarket:

    def __init__(self, prices: dict[str, str]):
        self.prices = prices

    async def get_current_price(self, ticker):
        return PriceQuote(
            ticker=ticker, price=Decimal(self.prices[ticker]),
            as_of=datetime.now(UTC), source="fake")


@pytest.fixture
def sizing_env(monkeypatch):
    env: dict[str, Any] = {
        "holdings": [{
            "ticker": "MSFT",
            "quantity": 20.0
        }, {
            "ticker": "AMD",
            "quantity": 5.0
        }],
        "cash": 7699.0,
        "prices": {
            "AMD": "600.00",
            "MSFT": "500.00"
        }
    }
    market = _FakeMarket(env["prices"])

    async def _holdings(portfolio_id):
        return env["holdings"]

    async def _cash(portfolio_id):
        return env["cash"]

    monkeypatch.setattr(
        sizing_module, "get_market_data_provider", lambda: market)
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _holdings)
    monkeypatch.setattr(sizing_module, "get_portfolio_cash_balance", _cash)
    return env


def _sizing_json(tiers: list[tuple[float, float]], window: int = 14) -> str:
    return json.dumps({
        "window_days": window,
        "tiers": [{
            "target_price": p,
            "quantity": q
        } for p, q in tiers]
    })


def _sizing_only(note: str | None) -> bool:
    return note is not None and note.startswith(
        "Tier sizing (computed in code):")


_AMD_BUY_TIERS = [(615.35, 6.25), (585.0, 6.5)]


class TestPositionSizing:

    @pytest.mark.asyncio
    async def test_buy_within_budget_after_same_ticker_replacement(
            self, sizing_env, monkeypatch):
        monkeypatch.setattr(
            sizing_module.settings, "MAX_POSITION_WEIGHT_PCT", 100.0)
        router = _Router(_sizing_json(_AMD_BUY_TIERS), repeat=True)
        result, note = await generate_position_sizing(
            ticker="AMD", action=RecommendationAction.BUY, confidence=0.8,
            reasoning="r", portfolio_id="p", committed_buy_cash=Decimal(0),
            llm_router=router)
        assert _sizing_only(note)
        assert result is not None and len(result.tiers) == 2
        prompt = router.calls[0][-1].content
        assert (
            "Current position in AMD: 5.0 shares, market value 3000.00"
            in prompt)
        assert (
            "Total portfolio net value (holdings at live prices + cash): "
            "20699.00") in prompt
        assert "Current weight of AMD in the portfolio: 14.5%" in prompt
        assert "Budget for this recommendation's BUY tiers" in prompt

    @pytest.mark.asyncio
    async def test_bad_tiers_are_dropped_or_never_requested(self, sizing_env):
        router = _Router(
            _sizing_json([(700.0, 3.0), (720.0, 3.0)]), repeat=True)
        result, note = await generate_position_sizing(
            ticker="AMD", action=RecommendationAction.SELL, confidence=0.7,
            reasoning="r", portfolio_id="p", llm_router=router)
        assert result is None and "more than the 5.0 held" in note

        router = _Router(
            _sizing_json([(600.0, -50.0), (590.0, 2.0)]), repeat=True)
        result, note = await generate_position_sizing(
            ticker="AMD", action=RecommendationAction.BUY, confidence=0.8,
            reasoning="r", portfolio_id="p", llm_router=router)
        assert _sizing_only(note)
        assert [t.quantity for t in result.tiers] == [Decimal("2.0")]

        raw = json.dumps({
            "window_days": 3650,
            "tiers": [{
                "target_price": 100.0,
                "quantity": 2.0
            }]
        })
        capped, _ = _validated_sizing(
            raw, action=RecommendationAction.BUY, ticker="KO",
            budget=Decimal("1000"), cash=Decimal("1000"),
            committed_buy_cash=Decimal("0"), capped_by_weight=False,
            min_notional=Decimal("100"), position=None)
        assert capped is not None and capped.window_days == 30

        no_tiers = {
            "buy-without-budget": (
                _sizing_json(_AMD_BUY_TIERS), None, {
                    "action": RecommendationAction.BUY,
                    "confidence": 0.8,
                    "committed_buy_cash": Decimal("8000")
                }, "leaves no budget"),
            "sell-not-held": (
                _sizing_json([(700.0, 1.0)]), [{
                    "ticker": "MSFT",
                    "quantity": 20.0
                }], {
                    "action": RecommendationAction.SELL,
                    "confidence": 0.7
                }, "bearish view; no position held")
        }
        for case_id, (sizing, holdings, kwargs,
                      expected) in (no_tiers.items()):
            if holdings is not None:
                sizing_env["holdings"] = holdings
            router = _Router(sizing, repeat=True)
            result, note = await generate_position_sizing(
                ticker="AMD", reasoning="r", portfolio_id="p",
                llm_router=router, **kwargs)
            assert result is None and expected in note, case_id
            assert router.calls == [], case_id

    @pytest.mark.asyncio
    async def test_unpriced_holding_makes_weight_unavailable(
            self, sizing_env, monkeypatch):
        sizing_env["holdings"].append({"ticker": "ZZZ", "quantity": 1.0})

        class _Partial(_FakeMarket):

            async def get_current_price(self, ticker):
                if ticker == "ZZZ":
                    raise ProviderRequestError("down")
                return await super().get_current_price(ticker)

        partial = _Partial(sizing_env["prices"])
        monkeypatch.setattr(
            sizing_module, "get_market_data_provider", lambda: partial)
        router = _Router(_sizing_json([(590.0, 2.0)]), repeat=True)
        await generate_position_sizing(
            ticker="AMD", action=RecommendationAction.BUY, confidence=0.8,
            reasoning="r", portfolio_id="p", llm_router=router)
        assert "unavailable this run (no live value for ZZZ)" in (
            router.calls[0][-1].content)


async def _invalidate(portfolio_id, ticker, action, prices):
    from database import SessionLocal
    from domains.analysis.repository import RecommendationTierRepository

    async with SessionLocal() as s:
        closed = await RecommendationTierRepository(
            s).invalidate_unfilled_tiers_for_new_recommendation(
                portfolio_id=portfolio_id, ticker=ticker,
                new_action=RecommendationAction(action),
                new_prices=[Decimal(p) for p in prices])
        await s.commit()
        return closed


async def _follow(analysis_id, portfolio_id):
    from unittest.mock import AsyncMock, patch
    import clients.core_client as core_client
    from database import SessionLocal
    from domains.decision.schema import InvestmentDecisionCreate
    from domains.decision.service import DecisionService
    from shared.security import CurrentUser

    user = CurrentUser(user_id=uuid4(), role="user")
    owner = {
        "portfolio_id": str(portfolio_id),
        "owner_id": str(user.user_id),
        "organization_id": None,
        "visibility": "private"
    }
    async with SessionLocal() as s:
        with patch.object(core_client, "get_portfolio_owner",
                          AsyncMock(return_value=owner)):
            await DecisionService(s).record_decision(
                user=user, analysis_id=analysis_id,
                data=InvestmentDecisionCreate(
                    action_taken="followed", portfolio_id=portfolio_id))
        await s.commit()


class TestTierReplacement:

    @pytest.mark.asyncio
    async def test_new_recommendation_replaces_unfilled_tiers(self, rows):
        pf = uuid4()
        _, low_buy, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "500", "2", _P)])
        _, high_buy, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "650", "1", _A)])
        _, old_sell, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="sell",
            tiers=[(_SELL, "800", "1", _P)])
        await _invalidate(pf, "AMD", "sell", ["640"])
        assert await rows.tier_statuses(low_buy) == [_P]
        assert await rows.tier_statuses(high_buy) == [_S]
        assert await rows.tier_statuses(old_sell) == [_S]

        pf = uuid4()
        _, low_buy, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "500", "2", _P), (_BUY, "490", "1", _A)])
        _, filled, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "480", "1", _T)])
        closed = await _invalidate(pf, "AMD", "sell", [])
        assert await rows.tier_statuses(low_buy) == [_S, _S]
        assert await rows.tier_statuses(filled) == [_T]
        assert any("without SELL tiers" in c.note() for c in closed)

        pf = uuid4()
        _, old_buy, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "500", "2", _P)])
        _, old_sell, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="sell",
            tiers=[(_SELL, "450", "1", _A)])
        await _invalidate(pf, "AMD", "buy", [])
        assert await rows.tier_statuses(old_buy) == [_S]
        assert await rows.tier_statuses(old_sell) == [_A]

    @pytest.mark.asyncio
    async def test_accepting_replaces_buys_and_hold_is_a_no_op(self, rows):
        from database import SessionLocal

        pf = uuid4()
        _, other_buy, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "580", "1", _P)])
        _, far_sell, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="sell",
            tiers=[(_SELL, "700", "1", _A)])
        _, near_sell, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="sell",
            tiers=[(_SELL, "595", "1", _P)])
        accepted_analysis, accepted, _ = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "600", "1", _P), (_BUY, "590", "1", _P)])
        await _follow(accepted_analysis, pf)
        assert await rows.tier_statuses(accepted) == [_A, _A]
        assert await rows.tier_statuses(other_buy) == [_S]
        assert await rows.tier_statuses(far_sell) == [_A]
        assert await rows.tier_statuses(near_sell) == [_S]

        pf = uuid4()
        analysis_id = await rows.analysis(
            ticker="AMD", portfolio_id=pf, status="completed")
        async with SessionLocal() as s:
            await AnalysisPersistenceRepository(s).save_recommendation(
                analysis_id=analysis_id, recommendation="hold", confidence=0.6,
                reasoning="r", confidence_adjustment_note=None,
                debate_converged=True)
            await s.commit()
        await _follow(analysis_id, pf)

    @pytest.mark.asyncio
    async def test_monitoring_skips_a_tier_closed_after_listing(
            self, rows, monkeypatch):
        import ai.workflows.tier_monitoring_workflow as tm
        from domains.analysis.repository import RecommendationTierRepository

        pf = uuid4()
        _, rec_id, tier_ids = await rows.recommendation(
            ticker="AMD", portfolio_id=pf, action="buy",
            tiers=[(_BUY, "510", "1", _A)])
        original = RecommendationTierRepository.list_open_tier_ids

        async def _list_then_close(self):
            listed = [t for t in await original(self) if t in tier_ids]
            await _invalidate(pf, "AMD", "hold", [])
            return listed

        checked: list = []

        async def _never(tier, *, today):
            checked.append(tier.id)
            return "still_open"

        monkeypatch.setattr(
            RecommendationTierRepository, "list_open_tier_ids",
            _list_then_close)
        monkeypatch.setattr(tm, "_check_one_tier", _never)
        counts = await tm.run_tier_monitoring_workflow()
        assert checked == []
        assert counts["closed_meanwhile"] == 1
        assert await rows.tier_statuses(rec_id) == [_S]


async def _decision_without_outcome(
        session, ticker, action, confidence, reasoning, notes=None):
    analysis = await AnalysisRepository(session).create(
        user_id=None, portfolio_id=None, ticker=ticker)
    rec = await AnalysisPersistenceRepository(session).save_recommendation(
        analysis_id=analysis.id, recommendation=action, confidence=confidence,
        reasoning=reasoning, confidence_adjustment_note=None,
        debate_converged=True)
    return await InvestmentDecisionRepository(session).create(
        user_id=uuid4(), portfolio_id=None, recommendation_id=rec.id,
        ticker=ticker, action_taken="followed", notes=notes)


class TestDecisionReview:

    @pytest.mark.asyncio
    async def test_success_persists_and_returns_review(self, db_session):
        from domains.decision.service import DecisionService

        decision = await _make_decision_with_outcome(db_session)
        router = _ScriptedReviewRouter(
            json.dumps({
                "review_text": ("Your AAPL buy paid off, up 12% vs SPY's 5%."),
                "lessons_learned": (
                    "Conviction on strong earnings momentum worked here.")
            }))
        result = await DecisionService(db_session).generate_review(
            decision.id, user_id=decision.user_id, llm_router=router)
        assert result.decision_id == decision.id
        assert "12%" in result.review_text
        assert result.lessons_learned is not None
        assert len(router.calls) == 1
        stored = await DecisionReviewRepository(db_session).get_by_decision_id(
            decision.id)
        assert stored is not None
        assert stored.review_text == result.review_text

    @pytest.mark.asyncio
    async def test_review_errors(self, db_session):
        from domains.decision.service import DecisionService

        pending = await _decision_without_outcome(
            db_session, "TSLA", "hold", 0.5, "mixed signals")
        with pytest.raises(ValidationAppError):
            await DecisionService(db_session).generate_review(
                pending.id, user_id=pending.user_id)
        decision = await _make_decision_with_outcome(db_session)
        with pytest.raises(NotFoundError):
            await DecisionService(db_session).get_review(
                decision.id, user_id=decision.user_id)
        with pytest.raises(NotFoundError):
            await DecisionService(db_session).generate_review(
                uuid4(), user_id=uuid4())

    @pytest.mark.asyncio
    async def test_failed_or_unusable_answer_stores_nothing(self, db_session):
        from domains.decision.service import (DecisionService,
                                              ReviewUnavailableError)

        routers = {
            "llm-failure": _AlwaysFailsRouter(),
            "malformed-json": _ScriptedReviewRouter("this is not json at all"),
            "no-review-text": _ScriptedReviewRouter(
                json.dumps({"lessons_learned": "something"}))
        }
        for case_id, router in routers.items():
            decision = await _make_decision_with_outcome(db_session)
            with pytest.raises(ReviewUnavailableError):
                await DecisionService(db_session).generate_review(
                    decision.id, user_id=decision.user_id, llm_router=router)
            stored = await DecisionReviewRepository(
                db_session).get_by_decision_id(decision.id)
            assert stored is None, case_id

    @pytest.mark.asyncio
    async def test_review_endpoints_503_and_404(
            self, client, db_session, monkeypatch):
        import domains.decision.service as decision_service_module

        decision = await _make_decision_with_outcome(db_session)
        monkeypatch.setattr(
            decision_service_module, "LLMRouter", lambda: _AlwaysFailsRouter())
        response = await client.post(
            f"/api/v1/decisions/{decision.id}/review",
            headers=bearer_headers(decision.user_id))
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"

        response = await client.post(
            f"/api/v1/decisions/{uuid4()}/review",
            headers=bearer_headers(uuid4()))
        assert response.status_code == 404
        decision = await _decision_without_outcome(
            db_session, "AMD", "buy", 0.6, "x")
        response = await client.get(
            f"/api/v1/decisions/{decision.id}/outcome",
            headers=bearer_headers(decision.user_id))
        assert response.status_code == 404
