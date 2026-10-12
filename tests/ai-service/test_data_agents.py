import ast
import importlib.util
import inspect
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
import ai.agents.loop as loop_module
import ai.knowledge.extraction as extraction_module
import ai.knowledge.kg_context as kg_context_module
import ai.providers.news_relevance as relevance_module
import ai.recommendation.position_sizing as sizing_module
import ai.tools.filing_tool as filing_tool_module
import ai.tools.fundamentals_tool as fundamentals_tool_module
import ai.tools.macro_tool as macro_tool_module
import ai.tools.market_tool as market_tool_module
import ai.tools.news_tool as news_tool_module
import ai.tools.research_archive_tool as research_archive_tool_module
import ai.tools.risk_tool as risk_tool_module
import ai.tools.technical_tool as technical_tool_module
import config
import pytest
from ai.agents import debate_common as dc
from ai.agents import (filing, fundamental, judge, macro, market, news,
                       portfolio, reflection, risk, sentiment, technical)
from ai.agents.debate_common import (_BEAR_SYSTEM_PROMPT, _BULL_SYSTEM_PROMPT,
                                     DebateSideAgent)
from ai.agents.filing import (FilingRecordsCache, key_records_for,
                              one_off_item_lines)
from ai.agents.fundamental import (fundamental_answer_issue,
                                   revenue_growth_period_issue)
from ai.agents.judge import JudgeAgent
from ai.agents.loop import AgentLoopResult, ScratchpadEntry
from ai.agents.news import _NewsFinishValidator
from ai.agents.portfolio import _portfolio_finish_validator
from ai.agents.reflection import ReflectionAgent
from ai.agents.risk import (RECORD_NAME_MESSAGE, _event_line,
                            _record_names_entity, _risk_answer_issue,
                            _risk_events_line, _sourced_risk_events)
from ai.graph.state import (ISSUE_LLM_REVIEW, ISSUE_NOT_RETRIED,
                            ISSUE_RETRY_NO_PROGRESS, AgentOutput,
                            AnalysisState, DebateArgument, DebateState,
                            EpisodicMemoryHit, JudgeVerdict, ReflectionIssue,
                            ReflectionVerdict, _classify_reflection_causes)
from ai.knowledge import filing_records as frec
from ai.llm.base import LLMConfigError, LLMResponse, LLMUsage
from ai.providers.base import (FundamentalsSnapshot, InstrumentMetadata,
                               MacroDataPoint, NewsArticle, PriceBar,
                               PriceQuote, ProviderRequestError,
                               WebSearchResult)
from ai.rag.retriever import RetrievedChunk
from ai.tools.base import Tool
from ai.tools.filing_tool import SearchFilingsTool
from ai.tools.news_tool import counts_as_live_search
from ai.tools.research_archive_tool import SearchResearchArchiveTool
from helpers import (_TSLA_ROWS, _events, _news_output, _NoCallRouter, _pad,
                     _Row, _ScriptedAgentRouter, _state, _validator)
from structlog.testing import capture_logs
from shared.enums import (AgentRole, AgentStatus, AnalysisType,
                          DocumentCategory, RecommendationAction)

_ALL_DATA_ROLES = [
    AgentRole.MARKET, AgentRole.NEWS, AgentRole.SENTIMENT, AgentRole.FILING,
    AgentRole.FUNDAMENTAL, AgentRole.TECHNICAL, AgentRole.RISK,
    AgentRole.MACRO, AgentRole.PORTFOLIO
]
_PRICE_HISTORY_OBS = (
    "Recent price history for 'AAPL' (1d, showing last 3 of 3 bars):\n"
    "2026-09-10: open=230.34 high=231.35 low=225.95 close=226.10 "
    "volume=5020300.0\n"
    "2026-09-11: open=225.10 high=229.11 low=222.58 close=223.06 "
    "volume=4772500.0\n"
    "2026-09-12: open=223.00 high=228.00 low=221.00 close=227.40 "
    "volume=4000000.0")
_FAKE_WEB_SNIPPET = (
    "Analysts say the outlook looks stable and overall sentiment is mixed.")
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
_GONE_FROM_PROMPTS = [
    "driven by", "primarily due to", "Round119", "round90",
    "assess_source_credibility", "15.6%", "quarterly:"
]
_NO_TRIGGER_WORDS = ["maintaining, remains", "(maintaining, remains)"]
_NO_CATALOG_PHRASES = ["[catalog outlet]", "[not in catalog]"]
_LIVE_NEWS_LINE = "[2026-10-02 Fri] Src: Tesla deliveries 486,532 units"


def _finish(answer: str, reported_metrics: Any = None) -> str:
    return json.dumps({
        "thought": "done",
        "action": None,
        "action_input": None,
        "finish_answer": answer,
        "reported_metrics": reported_metrics
    })


def _action(action: str, action_input: dict[str, Any]) -> str:
    return json.dumps({
        "thought": "checking",
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


class _Stop(Exception):
    pass


def _entry(
        action: str, observation: str,
        action_input: dict[str, Any] | None = None, *,
        is_tool_result: bool = True,
        tool_error: bool = False) -> ScratchpadEntry:
    return ScratchpadEntry(
        thought="t", action=action, action_input=action_input or {},
        observation=observation, is_tool_result=is_tool_result,
        tool_error=tool_error)


def _steps(*entries: tuple[str, str]) -> list[ScratchpadEntry]:
    return [
        ScratchpadEntry("t", action, {}, observation)
        for action, observation in entries
    ]


def _classified(fn):
    from ai.providers.news_relevance import ClassifiedArticle

    async def wrapper(articles, **kwargs):
        kept = await fn(articles, ticker=kwargs.get("ticker"))
        return [ClassifiedArticle(a, "unclassified") for a in kept]

    return wrapper


def _web_classified(fn):
    from ai.tools.web_search_tool import ClassifiedResult

    async def wrapper(query, results, **kwargs):
        kept = await fn(query, results)
        return [ClassifiedResult(r, "unclassified") for r in kept]

    return wrapper


@pytest.fixture
def fake_web_search(monkeypatch):
    import ai.tools.web_search_tool as web_search_tool_module

    class _FakeWebSearchProvider:

        async def search(self, query, *, limit=5):
            return [
                WebSearchResult(
                    title="Market outlook update",
                    url="https://www.example-news.com/outlook-2026-09-12",
                    snippet=_FAKE_WEB_SNIPPET)
            ]

    async def _passthrough_results(query, results, *, llm_router=None):
        return results

    monkeypatch.setattr(
        web_search_tool_module, "get_web_search_provider",
        lambda: _FakeWebSearchProvider())
    monkeypatch.setattr(
        web_search_tool_module, "classify_results",
        _web_classified(_passthrough_results))


def _rising_bars(ticker, interval, count, step) -> list[PriceBar]:
    bars = []
    price = 100.0
    for i in range(count):
        price += step
        bars.append(
            PriceBar(
                ticker=ticker, interval=interval,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
                open=price, high=price + 1, low=price - 1, close=price,
                volume=1000, source="fake"))
    return bars


class _RisingMarketProvider:

    def __init__(self, count: int, step: float) -> None:
        self.count = count
        self.step = step

    async def get_recent_price_history(
            self, ticker, *, interval="1d", lookback_days=90):
        return _rising_bars(ticker, interval, self.count, self.step)

    async def get_instrument_metadata(self, ticker):
        return InstrumentMetadata(
            name=f"{ticker} Inc.", sector=None, exchange=None)


def _news_with_evidence(
        summary: str = "Tesla beat earnings estimates.",
        **raw_extra) -> AgentOutput:
    raw_output = {
        "scratchpad": [{
            "thought": "t",
            "action": "search_news",
            "action_input": {
                "query": "x"
            },
            "observation": "News results for 'x' (2 articles):\n"
            "[2026-09-12] R: a\n[2026-09-12] R: b",
            "is_tool_result": True,
            "tool_error": False
        }],
        **raw_extra
    }
    return AgentOutput(
        role=AgentRole.NEWS, summary=summary, score=0.85,
        raw_output=raw_output)


def _live_news(text: str = _LIVE_NEWS_LINE) -> ScratchpadEntry:
    return _entry(
        "search_news",
        "News results for 'x' (1 articles: 1 company business, 0 stock "
        "market, 0 opinion/listicle, 0 consumer product, 0 unclassified):"
        "\n" + text)


def _prior_news_note(note: str) -> ScratchpadEntry:
    return _entry(
        "search_prior_news_notes",
        "Prior news note search results for 'q' (1 passages):\nNote: ...\n"
        "- (TSLA) [agent:news] (2026-10-02) [PRIOR AI-WRITTEN NOTE written "
        f"2026-10-02 -- unverified lead]: {note}")


async def _no_prefetch(*args: Any, **kwargs: Any) -> list[ScratchpadEntry]:
    return []


class _FixedResultLoop:
    result: AgentLoopResult

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def run(self, *args: Any, **kwargs: Any) -> AgentLoopResult:
        return self.result


def _fixed_result_loop(result: AgentLoopResult) -> type:
    return type("_Loop", (_FixedResultLoop, ), {"result": result})


@pytest.fixture
def record_prefetch(monkeypatch):
    seen: list[list[tuple[str, dict]]] = []

    async def _record(calls):
        seen.append([(tool.name, dict(args)) for tool, args in calls])
        raise _Stop

    for module in (market, technical, fundamental, filing, risk, portfolio,
                   news, macro):
        monkeypatch.setattr(module, "prefetch_tool_calls", _record)

    async def _no_fred(tool):
        return []

    monkeypatch.setattr(macro, "_prefetch_live_fred", _no_fred)
    return seen


@pytest.fixture
def captured_tools(monkeypatch) -> list[Any]:
    seen: list[Any] = []

    async def _seen_prefetch(calls):
        seen.extend(tool for tool, _args in calls)
        return []

    class _Loop:

        def __init__(self, *, tools, **kwargs):
            seen.extend(tools)
            raise _Stop

    async def _no_fred(tool):
        return []

    for module in (filing, fundamental, risk, news, macro):
        monkeypatch.setattr(module, "prefetch_tool_calls", _seen_prefetch)
        monkeypatch.setattr(module, "ReActAgentLoop", _Loop)
    monkeypatch.setattr(macro, "_prefetch_live_fred", _no_fred)
    return seen


_PROMPT_CASES = {
    "judge-without-optional-sections": (
        lambda: judge._build_system_prompt(False, False, False), [
            "trailing P/E LOWER than core earnings justify",
            "weigh the forward P/E instead",
            "LOWER than core earnings justify -- never higher",
            "'unrecognized tax benefits') is not a gain"
        ], []),
    "judge-with-all-optional-sections": (
        lambda: judge._build_system_prompt(True, True, True), [
            "trailing P/E LOWER than core earnings justify",
            "weigh the forward P/E instead"
        ], []),
    "judge-verdict-fields": (
        lambda: judge._build_system_prompt(False), [
            '"deciding_factor"', "single factor that decides your verdict",
            "only when the user message includes that valuation note",
            "opposing_points"
        ], ["trailing-twelve-month net income", "cited_figures"]),
    "judge-base": (
        lambda: judge._BASE_SYSTEM_PROMPT,
        ["[agent-derived", dc.PERIOD_LABEL_RULE], _NO_TRIGGER_WORDS),
    "bull": (lambda: _BULL_SYSTEM_PROMPT, [dc.PERIOD_LABEL_RULE], []),
    "bear": (lambda: _BEAR_SYSTEM_PROMPT, [dc.PERIOD_LABEL_RULE], []),
    "reflection": (
        lambda: reflection._SYSTEM_PROMPT,
        ["4. Period and label consistency",
         '"wrong_period"'], _NO_TRIGGER_WORDS),
    "profit-trend-rule": (
        lambda: dc.PROFIT_TREND_RULE,
        ["latest-quarter results computed from SEC XBRL"], _NO_TRIGGER_WORDS),
    "risk": (
        lambda: risk._SYSTEM_PROMPT, [
            "never describe regulatory or legal risk as contained",
            "Use the code-rendered values and the filing records",
            "state the status that the newest filing text you were shown "
            "supports", "management said on the Q2 2026 call",
            "already in your scratchpad", "catalog=no"
        ], _GONE_FROM_PROMPTS + _NO_CATALOG_PHRASES),
    "risk-prefetched-filing-search": (
        lambda: risk.PREFETCH_RISK_FILING_QUERY, ["legal proceedings"], []),
    "risk-prefetched-transcript-search": (
        lambda: risk.PREFETCH_RISK_TRANSCRIPT_QUERY, [], []),
    "fundamental": (
        lambda: fundamental._SYSTEM_PROMPT, [
            "Use the code-rendered values and the filing records",
            "management said on the Q2 2026 call",
            "already in your scratchpad", "always at least one of them",
            "catalog=no"
        ],
        _GONE_FROM_PROMPTS + _NO_CATALOG_PHRASES + ["whichever of these 7"]),
    "fundamental-prefetched-transcript-search": (
        lambda: fundamental.PREFETCH_TRANSCRIPT_QUERY, [], []),
    "filing": (
        lambda: filing._SYSTEM_PROMPT, [
            "Use the code-rendered values and the filing records",
            "'Six Months Ended' or 'Nine Months Ended' is six or nine months",
            "never annual", "never write one in your answer"
        ], _GONE_FROM_PROMPTS + ["F1.R...", "an id such as F1.L2", "one-off"]),
    "macro": (
        lambda: macro._SYSTEM_PROMPT, [
            "quote the year-over-year change", "You have no company data",
            "do not state this company's own financials", "catalog=no"
        ], _NO_CATALOG_PHRASES),
    "news": (lambda: news._SYSTEM_PROMPT, ["catalog=no"], _NO_CATALOG_PHRASES)
}
_REMOVED_NAMES = {
    "filing-phrase-checks": (
        filing, [
            "one_off_items_issue", "relabeled_net_income_issue",
            "filing_recency_issue", "filing_answer_issue",
            "strip_period_prefixes"
        ]),
    "risk-phrase-checks": (
        risk, [
            "_superseded_status_issue", "_completed_deal_issue",
            "_event_status_note"
        ]),
    "source-credibility-tool": (
        news_tool_module, ["AssessSourceCredibilityTool"]),
    "kg-context-legal-filter": (
        kg_context_module, ["drop_resolved_legal_relations"]),
    "extraction-legal-filter": (
        extraction_module, ["drop_resolved_legal_triples"]),
    "debate-common-word-lists": (
        dc, [
            "profit_changes_in_passage", "newest_quarter_changes",
            "profit_decline_line", "period_label_findings", "percent_mentions",
            "split_clauses", "annual_figures_as_current",
            "unstated_profit_declines", "_SIDE_RE_CACHE", "_CONFIRMED_OK_RE",
            "self_verify_argument", "extract_flagged_figures",
            "repeats_flagged_figures", "structured_facts_lines",
            "agent_derived_only_figures"
        ]),
    "reflection-word-lists": (
        reflection, [
            "_UNGROUNDED_CLAIM_RE", "_SIDE_NAME_RES",
            "_is_debate_ungrounded_claim", "_debate_period_mismatches"
        ]),
    "judge-figure-logging": (
        judge, ["_log_cited_figures", "_log_agent_derived_figures"]),
    "sizing-buy-cap": (
        sizing_module,
        ["BuyCap", "buy_cap_for", "buy_cap_pct", "_scale_tiers_to_cap"]),
    "buy-cap-settings": (
        config.settings, [
            "TIER_BUY_CAP_PCT_MIN", "TIER_BUY_CAP_PCT_MAX",
            "TIER_BUY_CAP_CONF_LOW", "TIER_BUY_CAP_CONF_HIGH"
        ])
}


class _EmptyNewsProvider:

    async def search_news(self, query, *, since=None, limit=20):
        return []


_KO_ROWS = [
    _Row(
        "KO_10-K_2026-02-20.txt", "10-K", date(2026, 2, 20),
        date(2025, 12, 31), {
            "legal_matters": [{
                "name": "Baltimore plastic packaging lawsuit",
                "status": "pending",
                "status_date": None,
                "detail": "Public nuisance claim remains."
            }]
        }),
    _Row(
        "KO_10-Q_2026-07-29.txt", "10-Q", date(2026, 7, 29), date(2026, 7, 3),
        {
            "legal_matters": [{
                "name": "Baltimore plastic packaging lawsuit",
                "status": "dismissed",
                "status_date": "2026-07-23",
                "detail": "Remaining claim dismissed."
            }],
            "one_off_items": [{
                "description": "Escrowed shares mark-to-market loss",
                "amount_text": "$12.5 billion",
                "period": "Q2 2026"
            }, {
                "description": "Restructuring charge",
                "amount_text": "$310 million",
                "period": None
            }]
        })
]
_KO_FILINGS = (
    frec.render_filing_records(_KO_ROWS) + "\n\n"
    "Filing search results for 'legal proceedings' (2 passages):\n"
    "- (KO) [KO_10-Q_2026-07-29] (filed 2026-07-29): the remaining claim of "
    "the Baltimore plastic packaging lawsuit was dismissed.\n"
    "- (KO) [KO_10-K_2026-02-20] (filed 2026-02-20): the Baltimore plastic "
    "packaging lawsuit's public nuisance claim remains active.")
_KEY_ROWS = [
    _Row(
        "KO_10-Q_2026-07-29.txt", "10-Q", date(2026, 7, 29), date(2026, 7, 3),
        {
            "legal_matters": [{
                "name": "U.S. Federal Income Tax Dispute",
                "status": "appeal",
                "status_date": "2026-06-25",
                "exposure_text": "$ 14 billion",
                "next_event": "Tax Court decision expected.",
                "next_event_date": "2027"
            }, {
                "name": "Derivative Action",
                "status": "pending",
                "next_event": "Stay ends.",
                "next_event_date": "2026-08-21"
            }, {
                "name": "Quiet matter",
                "status": "pending"
            }],
            "transactions": [{
                "name": "Term loan",
                "type": "financing",
                "status": "completed",
                "date": "2026-06",
                "detail": "A $ 17.5 billion term loan."
            }, {
                "name": "Lease guarantees",
                "type": "guarantee",
                "status": "pending"
            }, {
                "name": "Africa bottling sale",
                "type": "divestiture",
                "status": "announced",
                "next_event": "Closing expected.",
                "next_event_date": "2026-10"
            }, {
                "name": "Old acquisition",
                "type": "acquisition",
                "status": "completed"
            }],
            "guidance": [{
                "metric": "capital expenditures",
                "value_text": "approximately $2.2 billion",
                "period_covered": "full year 2026"
            }, {
                "metric": "effective tax rate",
                "value_text": "19.9%"
            }],
            "one_off_items": [{
                "description": "x",
                "amount_text": "$1 billion"
            }]
        }),
    _Row(
        "KO_10-K_2026-02-20.txt", "10-K", date(2026, 2,
                                               20), date(2025, 12, 31),
        {"guidance": [{
            "metric": "old guidance",
            "value_text": "$1 billion"
        }]})
]
_FUND_OBS_WITH_NOTE = (
    "Fundamentals for 'GOOGL' (source: sec_edgar+yahoo_finance, as of "
    "2026-09-20): P/E=22.00 operating margin (annual, latest FY)=32.0% net "
    "margin (annual, latest FY)=35.0% | gross margin: not disclosed "
    "separately by this filer; note: operating margin (32.0%) is below net "
    "margin (35.0%) -- can be legitimate (e.g. one-time gains, tax "
    "adjustments) but worth double-checking against the filing directly")
_ANNUAL_FUNDAMENTALS = (
    "Fundamentals for 'TSLA': P/E=325.51 forward P/E=164.04 P/B=16.13 "
    "revenue growth YoY (annual, latest FY vs prior FY)=-2.9% gross margin "
    "(annual, latest FY)=18.0% operating margin (annual, latest FY)=4.6% "
    "net margin (annual, latest FY)=4.0%")
_QUARTERLY_FUNDAMENTALS = (
    "Fundamentals for 'XOM': revenue growth YoY (quarterly, latest quarter "
    "vs year-ago quarter)=44.1% net margin (TTM)=9.1%")
_LOSS_FUNDAMENTALS = (
    "Fundamentals for 'INTC': revenue growth YoY (annual, latest FY vs "
    "prior FY)=-0.5% operating margin (annual, latest FY)=-4.2% net margin "
    "(annual, latest FY)=-0.5%")


def _fundamentals_pad(observation: str) -> list[ScratchpadEntry]:
    return [
        ScratchpadEntry(
            thought="t", action="get_fundamentals",
            action_input={"ticker": "X"}, observation=observation,
            is_tool_result=True)
    ]


class _FakeFundTool(Tool):
    name = "get_fundamentals"
    description = "fundamentals"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    async def run(self, ticker: str) -> str:
        return _FUND_OBS_WITH_NOTE

    async def latest_quarter(self, ticker: str) -> dict[str, Any] | None:
        return {
            "text": "Latest quarter (SEC XBRL, 10-Q filed 2026-07-24): "
            f"{ticker}"
        }


_INDICATORS_OBS = (
    "Technical indicators for 'AAPL' (60 daily bars):\n"
    "SMA(20): 187.42\n"
    "SMA(50): 182.10\n"
    "RSI(14): 65.3\n"
    "MACD: line=1.234 signal=0.987 histogram=0.247\n"
    "Bollinger Bands(20): upper=192.50 mid=187.42 lower=182.34\n"
    "ATR(14): 3.21")
_INDICATORS_PRICE_HISTORY = (
    "Recent price history for 'AAPL' (1d, showing last 2 of 60 bars):\n"
    "2026-09-01: open=185 high=186 low=184 close=185.5 volume=1000\n"
    "2026-09-02: open=185.5 high=190 low=185 close=189.1 volume=1200")
_NEAR_ZERO_MACD_OBS = (
    "Technical indicators for 'AAPL' (60 daily bars):\n"
    "Latest close: 180.00 (bar date 2026-09-12)\n"
    "SMA(20): 187.42\n"
    "SMA(50): 175.10\n"
    "Price vs SMA(20): below (-4.0%)\n"
    "Price vs SMA(50): above (+2.8%)\n"
    "RSI(14): 45.3\n"
    "MACD: line=1.234 signal=1.200 histogram=0.034\n"
    "ATR(14): 3.20")
_RSI_NEUTRAL_OBS = (
    "Technical indicators for 'KO' (64 daily bars):\n"
    "Latest close: 85.65 (bar date 2026-10-02)\n"
    "RSI(14): 38.0 (band: neutral (lower half, 30-50); standard "
    "bands: <=30 oversold, >=70 overbought, otherwise neutral)\n")


class _FakeVolTool(Tool):
    name = "get_volatility_and_drawdown"
    description = "vol"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    async def run(self, ticker: str) -> str:
        return (
            f"Volatility for {ticker}: annualized volatility=24.1%, max "
            "drawdown=-18.2%. Filing notes an antitrust review by the "
            "Federal Trade Commission.")


def _ko_issue(metrics: dict[str, Any], *, shown: bool = True) -> str | None:
    return _risk_answer_issue(
        "x", metrics, _steps(("search_filings", _KO_FILINGS)), rows=_KO_ROWS,
        shown=shown)[0]


def _baltimore_event(**fields: str) -> dict[str, Any]:
    return {
        "risk_events": [{
            "entity": "Baltimore plastic packaging lawsuit",
            "evidence_tool": "search_filings",
            **fields
        }]
    }


_MSFT_ROWS = [
    _Row(
        "MSFT_10-K_2026-07-29.txt", "10-K", date(2026, 7, 29),
        date(2026, 6, 30), {
            "legal_matters": [{
                "name": "Irish DPC LinkedIn GDPR inquiry",
                "status": "appeal",
                "status_date": None
            }]
        }),
    _Row(
        "MSFT_10-Q_2026-04-29.txt", "10-Q", date(2026, 4, 29),
        date(2026, 3, 31))
]
_MSFT_FILINGS = (
    frec.render_filing_records(_MSFT_ROWS) +
    "\n\nFiling search results (1 passages):"
    "\n- (MSFT) [MSFT_10-K_2026-07-29] (filed 2026-07-29): the IRS is "
    "seeking $28.9 billion plus penalties and interest.")
_TSLA_TWO_ROWS = [
    _Row(
        "TSLA_10-Q_2026-07-23.txt", "10-Q", date(2026, 7, 23),
        date(2026, 6, 30), {
            "legal_matters": [{
                "name": "Certain Derivative Lawsuits in Delaware",
                "status": "appeal",
                "status_date": "2026-05",
                "detail": None,
                "quote": "In May 2026, plaintiffs in the consolidated and "
                "coordinated cases appealed to the Delaware "
                "Supreme Court the decision granting Tesla's "
                "motion to dismiss."
            }, {
                "name": "CRD Action",
                "status": "pending",
                "status_date": "2022-02-09",
                "detail": None,
                "next_event": "The first phase of the trial is scheduled "
                "to begin.",
                "next_event_date": "2026-09-21",
                "quote": "The first phase of the trial is currently set "
                "for September 21, 2026."
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
                "quote": "On September 15, 2025, the Company filed a "
                "post-trial motion for judgment as a matter of "
                "law."
            }]
        })
]
_TSLA_TWO_FILINGS = (
    frec.render_filing_records(_TSLA_TWO_ROWS) +
    "\n\nFiling search results (2 passages):\n"
    "- (TSLA) [TSLA_10-Q_2026-07-23] (filed 2026-07-23): Benavides v. "
    "Tesla: $329M verdict, appeal brief filed Jul 2026.\n"
    "- (TSLA) [TSLA_10-K_2026-01-29] (filed 2026-01-29): Ongoing requests "
    "from NHTSA, NTSB, SEC, DOJ; no agency has concluded wrongdoing to "
    "date.")
_TSLA_DRAFT = _events(("NHTSA", "pending", "F1.L1"),
                      ("Benavides v. Tesla", "appeal", "F1.L1"))
_UNH_PRIOR_NOTES = (
    "Prior risk note search results for 'regulatory competition "
    "litigation' (1 passages):\n"
    "Note: passages tagged [agent:...] were written by AlphaMind agents in "
    "earlier runs.\n"
    "- (UNH) [agent:risk] (2026-10-01): UnitedHealth Group (UNH) ... driven "
    "by ongoing Department of Justice (DOJ) antitrust and False Claims Act "
    "investigations, Internal Revenue Service (IRS) transfer pricing "
    "disputes.")
_UNH_FILINGS = (
    "- (UNH) [UNH_10-K] (filed 2026-02-27): Legal/regulatory actions: "
    "investigations (CMS, DOJ, OIG, FTC, state AGs) may result in "
    "penalties.")


def _irs(source_id: str) -> dict[str, Any]:
    return {
        "risk_events": [{
            "entity": "IRS",
            "evidence_tool": "search_filings",
            "status": "pending",
            "source_id": source_id
        }]
    }


class _FakeMacroProvider:

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = set(failing or ())
        self.calls: list[str] = []

    async def get_indicator(self, series: str, *, lookback_days: int = 365):
        self.calls.append(series)
        if series in self.failing:
            raise ProviderRequestError(
                f"FRED lookup failed for series {series!r}: ReadTimeout")
        sid, value, day = {
            "fed_funds_rate": ("DFF", 4.33, date(2026, 8, 1)),
            "10y_treasury_yield": ("DGS10", 4.12, date(2026, 9, 22))
        }[series]
        return [
            MacroDataPoint(
                series_id=sid, label=sid, value=value, unit="%",
                observed_at=day, source="fake")
        ]


_COMPOSITION_OBS = (
    "Portfolio composition (2 holdings, total market value ~12640.40). "
    "Available cash: 7698.95.\n- MSFT: qty=20.0 price=498.62 weight=49.0%")
_SCENARIO_OBS = (
    "Scenario: MSFT +10 shares at 498.62. Position concentration: 49.0% -> "
    "73.5% of portfolio (total net value, holdings + cash, ~20339.35).\n"
    "cost=4986.20, available_cash=7698.95, shortfall=0.00")


class _FakeCompositionTool(Tool):
    name = "get_portfolio_composition"
    description = "composition"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def run(self) -> str:
        return _COMPOSITION_OBS


class _FakeScenarioTool(Tool):
    name = "recompute_scenario"
    description = "scenario"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def run(self, ticker: str, additional_shares: float) -> str:
        return _SCENARIO_OBS


_QCOM_QUARTER: dict[str, Any] = {
    "text": (
        "Latest quarter (SEC XBRL, 10-Q filed 2026-07-29, quarter 2026-03-30 "
        "to 2026-06-28, compared with the quarter ended 2025-06-29): revenue "
        "$9.95B vs $10.37B (-4%); operating income $1.63B vs $2.76B (-41%); "
        "net income $2.00B vs $2.67B (-25%)\nNet income minus operating "
        "income: +$376M"),
    "form": "10-Q",
    "filed": "2026-07-29",
    "period_start": "2026-03-30",
    "period_end": "2026-06-28",
    "revenue": 9.947e9,
    "revenue_prior": 10.365e9,
    "revenue_yoy_pct": -4.0,
    "operating_income": 1.626e9,
    "operating_income_prior": 2.762e9,
    "operating_income_yoy_pct": -41.1,
    "net_income": 2.002e9,
    "net_income_prior": 2.666e9,
    "net_income_yoy_pct": -24.9,
    "net_minus_operating": 0.376e9
}
_INTC_QUARTER: dict[str, Any] = {
    "text": "Latest quarter (SEC XBRL, 10-Q filed 2026-07-24): net income "
    "-$11.03B vs -$2.92B (loss widened 278%)",
    "period_end": "2026-06-27",
    "operating_income": 1.80e9,
    "operating_income_prior": -3.18e9,
    "operating_income_yoy_pct": None,
    "net_income": -11.03e9,
    "net_income_prior": -2.92e9,
    "net_income_yoy_pct": -277.7
}
_TSLA_QUARTER: dict[str, Any] = {
    "text": "Latest quarter (SEC XBRL): operating income $398M vs $923M "
    "(-57%)",
    "period_end": "2026-06-30",
    "operating_income": 398e6,
    "operating_income_prior": 923e6,
    "operating_income_yoy_pct": -56.9,
    "net_income": 1.11e9,
    "net_income_prior": 1.17e9,
    "net_income_yoy_pct": -5.1
}


def _quarter_fundamental(
        quarter: dict[str, Any] | None = None, **raw: Any) -> AgentOutput:
    raw_output: dict[str, Any] = {
        "scratchpad": [{
            "action": "get_fundamentals",
            "observation": "P/E=21.10 forward P/E=18.12",
            "is_tool_result": True,
            "tool_error": False
        }],
        **raw
    }
    if quarter is not None:
        raw_output["latest_quarter"] = quarter
    return AgentOutput(
        role=AgentRole.FUNDAMENTAL, status=AgentStatus.COMPLETED,
        summary="Forward P/E is 18.12.", score=0.84, raw_output=raw_output)


def _qcom(**overrides: Any) -> AnalysisState:
    return _state(
        "QCOM", agent_outputs={
            AgentRole.FUNDAMENTAL: _quarter_fundamental(_QCOM_QUARTER)
        }, **overrides)


def _margins_state(margins: dict[str, float] | None) -> AnalysisState:
    outputs: dict[AgentRole, AgentOutput] = {}
    if margins is not None:
        outputs[AgentRole.FUNDAMENTAL] = AgentOutput(
            role=AgentRole.FUNDAMENTAL, summary="Fundamentals.", score=0.9,
            raw_output={"key_facts": margins})
    return _state("AMD", agent_outputs=outputs)


def _pe_state(pe: str) -> AnalysisState:
    output = AgentOutput(
        role=AgentRole.FUNDAMENTAL, status=AgentStatus.COMPLETED, summary="f",
        score=0.84, raw_output={
            "scratchpad": [{
                "action": "get_fundamentals",
                "observation": pe,
                "is_tool_result": True,
                "tool_error": False
            }],
            "key_facts": {
                "net_margin_pct": 12.5,
                "operating_margin_pct": 10.7
            }
        })
    return _state("QCOM", agent_outputs={AgentRole.FUNDAMENTAL: output})


def _judge_json(
        converged: bool, leaning: str = "buy", confidence: float = 0.7) -> str:
    return json.dumps({
        "converged": converged,
        "leaning": leaning,
        "confidence": confidence,
        "rationale": "test rationale"
    })


def _failed_outputs(roles: list[AgentRole]) -> dict[AgentRole, AgentOutput]:
    return {
        role: AgentOutput(
            role=role, status=AgentStatus.FAILED,
            summary=f"{role.value} agent failed: 402 error", score=0.0,
            is_well_supported=False)
        for role in roles
    }


def _market_state() -> AnalysisState:
    return _state(
        agent_outputs={
            AgentRole.MARKET: AgentOutput(
                role=AgentRole.MARKET, summary="close 151.23, up 4%",
                score=0.8)
        })


def _review_state() -> AnalysisState:
    market_output = AgentOutput(
        role=AgentRole.MARKET, summary="close 151.23, up 4% this month",
        score=0.9)
    debate = DebateState(
        round_number=1, arguments=[
            DebateArgument(
                round_number=1, side="bull",
                argument="Q3 margin 27.9% and a $30B buyback "
                "[fundamental]."),
            DebateArgument(
                round_number=1, side="bear",
                argument="Operating income fell 41% [fundamental].")
        ], verdicts=[
            JudgeVerdict(
                round_number=1, converged=True,
                leaning=RecommendationAction.HOLD, confidence=0.6,
                rationale="r")
        ])
    return _state(
        "QCOM", agent_outputs={
            AgentRole.FUNDAMENTAL: _quarter_fundamental(_QCOM_QUARTER),
            AgentRole.MARKET: market_output
        }, debate=debate)


async def _review(monkeypatch, *issues: Any) -> ReflectionVerdict:
    router = _Router(
        [json.dumps({
            "checked_claims": ["margin"],
            "issues": list(issues)
        })], finish_reason="STOP")
    monkeypatch.setattr(reflection, "LLMRouter", lambda: router)
    return await ReflectionAgent().check(_review_state())


class _TextRouter:

    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls: list[Any] = []

    async def complete(self, tier: Any, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(messages)
        return LLMResponse(
            content=self.contents.pop(0), provider="f", model="f",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class TestAgentSetup:

    @pytest.mark.asyncio
    async def test_iteration_caps_and_prefetched_first_calls(
            self, record_prefetch):
        assert sentiment.MAX_ITERATIONS == 4
        assert filing.MAX_ITERATIONS == 5
        assert fundamental.MAX_ITERATIONS == 8
        assert technical.MAX_ITERATIONS == 4
        assert risk.MAX_ITERATIONS == 7
        assert macro.MAX_ITERATIONS == 7
        assert news.MAX_ITERATIONS == 7
        assert portfolio.MAX_ITERATIONS == 7
        cases = {
            "market": (
                lambda: market.MarketAgent(role=AgentRole.MARKET), [(
                    "get_recent_price_history", {
                        "ticker": "MSFT",
                        "interval": "1d",
                        "lookback_days": 30
                    })]),
            "technical": (
                lambda: technical.TechnicalAgent(role=AgentRole.TECHNICAL), [(
                    "get_technical_indicators", {
                        "ticker": "MSFT",
                        "lookback_days": 90
                    })]),
            "fundamental": (
                lambda: fundamental.FundamentalAgent(
                    role=AgentRole.FUNDAMENTAL),
                [("get_fundamentals", {
                    "ticker": "MSFT"
                }),
                 (
                     "search_filings", {
                         "query": fundamental.PREFETCH_LATEST_RESULTS_QUERY
                     }),
                 (
                     "search_earnings_call_transcripts", {
                         "query": fundamental.PREFETCH_TRANSCRIPT_QUERY
                     })]),
            "filing": (
                lambda: filing.FilingAgent(role=AgentRole.FILING),
                [("search_filings", {
                    "query": filing.PREFETCH_FILING_QUERY
                })]),
            "risk": (
                lambda: risk.RiskAgent(role=AgentRole.RISK),
                [("get_volatility_and_drawdown", {
                    "ticker": "MSFT"
                }),
                 (
                     "search_prior_risk_notes", {
                         "query": risk.PREFETCH_RISK_NOTES_QUERY
                     }),
                 (
                     "search_filings", {
                         "query": risk.PREFETCH_RISK_FILING_QUERY
                     }),
                 (
                     "search_earnings_call_transcripts", {
                         "query": risk.PREFETCH_RISK_TRANSCRIPT_QUERY
                     })]),
            "portfolio": (
                lambda: portfolio.PortfolioAgent(
                    role=AgentRole.PORTFOLIO, portfolio_id="p1"),
                [("get_portfolio_composition", {})]),
            "news": (
                lambda: news.NewsAgent(role=AgentRole.NEWS), [
                    (
                        "search_prior_news_notes", {
                            "query": news.PREFETCH_PRIOR_NOTES_QUERY
                        }),
                    (
                        "search_news", {
                            "query": news.PREFETCH_NEWS_QUERY,
                            "limit": 10
                        })
                ]),
            "macro": (
                lambda: macro.MacroAgent(role=AgentRole.MACRO), [
                    (
                        "get_macro_indicators", {
                            "series": "cpi",
                            "lookback_days": 430
                        }),
                    (
                        "search_prior_macro_notes", {
                            "query": macro.PREFETCH_MACRO_NOTES_QUERY
                        })
                ])
        }
        for case_id, (agent_factory, expected) in cases.items():
            record_prefetch.clear()
            with pytest.raises(_Stop):
                await agent_factory().run(_state("MSFT"))
            assert record_prefetch == [expected], case_id

    @pytest.mark.asyncio
    async def test_search_tools_are_bound_to_the_ticker(self, captured_tools):
        cases = {
            "filing": (
                lambda: filing.FilingAgent(role=AgentRole.FILING),
                {"search_filings"}, "MSFT"),
            "fundamental": (
                lambda: fundamental.FundamentalAgent(
                    role=AgentRole.FUNDAMENTAL), {
                        "search_filings", "search_prior_analysis",
                        "search_earnings_call_transcripts"
                    }, "MSFT"),
            "risk": (
                lambda: risk.RiskAgent(role=AgentRole.RISK), {
                    "search_filings", "search_prior_risk_notes",
                    "search_earnings_call_transcripts"
                }, "MSFT"),
            "news": (
                lambda: news.NewsAgent(role=AgentRole.NEWS),
                {"search_prior_news_notes"}, "MSFT"),
            "macro-notes-stay-cross-ticker": (
                lambda: macro.MacroAgent(role=AgentRole.MACRO),
                {"search_prior_macro_notes"}, None)
        }
        for case_id, (agent_factory, expected_names, ticker) in cases.items():
            captured_tools.clear()
            with pytest.raises(_Stop):
                await agent_factory().run(_state("MSFT"))
            tools = {
                t.name: t
                for t in captured_tools if isinstance(
                    t, SearchFilingsTool | SearchResearchArchiveTool)
            }
            assert set(tools) == expected_names, case_id
            assert {t.ticker for t in tools.values()} == {ticker}, case_id


class TestPromptsAndSettings:

    def test_prompt_wording(self):
        for case_id, (prompt_of, present, absent) in _PROMPT_CASES.items():
            prompt = prompt_of()
            assert prompt, case_id
            for text in present:
                assert text in prompt, (case_id, text)
            for text in absent:
                assert text not in prompt, (case_id, text)

    def test_removed_helpers_modules_and_settings_defaults(self):
        for case_id, (owner, names) in _REMOVED_NAMES.items():
            for name in names:
                assert not hasattr(owner, name), (case_id, name)
        assert importlib.util.find_spec("ai.knowledge.filing_recency") is None
        assert importlib.util.find_spec("ai.agents.source_credibility") is None
        settings = config.settings
        assert settings.MIN_BUY_TIER_CONFIDENCE == 0.5
        assert settings.JUDGE_RETRY_BUDGET_SECONDS == 240.0
        assert settings.DEBATE_SIDE_REPAIR_DELAY_SECONDS == 60.0
        assert dc.DEBATE_MAX_REGENERATIONS == 1


class TestMarketAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_output_mapping_with_and_without_tool_evidence(
            self, monkeypatch):
        router = _Router([_finish("AAPL is trending up.")])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await market.MarketAgent(role=AgentRole.MARKET).run(_state())

        assert output.role == AgentRole.MARKET
        assert output.status == AgentStatus.COMPLETED
        assert AgentStatus.COMPLETED.value
        assert output.summary.startswith("[VERIFICATION WARNING: ")
        assert output.summary.endswith("\n\nAAPL is trending up.")
        assert len(output.raw_output["verification_warnings"]) == 1
        assert len(router.calls) == 3
        assert output.is_well_supported is False
        assert output.score == 0.35
        assert output.hit_iteration_cap is False

        class _FakeMarketProvider:

            async def get_current_price(self, ticker):
                return PriceQuote(
                    ticker=ticker, price=123.45, as_of=datetime.now(UTC),
                    source="fake")

        router = _Router([
            _action("get_current_price", {"ticker": "AAPL"}),
            _finish(
                "AAPL is trending up at 123.45.", {"latest_close": 123.45})
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            market_tool_module, "get_market_data_provider",
            lambda: _FakeMarketProvider())

        output = await market.MarketAgent(role=AgentRole.MARKET).run(_state())
        assert output.is_well_supported is True
        assert output.score == 0.84
        assert len(router.calls) == 2

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_key_facts_fallback_and_reflection_feedback(
            self, monkeypatch, _schema_ready):
        router = _Router([
            _action("get_recent_price_history", {"ticker": "ZKFT"}),
            _finish(
                "trend looks stable, last close 105.5", {
                    "latest_close": 105.5,
                    "period_high": 106.0,
                    "period_low": 99.0
                })
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        class _FakeMarketProvider:

            async def get_recent_price_history(
                    self, ticker, *, interval="1d", lookback_days=30):
                return [
                    PriceBar(
                        ticker=ticker, interval=interval,
                        timestamp=datetime(2026, 1, 1,
                                           tzinfo=UTC), open=100.0, high=101.0,
                        low=99.0, close=100.0, volume=1000, source="fake"),
                    PriceBar(
                        ticker=ticker, interval=interval,
                        timestamp=datetime(2026, 1, 2,
                                           tzinfo=UTC), open=100.0, high=106.0,
                        low=99.0, close=105.5, volume=1200, source="fake")
                ]

            async def get_instrument_metadata(self, ticker):
                return InstrumentMetadata(
                    name=f"{ticker} Inc.", sector=None, exchange=None)

        monkeypatch.setattr(
            market_tool_module, "get_market_data_provider",
            lambda: _FakeMarketProvider())

        output = await market.MarketAgent(role=AgentRole.MARKET
                                          ).run(_state("ZKFT"))

        assert output.raw_output["key_facts"] == {"reference_price": 105.5}
        assert output.is_well_supported is True

        router = _Router([_finish("ok")])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        state = _state(
            reflection_feedback={AgentRole.MARKET: "check a longer window"})
        output = await market.MarketAgent(role=AgentRole.MARKET).run(state)

        assert output.retry_of_feedback == "check a longer window"
        assert "check a longer window" in router.calls[0][-1].content

    def test_reported_metrics_must_match_the_prices(self):
        pad = [
            ScratchpadEntry(
                "t", "get_recent_price_history", {}, _PRICE_HISTORY_OBS)
        ]
        cases = {
            "missing-metrics": (None, "latest_close, period_high, period_low"),
            "wrong-value": ({
                "latest_close": 440.50,
                "period_high": 231.35,
                "period_low": 221.0
            }, "latest_close=440.5")
        }
        for case_id, (metrics, expected) in cases.items():
            issue = market._market_claims_disagree_with_prices(
                "x", metrics, pad)
            assert expected in issue, case_id


class TestNewsAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_provider_failure_and_stale_prior_note_are_observations(
            self, monkeypatch):

        class _BrokenNewsProvider:

            async def search_news(self, query, *, since=None, limit=20):
                raise ProviderRequestError("upstream down")

        router = _Router([
            _action("search_news", {"query": "AAPL"}),
            _finish("could not confirm sentiment, provider was down")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            news_tool_module, "get_news_provider",
            lambda: _BrokenNewsProvider())

        output = await news.NewsAgent(role=AgentRole.NEWS).run(_state())

        assert output.status == AgentStatus.COMPLETED
        first = output.raw_output["scratchpad"][0]
        assert "upstream down" in first["observation"]
        assert first["tool_error"] is True
        assert output.raw_output["no_evidence"] is True
        assert output.is_well_supported is False

        class _StaleNewsNoteRetriever:

            async def retrieve(self, query, *, ticker=None):
                return [
                    RetrievedChunk(
                        chunk_id="c1", text="margin pressure from input costs",
                        score=0.9, category=DocumentCategory.NEWS,
                        ticker=ticker, content_date="2026-08-27")
                ]

        router = _Router([
            _action(
                "search_prior_news_notes", {
                    "query": "recent material developments",
                    "ticker": "AAPL"
                }),
            _action("web_search", {"query": "AAPL recent developments"}),
            _finish("no new material developments since the prior note")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            research_archive_tool_module, "BaseRetriever",
            lambda **kw: _StaleNewsNoteRetriever())

        output = await news.NewsAgent(role=AgentRole.NEWS).run(_state())

        observation = output.raw_output["scratchpad"][0]["observation"]
        assert "margin pressure from input costs" in observation
        assert "[STALE" in observation

    @pytest.mark.asyncio
    async def test_unresolved_issue_kind_and_live_search_rule(
            self, monkeypatch):
        cases = {
            "live-search-ran": ([_live_news()], "generic"),
            "only-prior-notes": ([_prior_news_note("Old note.")],
                                 "live_search")
        }
        monkeypatch.setattr(news, "prefetch_tool_calls", _no_prefetch)
        for case_id, (pad, kind) in cases.items():
            result = AgentLoopResult(
                final_answer="Tesla deliveries 486,532 units.", scratchpad=pad,
                unresolved_finish_validation_issue=(
                    "the answer shares nothing with the tools"))
            monkeypatch.setattr(
                news, "ReActAgentLoop", _fixed_result_loop(result))
            with capture_logs() as logs:
                await news.NewsAgent(role=AgentRole.NEWS).run(_state())
            events = [log["event"] for log in logs]
            assert "news.live_search_validator_exhausted" not in events
            unresolved = [
                log for log in logs
                if log["event"] == "news.finish_issue_unresolved"
            ]
            assert unresolved, case_id
            assert unresolved[0]["kind"] == kind, case_id

        ok = _live_news()
        failed_search = _entry(
            "search_news", "News search failed for 'AAPL': quota",
            tool_error=True)
        bad_args = _entry(
            "search_news",
            "Tool 'search_news' was called with invalid arguments: "
            "unexpected 'q'", tool_error=True)
        raised = _entry(
            "search_news", "Tool 'search_news' raised an error: boom",
            tool_error=True)
        blocked = _entry(
            "search_news", "[Blocked before running]", is_tool_result=False)
        assert counts_as_live_search(ok)
        assert counts_as_live_search(failed_search)
        assert not counts_as_live_search(bad_args)
        assert not counts_as_live_search(raised)
        assert not counts_as_live_search(blocked)
        assert counts_as_live_search(vars(ok))

        validator = _NewsFinishValidator()
        issue = validator(
            "Summary from notes.", None,
            [_prior_news_note("Old note."), bad_args])
        assert issue is not None and "search_news" in issue

    @pytest.mark.asyncio
    async def test_wrong_weekday_is_logged_only(self, monkeypatch):
        live = _entry(
            "search_news",
            "News results for 'x' (1 articles: 1 company business, 0 stock "
            "market, 0 opinion/listicle, 0 consumer product, 0 "
            "unclassified):\n"
            "[2026-10-02 Fri] Src: Boeing review | catalog=yes")
        answer = "The FAA set a review for Friday, October 3, 2026."
        assert _NewsFinishValidator()(answer, None, [live]) is None

        result = AgentLoopResult(
            final_answer=answer, scratchpad=[live],
            finish_meta={"finish_reason": "stop"})
        monkeypatch.setattr(news, "ReActAgentLoop", _fixed_result_loop(result))
        monkeypatch.setattr(news, "prefetch_tool_calls", _no_prefetch)
        with capture_logs() as logs:
            output = await news.NewsAgent(role=AgentRole.NEWS).run(_state())
        assert output.summary == answer
        events = [
            e for e in logs if e["event"] == "news.weekday_date_mismatch"
        ]
        assert events and events[0]["mismatches"] == [{
            "written": "Friday, October 3, 2026",
            "actual_weekday": "Saturday"
        }]
        assert "without weekday names" in news._SYSTEM_PROMPT


class TestSentimentAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_news_caveat_and_degrading_without_news(self, monkeypatch):
        router = _Router([_finish("Sentiment: positive")])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        news_output = _news_with_evidence(
            "[VERIFICATION WARNING: unverified]\n\nCPU prices rising.",
            verification_warnings=["used web_search unchecked"])
        state = _state(agent_outputs={AgentRole.NEWS: news_output})

        await sentiment.SentimentAgent(role=AgentRole.SENTIMENT).run(state)

        user_message = router.calls[0][-1].content
        assert "CPU prices rising." in user_message
        assert "Caveat:" in user_message
        assert "unconfirmed" in user_message

        class _FakeNewsProvider:

            async def search_news(self, query, *, since=None, limit=20):
                return [
                    NewsArticle(
                        title="Tesla deliveries flat quarter over quarter",
                        source_name="Reuters", url="https://reuters.com/t",
                        published_at=datetime.now(UTC),
                        summary="Deliveries were flat.")
                ]

        async def _passthrough_articles(articles, *, ticker):
            return articles

        router = _Router([
            _action("search_news", {"query": "TSLA"}),
            _finish(
                "Sentiment: neutral (searched independently) -- Tesla "
                "deliveries flat quarter over quarter.")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            news_tool_module, "get_news_provider", lambda: _FakeNewsProvider())
        monkeypatch.setattr(
            relevance_module, "classify_articles",
            _classified(_passthrough_articles))

        output = await sentiment.SentimentAgent(role=AgentRole.SENTIMENT
                                                ).run(_state())
        assert output.status == AgentStatus.COMPLETED
        assert output.raw_output["sentiment_label"] == "neutral"
        assert output.is_well_supported is True

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_label_is_read_from_the_line_or_recovered_by_classifier(
            self, monkeypatch):
        router = _Router([_finish("Overall, the sentiment looks mixed.")])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            sentiment, "LLMRouter", lambda: _AlwaysFailsRouter())

        output = await sentiment.SentimentAgent(role=AgentRole.SENTIMENT).run(
            _state(agent_outputs={AgentRole.NEWS: _news_with_evidence()}))
        assert output.raw_output["sentiment_label"] is None

        router = _Router(
            [_finish("Sentiment: cautiously optimistic, tempered by risk.")])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        classification_router = _Router(
            [json.dumps({"sentiment_label": "positive"})])
        monkeypatch.setattr(
            sentiment, "LLMRouter", lambda: classification_router)

        output = await sentiment.SentimentAgent(role=AgentRole.SENTIMENT).run(
            _state(agent_outputs={AgentRole.NEWS: _news_with_evidence()}))

        assert output.raw_output["sentiment_label"] == "positive"
        assert "cautiously optimistic" in (
            classification_router.calls[0][-1].content)

    @pytest.mark.asyncio
    async def test_no_news_and_no_own_results_is_no_evidence(
            self, monkeypatch):
        assert (
            sentiment._news_ran_fresh_search(
                _news_output(status=AgentStatus.FAILED)) is False)
        assert sentiment._news_ran_fresh_search(None) is None

        def no_news_state() -> AnalysisState:
            return _state(
                agent_outputs={
                    AgentRole.NEWS: _news_output(
                        observation="No news articles found for 'AAPL'.")
                })

        monkeypatch.setattr(
            news_tool_module, "get_news_provider",
            lambda: _EmptyNewsProvider())
        answer = _finish("Sentiment: neutral -- a quiet period with no news.")
        router = _ScriptedAgentRouter(
            [_action("search_news", {"query": "AAPL"}), answer, answer])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        output = await sentiment.SentimentAgent(
            role=AgentRole.SENTIMENT,
            llm_router=_NoCallRouter()).run(no_news_state())
        assert output.raw_output["no_evidence"] is True
        assert output.raw_output["sentiment_label"] is None
        assert output.summary.startswith("[NO EVIDENCE:")
        assert "Sentiment: neutral -- a quiet period" in output.summary
        assert (output.score, output.is_well_supported) == (0.35, False)
        assert "returned 0 relevant articles" in (router.calls[0][-1].content)

        router = _ScriptedAgentRouter([
            _action("search_news", {"query": "AAPL"}),
            _finish(
                "No sentiment reading is possible this run: no relevant "
                "coverage.")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        output = await sentiment.SentimentAgent(
            role=AgentRole.SENTIMENT,
            llm_router=_NoCallRouter()).run(no_news_state())
        assert len(router.calls) == 2
        assert output.raw_output["no_evidence"] is True

    @pytest.mark.asyncio
    async def test_unverified_fallback_news_is_thin_evidence(
            self, monkeypatch):
        captured: dict[str, Any] = {}
        real = loop_module.derive_agent_quality

        def _spy(result: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return real(result, **kwargs)

        monkeypatch.setattr(loop_module, "derive_agent_quality", _spy)
        monkeypatch.setattr(sentiment, "_news_live_yield", lambda out: 5)
        router = _ScriptedAgentRouter([
            _finish(
                "Sentiment: positive. Apple reported strong iPhone "
                "demand.")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        news_output = _news_output(
            observation="Web search results for 'x' (5 results, 0 from "
            "catalog outlets):\n- a\n- b",
            summary="Apple reported strong iPhone demand.")
        news_output.raw_output["fallback_sources_unverified"] = True
        state = _state(agent_outputs={AgentRole.NEWS: news_output})
        await sentiment.SentimentAgent(
            role=AgentRole.SENTIMENT, llm_router=_NoCallRouter()).run(state)
        assert captured["thin_evidence"] is True

    @pytest.mark.asyncio
    async def test_news_summary_is_the_anchor_only_when_building_on_news(
            self, monkeypatch):
        seen: dict[str, Any] = {}

        class _FakeLoop:

            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

            async def run(self, *args: Any, **kwargs: Any):
                raise _Stop

        news_output = AgentOutput(
            role=AgentRole.NEWS, summary=_AMZN_NEWS, score=0.9)
        state = AnalysisState(
            analysis_id="a1", user_id="", ticker="AMZN",
            analysis_type=AnalysisType.FULL,
            agent_outputs={AgentRole.NEWS: news_output})
        monkeypatch.setattr(sentiment, "ReActAgentLoop", _FakeLoop)
        agent = sentiment.SentimentAgent(AgentRole.SENTIMENT)

        monkeypatch.setattr(sentiment, "_news_live_yield", lambda _o: 12)
        with pytest.raises(_Stop):
            await agent.run(state)
        assert seen["anchor_fallback_reference"] == _AMZN_NEWS

        seen.clear()
        monkeypatch.setattr(sentiment, "_news_live_yield", lambda _o: 0)
        with pytest.raises(_Stop):
            await agent.run(state)
        assert seen["anchor_fallback_reference"] is None


class TestFilingAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_retrieved_passages_reach_the_agent(self, monkeypatch):

        class _FakeFilingRetriever:

            async def retrieve(self, query, *, ticker=None):
                return [
                    RetrievedChunk(
                        chunk_id="c1",
                        text="Supply chain risk disclosed in Item 1A.",
                        score=0.9, category=DocumentCategory.FILING,
                        ticker=ticker)
                ]

        router = _Router([
            _action("search_filings", {"query": "risk factors"}),
            _finish("Filing discloses supply chain risk.")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            filing_tool_module, "FilingRetriever",
            lambda **kw: _FakeFilingRetriever())

        output = await filing.FilingAgent(role=AgentRole.FILING).run(_state())

        observation = output.raw_output["scratchpad"][0]["observation"]
        assert "Supply chain risk disclosed" in observation

    @pytest.mark.asyncio
    async def test_one_off_items_and_key_records_from_filing_rows(self):
        assert one_off_item_lines(_KO_ROWS) == [
            "Escrowed shares mark-to-market loss: $12.5 billion (Q2 2026) "
            "[F1.O1, 10-Q filed 2026-07-29]",
            "Restructuring charge: $310 million [F1.O2, 10-Q filed "
            "2026-07-29]"
        ]
        assert one_off_item_lines(_KO_ROWS[:1]) == []
        assert one_off_item_lines([]) == []

        class _Broken(FilingRecordsCache):

            async def load(self, ticker: str) -> list[Any]:
                raise RuntimeError("db down")

        assert await key_records_for("KO", _Broken(), role="filing") == []
        cache = FilingRecordsCache()
        cache.rows = list(_KEY_ROWS)
        lines = await key_records_for("KO", cache, role="filing")
        assert len(lines) == 6 and not any("F1" in line for line in lines)


class TestFundamentalAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_get_fundamentals_tool_call_flows_through(self, monkeypatch):

        class _FakeFundamentalProvider:

            async def get_fundamentals(self, ticker):
                return FundamentalsSnapshot(
                    ticker=ticker, pe_ratio=28.4, revenue_growth_yoy=0.06,
                    as_of=datetime.now(UTC), source="fake")

        router = _Router([
            _action("get_fundamentals", {"ticker": "AAPL"}),
            _finish(
                "valuation looks reasonable given growth", reported_metrics={
                    "trailing_pe": 28.4,
                    "revenue_growth_yoy_pct": 6.0
                })
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            fundamentals_tool_module, "get_fundamental_data_providers",
            lambda: [_FakeFundamentalProvider()])

        output = await fundamental.FundamentalAgent(
            role=AgentRole.FUNDAMENTAL).run(_state())

        observation = output.raw_output["scratchpad"][0]["observation"]
        assert "P/E=28.40" in observation
        assert (
            "revenue growth YoY (annual, latest FY vs prior FY)=6.0%"
        ) in observation

    def test_reported_metrics_schema_and_revenue_growth_period(self):
        cases = {
            "unknown-key-is-never-flagged": (
                "Data Center revenue grew 107% YoY, per this run's own filing "
                "search.", {
                    "data_center_revenue_growth_pct": 107.0
                }, [
                    _entry(
                        "get_fundamentals",
                        "Fundamentals for 'AMD': net margin=20.0% operating "
                        "margin=17.3%"),
                    _entry(
                        "search_prior_analysis",
                        "- (AMD) [note] (2026-08-08): some unrelated figure "
                        "is "
                        "107%")
                ], None),
            "missing-metrics-are-flagged": (
                "GAAP net margin is 20.0% this quarter.", None, [
                    _entry(
                        "get_fundamentals",
                        "Fundamentals for 'AMD': net margin=20.0%")
                ], "reported_metrics"),
            "non-numeric-value-is-flagged": (
                "GAAP net margin looks healthy this quarter.", {
                    "net_margin_pct": "healthy"
                }, [
                    _entry(
                        "get_fundamentals",
                        "Fundamentals for 'AMD': net margin=20.0%")
                ], "plain number")
        }
        for case_id, (answer, metrics, pad, expected) in cases.items():
            issue = fundamental._reported_metrics_disagree_with_fundamentals(
                answer, metrics, pad)
            if expected is None:
                assert issue is None, case_id
            else:
                assert issue is not None, case_id
                assert expected in issue, case_id

        schema = fundamental._REPORTED_METRICS_SCHEMA
        numeric = {
            k: v
            for k, v in schema["properties"].items()
            if k != "revenue_growth_period"
        }
        assert set(numeric) == set(
            fundamental._FRESH_FUNDAMENTALS_FIELD_PATTERNS)
        assert schema["additionalProperties"] is False
        assert all(prop == {"type": "number"} for prop in numeric.values())
        assert schema["properties"]["revenue_growth_period"]["enum"] == [
            "annual", "quarterly"
        ]
        run_source = inspect.getsource(fundamental.FundamentalAgent.run)
        assert "finish_answer_validator=" in run_source
        assert "reported_metrics_schema=" in run_source
        tree = ast.parse(Path(fundamental.__file__).read_text())
        validators = [
            kw.value for node in ast.walk(tree) if isinstance(node, ast.Call)
            for kw in node.keywords if kw.arg == "finish_answer_validator"
        ]
        assert validators
        call = validators[0]
        assert isinstance(call, ast.Call)
        assert ast.unparse(call.func) == "InternalTokenValidator"
        assert [ast.unparse(a) for a in call.args[:2]
                ] == ["self.role.value", "fundamental_answer_issue"]

        assert (
            revenue_growth_period_issue(
                "revenue growth of -2.9%", {
                    "revenue_growth_yoy_pct": -2.9,
                    "revenue_growth_period": "annual"
                }, _fundamentals_pad(_ANNUAL_FUNDAMENTALS)) is None)
        assert (
            revenue_growth_period_issue(
                "", {
                    "revenue_growth_yoy_pct": 44.1,
                    "revenue_growth_period": "quarterly"
                }, _fundamentals_pad(_QUARTERLY_FUNDAMENTALS)) is None)
        assert (
            revenue_growth_period_issue(
                "", {"net_margin_pct": -0.5},
                _fundamentals_pad(_LOSS_FUNDAMENTALS)) is None)
        assert (
            revenue_growth_period_issue(
                "", {"revenue_growth_yoy_pct": 1.0},
                _fundamentals_pad("Fundamentals: P/E=3")) is None)
        assert (
            fundamental_answer_issue(
                "revenue growth of -2.9%", {"revenue_growth_yoy_pct": -2.9},
                _fundamentals_pad(_ANNUAL_FUNDAMENTALS)) is not None)
        assert "reported_metrics" in (
            fundamental_answer_issue(
                "annual revenue growth of -2.9%", None,
                _fundamentals_pad(_ANNUAL_FUNDAMENTALS)) or "")

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_stale_quote_is_corrected_and_checked_web_search_is_clean(
            self, monkeypatch):

        class _FakeFundamentalProvider:

            async def get_fundamentals(self, ticker):
                return FundamentalsSnapshot(
                    ticker=ticker, net_margin=0.200, operating_margin=0.173,
                    as_of=datetime.now(UTC), source="fake")

        class _StaleNoteRetriever:

            async def retrieve(self, query, *, ticker=None):
                return [
                    RetrievedChunk(
                        chunk_id="c1", text="net margin is 15.6%", score=0.9,
                        category=DocumentCategory.ANALYST_REPORT,
                        ticker=ticker, content_date="2026-08-08")
                ]

        corrected = (
            "GAAP net margin is 20.0% this quarter, per this run's own "
            "figures.")
        router = _Router([
            _action("get_fundamentals", {"ticker": "AMD"}),
            _action(
                "search_prior_analysis", {
                    "query": "margin",
                    "ticker": "AMD"
                }),
            _finish(
                "GAAP net margin is 15.6% this quarter.",
                reported_metrics={"net_margin_pct": 15.6}),
            _finish(corrected, reported_metrics={"net_margin_pct": 20.0})
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            fundamentals_tool_module, "get_fundamental_data_providers",
            lambda: [_FakeFundamentalProvider()])
        monkeypatch.setattr(
            research_archive_tool_module, "BaseRetriever",
            lambda **kw: _StaleNoteRetriever())

        output = await fundamental.FundamentalAgent(
            role=AgentRole.FUNDAMENTAL).run(_state("AMD"))

        assert output.summary == corrected
        assert "[VERIFICATION WARNING" not in output.summary
        assert len(router.calls) == 4
        rejection = output.raw_output["scratchpad"][-1]["observation"]
        assert "not accepted" in rejection
        assert "15.6" in rejection
        assert "20.0" in rejection

        router = _Router([
            _action("web_search", {"query": "AAPL forward P/E analyst"}),
            _action(
                "assess_source_credibility",
                {"url": "https://www.reuters.com/some-article"}),
            _finish(
                "forward P/E around 30x, source verified; analysts say "
                "the "
                "outlook looks stable")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await fundamental.FundamentalAgent(
            role=AgentRole.FUNDAMENTAL).run(_state())

        assert not output.summary.startswith("[VERIFICATION WARNING:")

    @pytest.mark.asyncio
    async def test_caveat_and_latest_quarter_are_carried_into_output(
            self, monkeypatch):
        monkeypatch.setattr(fundamental, "GetFundamentalsTool", _FakeFundTool)
        router = _Router([
            _action("get_fundamentals", {"ticker": "GOOGL"}),
            _finish(
                "GOOGL trades at P/E 22.00 with a 35.0% net margin.", {
                    "trailing_pe": 22.0,
                    "net_margin_pct": 35.0
                })
        ], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await fundamental.FundamentalAgent(
            role=AgentRole.FUNDAMENTAL).run(
                _state(
                    "GOOGL", kg_context="KG facts",
                    fresh_filing_context="fresh 10-Q"))

        assert output.raw_output["data_caveats"] == [
            fundamental._fundamentals_data_caveats(
                [_entry("get_fundamentals", _FUND_OBS_WITH_NOTE)])[0]
        ]
        assert (
            "\n\nData caveat (from get_fundamentals): note: operating margin"
        ) in output.summary
        assert output.raw_output["latest_quarter"]["text"].endswith("GOOGL")
        assert output.summary.endswith(
            "Latest quarter (SEC XBRL, 10-Q filed 2026-07-24): GOOGL")
        assert output.raw_output["verification_warnings"] == []
        assert output.raw_output["injected_context"] == (
            "fresh 10-Q\n\nKG facts")
        assert output.raw_output["finish_meta"] is not None
        assert output.is_well_supported is True


class TestTechnicalAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_indicators_flow_through_and_disagreement_is_flagged(
            self, monkeypatch, _schema_ready):
        answer = (
            "uptrend confirmed by RSI and MACD; price 130.00 is above "
            "SMA(20) 125.25")
        router = _Router([
            _action("get_technical_indicators", {"ticker": "AAPL"}),
            _finish(
                answer, reported_metrics={
                    "sma20": 125.25,
                    "sma50": 117.75,
                    "rsi14": 100.0,
                    "macd_line": 3.5,
                    "macd_signal": 3.5,
                    "macd_histogram": 0.0,
                    "bb_upper": 131.02,
                    "bb_mid": 125.25,
                    "bb_lower": 119.48,
                    "atr14": 2.0,
                    "price_vs_sma20": "above",
                    "price_vs_sma50": "above",
                    "macd_trend": "neutral",
                    "rsi_band": "overbought"
                })
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            technical_tool_module, "get_market_data_provider",
            lambda: _RisingMarketProvider(60, 0.5))

        output = await technical.TechnicalAgent(role=AgentRole.TECHNICAL
                                                ).run(_state())

        assert output.summary == answer
        assert "[VERIFICATION WARNING" not in output.summary
        assert output.is_well_supported is True
        observation = output.raw_output["scratchpad"][0]["observation"]
        assert "Latest close: 130.00" in observation
        assert "Price vs SMA(20): above" in observation
        assert "Price vs SMA(50): above" in observation
        assert output.raw_output["key_facts"] == {"reference_price": 130.0}
        assert "RSI(14)" in observation
        assert "MACD" in observation
        assert "Bollinger" in observation

        router = _Router([
            _action("get_technical_indicators", {"ticker": "AAPL"}),
            _finish("SMA(20) is 200.00.", reported_metrics={"sma20": 200.0}),
            _finish(
                "SMA(20) is still 200.00.", reported_metrics={"sma20": 200.0}),
            _finish(
                "SMA(20) is still 200.00.", reported_metrics={"sma20": 200.0})
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            technical_tool_module, "get_market_data_provider",
            lambda: _RisingMarketProvider(60, 0.5))

        output = await technical.TechnicalAgent(role=AgentRole.TECHNICAL
                                                ).run(_state())

        assert output.summary.startswith("[VERIFICATION WARNING:")
        assert len(output.raw_output["verification_warnings"]) == 1

    def test_trend_helpers_reported_metrics_and_schema(self):
        assert technical._actual_price_vs_sma20(200.0, 187.42) == "above"
        assert technical._actual_price_vs_sma20(170.0, 187.42) == "below"
        assert technical._actual_price_vs_sma20(187.42, 187.42) == "at"
        assert technical._actual_macd_trend(0.247) == "bullish"
        assert technical._actual_macd_trend(-0.247) == "bearish"
        assert technical._actual_macd_trend(0.001) == "neutral"
        assert technical._accepted_macd_trends(0.5, 3.2) == ("bullish", )
        assert technical._accepted_macd_trends(-0.5, 3.2) == ("bearish", )
        assert technical._accepted_macd_trends(0.0, 3.2) == ("neutral", )
        assert technical._accepted_macd_trends(0.034, None) == ("bullish", )

        cases = {
            "unknown-key-is-never-flagged": (
                "Stochastic %K is 82.", {
                    "stochastic_k": 82.0,
                    "macd_trend": "bullish"
                }, [_entry("get_technical_indicators",
                           _INDICATORS_OBS)], None),
            "missing-metrics-are-flagged": (
                "RSI shows overbought conditions.", None,
                [_entry("get_technical_indicators",
                        _INDICATORS_OBS)], ["reported_metrics"]),
            "nothing-to-check-without-indicators": (
                "Price looks strong.", None, [
                    _entry(
                        "get_recent_price_history", _INDICATORS_PRICE_HISTORY)
                ], None),
            "price-vs-sma20-mismatch-is-flagged": (
                "Price is trading below its 20-day moving average.", {
                    "price_vs_sma20": "below",
                    "macd_trend": "bullish"
                }, [
                    _entry("get_technical_indicators", _INDICATORS_OBS),
                    _entry(
                        "get_recent_price_history", _INDICATORS_PRICE_HISTORY)
                ], ["189.1", "above"]),
            "price-vs-sma20-unchecked-without-price-history": (
                "Price is trading above its 20-day moving average.", {
                    "price_vs_sma20": "above",
                    "macd_trend": "bullish"
                }, [_entry("get_technical_indicators",
                           _INDICATORS_OBS)], None),
            "near-zero-macd-accepts-neutral": (
                "x", {
                    "price_vs_sma20": "below",
                    "price_vs_sma50": "above",
                    "macd_trend": "neutral"
                }, [_entry("get_technical_indicators",
                           _NEAR_ZERO_MACD_OBS)], None),
            "near-zero-macd-accepts-its-sign": (
                "x", {
                    "price_vs_sma20": "below",
                    "price_vs_sma50": "above",
                    "macd_trend": "bullish"
                }, [_entry("get_technical_indicators",
                           _NEAR_ZERO_MACD_OBS)], None),
            "near-zero-macd-rejects-the-opposite-sign": (
                "x", {
                    "price_vs_sma20": "below",
                    "price_vs_sma50": "above",
                    "macd_trend": "bearish"
                }, [_entry("get_technical_indicators",
                           _NEAR_ZERO_MACD_OBS)], []),
            "wrong-rsi-band-is-flagged": (
                "RSI at 38.0.", {
                    "rsi14": 38.0,
                    "rsi_band": "oversold"
                }, [_entry("get_technical_indicators",
                           _RSI_NEUTRAL_OBS)], ["'neutral'"]),
            "right-rsi-band-is-accepted": (
                "RSI at 38.0 is just above oversold.", {
                    "rsi14": 38.0,
                    "rsi_band": "neutral"
                }, [_entry("get_technical_indicators",
                           _RSI_NEUTRAL_OBS)], None),
            "missing-rsi-band-is-flagged": (
                "RSI at 38.0.", {
                    "rsi14": 38.0
                }, [_entry("get_technical_indicators",
                           _RSI_NEUTRAL_OBS)], ["rsi_band"])
        }
        for case_id, (answer, metrics, pad, expected) in cases.items():
            issue = technical._technical_claims_disagree_with_indicators(
                answer, metrics, pad)
            if expected is None:
                assert issue is None, case_id
            else:
                assert issue is not None, case_id
                for text in expected:
                    assert text in issue, case_id

        schema = technical._TECHNICAL_REPORTED_SCHEMA
        props = schema["properties"]
        numeric_props = {
            k: v
            for k, v in props.items() if k not in (
                "price_vs_sma20", "price_vs_sma50", "macd_trend", "rsi_band")
        }
        assert set(numeric_props) == set(technical._TECHNICAL_FIELD_PATTERNS)
        assert all(
            prop == {"type": "number"} for prop in numeric_props.values())
        assert schema["additionalProperties"] is False
        assert props["price_vs_sma20"] == {
            "type": "string",
            "enum": ["above", "below", "at"]
        }
        assert props["price_vs_sma50"] == {
            "type": "string",
            "enum": ["above", "below", "at"]
        }
        assert props["macd_trend"] == {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral"]
        }
        assert props["rsi_band"] == {
            "type": "string",
            "enum": ["oversold", "neutral", "overbought"]
        }
        source = inspect.getsource(technical.TechnicalAgent.run)
        assert "finish_answer_validator=" in source
        assert "reported_metrics_schema=" in source


class TestRiskAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_evidence_is_required_and_volatility_satisfies_it(
            self, monkeypatch):
        router = _Router([_finish("low volatility, shallow drawdowns")] * 3)
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await risk.RiskAgent(role=AgentRole.RISK).run(_state())

        assert output.role == AgentRole.RISK
        assert len(router.calls) == 3
        assert output.summary.startswith("[VERIFICATION WARNING:")
        assert "low volatility, shallow drawdowns" in output.summary
        assert any(
            "without ever calling any real tool" in w
            for w in output.raw_output["verification_warnings"])

        answer = "volatility and drawdown both look contained"
        router = _Router([
            _action("get_volatility_and_drawdown", {"ticker": "AAPL"}),
            _finish(answer)
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            risk_tool_module, "get_market_data_provider",
            lambda: _RisingMarketProvider(30, 0.3))

        output = await risk.RiskAgent(role=AgentRole.RISK).run(_state())

        assert not output.summary.startswith("[VERIFICATION WARNING:")
        assert output.raw_output["final_answer"] == answer
        assert output.summary.startswith(
            answer + "\n\nNamed risk events (sourced): ")
        assert output.raw_output["verification_warnings"] == []

    @pytest.mark.asyncio
    async def test_unsourced_event_is_corrected_or_never_rendered(
            self, monkeypatch):
        monkeypatch.setattr(risk, "GetVolatilityAndDrawdownTool", _FakeVolTool)
        router = _Router([
            _action("get_volatility_and_drawdown", {"ticker": "MSFT"}),
            _finish(
                "Volatility 24.1%; European Commission probe is a risk.", {
                    "risk_events": [{
                        "entity": "European Commission",
                        "evidence_tool": "get_volatility_and_drawdown"
                    }]
                }),
            _finish(
                "Volatility 24.1% with an FTC antitrust review noted.", {
                    "risk_events": [{
                        "entity": "Federal Trade Commission",
                        "evidence_tool": "get_volatility_and_drawdown",
                        "status": "pending"
                    }]
                })
        ], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await risk.RiskAgent(role=AgentRole.RISK).run(
            _state("MSFT", kg_context="MSFT --[probe]--> European Commission"))

        assert len(router.calls) == 3
        assert output.summary.endswith(
            "Named risk events (sourced): Federal Trade Commission "
            "(status pending)")
        assert output.raw_output["risk_events"] == [{
            "entity": "Federal Trade Commission",
            "evidence_tool": "get_volatility_and_drawdown",
            "status": "pending",
            "source_id": ""
        }]
        assert output.raw_output["injected_context"] == (
            "MSFT --[probe]--> European Commission")
        assert output.raw_output["final_answer"].startswith(
            "Volatility 24.1% with an FTC")
        assert output.is_well_supported is True

        router = _Router([
            _action("get_volatility_and_drawdown", {"ticker": "MSFT"}),
            _finish(
                "Volatility 24.1%; European Commission probe.", {
                    "risk_events": [{
                        "entity": "European Commission",
                        "evidence_tool": "web_search"
                    }]
                })
        ], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await risk.RiskAgent(role=AgentRole.RISK).run(_state("MSFT"))

        assert output.is_well_supported is False
        assert "European Commission (via" not in output.summary
        assert output.raw_output["risk_events"] == []
        assert output.summary.startswith("[VERIFICATION WARNING:")

    def test_filing_sourced_events_are_kept_and_note_only_one_dropped(self):
        pad = _steps(("search_prior_risk_notes", _UNH_PRIOR_NOTES),
                     ("search_filings", _UNH_FILINGS))
        metrics = {
            "risk_events": [{
                "entity": "Department of Justice",
                "evidence_tool": "search_prior_risk_notes",
                "status": "not_applicable"
            }, {
                "entity": "DOJ",
                "evidence_tool": "search_filings",
                "status": "not_applicable"
            }, {
                "entity": "CMS",
                "evidence_tool": "search_filings",
                "status": "not_applicable"
            }]
        }
        kept = [e["entity"] for e in _sourced_risk_events(metrics, pad)]
        assert kept == ["DOJ", "CMS"]

    def test_status_mismatch_is_logged_and_cited_source_must_be_shown(
            self, capsys):
        assert (
            _ko_issue(_baltimore_event(status="dismissed", source_id="F1.L1"))
            is None)
        assert "risk.record_status_mismatch" not in capsys.readouterr().out
        assert (
            _ko_issue(_baltimore_event(status="pending", source_id="F1.L1"))
            is None)
        out = capsys.readouterr().out
        assert "risk.record_status_mismatch" in out
        assert "record_status=dismissed" in out

        issue = _ko_issue(
            _baltimore_event(status="dismissed", source_id="F7.L1"))
        assert issue is not None and "no filing record with that id" in issue
        unshown = _baltimore_event(status="dismissed", source_id="F1.L1")
        assert _ko_issue(unshown, shown=False) is not None
        shown_name = _baltimore_event(
            status="dismissed", source_id="KO_10-Q_2026-07-29.txt")
        assert _ko_issue(shown_name) is None
        issue = _ko_issue(
            _baltimore_event(status="pending", source_id="KO_10-Q_2025-04-29"))
        assert issue is not None and "not a filing shown" in issue
        assert (
            _ko_issue(
                _baltimore_event(
                    status="pending", source_id="KO_10-Q_2026-07-29")) is None)
        issue = _ko_issue(_baltimore_event(status="pending"))
        assert issue is not None and "no source_id" in issue

    def test_risk_schema_has_the_status_enum_and_source_id(self):
        item = risk._RISK_REPORTED_SCHEMA["properties"]["risk_events"]["items"]
        assert item["required"] == ["entity", "evidence_tool", "status"]
        assert item["properties"]["status"]["enum"] == [
            "pending", "dismissed", "settled", "judgment", "appeal", "other",
            "announced", "completed", "terminated", "not_applicable"
        ]
        assert item["properties"]["source_id"] == {"type": "string"}

    def test_filing_label_is_resolved_and_bad_labels_are_rejected(
            self, capsys):
        pad = _pad(_MSFT_FILINGS)
        assert _risk_answer_issue(
            "x", _irs("F1"), pad, rows=_MSFT_ROWS,
            shown=True) == (None, False)
        out = capsys.readouterr().out
        assert "risk.filing_label_resolved" in out
        assert "filename=MSFT_10-K_2026-07-29" in out
        events = _sourced_risk_events(
            _irs("f1"), pad, rows=_MSFT_ROWS, shown=True)
        assert _risk_events_line(events, _MSFT_ROWS, True) == (
            "Named risk events (sourced): IRS (status pending; "
            "source: 10-K filed 2026-07-29)")
        for source, shown in (("F3", True), ("F1", False), ("F1.L", True)):
            issue = _risk_answer_issue(
                "x", _irs(source), pad, rows=_MSFT_ROWS, shown=shown)[0]
            assert issue is not None and source in issue, source

    def test_empty_events_check_rejects_once_only_with_shown_records(self):
        validator = _validator(_TSLA_ROWS)
        pad = _pad("search_filings result")
        legal = sum(
            len(row.records.get("legal_matters") or []) for row in _TSLA_ROWS)
        assert legal >= 1
        with capture_logs() as logs:
            issue = validator("Litigation risk remains.", {}, pad)
        assert issue is not None
        assert issue.startswith("Risk events check:")
        assert f"list {legal} legal matter(s)" in issue
        assert "empty_risk_events" in validator.fired
        found = [
            e for e in logs
            if e["event"] == "risk.empty_events_with_legal_records"
        ]
        assert [e["legal_records"] for e in found] == [legal]
        assert validator("Litigation risk remains.", {}, pad) is None
        assert validator.rejections == 1

        cases = {
            "records-not-shown": (_TSLA_ROWS, False, {}),
            "no-legal-records": ([
                _Row(
                    "X_10-Q_2026-07-01.txt", "10-Q", date(2026, 7, 1), None,
                    {"legal_matters": []})
            ], True, {}),
            "events-listed": (
                _TSLA_ROWS, True,
                _events(("Benavides v. Tesla, Inc.", "pending", "F2.L1")))
        }
        for case_id, (rows, shown, metrics) in cases.items():
            validator = _validator(rows)
            validator.records.shown = shown
            validator("Risk remains.", metrics, _pad("search_filings result"))
            assert "empty_risk_events" not in validator.fired, case_id

    def test_record_of_another_matter_is_rejected_once_with_all_events(
            self, capsys):
        validator = _validator(_TSLA_TWO_ROWS)
        pad = _pad(_TSLA_TWO_FILINGS)
        issue = validator("x", _TSLA_DRAFT, pad)
        assert issue is not None and issue.startswith("Record check:")
        assert "'NHTSA' cites F1.L1" in issue
        assert "no record names it" in issue
        assert "'Benavides v. Tesla' cites F1.L1" in issue
        assert "the record that names it: F2.L1" in issue
        assert "record_name" in validator.fired
        out = capsys.readouterr().out
        assert "risk.record_name_mismatch" in out and "rejected=True" in out
        assert "risk.record_status_mismatch" not in out
        assert validator("x", _TSLA_DRAFT, pad) is None
        out = capsys.readouterr().out
        assert "risk.record_name_mismatch" in out and "rejected=False" in out
        text = RECORD_NAME_MESSAGE.format(parts="a; b")
        assert "never cite another matter's record" in text
        assert "a; b" in text

    def test_hint_for_a_record_in_the_newest_filing_has_no_age_note(self):
        rows = [_TSLA_ROWS[1], _TSLA_ROWS[2]]
        rows[0] = _Row(
            rows[0].filename, rows[0].form, rows[0].filing_date,
            rows[0].report_date, {
                "legal_matters": [
                    _TSLA_ROWS[0].records["legal_matters"][0],
                    *_TSLA_ROWS[1].records["legal_matters"]
                ]
            })
        issue = _validator(rows)(
            "x", _events(("Benavides v. Tesla", "appeal", "F1.L1")),
            _pad(frec.render_filing_records(rows)))
        assert issue is not None
        assert "the record that names it: F1.L2)" in issue
        assert "older than the newest filing" not in issue

    def test_record_quote_naming_the_entity_is_its_record(self):
        record = {
            "category": "legal_matters",
            "name": "Illinois Biometric Information Privacy Act litigation",
            "quote": "Beginning in June 2019 with Wilcosky v. Amazon.com, "
            "Inc., now pending in the United States District Court"
        }
        assert _record_names_entity("Wilcosky v. Amazon.com, Inc.", record)
        assert not _record_names_entity(
            "Frame-Wilson v. Amazon.com, Inc.", record)
        assert _record_names_entity(
            "NHTSA", {
                "category": "guidance",
                "metric": "capex"
            })

    def test_events_line_names_sources_in_plain_words(self):
        pad = _pad(_TSLA_TWO_FILINGS)
        events = _sourced_risk_events(
            _TSLA_DRAFT, pad, rows=_TSLA_TWO_ROWS, shown=True)
        line = _risk_events_line(events, _TSLA_TWO_ROWS, True)
        assert line == (
            "Named risk events (sourced): NHTSA (status pending; "
            "source: 10-Q filed 2026-07-23); Benavides v. Tesla ("
            "status appeal; source: 10-Q filed 2026-07-23)")
        assert "extracted record says" not in line and "F1" not in line

        rows = [
            _Row(
                "KO_10-Q_2026-07-29.txt", "10-Q", date(2026, 7, 29),
                date(2026, 7, 3), {
                    "legal_matters": [{
                        "name": "Baltimore plastic packaging lawsuit",
                        "status": "dismissed",
                        "quote": ""
                    }],
                    "transactions": [{
                        "name": "Africa bottling sale",
                        "type": "divestiture",
                        "status": "pending",
                        "quote": ""
                    }]
                })
        ]
        observation = frec.render_filing_records(rows) + (
            "\n- (KO) [KO_10-Q_2026-07-29] (filed 2026-07-29): the City of "
            "Baltimore plastic packaging lawsuit was dismissed; the Africa "
            "sale is pending; IRS appeal.")
        metrics = _events((
            "City of Baltimore plastic packaging lawsuit", "pending", "F1.L1"),
                          ("Africa bottling sale", "pending", "F1.T1"),
                          ("IRS", "appeal", "KO_10-Q_2026-07-29.txt"))
        events = _sourced_risk_events(
            metrics, _pad(observation), rows=rows, shown=True)
        assert [e["source_id"] for e in events
                ] == ["F1.L1", "F1.T1", "KO_10-Q_2026-07-29.txt"]
        assert _risk_events_line(events, rows, True) == (
            "Named risk events (sourced): City of Baltimore plastic "
            "packaging lawsuit (status pending; source: 10-Q filed "
            "2026-07-29, legal matter: Baltimore plastic packaging "
            "lawsuit); Africa bottling sale (status pending; source: "
            "10-Q filed 2026-07-29); IRS (status appeal; source: 10-Q filed "
            "2026-07-29)")

        event = {
            "entity": "FTC",
            "evidence_tool": "web_search",
            "status": "not_applicable",
            "source_id": ""
        }
        assert _event_line(event, [], False) == "FTC"
        pending = {**event, "status": "pending"}
        assert _event_line(pending, [], False) == "FTC (status pending)"


class TestMacroAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_get_macro_indicators_tool_call_flows_through(
            self, monkeypatch):

        class _FedFundsProvider:

            async def get_indicator(self, series, *, lookback_days=365):
                return [
                    MacroDataPoint(
                        series_id="FEDFUNDS",
                        label="Effective Federal Funds Rate", value=5.33,
                        observed_at=date(2026, 1, 1), source="fake")
                ]

        router = _Router([
            _action("get_macro_indicators", {"series": "fed_funds_rate"}),
            _finish("rates remain restrictive")
        ])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)
        monkeypatch.setattr(
            macro_tool_module, "get_macro_data_provider",
            lambda: _FedFundsProvider())

        output = await macro.MacroAgent(role=AgentRole.MACRO).run(_state())

        observation = output.raw_output["scratchpad"][0]["observation"]
        assert "5.33" in observation

    @pytest.mark.asyncio
    async def test_unresolved_rate_mismatch_gets_a_warning(self, monkeypatch):
        provider = _FakeMacroProvider()
        monkeypatch.setattr(
            macro_tool_module, "get_macro_data_provider", lambda: provider)
        wrong = _finish(
            "Policy rate at 4.60% and the 10-year at 4.12% for MSFT.", {
                "fed_funds_rate_pct": 4.60,
                "ten_year_yield_pct": 4.12
            })
        router = _Router([wrong, wrong, wrong], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await macro.MacroAgent(role=AgentRole.MACRO
                                        ).run(_state("MSFT"))

        assert output.summary.startswith("[VERIFICATION WARNING: ")
        assert "4.33" in output.raw_output["verification_warnings"][0]
        assert output.is_well_supported is False

    def test_reported_rates_must_match_live_fred(self):
        scratchpad = [
            _entry(
                "get_macro_indicators",
                "Effective Federal Funds Rate (daily) (DFF): "
                "latest=4.33% as of 2026-08-01. 1 observations in "
                "range."),
            _entry(
                "get_macro_indicators",
                "10-Year Treasury (DGS10): latest=4.12% as of "
                "2026-09-22. 1 observations in range.")
        ]
        check = macro._macro_metrics_disagree_with_live_fred
        ok = {"fed_funds_rate_pct": 4.33, "ten_year_yield_pct": 4.124}
        assert check("x", ok, scratchpad) is None
        stale = {"fed_funds_rate_pct": 4.60, "ten_year_yield_pct": 4.12}
        issue = check("x", stale, scratchpad)
        assert issue is not None and "4.33" in issue
        missing = check("x", {"fed_funds_rate_pct": 4.33}, scratchpad)
        assert missing is not None and "ten_year_yield_pct" in missing

    @pytest.mark.asyncio
    async def test_prefetched_rates_are_shown_and_a_failed_one_is_null(
            self, monkeypatch):
        provider = _FakeMacroProvider()
        monkeypatch.setattr(
            macro_tool_module, "get_macro_data_provider", lambda: provider)
        router = _Router([
            _finish(
                "Policy rate at 4.33% and the 10-year at 4.12% keep "
                "financing "
                "costs high for MSFT.", {
                    "fed_funds_rate_pct": 4.33,
                    "ten_year_yield_pct": 4.12
                })
        ], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await macro.MacroAgent(role=AgentRole.MACRO
                                        ).run(_state("MSFT"))

        assert provider.calls == ["fed_funds_rate", "10y_treasury_yield"]
        assert len(router.calls) == 1
        assert output.summary.endswith(
            "Live FRED this run: DFF 4.33% (2026-08-01); DGS10 4.12% "
            "(2026-09-22)")
        assert output.raw_output["verification_warnings"] == []
        assert output.raw_output["live_fred"]["fed_funds_rate_pct"] == {
            "value": 4.33,
            "as_of": "2026-08-01"
        }
        assert output.is_well_supported is True
        scratchpad = output.raw_output["scratchpad"]
        assert scratchpad[0]["action"] == "get_macro_indicators"
        assert scratchpad[0]["is_tool_result"] is True
        system_prompt = router.calls[0][0].content
        assert "not both" not in system_prompt
        assert "at most twice" not in system_prompt

        provider = _FakeMacroProvider(failing={"10y_treasury_yield"})
        monkeypatch.setattr(
            macro_tool_module, "get_macro_data_provider", lambda: provider)
        router = _Router([
            _finish(
                "Policy rate at 4.33%; the 10-year is near 4.90%.", {
                    "fed_funds_rate_pct": 4.33,
                    "ten_year_yield_pct": 4.90
                }),
            _finish(
                "Policy rate at 4.33%; no live 10-year reading this run.", {
                    "fed_funds_rate_pct": 4.33,
                    "ten_year_yield_pct": None
                })
        ], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await macro.MacroAgent(role=AgentRole.MACRO
                                        ).run(_state("MSFT"))

        assert len(router.calls) == 2
        assert "DGS10: lookup FAILED this run" in output.summary
        assert output.raw_output["live_fred"]["ten_year_yield_pct"] is None
        assert any(
            "DGS10" in w for w in output.raw_output["verification_warnings"])

    @pytest.mark.asyncio
    async def test_reuse_note_goes_to_disclosures_not_summary(
            self, monkeypatch):
        answer = (
            "Rates stay restrictive; staples like KO face higher discount "
            "rates.")
        result = AgentLoopResult(
            final_answer=answer,
            scratchpad=[_entry("get_macro_indicators",
                               "DFF latest=3.88%")], finish_meta={
                                   "model": "m",
                                   "finish_reason": "stop",
                                   "rate_limited": False
                               })
        monkeypatch.setattr(
            macro, "ReActAgentLoop", _fixed_result_loop(result))
        monkeypatch.setattr(macro, "_prefetch_live_fred", _no_prefetch)
        monkeypatch.setattr(macro, "prefetch_tool_calls", _no_prefetch)
        monkeypatch.setattr(macro, "recycled_share", lambda *a: 0.9)

        output = await macro.MacroAgent(role=AgentRole.MACRO).run(_state("KO"))

        assert "[NOTE" not in output.summary
        assert "reused" not in output.summary
        assert output.summary.startswith(answer)
        raw = output.raw_output
        assert raw["reused_prior_note"] is True
        assert raw["disclosures"] == [macro.MACRO_REUSED_DISCLOSURE]
        assert not any("reused" in w for w in raw["verification_warnings"])
        assert output.score < 0.84

        evidence = dc.format_evidence(
            _state("KO", agent_outputs={AgentRole.MACRO: output}))
        assert "System note, not part of the agent's text" in evidence
        assert macro.MACRO_REUSED_DISCLOSURE in evidence


class TestPortfolioAgent:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_web_search")
    async def test_finishes_and_maps_to_agent_output(self, monkeypatch):
        router = _Router(
            [_finish("adding to AAPL would raise concentration modestly")])
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await portfolio.PortfolioAgent(
            role=AgentRole.PORTFOLIO, portfolio_id="p1").run(_state())

        assert output.role == AgentRole.PORTFOLIO
        assert output.status == AgentStatus.COMPLETED
        assert "concentration" in output.summary

    def test_finish_needs_a_scenario_and_verbatim_percentages(self):
        base = [_entry("get_portfolio_composition", _COMPOSITION_OBS)]
        other_ticker = [
            *base,
            _entry(
                "recompute_scenario", _SCENARIO_OBS.replace("MSFT", "AMD"), {
                    "ticker": "AMD",
                    "additional_shares": 10
                })
        ]
        refused = [
            *base,
            _entry(
                "recompute_scenario",
                "Scenario recompute failed: live price unavailable for AMD; "
                "total net value cannot be computed.", {
                    "ticker": "MSFT",
                    "additional_shares": 10
                }, tool_error=True)
        ]
        for scratchpad in (base, other_ticker, refused):
            issue = _portfolio_finish_validator("MSFT", None)(
                "fine", None, scratchpad)
            assert issue is not None and "recompute_scenario" in issue

        scratchpad = [
            _entry("get_portfolio_composition", _COMPOSITION_OBS),
            _entry(
                "recompute_scenario", _SCENARIO_OBS, {
                    "ticker": "msft",
                    "additional_shares": 10
                })
        ]
        validate = _portfolio_finish_validator("MSFT", None)
        assert (
            validate("MSFT would go from 49.0% to 73.5%.", None, scratchpad)
            is None)
        issue = validate("20 more shares would reach 98.0%.", None, scratchpad)
        assert issue is not None and "98.0%" in issue
        with_kg = _portfolio_finish_validator(
            "MSFT", "AMD --[has_volatility]--> 79.5%")
        assert with_kg("AMD vol 79.5%; 49.0%", None, scratchpad) is None

    @pytest.mark.asyncio
    async def test_persistent_mismatch_kept_and_scenario_prefetch_fails_open(
            self, monkeypatch):
        monkeypatch.setattr(
            portfolio, "GetPortfolioCompositionTool", _FakeCompositionTool)
        monkeypatch.setattr(
            portfolio, "RecomputeScenarioTool", _FakeScenarioTool)
        bad = "20 shares would reach 98.0%, from 49.0%."
        router = _Router([
            _action("get_portfolio_composition", {}),
            _action(
                "recompute_scenario", {
                    "ticker": "MSFT",
                    "additional_shares": 10
                }),
            _finish(bad)
        ], finish_reason="STOP")
        monkeypatch.setattr(loop_module, "LLMRouter", lambda: router)

        output = await portfolio.PortfolioAgent(AgentRole.PORTFOLIO,
                                                "p1").run(_state("MSFT"))

        assert output.is_well_supported is False and output.score == 0.35
        assert output.summary.startswith("[VERIFICATION WARNING:")
        assert output.raw_output["final_answer"] == bad
        assert "[VERIFICATION WARNING" not in output.raw_output["final_answer"]
        assert output.raw_output["verification_warnings"]

        class _Tool:

            async def suggested_share_counts(
                    self, ticker: str) -> tuple[int, int] | None:
                raise TypeError(
                    "float() argument must be a string or a real number")

        with capture_logs() as logs:
            assert await portfolio._prefetch_scenarios(_Tool(), "AAPL") == []
        assert logs[0]["event"] == "portfolio.scenario_prefetch_skipped"
        assert "TypeError" in logs[0]["error"]


class TestDebateSides:

    @pytest.mark.asyncio
    async def test_bull_cites_agent_evidence_in_prompt(self, monkeypatch):
        router = _Router(["Strong buy case."])
        monkeypatch.setattr(dc, "LLMRouter", lambda: router)

        state = _state(
            agent_outputs={
                AgentRole.MARKET: AgentOutput(
                    role=AgentRole.MARKET, summary="uptrend confirmed",
                    score=0.8)
            })
        argument = await DebateSideAgent("bull").make_argument(state)

        assert argument.side == "bull"
        assert argument.round_number == 1
        assert argument.argument == "Strong buy case."
        assert "uptrend confirmed" in router.calls[0][-1].content

    @pytest.mark.asyncio
    async def test_flagged_draft_gets_one_regeneration_then_is_logged(self):
        router = _TextRouter("Close 151.23 [market].")
        argument = await dc.generate_debate_argument(
            side="bull", state=_market_state(), llm_router=router,
            llm_tier=None, system_prompt="s")
        assert argument.argument == "Close 151.23 [market]."
        assert len(router.calls) == 1

        router = _TextRouter("Up 25% [market].", "Up 26% [market].")
        with capture_logs() as logs:
            argument = await dc.generate_debate_argument(
                side="bear", state=_market_state(), llm_router=router,
                llm_tier=None, system_prompt="s")
        assert argument.argument == "Up 26% [market]."
        assert len(router.calls) == 2
        assert "no matching number anywhere in the evidence: 25%" in (
            router.calls[1][-1].content)
        final = [
            e for e in logs if e["event"] == "bear.final_draft_still_flagged"
        ]
        assert final and final[0]["ungrounded_figures"] == ["26%"]

    def test_evidence_text_for_failed_quarters_and_one_off_items(self):
        all_failed = _failed_outputs(
            [AgentRole.MARKET, AgentRole.NEWS, AgentRole.FILING])
        result = dc.format_evidence(_state(agent_outputs=all_failed))
        assert "no real evidence available" in result
        assert "402 error" not in result
        assert "- market:" not in result

        cases = {
            "absent": (None),
            "empty-text": ({
                "text": ""
            }),
            "no-text": ({
                "form": "10-Q"
            }),
            "not-a-dict": ("x")
        }
        for case_id, quarter in cases.items():
            out = _quarter_fundamental()
            if quarter is not None:
                out.raw_output["latest_quarter"] = quarter
            state = _state("QCOM", agent_outputs={AgentRole.FUNDAMENTAL: out})
            assert dc.latest_quarter(state) is None, case_id
            assert "SEC XBRL" not in dc.format_evidence(state), case_id

        filing_output = AgentOutput(
            role=AgentRole.FILING, summary="f", score=0.8, raw_output={
                "one_off_items": [
                    "$5.7B tax benefit (Q3 FY2026)", {
                        "description": "Unrealized gain on equity securities",
                        "amount_text": "$53.4B"
                    }, {
                        "no": "description"
                    }
                ]
            })
        state = _state("QCOM", agent_outputs={AgentRole.FILING: filing_output})
        assert dc.one_off_items(state) == ["$5.7B tax benefit (Q3 FY2026)"]
        assert "filing (unusual items from the filing records" in (
            dc.format_evidence(state))


class TestJudgeAgent:

    @pytest.mark.asyncio
    async def test_leaning_fallback_no_evidence_skip_and_prior_clause(
            self, monkeypatch):
        router = _Router([_judge_json(True, leaning="strong_buy")])
        monkeypatch.setattr(judge, "LLMRouter", lambda: router)
        verdict = await JudgeAgent().assess_round(_state())
        assert verdict.leaning == RecommendationAction.HOLD

        router = _Router([_judge_json(True, "buy", 0.75)])
        monkeypatch.setattr(judge, "LLMRouter", lambda: router)

        verdict = await JudgeAgent().assess_round(
            _state(agent_outputs=_failed_outputs(_ALL_DATA_ROLES)))

        assert verdict.leaning == RecommendationAction.HOLD
        assert verdict.confidence == 0.0
        assert verdict.judge_failed is True
        assert verdict.failure_reason == "no_evidence"
        assert verdict.converged is False
        assert len(router.calls) == 0

        cases = {
            "no-pending-recommendation": (None, False),
            "pending-recommendation": (
                "A prior BUY recommendation for AAPL in this portfolio is "
                "still "
                "open.", True)
        }
        for case_id, (context, superseded) in cases.items():
            router = _Router([_judge_json(True, "buy", 0.75)])
            monkeypatch.setattr(judge, "LLMRouter", lambda r=router: r)
            await JudgeAgent().assess_round(
                _state(pending_recommendation_context=context))
            system_message = router.calls[0][0]
            assert system_message.role == "system", case_id
            content = system_message.content.lower()
            assert ("supersed" in content) is superseded, case_id
            if not superseded:
                assert "prior" not in content, case_id

    def test_trailing_pe_line_needs_net_above_operating_and_a_gap(self):
        amd = judge.trailing_pe_line(
            _margins_state({
                "operating_margin_pct": 10.7,
                "net_margin_pct": 12.5
            }))
        assert "net margin 12.5% is above operating margin 10.7%" in amd
        assert "trailing P/E is LOWER than core earnings justify" in amd
        assert "32.8% is above operating margin 32.0%" in (
            judge.trailing_pe_line(
                _margins_state({
                    "operating_margin_pct": 32.0,
                    "net_margin_pct": 32.8
                })))
        for margins in ({"operating_margin_pct": 11.2, "net_margin_pct": 10.8},
                        {"operating_margin_pct": -4.2, "net_margin_pct": -0.5
                         }, {"gross_margin_pct": 50.0}, None):
            assert judge.trailing_pe_line(_margins_state(margins)) == ""
        assert (
            judge.trailing_pe_line(
                _pe_state("P/E=160.48 forward P/E=40.69 P/B=15.39")) == "")
        assert "trailing P/E is LOWER" in judge.trailing_pe_line(
            _pe_state("P/E=17.24 forward P/E=22.79 P/B=7.1"))

    def test_quarter_changes_and_lagging_xbrl_are_stated(self):
        state = _state(
            "QCOM", agent_outputs={
                AgentRole.FUNDAMENTAL: _quarter_fundamental(_INTC_QUARTER)
            })
        assert judge.latest_quarter_changes_to_state(state) == [
            "net income: loss widened 278% year over year"
        ]
        swing = dict(
            _TSLA_QUARTER, net_income=-1e8, net_income_prior=1e9,
            net_income_yoy_pct=None)
        state = _state(
            "QCOM",
            agent_outputs={AgentRole.FUNDAMENTAL: _quarter_fundamental(swing)})
        assert "net income swung to a loss" in (
            judge.latest_quarter_changes_to_state(state))

        lagging = {**_QCOM_QUARTER, "newer_filing": "10-Q filed 2026-07-29"}
        state = _state(
            "QCOM", agent_outputs={
                AgentRole.FUNDAMENTAL: _quarter_fundamental(lagging)
            })
        line = judge.profit_trend_line(state)
        assert line.startswith(
            "\n\nLatest quarter in SEC XBRL (quarter ended 2026-06-28)")
        assert "the newer 10-Q filed 2026-07-29 is not in XBRL yet" in line
        assert "Latest quarter (quarter ended" not in line
        plain = judge.profit_trend_line(_qcom())
        assert "Latest quarter (quarter ended" in plain

    @pytest.mark.asyncio
    async def test_flip_without_change_is_logged_not_retried(
            self, monkeypatch):
        payload = {
            "converged": True,
            "leaning": "hold",
            "confidence": 0.6,
            "rationale": "The tariff debate in Congress weighs on margins. "
            "Operating income fell 41% to $1.63B in the quarter "
            "ended 2026-06-28.",
            "deciding_factor": "profit decline"
        }
        router = _Router([json.dumps(payload)], finish_reason="STOP")
        monkeypatch.setattr(judge, "LLMRouter", lambda: router)
        hit = EpisodicMemoryHit(
            analysis_id="x", ticker="QCOM",
            recommendation=RecommendationAction.BUY, confidence=0.7,
            analyzed_at=datetime(2026, 10, 2, 21, 0, tzinfo=UTC))
        debate = DebateState(
            round_number=1, arguments=[
                DebateArgument(round_number=1, side="bull", argument="bull"),
                DebateArgument(round_number=1, side="bear", argument="bear")
            ])
        with capture_logs() as logs:
            verdict = await judge.JudgeAgent().assess_round(
                _qcom(debate=debate, episodic_context=[hit]))
        assert len(router.calls) == 1
        events = [e["event"] for e in logs]
        assert "judge.change_since_previous_missing" in events
        assert verdict.rationale.startswith("The tariff debate in Congress")
        prompt = router.calls[0][0].content
        assert '"change_since_previous"' in prompt
        assert '"cited_figures"' not in prompt
        assert "trailing-twelve-month net income" not in prompt
        user = router.calls[0][1].content
        assert "operating income -41% year over year" in user


class TestReflectionAgent:

    @pytest.mark.asyncio
    async def test_llm_cannot_flag_bull_if_bull_never_argued(
            self, monkeypatch):
        llm_response = json.dumps({
            "issues": [{
                "role": "bull",
                "kind": "not_engaged",
                "evidence_quote": "fine",
                "description": "seems off"
            }]
        })
        router = _Router([llm_response])
        monkeypatch.setattr(reflection, "LLMRouter", lambda: router)

        state = _state(
            agent_outputs={
                AgentRole.NEWS: AgentOutput(
                    role=AgentRole.NEWS, summary="fine", score=0.8)
            })
        verdict = await ReflectionAgent().check(state)
        assert verdict.retry_agents == {}

    @pytest.mark.asyncio
    async def test_no_agent_outputs_skips_llm_call_entirely(self, monkeypatch):
        router = _Router([])
        monkeypatch.setattr(reflection, "LLMRouter", lambda: router)
        verdict = await ReflectionAgent().check(_state())
        assert verdict.verified is False
        assert verdict.llm_review_ran is False
        assert router.calls == []

    def test_without_supersession_every_round_is_shown(self):
        debate = DebateState(
            round_number=2, superseded_through_round=0, arguments=[
                DebateArgument(
                    round_number=1, side="bull",
                    argument="OLD BULL built on stale evidence"),
                DebateArgument(
                    round_number=1, side="bear",
                    argument="OLD BEAR built on stale evidence"),
                DebateArgument(
                    round_number=2, side="bull", argument="NEW BULL"),
                DebateArgument(
                    round_number=2, side="bear", argument="NEW BEAR")
            ])
        user = (
            ReflectionAgent()._build_messages(_state(
                "MSFT", debate=debate))[-1].content)
        assert "Round 1 bull: OLD BULL" in user
        assert "Round 2 bull (LATEST): NEW BULL" in user

    @pytest.mark.asyncio
    async def test_issue_is_routed_by_role_or_kept_unrouted(self, monkeypatch):
        verdict = await _review(
            monkeypatch, {
                "role": "bull",
                "kind": "wrong_period",
                "figure": "27.9%",
                "claimed_period": "Q3",
                "evidence_quote": "operating income $1.63B vs $2.76B (-41%)",
                "description": "27.9% is not a Q3 margin"
            })
        assert list(verdict.retry_agents) == [AgentRole.BULL]
        issue = verdict.issue_details[-1]
        assert (issue.code, issue.role,
                issue.kind) == (ISSUE_LLM_REVIEW, "bull", "wrong_period")
        assert verdict.issues[-1] == (
            "Bull (wrong period): 27.9% is not a Q3 margin [figure 27.9%; "
            'given for Q3; evidence: "operating income $1.63B vs $2.76B '
            '(-41%)"]')

        verdict = await _review(
            monkeypatch, {
                "role": "analyst",
                "kind": "odd",
                "evidence_quote": "close 151.23",
                "description": "general observation"
            })
        assert verdict.retry_agents == {}
        assert verdict.issue_details[-1].role is None
        assert verdict.issue_details[-1].kind is None
        causes = _classify_reflection_causes(verdict.issue_details, verdict)
        assert "other problems the LLM citation review raised" in causes

    def test_causes_name_unsupported_claims_and_capacity(self):
        causes = " | ".join(
            _classify_reflection_causes([
                ReflectionIssue(
                    code=ISSUE_LLM_REVIEW, role="bull", kind="unsupported",
                    text="x"),
                ReflectionIssue(
                    code=ISSUE_NOT_RETRIED, role="news",
                    failure_kind="rate_limit", text="y"),
                ReflectionIssue(
                    code=ISSUE_RETRY_NO_PROGRESS, role="news", text="z")
            ]))
        assert "no agent's actual evidence supports" in causes
        assert "(429)" in causes and "same feedback" in causes
