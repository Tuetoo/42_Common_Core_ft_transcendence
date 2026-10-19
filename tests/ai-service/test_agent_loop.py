import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4
import ai.agents.finish_checks as finish_checks_module
import ai.agents.loop as loop_module
import ai.agents.news as news_module
import ai.memory.agent_memory as memory_module
import pytest
from ai.agents import (filing, fundamental, macro, market, portfolio, risk,
                       sentiment, technical)
from ai.agents.evidence_grounding import (non_english_reason,
                                          non_english_reason_in_output)
from ai.agents.finish_checks import (InternalTokenValidator,
                                     own_evidence_figure_check)
from ai.agents.loop import (MAX_FINISH_VALIDATION_RETRIES, AgentLoopResult,
                            ReActAgentLoop, ScratchpadEntry,
                            derive_agent_quality, evidence_reference_text,
                            has_successful_tool_evidence,
                            is_successful_tool_entry, loosely_anchored,
                            prefetch_tool_call, word_share)
from ai.agents.market import _market_claims_disagree_with_prices
from ai.agents.news import _NewsFinishValidator
from ai.agents.risk import _CapturingRiskEventsValidator
from ai.graph import nodes as nodes_module
from ai.graph.state import AgentOutput
from ai.knowledge.recycled_content import prior_note_only_claims
from ai.llm.base import (LLMNonEnglishOutputError, LLMRateLimitError,
                         LLMResponse, LLMServerOverloadedError, LLMUsage)
from ai.memory.agent_memory import procedural_memory_skip_reason
from ai.tools.base import Tool
from config import settings
from helpers import _TSLA_FILINGS, _TSLA_ROWS
from helpers import _state
from helpers import (_action, _events, _finish, _pad, _ScriptedAgentRouter,
                     _validator)
from structlog.testing import capture_logs
from shared.enums import AgentRole, AgentStatus

_RUSSIAN_BRAND = (
    "\u041d\u0430 \u0444\u043e\u0440\u0443\u043c\u0435 "
    "\u043f\u0440\u0435\u0434\u0441\u0442\u0430\u0432\u0438\u043b\u0438 "
    "\u0431\u0440\u0435\u043d\u0434")
_RUSSIAN_PROFIT = (
    "\u041f\u0440\u0438\u0431\u044b\u043b\u044c \u043a\u043e\u043c\u043f\u0430"
    "\u043d\u0438\u0438 \u0432\u044b\u0440\u043e\u0441\u043b\u0430 "
    "\u043d\u0430 "
    "\u0434\u0435\u0441\u044f\u0442\u044c \u043f\u0440\u043e\u0446\u0435\u043d"
    "\u0442\u043e\u0432.")
_ARABIC_PROFIT = (
    "\u0627\u0631\u062a\u0641\u0639\u062a \u0623\u0631\u0628\u0627\u062d "
    "\u0627\u0644\u0634\u0631\u0643\u0629 \u0628\u0646\u0633\u0628\u0629 "
    "\u0639\u0634\u0631\u0629 \u0628\u0627\u0644\u0645\u0627\u0626\u0629")
_CHINESE_PRODUCT_NEWS = (
    "\u82f9\u679c\u516c\u53f8\u53d1\u5e03\u4e86"
    "\u65b0\u4ea7\u54c1\u548c\u65b0\u670d\u52a1")
_CHINESE_PROFIT = (
    "\u516c\u53f8\u5229\u6da6\u672c\u5b63\u5ea6\u589e\u957f\u4e86"
    "\u767e\u5206\u4e4b\u5341\uff0c\u524d\u666f\u826f\u597d\u3002")
_PRICE_OBS = "AAPL last close 227.40 on 2026-09-12 (source: fake)"
_PRICE_HISTORY_OBS = (
    "Recent price history for 'AAPL' (1d, showing last 3 of 3 bars):\n"
    "2026-09-10: open=230.34 high=231.35 low=225.95 close=226.10 "
    "volume=5020300.0\n"
    "2026-09-11: open=225.10 high=229.11 low=222.58 close=223.06 "
    "volume=4772500.0\n"
    "2026-09-12: open=223.00 high=228.00 low=221.00 close=227.40 "
    "volume=4000000.0")
_NEWS_OBS = (
    "News results for 'AAPL' (2 articles):\n"
    "[2026-09-12] Apple unveils foldable iPhone lineup\n"
    "[2026-09-11] Services revenue keeps growing")
_QUOTE_OBS = "AAPL current price: 227.4 USD as of 2026-09-12 (source: fake)"
_AMZN_NEWS = (
    "Recent sentiment surrounding Amazon remains predominantly positive, "
    "driven by robust AWS cloud growth, aggressive AI investments, and "
    "bullish analyst backing, including its recent first-ever sterling bond "
    "issuance in the UK to fund its artificial intelligence buildout. On the "
    "operational and legal front, notable developments include ongoing "
    "workforce restructuring to pivot toward AI, heightened aviation safety "
    "scrutiny following a September 2026 cargo plane incident in Miami, and "
    "a proposed nationwide class action concerning the treatment of pregnant "
    "warehouse workers alongside ongoing FTC antitrust oversight.")
_AMZN_IPHONE = (
    "Here is the explanation of the secrets behind the powerful performance "
    "of the iPhone 16 Pro Max, covering the A18 Pro chip, Apple "
    "Intelligence, the new thermal cooling design, and the expansive display.")
_AMZN_GOOD = (
    "Current market sentiment for AMZN is generally positive, driven by "
    "strong AWS growth, robust AI investments, and favorable analyst "
    "commentary. This optimism is slightly tempered by mixed operational and "
    "regulatory developments, including workforce restructuring, ongoing "
    "antitrust oversight, and recent scrutiny regarding workplace practices "
    "and aviation safety. Sentiment: positive")


def _entry(
        action: str, observation: str, *, tool_error: bool = False,
        is_tool_result: bool = True) -> ScratchpadEntry:
    return ScratchpadEntry(
        thought="t", action=action, action_input={}, observation=observation,
        is_tool_result=is_tool_result, tool_error=tool_error)


class _Truncated:

    def __init__(self, content: str, finish_reason: str) -> None:
        self.content = content
        self.finish_reason = finish_reason


class _ScriptedRouter:

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[list[Any]] = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        content, finish_reason = item, "STOP"
        if isinstance(item, _Truncated):
            content, finish_reason = item.content, item.finish_reason
        return LLMResponse(
            content=content, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=5,
                           completion_tokens=5), finish_reason=finish_reason)


class _FakeTool(Tool):
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "q": {
                "type": "string"
            }
        }
    }

    def __init__(
            self, name: str,
            responder: str | Callable[[dict[str, Any]], str]) -> None:
        self.name = name
        self.description = f"{name} tool"
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if callable(self._responder):
            return self._responder(kwargs)
        return self._responder


class _TickerOnlyTool(Tool):
    name = "get_price"
    description = "price for a ticker"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string"
            }
        },
        "required": ["ticker"]
    }

    async def run(self, *, ticker: str) -> str:
        return f"{ticker} last close 227.40 on 2026-09-12"


class _RaisingTool(Tool):
    name = "get_boom"
    description = "always raises"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> str:
        raise RuntimeError("provider exploded")


class _FixedTool(Tool):
    name = "get_fixed_thing"
    description = "fixed"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(
            self, result: str = "Close 100.0",
            error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    async def run(self, **kwargs: Any) -> str:
        if self.error:
            raise self.error
        return self.result


class _MacroEchoTool(Tool):
    name = "get_macro_indicators"
    description = "echo"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> str:
        return "Federal funds rate 3.63% (FRED DFF, 2026-10-01)"


def _loop(router: Any, tools: list[Tool], **kwargs: Any) -> ReActAgentLoop:
    return ReActAgentLoop(
        role=kwargs.pop("role", AgentRole.FILING), tools=tools,
        system_prompt=kwargs.pop("system_prompt", "You are a test agent."),
        max_iterations=kwargs.pop("max_iterations",
                                  4), llm_router=router, **kwargs)


class _RaisingAgent:

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def run(self, state):
        raise self.exc


class _FixedAgent:

    def __init__(self, summary: str) -> None:
        self.summary = summary

    async def run(self, state):
        return AgentOutput(
            role=AgentRole.FILING, summary=self.summary, score=0.9,
            raw_output={"scratchpad": []})


class TestToolFailures:

    @pytest.mark.asyncio
    async def test_errored_steps_and_cancellation(self):
        router = _ScriptedRouter([
            _action("get_price", {"symbol": "AAPL"}),
            _action("get_price", {"ticker": "AAPL"}),
            _finish("AAPL last close 227.40.")
        ])
        result = await _loop(router, [_TickerOnlyTool()]).run("t")
        bad, good = result.scratchpad
        assert bad.observation.startswith(
            "Tool 'get_price' was called with invalid arguments:")
        assert bad.tool_error is True
        assert good.tool_error is False
        assert good.observation == "AAPL last close 227.40 on 2026-09-12"
        assert result.unresolved_finish_validation_issue is None

        router = _ScriptedRouter([
            _action("get_boom", {}),
            _action("get_price", {"ticker": "AAPL"}),
            _finish("AAPL last close 227.40.")
        ])
        tools = [_RaisingTool(), _TickerOnlyTool()]
        result = await _loop(router, tools).run("t")
        boom, good = result.scratchpad
        assert boom.observation == (
            "Tool 'get_boom' raised an error: RuntimeError: provider exploded")
        assert boom.tool_error is True
        assert good.tool_error is False
        assert result.final_answer == "AAPL last close 227.40."

        class _Cancelled(Tool):
            name = "get_cancel"
            description = "cancelled"
            parameters: ClassVar[dict[str, Any]] = {
                "type": "object",
                "properties": {}
            }

            async def run(self, **kwargs: Any) -> str:
                raise asyncio.CancelledError

        router = _ScriptedRouter([_action("get_cancel", {})])
        with pytest.raises(asyncio.CancelledError):
            await _loop(router, [_Cancelled()]).run("t")


class TestParseResponse:
    parse = staticmethod(ReActAgentLoop._parse_response)

    def test_invalid_json_and_non_string_finish_answers(self):
        invalid = {
            "plain-text": "not json at all",
            "cut-off-object": '{"thought": "cut off mid-obj',
            "list": '["a", "list"]',
            "string": '"just a string"',
            "number": "42"
        }
        for case_id, raw in invalid.items():
            assert self.parse(raw) is None, case_id
        answers = {
            "number-is-stringified": (42, "42"),
            "object-is-serialized-as-json": ({
                "pe": 31.2,
                "view": "rich"
            }, '{"pe": 31.2, "view": "rich"}')
        }
        for case_id, (finish_answer, expected) in answers.items():
            step = self.parse(
                json.dumps({
                    "thought": "t",
                    "finish_answer": finish_answer
                }))
            assert step is not None, case_id
            assert step.finish_answer == expected, case_id

    def test_non_string_action_and_non_object_input_are_dropped(self):
        for case_id, action, action_input in (
                ("list-action", ["search_news"], {"q": "x"}),
                ("object-action", {"name": "search_news"}, None),
                ("list-input", "get_price", ["AAPL"])):
            step = self.parse(
                json.dumps({
                    "thought": "t", "action": action,
                    "action_input": action_input}))
            assert step is not None, case_id
            if isinstance(action, str):
                assert step.action == action, case_id
            else:
                assert step.action is None, case_id
            assert step.action_input in (None, {"q": "x"}), case_id
            if not isinstance(action_input, dict):
                assert step.action_input is None, case_id

    @pytest.mark.asyncio
    async def test_list_action_gets_a_correction_turn_not_a_crash(self):
        router = _ScriptedRouter([
            json.dumps({"thought": "t", "action": ["get_price"]}),
            _action("get_price", {"ticker": "AAPL"}),
            _finish("AAPL last close 227.40.")
        ])
        result = await _loop(router, [_TickerOnlyTool()]).run("t")
        assert result.final_answer == "AAPL last close 227.40."
        assert result.scratchpad[-1].observation == (
            "AAPL last close 227.40 on 2026-09-12")


class TestCorrectionTurns:

    @pytest.mark.asyncio
    async def test_correction_turns_and_their_cumulative_budget(self):
        cases = {
            "truncated-reply-gets-the-shorter-thought-nudge": (
                _Truncated(
                    '{"thought": "a very long thought that',
                    "MAX_TOKENS"), _PRICE_OBS,
                ["cut off before it finished",
                 "single short sentence"], ["not valid JSON"]),
            "google-enum-finish-reason-gets-the-nudge": (
                _Truncated(
                    '{"thought": "a very long thought that',
                    "FinishReason.MAX_TOKENS"), _PRICE_OBS,
                ["cut off before it finished"], ["not valid JSON"]),
            "anthropic-lowercase-finish-reason-gets-the-nudge": (
                _Truncated(
                    '{"thought": "a very long thought that',
                    "max_tokens"), _PRICE_OBS,
                ["cut off before it finished"], ["not valid JSON"]),
            "mistral-length-finish-reason-gets-no-nudge": (
                _Truncated(
                    '{"thought": "a very long thought that',
                    "length"), _PRICE_OBS, ["not valid JSON"],
                ["cut off before it finished"]),
            "non-english-reply-is-asked-again-in-english": (
                LLMNonEnglishOutputError(
                    "rejected", reason="CYRILLIC letters",
                    model="m"), _PRICE_HISTORY_OBS, ["not in English"], [])
        }
        for case_id, (bad_reply, observation, present,
                      absent) in (cases.items()):
            tool = _FakeTool("get_price", observation)
            router = _ScriptedRouter([
                bad_reply,
                _action("get_price", {"q": "AAPL"}),
                _finish("closed at 227.40")
            ])
            result = await _loop(router, [tool]).run("t")
            assert result.final_answer == "closed at 227.40", case_id
            first = result.scratchpad[0]
            assert first.is_tool_result is False, case_id
            for text in present:
                assert text in first.observation, case_id
            for text in absent:
                assert text not in first.observation, case_id

        tool = _FakeTool("get_price", _PRICE_OBS)
        router = _ScriptedRouter([
            "bad 1",
            _action("get_price", {"q": "AAPL"}), "bad 2",
            json.dumps({
                "thought": "t",
                "action": None,
                "finish_answer": None
            })
        ])
        result = await _loop(router, [tool], max_iterations=10).run("t")
        assert len(router.calls) == 4
        assert result.hit_iteration_cap is True
        assert result.iterations_used == 1
        assert result.finish_meta is None
        assert result.final_answer == (
            "Repeated invalid responses (after 1 genuine research step(s)) "
            "reached before the agent volunteered a final answer. "
            "Best-effort summary from the last observation gathered: "
            f"{_PRICE_OBS}")
        assert derive_agent_quality(result, is_retry=False) == (0.35, False)


class TestIterationCap:

    @pytest.mark.asyncio
    async def test_final_turn_action_is_not_run_and_cap_fallbacks(self):
        price = _FakeTool("get_price", _PRICE_OBS)
        news = _FakeTool("search_news", _NEWS_OBS)
        router = _ScriptedRouter([
            _action("get_price", {"q": "AAPL"}),
            _action(
                "search_news", {"q": "AAPL"},
                thought="  Price is 227.40; news would help.  ")
        ])
        result = await _loop(router, [price, news], max_iterations=2).run("t")
        assert news.calls == []
        assert result.final_answer == "Price is 227.40; news would help."
        assert result.hit_iteration_cap is True
        assert result.iterations_used == 2
        assert len(result.scratchpad) == 1
        assert result.finish_meta is not None
        assert derive_agent_quality(result, is_retry=False) == (0.35, False)

        router = _ScriptedRouter(
            [_action("get_price", {"q": "AAPL"}, thought="   ")])
        result = await _loop(
            router, [_FakeTool("get_price", _PRICE_OBS)],
            max_iterations=1).run("t")
        assert result.final_answer.startswith(
            "Iteration cap reached; the agent's")
        assert result.hit_iteration_cap is True

        router = _ScriptedRouter([])
        result = await _loop(router, [], max_iterations=0).run("t")
        assert router.calls == []
        assert result.hit_iteration_cap is True
        assert result.final_answer == (
            "Iteration cap (0) reached with no usable evidence gathered.")


class TestRepeatedCalls:

    @pytest.mark.asyncio
    async def test_duplicate_near_duplicate_and_errored_repeats(self):
        tool = _FakeTool("get_x", _PRICE_OBS)
        router = _ScriptedRouter([_action("get_x", {"q": "AAPL"})] * 4)
        result = await _loop(router, [tool], max_iterations=10).run("t")
        assert len(tool.calls) == 1
        assert len(router.calls) == 4
        assert result.hit_iteration_cap is True
        assert result.iterations_used == 1
        assert result.final_answer == (
            "Repeated duplicate/blocked tool calls (after 1 genuine research "
            "step(s)) reached before the agent volunteered a final answer. "
            "Best-effort summary from the last observation gathered: "
            f"{_PRICE_OBS}")

        tool = _FakeTool(
            "get_current_price", lambda kw: (
                _QUOTE_OBS if kw["ticker"] == "AAPL" else (
                    "MSFT quote unavailable for this session, try later "
                    "please")))
        router = _ScriptedRouter([
            _action("get_current_price", {"ticker": "AAPL"}),
            _action("get_current_price", {"ticker": "AAPL"}),
            _action("get_current_price", {"ticker": "AAPL"}),
            _action("get_current_price", {"ticker": "MSFT"}),
            _action("get_current_price", {"ticker": "AAPL"}),
            _finish("AAPL trades at 227.4 USD.")
        ])
        result = await _loop(router, [tool], max_iterations=10).run("t")
        pad = result.scratchpad
        assert len(tool.calls) == 4
        assert pad[0].observation == _QUOTE_OBS
        assert pad[1].observation == (
            _QUOTE_OBS + loop_module._NEAR_DUPLICATE_NUDGE)
        assert pad[2].is_tool_result is False
        assert "nearly identical to one you already had" in pad[2].observation
        assert pad[3].is_tool_result is True
        assert pad[4].is_tool_result is True
        assert result.iterations_used == 4

        tool = _FakeTool("get_x", "Price lookup failed for 'AAPL': 503")
        router = _ScriptedAgentRouter([
            _action("get_x", {"q": "AAPL"}),
            _action("get_x", {"q": "AAPL"}),
            _action("get_x", {"q": "AAPL"}),
            _finish("x"),
            _finish("x"),
            _finish("x")
        ])
        result = await _loop(router, [tool], max_iterations=6).run("task")
        assert len(tool.calls) == 2
        assert [e.tool_error for e in result.scratchpad[:2]] == [True, True]
        assert result.scratchpad[2].observation.startswith(
            "[Blocked before running")


class _PeerAwarePriceTool(_TickerOnlyTool):
    analysis_ticker = "AAPL"


class TestPeerTickerCalls:

    @pytest.mark.asyncio
    async def test_other_ticker_calls_are_not_own_evidence(self):
        router = _ScriptedAgentRouter([
            _action("get_price", {"ticker": "MSFT"}),
            _finish("x"),
            _finish("x"),
            _finish("x")
        ])
        result = await _loop(
            router, [_PeerAwarePriceTool()], max_iterations=4).run("task")
        assert result.scratchpad[0].peer_ticker == "MSFT"
        assert not has_successful_tool_evidence(result.scratchpad)
        assert result.unresolved_finish_validation_issue is not None

        peer = _entry(
            "get_recent_price_history",
            "2026-09-12: open=1 high=500 low=400 close=450 volume=1")
        peer.peer_ticker = "SPY"
        own = _entry(
            "get_recent_price_history",
            "2026-09-12: open=1 high=230 low=220 close=227.4 volume=1")
        metrics = {"latest_close": 450, "period_high": 500, "period_low": 400}
        issue = _market_claims_disagree_with_prices("a", metrics, [peer, own])
        assert issue is not None and "227.4" in issue
        assert _market_claims_disagree_with_prices("a", metrics,
                                                   [peer]) is (None)


class TestPromptAndFinishMeta:

    @pytest.mark.asyncio
    async def test_system_prompt_contract_and_finish_meta(self):
        tools = [_FakeTool("get_price", "x"), _FakeTool("search_news", "y")]
        router = _ScriptedRouter([_finish("x")])
        await _loop(
            router, tools, system_prompt="AGENT-SPECIFIC RULES.",
            require_tool_evidence=False).run("t")
        system = router.calls[0][0].content
        assert "AGENT-SPECIFIC RULES." in system
        assert f"Today's real date is {datetime.now(UTC):%Y-%m-%d}" in system
        assert (
            "Available tools:\n- get_price: get_price tool\n"
            "- search_news: search_news tool") in system
        assert (
            "Exactly one of `action` or `finish_answer` must be non-null"
        ) in system
        assert '"reported_metrics"' in system

        tool = _FakeTool("get_x", _PRICE_HISTORY_OBS)
        router = _ScriptedAgentRouter(
            [_action("get_x", {"q": "A"}),
             _finish("closed at 227.40")])
        result = await _loop(router, [tool]).run("task")
        assert result.finish_meta == {
            "thought": "done",
            "provider": "fake",
            "model": "fake-model",
            "finish_reason": "STOP",
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "thinking_tokens": 3,
            "rate_limited": False
        }

    @pytest.mark.asyncio
    async def test_prefetched_call_is_marked_and_told_to_the_model(self):
        ok = await prefetch_tool_call(_FixedTool(), {"ticker": "MSFT"})
        assert ok.prefetched and ok.action == "get_fixed_thing"
        assert ok.action_input == {"ticker": "MSFT"}
        bad = await prefetch_tool_call(
            _FixedTool(error=RuntimeError("down")), {})
        assert bad.observation.startswith("Prefetched tool call failed:")
        assert loop_module.tool_reported_error(bad.observation)

        tool = _FixedTool()
        router = _ScriptedRouter(
            [_finish("MSFT closed at 100.0 in the latest session.")])
        loop = _loop(
            router, [tool], role=AgentRole.MARKET,
            system_prompt="Your very first action must be get_fixed_thing.",
            max_iterations=3)
        prefetched = [await prefetch_tool_call(tool, {"ticker": "MSFT"})]
        result = await loop.run(
            "Price for MSFT", prefetched_evidence=prefetched)
        assert len(router.calls) == 1
        user_text = router.calls[0][1].content
        assert "Already done for you in code before your first turn" in (
            user_text)
        assert 'get_fixed_thing({"ticker": "MSFT"})' in user_text
        assert "already satisfied" in user_text
        assert result.scratchpad[0].prefetched is True
        assert result.final_answer.startswith("MSFT closed at 100.0")


class TestAnswerAnchoring:

    @pytest.mark.asyncio
    async def test_off_topic_rewrite_and_word_share(self):
        router = _ScriptedRouter([_finish(_AMZN_IPHONE), _finish(_AMZN_GOOD)])
        loop = _loop(
            router, [], role=AgentRole.SENTIMENT, system_prompt="s",
            max_iterations=3, require_tool_evidence=False,
            evidence_yield=lambda _scratchpad: 12,
            anchor_fallback_reference=_AMZN_NEWS)
        result = await loop.run("Characterize sentiment for AMZN.")

        assert result.final_answer == _AMZN_GOOD
        assert len(router.calls) == 2
        second_prompt = router.calls[1][-1].content
        assert "shares nothing with the other agent's summary" in (
            second_prompt)
        assert "from the other agent's summary in the context above" in (
            second_prompt)

        news = (
            "Amazon sentiment is positive on AWS cloud growth, AI "
            "investments and analyst backing; FTC antitrust oversight "
            "continues.")
        paraphrase = (
            "Sentiment for AMZN is positive, driven by AWS growth, AI "
            "investments and antitrust oversight.")
        off_topic = (
            "Here is how the iPhone 16 Pro Max thermal design and display "
            "work.")
        minimum = settings.FALLBACK_ANCHOR_MIN_WORD_SHARE
        assert word_share(paraphrase, news) >= minimum
        assert word_share(off_topic, news) < minimum
        assert loosely_anchored(paraphrase, news)
        assert not loosely_anchored(off_topic, news)
        assert word_share("", news) == 0.0

    @pytest.mark.asyncio
    async def test_outlet_names_are_not_a_gate(self):
        answer = (
            "Rates sit at 3.63%. According to recent coverage from Reuters, "
            "the Fed remains cautious.")
        router = _ScriptedRouter([
            json.dumps({
                "thought": "t",
                "action": "get_macro_indicators",
                "action_input": {}
            }),
            json.dumps({
                "thought": "t",
                "finish_answer": answer
            })
        ])
        loop = _loop(
            router, [_MacroEchoTool()], role=AgentRole.MACRO,
            system_prompt="test")
        result = await loop.run("task")
        assert result.final_answer == answer
        assert result.unresolved_finish_validation_issue is None


class TestAgentQuality:

    def test_graded_score_spreads_and_keeps_the_contract(self):
        bare = loop_module.AgentLoopResult(final_answer="x")
        assert derive_agent_quality(bare, is_retry=False) == (0.76, True)
        assert derive_agent_quality(bare, is_retry=False,
                                    no_evidence=True) == (0.35, False)

        meta: dict[str, Any] = {
            "model": "m",
            "finish_reason": "stop",
            "rate_limited": False
        }

        def result(
                pad: list[ScratchpadEntry], answer: str = "close 227.40",
                **kwargs: Any) -> AgentLoopResult:
            kwargs.setdefault("finish_meta", dict(meta))
            return AgentLoopResult(
                final_answer=answer, scratchpad=pad, **kwargs)

        def entry(action: str, observation: str, **kwargs: Any):
            return ScratchpadEntry("t", action, {}, observation, **kwargs)

        price = "close=227.40 high=231.35 low=221.00"
        ratios = "P/E=25.72 operating margin (annual, latest FY)=28.7%"
        filing_text = "Filing: revenue $13.4B (+7% YoY) this quarter"
        failed = "Price lookup failed for 'X': boom"
        cases = {
            "three_tools_clean": (
                result([
                    entry("a", price),
                    entry("b", ratios),
                    entry("c", filing_text)
                ], "close 227.40, margin 28.7%, revenue +7%"), {}),
            "one_tool_clean": (result([entry("a", price)]), {}),
            "built_on_peer_text": (result([]), {}),
            "tool_errors_and_corrections": (
                result([
                    entry("a", failed, tool_error=True),
                    entry("a", failed, tool_error=True),
                    entry("a", price),
                    entry("t", "correction", is_tool_result=False),
                    entry("t", "correction", is_tool_result=False)
                ]), {}),
            "recycled_with_warning": (
                result([entry("a", price),
                        entry("b", ratios)]), {
                            "recycled": True,
                            "verification_warnings": ["w"]
                        }),
            "truncated_fallback": (
                result([entry("a", price)], finish_meta={
                    **meta, "finish_reason": "MAX_TOKENS",
                    "rate_limited": True
                }), {}),
            "ungrounded_figures": (
                result([entry("a", price)],
                       "close 227.40, target 260.00, upside 14.3%"), {}),
            "retry": (
                result([entry("a", price),
                        entry("b", ratios)]), {
                            "is_retry": True
                        }),
            "thin": (result([entry("a", price)]), {
                "thin_evidence": True
            })
        }
        scores: dict[str, float] = {}
        for name, (loop_result, kwargs) in cases.items():
            kwargs = {"is_retry": False, **kwargs}
            score, well_supported = derive_agent_quality(loop_result, **kwargs)
            assert well_supported is True
            assert 0.4 <= score <= 1.0
            scores[name] = score
        assert scores == {
            "three_tools_clean": 0.92,
            "one_tool_clean": 0.84,
            "built_on_peer_text": 0.76,
            "tool_errors_and_corrections": 0.66,
            "recycled_with_warning": 0.70,
            "truncated_fallback": 0.76,
            "ungrounded_figures": 0.84,
            "retry": 0.65,
            "thin": 0.6
        }
        assert len(set(scores.values())) >= 6

    def test_prior_note_only_amounts_are_listed_but_not_scored(self, capsys):
        note = (
            "Prior risk note search results (1 passages):\n"
            "- (KO) [agent:risk] (2026-09-01): a $4.4B claim, trial in "
            "March 2027.")
        fresh = (
            "- (KO) [KO_10-Q_2026-07-29] (filed 2026-07-29): revenue "
            "$13.4B.")
        answer = (
            "Revenue was $13.4B; a $4.4B claim goes to trial in March 2027.")
        assert prior_note_only_claims(answer, [note, fresh]) == ["$4.4B"]
        target_note = (
            "- (UNH) [agent:news] (2026-10-18): average price target $480.")
        target_fresh = (
            "- (UNH) [web] (2026-10-14): Wall Street's average price target "
            "is $480.60.")
        assert prior_note_only_claims(
            "The average target is $480.", [target_note, target_fresh]) == []
        assert prior_note_only_claims(
            "The average target is $480.",
            [target_note, "- (UNH) [web]: target $490.00."]) == ["$480"]
        pad = [
            ScratchpadEntry("t", "search_prior_risk_notes", {}, note),
            ScratchpadEntry("t", "search_filings", {}, fresh)
        ]
        meta = {"finish_reason": "stop"}
        flagged = AgentLoopResult(
            final_answer=answer, scratchpad=pad, finish_meta=meta, role="news")
        clean = AgentLoopResult(
            final_answer="Revenue was $13.4B.", scratchpad=pad,
            finish_meta=meta, role="news")
        flagged_score, ok = derive_agent_quality(flagged, is_retry=False)
        assert ok is True
        out = capsys.readouterr().out
        assert "agent_loop.prior_note_only_claims" not in out
        clean_score, _ = derive_agent_quality(clean, is_retry=False)
        assert flagged_score == clean_score

    def test_state_and_loop_share_one_evidence_predicate(self):
        from ai.graph import state as state_module

        assert state_module.is_evidence_record is (
            loop_module.is_evidence_record)
        peer = _entry("get_fundamentals", "P/E=40")
        peer.peer_ticker = "AMD"
        own = _entry("get_fundamentals", "P/E=30")
        assert not has_successful_tool_evidence([peer])
        assert has_successful_tool_evidence([peer, own])
        assert not is_successful_tool_entry(peer)
        assert "P/E=40" in evidence_reference_text([peer, own])
        output = AgentOutput(
            role=AgentRole.FUNDAMENTAL, summary="s", score=0.8,
            raw_output={"scratchpad": [vars(peer)]})
        assert state_module.lacks_successful_tool_evidence(output)


class TestRunAgentSafely:

    @pytest.fixture(autouse=True)
    def _no_stagger(self, monkeypatch):
        monkeypatch.setattr(nodes_module, "_FAN_OUT_STAGGER_SECONDS", 0.0)

    @pytest.mark.asyncio
    async def test_status_failure_kind_and_non_english_summary(self):
        cases = {
            "server-overload": (
                _RaisingAgent(LLMServerOverloadedError("503 overloaded")),
                AgentStatus.FAILED, "server_overload"),
            "rate-limit": (
                _RaisingAgent(LLMRateLimitError("429 quota")),
                AgentStatus.FAILED, "rate_limit"),
            "non-english-error": (
                _RaisingAgent(LLMNonEnglishOutputError("non-English output")),
                AgentStatus.FAILED, "non_english"),
            "other-error": (
                _RaisingAgent(RuntimeError("boom")), AgentStatus.FAILED,
                "other"),
            "completed-has-no-failure-kind": (
                _FixedAgent("Filing shows 10-K risk factors."),
                AgentStatus.COMPLETED, None)
        }
        for case_id, (agent, status, kind) in cases.items():
            output = await nodes_module._run_agent_safely(
                AgentRole.FILING, agent, _state())
            assert output.status == status, case_id
            assert output.failure_kind == kind, case_id

        texts = {"russian": _RUSSIAN_BRAND, "chinese": _CHINESE_PRODUCT_NEWS}
        for case_id, text in texts.items():
            output = await nodes_module._run_agent_safely(
                AgentRole.FILING, _FixedAgent(text), _state())
            assert output.status == AgentStatus.FAILED, case_id
            assert output.summary == (
                "filing agent output rejected: non-English text"), case_id
            assert output.failure_kind == "non_english", case_id
            assert output.is_well_supported is False, case_id
            assert output.score == 0.0, case_id
            assert output.started_at is not None, case_id
            assert output.completed_at is not None, case_id


class TestFanOutStagger:

    def test_start_offsets_cover_the_nine_fanned_out_roles(self):
        order = nodes_module._FAN_OUT_STAGGER_ORDER
        assert set(order) == {
            AgentRole.MARKET, AgentRole.NEWS, AgentRole.SENTIMENT,
            AgentRole.FILING, AgentRole.TECHNICAL, AgentRole.FUNDAMENTAL,
            AgentRole.RISK, AgentRole.MACRO, AgentRole.PORTFOLIO
        }
        offset = (
            order.index(AgentRole.NEWS) *
            nodes_module._FAN_OUT_STAGGER_SECONDS)
        assert offset == pytest.approx(0.65)


class TestNonEnglishDetection:

    def test_ratio_floor_marks_and_decoded_model_output(self):
        ratio_cases = {
            "foreign-word-below-two-percent": (
                _RUSSIAN_PROFIT[:7] + " " + "a" * 400, False),
            "foreign-word-above-two-percent": (
                _RUSSIAN_PROFIT[:7] + " " + "a" * 300, True),
            "arabic-with-combining-marks": (
                "\u0643\u064e\u062a\u064e\u0628\u064e " * 3, True)
        }
        for case_id, (text, flagged) in ratio_cases.items():
            assert (non_english_reason(text) is not None) is flagged, case_id
        outputs = {
            "nested-json-values": json.dumps(
                {"a": {
                    "b": ["fine", {
                        "c": _ARABIC_PROFIT
                    }]
                }}),
            "fenced-json": "```json\n" +
            json.dumps({"summary": _CHINESE_PROFIT}) + "\n```",
            "unparseable-text-with-escapes": 'prefix {"x": ' +
            json.dumps(_RUSSIAN_PROFIT) + " trailing"
        }
        for case_id, content in outputs.items():
            assert non_english_reason_in_output(content) is not None, case_id


class TestProceduralMemoryGate:
    _OK: ClassVar[dict[str, Any]] = {
        "score": 0.9,
        "final_answer": "AAPL closed at 227.40.",
        "is_well_supported": True,
        "hit_iteration_cap": False,
        "verification_warnings": [],
        "no_evidence": False
    }

    @pytest.mark.asyncio
    async def test_each_gate_and_prefetch_only_trajectory_is_not_stored(
            self, monkeypatch):
        gates = {
            "score": ({
                "score": 0.65
            }, "score_below_threshold"),
            "not-well-supported": ({
                "is_well_supported": False
            }, "not_well_supported"),
            "iteration-cap": ({
                "hit_iteration_cap": True
            }, "hit_iteration_cap"),
            "warnings": ({
                "verification_warnings": ["unverified claim"]
            }, "verification_warnings"),
            "no-evidence": ({
                "no_evidence": True
            }, "no_evidence"),
            "non-english": ({
                "final_answer": _RUSSIAN_BRAND
            }, "non_english")
        }
        for case_id, (override, reason) in gates.items():
            assert (
                procedural_memory_skip_reason(**{
                    **self._OK,
                    **override
                }) == reason), case_id
        assert procedural_memory_skip_reason(**self._OK) is None

        def _no_session():
            raise AssertionError("must not write")

        monkeypatch.setattr(memory_module, "SessionLocal", _no_session)
        written = await memory_module.maybe_remember_trajectory(
            agent_id=None, user_id=None, ticker="AAPL", scratchpad=[], **{
                **self._OK, "no_evidence": True
            })
        assert written is False

        prefetched = ScratchpadEntry(
            thought="(fetched in code before the first turn)",
            action="get_technical_indicators", action_input={"ticker": "AAPL"},
            observation="RSI(14): 50", prefetched=True)
        with capture_logs() as logs:
            written = await memory_module.maybe_remember_trajectory(
                agent_id=None, user_id=None, ticker="AAPL",
                scratchpad=[vars(prefetched)], **self._OK)
        assert written is False
        skipped = [
            log for log in logs
            if log["event"] == "procedural_memory.write_skipped"
        ]
        assert skipped[0]["reason"] == "no_model_chosen_calls"

    def test_trajectory_steps_and_owner_ids(self):
        pad = [
            vars(e) for e in (
                ScratchpadEntry(
                    thought="(fetched in code before the first turn)",
                    action="get_recent_price_history", action_input={
                        "ticker": "AAPL"
                    }, observation="close=1", prefetched=True),
                _entry("get_current_price", "AAPL current price: 1"),
                _entry("get_current_price", "[Blocked]", is_tool_result=False),
                ScratchpadEntry(
                    thought="done", action=None, action_input=None,
                    observation=None, is_tool_result=False))
        ]
        content = memory_module.build_trajectory_content(
            final_answer="a", scratchpad=pad)
        assert [s["action"] for s in content["steps"]] == ["get_current_price"]
        assert memory_module.usable_hint_steps(content) == (
            content["steps"])
        assert memory_module.usable_hint_steps({"steps": content["steps"]
                                                }) == []
        owner = uuid4()
        assert memory_module.owner_uuid(str(owner)) == owner
        assert memory_module.owner_uuid("") is None
        assert memory_module.owner_uuid(None) is None
        assert memory_module.owner_uuid("not-a-uuid") is None

    @pytest.mark.asyncio
    async def test_hints_carry_tool_pattern_but_not_final_answer(
            self, monkeypatch):

        class _Row:
            score = 0.9
            content: ClassVar[dict[str, Any]] = {
                "final_answer": "AAPL is at $440.50 and rising",
                "steps": [{
                    "action": "get_current_price",
                    "action_input": {
                        "ticker": "AAPL"
                    }
                }],
                memory_module.MODEL_STEPS_ONLY_KEY: True
            }

        class _OldRow:
            score = 0.95
            content: ClassVar[dict[str, Any]] = {
                "final_answer": "old",
                "steps": [{
                    "action": "get_recent_price_history",
                    "action_input": {
                        "ticker": "AAPL"
                    }
                }]
            }

        rows: list[Any] = [_OldRow(), _Row()]
        seen_limits: list[int] = []

        class _Session:

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Catalog:

            def __init__(self, session):
                pass

            async def get_by_name(self, name):

                class _Agent:
                    id = "agent-1"

                return _Agent()

        class _Memory:

            def __init__(self, session):
                pass

            async def recent_qualifying_for_agent(
                    self, agent_id, *, user_id, ticker, limit):
                seen_limits.append(limit)
                return list(rows)

        monkeypatch.setattr(memory_module, "SessionLocal", _Session)
        monkeypatch.setattr(
            memory_module, "AgentCatalogRepository", _Catalog)
        monkeypatch.setattr(
            memory_module, "AgentMemoryRepository", _Memory)
        hints = await memory_module.retrieve_procedural_hints(
            AgentRole.MARKET, "AAPL", user_id=None)
        assert hints is not None
        assert "get_current_price" in hints
        assert "get_recent_price_history" not in hints
        assert "440.50" not in hints
        assert "->" not in hints
        assert seen_limits and seen_limits[0] > 2

        rows[:] = [_OldRow()]
        assert (
            await memory_module.retrieve_procedural_hints(
                AgentRole.MARKET, "AAPL", user_id=None) is None)


class TestFinishChecks:

    def test_own_evidence_check_fresh_counts_and_fail_open(self, monkeypatch):
        check = own_evidence_figure_check()
        pad = [_entry("get_price", "close 100.0")]
        issue = check("Up 25% to $125.", None, pad)
        assert issue is not None and issue.startswith("Figure check")
        assert '"25%"' in issue and '"$125"' in issue
        assert check("Closed at $100.", None, pad) is None
        guide = [_entry("search_filings", "expenses of $165-169 billion")]
        assert check("Expenses up to $169 billion.", None, guide) is None
        issue = check("Expenses of $170-172 billion.", None, guide)
        assert issue is not None and '"$170-172 billion"' in issue

        note = (
            "Prior news note search results (1 passages):\n"
            "- (TSLA) [agent:news] (2026-10-06): deliveries rose from "
            "496,579 units a year ago.")
        fresh = "- (TSLA) [search_news] 2026-10-02: 435,012 vehicles delivered"
        answer = "Tesla delivered 435,012 vehicles, down from 496,579."
        cases = {
            "only-in-prior-note": ([note, fresh], answer, ["496,579"]),
            "in-fresh-output": (
                [note, fresh + "; 496,579 a year earlier"], answer, []),
            "plain-digits-in-fresh-output": (
                ["volume 48720500 shares"], "Volume was 48,720,500.", []),
            "within-one-percent": (
                ["volume 48720500 shares"], "Volume was 48,600,000.", []),
            "dollar-and-percent-unchanged": (
                ["close 100.0"], "Closed at $100, up 25%.", ["25%"]),
            "scaled-claim-of-plain-dollar-amount": (
                ["a total transaction of $2,015,046.00"],
                "The sale was worth $2.0 million.", []),
            "scaled-claim-off-plain-dollar-amount": (
                ["a total transaction of $2,015,046.00"],
                "The sale was worth $2.1 million.", ["$2.1 million"])
        }
        for case_id, (observations, text, expected) in cases.items():
            pad = [_entry("search_news", o) for o in observations]
            assert finish_checks_module.own_evidence_ungrounded_figures(
                text, pad) == expected, case_id
            issue = check(text, None, pad)
            assert (issue is None) == (not expected), case_id
        mixed = _entry("search_prior_news_notes", note + "\n" + fresh)
        reference = finish_checks_module.primary_reference_text([mixed])
        assert "435,012" in reference and "496,579" not in reference

        def _boom(*args: Any) -> set[str]:
            raise ValueError("boom")

        monkeypatch.setattr(
            finish_checks_module, "find_ungrounded_figures", _boom)
        pad = [_entry("get_price", "close 100.0")]
        with capture_logs() as logs:
            assert check("Up 25%.", None, pad) is None
        assert logs[0]["event"] == "finish_checks.own_evidence_check_failed"

    def test_agents_wire_the_shared_checks_and_budget(self):
        figure = "own_evidence_figure_check("
        token = "InternalTokenValidator("
        cases = {
            "market": (market, figure, True),
            "technical": (technical, figure, True),
            "fundamental": (fundamental, figure, True),
            "macro": (macro, figure, True),
            "filing": (filing, figure, True),
            "risk": (risk, figure, True),
            "news": (news_module, figure, True),
            "portfolio": (portfolio, figure, True),
            "sentiment-does-not": (
                sentiment, "own_evidence_figure_check", False),
            "fundamental-token-check": (fundamental, token, True),
            "macro-token-check": (macro, token, True),
            "filing-token-check": (filing, token, True),
            "sentiment-token-check": (sentiment, token, True)
        }
        for case_id, (module, needle, wired) in cases.items():
            source = inspect.getsource(module)
            assert (needle in source) is wired, case_id

        assert issubclass(_NewsFinishValidator, InternalTokenValidator)
        assert issubclass(
            _CapturingRiskEventsValidator, InternalTokenValidator)

    def test_once_checks_are_logged_only_after_the_budget(self):
        validator = _validator(_TSLA_ROWS)
        pad = _pad(_TSLA_FILINGS)
        hard_events = {
            "risk_events": [{
                "entity": "Federal Trade Commission inquiry",
                "evidence_tool": "web_search",
                "status": "pending"
            }]
        }
        for _ in range(MAX_FINISH_VALIDATION_RETRIES):
            assert validator("x", hard_events, pad) is not None
        assert validator.rejections == MAX_FINISH_VALIDATION_RETRIES
        answer = (
            "Benavides is on appeal (source F1.L1, catalog outlet Reuters).")
        with capture_logs() as logs:
            issue = validator(
                answer, _events(("Benavides v. Tesla", "appeal", "F1.L1")),
                pad)
        assert issue is None
        events = [log["event"] for log in logs]
        assert "finish_checks.once_check_logged_only" in events
        mismatch = [
            log for log in logs if log["event"] == "risk.record_name_mismatch"
        ]
        assert mismatch and mismatch[0]["rejected"] is False
        assert validator.fired == set()
