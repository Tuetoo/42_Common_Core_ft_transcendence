import inspect
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4
import ai.agents.loop as loop_module
import ai.agents.news as news_module
import ai.agents.reflection as reflection_module
import ai.agents.sentiment as sentiment_module
import ai.graph.nodes as nodes_module
import ai.ingestion.macro_ingestion as macro_ingestion
import ai.ingestion.news_ingestion as news_ingestion
import ai.llm.capacity as capacity_module
import ai.llm.google_provider as google_provider
import ai.providers.news_filters as filters_module
import ai.providers.news_relevance as relevance_module
import ai.providers.newsapi as newsapi_module
import ai.rag.indexing as indexing_module
import ai.recommendation.position_sizing as sizing_module
import ai.tools.news_tool as news_tool_module
import ai.tools.portfolio_tool as portfolio_tool_module
import ai.tools.search_relevance as search_relevance_module
import ai.tools.web_search_tool as web_tool_module
import httpx
import pytest
from ai.agents import debate_side
from ai.agents import macro as macro_module
from ai.agents.finish_checks import INTERNAL_TOKEN_MESSAGE
from ai.agents.loop import ScratchpadEntry
from ai.agents.news import (FALLBACK_UNVERIFIED_REASON,
                            PRIOR_NOTE_CLAIMS_MESSAGE, NewsAgent,
                            _NewsFinishValidator, prefetch_web_fallback,
                            prior_news_note_similarity,
                            weekday_date_mismatches)
from ai.agents.reflection import ReflectionAgent
from ai.graph.state import AgentOutput, AnalysisState
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, GatedReservation, LLMError,
                         LLMMessage, LLMRateLimitError, LLMResponse,
                         LLMServerOverloadedError, LLMTier, LLMUsage)
from ai.providers import tavily
from ai.providers.base import (MacroDataPoint, NewsArticle, PriceQuote,
                               ProviderError, ProviderRequestError,
                               WebSearchResult)
from ai.providers.fred import _PERCENT_SERIES_IDS, _SERIES_ALIASES
from ai.providers.news_filters import (company_aliases, consent_target_url,
                                       precheck_reason)
from ai.providers.news_relevance import (COMPANY_BUSINESS, ClassifiedArticle,
                                         align_verdicts, read_verdicts)
from ai.providers.newsapi import NewsApiArticle, NewsApiProvider, SourceCatalog
from ai.providers.tavily import TavilyProvider
from ai.rag.chunking import Chunk
from ai.rag.retriever import RetrievedChunk
from ai.rag.visibility import ChunkVisibility
from ai.recommendation.position_sizing import generate_position_sizing
from ai.tools.base import Tool
from ai.tools.macro_tool import GetMacroIndicatorsTool, last_step_move
from ai.tools.news_tool import (SearchNewsTool, build_ticker_news_query,
                                format_article_line, observation_yield)
from ai.tools.research_archive_tool import (AGENT_NOTE_GUIDANCE,
                                            SearchResearchArchiveTool)
from ai.tools.web_search_tool import WebSearchTool
from ai.workflows.analysis_workflow import _archival_recycled_share
from config import settings
from helpers import _finish, _ScriptedReviewRouter
from qdrant_client.http.exceptions import ResponseHandlingException
from structlog.testing import capture_logs
from shared.enums import (AgentRole, AnalysisType, DocumentCategory,
                          RecommendationAction)

_LEGAL = {
    "AMZN": "AMAZON COM INC",
    "TSLA": "Tesla, Inc.",
    "QCOM": "QUALCOMM INC/DE",
    "JPM": "JPMORGAN CHASE & CO",
    "KO": "COCA COLA CO",
    "UNH": "UNITEDHEALTH GROUP INC",
    "AMD": "ADVANCED MICRO DEVICES INC",
    "DIS": "Walt Disney Co",
    "BA": "BOEING CO",
    "GOOGL": "Alphabet Inc.",
    "INTC": "INTEL CORP",
    "NVDA": "NVIDIA CORP",
    "XOM": "EXXON MOBIL CORP",
    "MSFT": "MICROSOFT CORP"
}

_ITEM_RE = re.compile(r"^(\d+)\. (?:Title: )?(.*?)(?: \([^()]*\): .*)?$", re.M)


def _response(content: str, finish: str | None = "STOP") -> LLMResponse:
    return LLMResponse(
        content=content, provider="fake", model="fake-model",
        usage=LLMUsage(prompt_tokens=1,
                       completion_tokens=1), finish_reason=finish)


def _article(
        title: str, url: str = "https://example.com/a", summary: str = "",
        day: int = 1) -> NewsArticle:
    return NewsArticle(
        title=title, source_name="x", url=url,
        published_at=datetime(2026, 10, day,
                              tzinfo=UTC), summary=summary or None)


def _src_article(title: str, url: str) -> NewsArticle:
    return NewsArticle(
        title=title, source_name="Src", url=url,
        published_at=datetime(2026, 10, 2, tzinfo=UTC), summary=None)


def _newsapi_article(
        title: str, url: str = "https://example.com/a",
        source_id: str | None = None) -> NewsArticle:
    return NewsApiArticle(
        title=title, source_name="Src", source_id=source_id, url=url,
        published_at=datetime(2026, 10, 1, tzinfo=UTC), summary=None)


def _reuters(title: str, url: str = "u", day: int = 24) -> NewsArticle:
    return NewsArticle(
        title=title, source_name="Reuters", url=url,
        published_at=datetime(2026, 9, day, tzinfo=UTC), summary="s")


def _web_result(
        title: str, url: str = "https://example.com/r",
        snippet: str = "s") -> WebSearchResult:
    return WebSearchResult(
        title=title, url=url, snippet=snippet,
        published_at=datetime(2026, 10, 1, tzinfo=UTC))


def _echo(title: str) -> str:
    return " ".join(title.split()[:5])


class _TitleRouter:

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []
        self.prompts: list[str] = []

    async def complete(
            self, tier: Any, messages: Any, **kwargs: Any) -> LLMResponse:
        content = messages[-1].content
        titles = [m.group(2) for m in _ITEM_RE.finditer(content)]
        self.calls.append(titles)
        self.prompts.append(messages[0].content)
        return _response(json.dumps(self.answer(titles)))


def _aligned(decide: Any) -> Any:

    def answer(titles: list[str]) -> list[dict[str, Any]]:
        out = []
        for i, t in enumerate(titles):
            subject, kind = decide(t)
            out.append({
                "index": i,
                "title": _echo(t),
                "reasoning": "r",
                "subject": subject,
                "kind": kind,
                "relevant": subject == "this_company"
            })
        return out

    return answer


def _verdict_fields(decide: Any) -> Any:

    def answer(titles: list[str]) -> list[dict[str, Any]]:
        return [{
            "index": i,
            "title": _echo(t),
            "reasoning": "r",
            **decide(t)
        } for i, t in enumerate(titles)]

    return answer


def _business(title: str) -> tuple[str, str]:
    return "this_company", "company_business"


def _business_if(relevant: Any) -> Any:

    def decide(title: str) -> tuple[str, str]:
        subject = "this_company" if relevant(title) else "other_company"
        return subject, "company_business"

    return decide


class _ScriptedRouter:

    def __init__(self, answers: list[str | Exception]) -> None:
        self.answers = answers
        self.max_tokens: list[int | None] = []
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.max_tokens)

    async def complete(
            self, tier: Any, messages: Any, **kwargs: Any) -> LLMResponse:
        self.max_tokens.append(kwargs.get("max_tokens"))
        self.prompts.append(messages[0].content)
        answer = self.answers[self.calls - 1]
        if isinstance(answer, Exception):
            raise answer
        return _response(answer)


def _patch_catalog(
        monkeypatch: pytest.MonkeyPatch,
        hosts: set[str] | None = None) -> None:
    catalog = (
        SourceCatalog(hosts=frozenset(hosts)) if hosts is not None else None)

    async def _catalog() -> SourceCatalog | None:
        return catalog

    monkeypatch.setattr(news_tool_module, "source_catalog", _catalog)
    monkeypatch.setattr(web_tool_module, "source_catalog", _catalog)


async def _run_steps(*steps: Any, setup: Any = None) -> None:
    for step in steps:
        with pytest.MonkeyPatch.context() as mp:
            if setup is not None:
                setup(mp)
            await step(mp)


@pytest.fixture
def known_companies(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return _install_known_companies(monkeypatch)


def _install_known_companies(
        monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:

    async def _lookup(ticker: str) -> str:
        return _LEGAL[ticker]

    for module in (relevance_module, news_tool_module, search_relevance_module,
                   news_module, news_ingestion):
        monkeypatch.setattr(module, "lookup_company_name", _lookup)
    _patch_catalog(monkeypatch)
    return monkeypatch


async def _keep_all_unclassified(articles, **kwargs):
    return [ClassifiedArticle(a, "unclassified") for a in articles]


def _company_news_env(monkeypatch, name: str) -> None:

    async def _name(ticker):
        return name

    monkeypatch.setattr(news_tool_module, "lookup_company_name", _name)
    monkeypatch.setattr(
        relevance_module, "classify_articles", _keep_all_unclassified)


class _StaticNewsProvider:

    def __init__(self, articles: list[NewsArticle]) -> None:
        self.articles = articles
        self.queries: list[str] = []

    async def search_news(self, query: str, **kwargs: Any):
        self.queries.append(query)
        return self.articles


def test_ticker_news_query():
    cases = {
        "topic-words-are-kept": (
            "Tesla earnings guidance", "TSLA", "TESLA",
            '(TSLA OR "Tesla") AND (earnings guidance)'),
        "single-letter-ticker-uses-the-name-only": (
            "latest news", "F", "FORD MOTOR", '("Ford Motor")')
    }
    for case_id, (topic, ticker, name, expected) in cases.items():
        got = build_ticker_news_query(topic, ticker=ticker, company_name=name)
        assert got == expected, case_id


class _RecordingNewsProvider:

    def __init__(self, articles: list[NewsArticle]) -> None:
        self.articles = articles
        self.calls: list[tuple[str, int]] = []

    async def search_news(self, query: str, *, since=None, limit: int = 20):
        self.calls.append((query, limit))
        return self.articles


class _RecentAndRelevantNewsApi(NewsApiProvider):

    def __init__(self, relevant, recent, recent_error=None) -> None:
        self.relevant = relevant
        self.recent = recent
        self.recent_error = recent_error
        self.calls: list[tuple[int, str | None]] = []

    async def search_news(self, query, *, since=None, limit=20, sort_by=None):
        self.calls.append((limit, sort_by))
        if sort_by == "publishedAt":
            if self.recent_error:
                raise self.recent_error
            return self.recent
        return self.relevant


class _TopicSensitiveProvider:

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search_news(self, query, *, since=None, limit=20):
        self.queries.append(query)
        if " AND (" in query:
            return []
        title = "Coca-Cola raises dividend"
        return [_reuters(title, f"u/{title}", 25)]


def _amd_recent_news(monkeypatch):
    _company_news_env(monkeypatch, "ADVANCED MICRO DEVICES INC")
    monkeypatch.setattr(
        news_tool_module.settings, "NEWSAPI_SORT_BY", "relevancy")
    monkeypatch.setattr(
        news_tool_module.settings, "NEWSAPI_RECENT_EXTRA_LIMIT", 10)


_CONSENT = (
    "https://consent.yahoo.com/v2/collectConsent?sessionId=1_cc-session_ab")
_XOM_TITLES = [
    "Same Conference, Same Day, Same Bet: Chevron and ExxonMobil Double "
    "Down on LNG", "XOM Keeps Climbing. Should You Climb On?",
    "ExxonMobil Has Raised Its Dividend 43 Years Running. Here's the One "
    "Year the Streak Almost Ended"
]

_DIS = [
    (
        "Ghost Town at the Most Magical Place on Earth? Shockingly Low Wait "
        "Times",
        "https://insidethemagic.net/2026/10/magic-kingdom-celebrates-55th"),
    (
        "Disney World Reveals New Magic Kingdom Exclusive With Limited "
        "Availability", "https://insidethemagic.net/?p=849165"),
    (
        "Dana Walden Addresses Disney Layoffs: \u201cTechnology Set Their "
        "Sights On Our Business\u201d",
        "https://biztoc.com/x/e93b96313e1986d6"),
    (
        "38 Years Later, Disney World Says Goodbye to Original Grand "
        "Floridian Resort Design",
        "https://insidethemagic.net/2026/10/reopening"),
    (
        "Disney\u2019s Walden Says She Would Be Interested in Buying "
        "\u2018Bluey\u2019",
        "https://www.livemint.com/companies/disneys-walden"),
    (
        "Magic Kingdom Reopens Area Designated for Families Only in Orlando",
        "https://insidethemagic.net/2026/10/magic-kingdom-reopens"),
    (
        "Disney World Announces Branch & Brush Lakeshore Lodge Lobby Lounge",
        "https://www.disneytouristblog.com/disney-world-announces-branch"),
    (
        "31 Facts, Guesses, and Quotes About Walt Disney World 55 Years Ago",
        "https://touringplans.com/blog/31-facts"),
    (
        "Walt Disney World Brings AI Into Its Booking Site",
        "https://www.cnet.com/tech/services-and-software/walt-disney-world"),
    (
        "Disney built a subway inside Houston's airport",
        "https://boingboing.net/2026/09/24/disney-built-a-subway"),
    (
        "DIS Is Priced Like The Best Of Its Group. Is It?",
        "https://www.trefis.com/articles/614210/dis-is-priced"),
    (
        "Celebrating Walt Disney World at 55",
        "https://disneyparksblog.com/wdw/celebrating-walt-disney-world-at-55/"
    ),
    (
        "Walt Disney Eyes Disney+ Ecosystem, Content Push as Streaming "
        "Margins Top 13%",
        "https://www.marketbeat.com/instant-alerts/event-walt-disney"),
    (
        "Walt Disney borrowed $60,000 against his life insurance to build "
        "Disneyland",
        "https://finance.yahoo.com/small-business/articles/walt-disney-"
        "borrowed"),
    (
        "Spring Back to Walt Disney World with Special Offers and Returning "
        "Events",
        "https://disneyparksblog.com/wdw/walt-disney-world-spring-offers"),
    (
        "Disney Eats: Foodie Guide to Halloween at Walt Disney World Resort "
        "2026", "https://disneyparksblog.com/wdw/disney-eats-foodie-guide"),
    (
        "Disney Eats: New Treats and Drinks Coming to Walt Disney World",
        "https://disneyparksblog.com/wdw/new-treats"),
    (
        "Walt Disney World Opening Day Cast Celebrate 55 Years of Memories",
        "https://disneyparksblog.com/wdw/disney-world-55th-anniversary-merch/"
    ),
    (
        "200 Eyvind Earle Works Will Go On Display At Walt Disney Family "
        "Museum", "https://www.cartoonbrew.com/museums/eyvind-earle"),
    (
        "Walt Disney mortgaged his house and sold his car to fund Snow White",
        "https://finance.yahoo.com/media-advertising/articles/walt-disney-"
        "mortg"),
    (
        "Is Disney Stock A Bargain Or Just A Slower Grower?",
        "https://www.trefis.com/articles/615832/is-disney-stock"),
    (
        "Disney\u2019s Not Stressing Over Bob Chapek\u2019s "
        "\u2018Yawner\u2019 "
        "Book", "https://www.cinemablend.com/movies/disney-bob-chapek"),
    (
        "Bob v. Bob: In a New Memoir, Bob Chapek Details His Short Time "
        "Leading Disney",
        "https://www.vanityfair.com/story/bob-chapek-memoir-bob-iger"),
    (
        "DISNEY WEEKLY UPDATES: New Menus, Massive Ride Changes, And MORE",
        "https://www.disneyfoodblog.com/2026/09/27/disney-weekly-updates"),
    (
        "New Wave of Layoffs Hits Disney Just Days After Warnings of "
        "Restructuring",
        "https://www.disneyfoodblog.com/2026/09/30/new-wave-of-layoffs")
]

_FAN_HOSTS = (
    "insidethemagic", "disneyparksblog", "disneyfoodblog", "disneytouristblog",
    "touringplans")


def _dis_verdict(title: str) -> tuple[str, str]:
    url = dict(_DIS)[title]
    if "Layoffs" in title:
        return "this_company", "company_business"
    if any(host in url for host in _FAN_HOSTS):
        return "this_company", "consumer_product"
    return "this_company", "company_business"


class TestSearchNewsTool:

    async def _unscoped_search_is_unclassified(self, monkeypatch):
        provider = _RecordingNewsProvider([_reuters("Anything")])
        out = await SearchNewsTool(provider=provider
                                   ).run("fed decision", limit=5)
        assert provider.calls == [("fed decision", 5)]
        assert "(1 articles: 0 company business," in out
        assert "1 unclassified" in out
        assert observation_yield(out) == 1

    async def _recent_news_is_merged_first_and_its_failure_is_ignored(
            self, monkeypatch):
        _amd_recent_news(monkeypatch)
        relevant = [
            _reuters("AMD tops $1 trillion", "u1", 21),
            _reuters("AMD guidance upside", "u2", 9)
        ]
        recent = [
            _reuters("Analyst says AMD should be investigated", "u3", 24),
            _reuters("AMD tops $1 trillion", "u1", 21)
        ]
        provider = _RecentAndRelevantNewsApi(relevant, recent)
        out = await SearchNewsTool(provider=provider,
                                   ticker="AMD").run("latest news")
        assert provider.calls == [(20, None), (10, "publishedAt")]
        assert "(3 articles: 0 company business," in out
        lines = out.splitlines()[1:]
        assert "investigated" in lines[0] and "trillion" in lines[1]
        assert "guidance" in lines[2]

        provider = _RecentAndRelevantNewsApi(
            [_reuters("AMD guidance upside", "u2", 9)], [],
            recent_error=ProviderRequestError("429"))
        out = await SearchNewsTool(provider=provider,
                                   ticker="AMD").run("latest news")
        assert "(1 articles" in out

    async def _zero_results_with_topic_falls_back_to_company_only(
            self, monkeypatch):
        _company_news_env(monkeypatch, "COCA COLA CO")
        provider = _TopicSensitiveProvider()
        out = await SearchNewsTool(provider=provider,
                                   ticker="KO").run("financial sentiment")
        assert provider.queries == [
            '(KO OR "Coca Cola") AND (financial sentiment)',
            '(KO OR "Coca Cola")'
        ]
        assert "(1 articles" in out and "Coca-Cola raises dividend" in out

    async def _every_usable_item_is_sent_to_the_classifier(
            self, known_companies):
        known_companies.setattr(settings, "NEWSAPI_RECENT_EXTRA_LIMIT", 0)
        router = _TitleRouter(
            _aligned(_business_if(lambda t: "WEB-DL" not in t)))
        known_companies.setattr(relevance_module, "LLMRouter", lambda: router)
        articles = [
            _article(
                "MobLand S02E03 1080p AMZN WEB-DL H264-FLUX",
                "https://post.example.cc/mobland/"),
            _article(
                "amzn-inspectlens 0.2.0", "https://example.org/project/x/"),
            _article(
                "Amazon (AMZN) Taps the UK Bond Market for the First Time"),
            _article(
                "How Far Can Amazon Stock Fall When The Business Is Fine?",
                "https://www.example.com/b")
        ]
        provider = _StaticNewsProvider(articles)
        text = await SearchNewsTool(
            provider=provider,
            ticker="AMZN"  # type: ignore[arg-type]
        ).run("latest news")
        assert provider.queries == ['(AMZN OR "Amazon")']
        assert text.startswith(
            "News results for '(AMZN OR \"Amazon\")' (2 articles: 2 company "
            "business")
        assert "WEB-DL" not in text and "inspectlens" not in text
        assert router.calls == [[
            articles[0].title, articles[2].title, articles[3].title
        ]]

    async def _consent_page_headlines_are_kept_without_links(
            self, known_companies):
        known_companies.setattr(settings, "NEWSAPI_RECENT_EXTRA_LIMIT", 0)
        router = _TitleRouter(_aligned(_business))
        known_companies.setattr(relevance_module, "LLMRouter", lambda: router)
        articles = [_article(t, _CONSENT) for t in _XOM_TITLES]
        text = await SearchNewsTool(
            provider=_StaticNewsProvider(articles),  # type: ignore[arg-type]
            ticker="XOM").run("latest")
        assert "(3 articles:" in text
        assert text.count("(link unavailable)") == 3
        assert "consent.yahoo.com" not in text

    async def _consumer_items_are_listed_last_and_not_counted(
            self, known_companies):
        known_companies.setattr(settings, "NEWSAPI_RECENT_EXTRA_LIMIT", 0)
        known_companies.setattr(settings, "NEWS_CONSUMER_MAX_ITEMS", 2)
        router = _TitleRouter(_aligned(_dis_verdict))
        known_companies.setattr(relevance_module, "LLMRouter", lambda: router)
        articles = [_article(t, u) for t, u in _DIS]
        tool = SearchNewsTool(
            provider=_StaticNewsProvider(articles),  # type: ignore[arg-type]
            ticker="DIS")
        text = await tool.run("latest news")
        asked = {t for call in router.calls for t in call}
        assert asked == {t for t, _ in _DIS}
        header, _, rest = text.partition("\n")
        assert "(15 articles: 13 company business, 0 stock market, " in header
        assert "2 consumer product" in header
        assert observation_yield(text) == 13
        lines = rest.split("\n")
        assert all("[consumer product]" in line for line in lines[-2:])
        assert not any("[consumer product]" in line for line in lines[:-2])
        assert "New Wave of Layoffs Hits Disney" in rest

        second = await tool.run("parks")
        assert second.count("[consumer product]") == 2

    async def _lines_from_catalog_outlets_are_marked(self, known_companies):
        known_companies.setattr(settings, "NEWSAPI_RECENT_EXTRA_LIMIT", 0)
        _patch_catalog(known_companies, {"apnews.com"})
        router = _TitleRouter(
            _verdict_fields(
                lambda t: {
                    "subject": "this_company",
                    "kind": "company_business"
                }))
        known_companies.setattr(relevance_module, "LLMRouter", lambda: router)
        articles = [
            _newsapi_article(
                "Coca-Cola raises its dividend again", "https://apnews.com/a"),
            _newsapi_article(
                "Coca-Cola opens a bottling plant", "https://blog.example/b",
                source_id="some-source-id"),
            _newsapi_article(
                "Coca-Cola reports quarterly volumes",
                "https://blog.example/c")
        ]
        text = await SearchNewsTool(
            provider=_StaticNewsProvider(articles),  # type: ignore[arg-type]
            ticker="KO").run("x")
        lines = text.split("\n")
        assert "; 2 from catalog outlets" in lines[0]
        flags = [(ln.endswith("catalog=yes"), ln.endswith("catalog=no"))
                 for ln in lines[1:]]
        assert flags == [(True, False), (True, False), (False, True)]

    def _news_lines_carry_the_weekday_and_the_prompt_resolves_it(self):
        line = format_article_line(
            _article("Google launched Gemini 4 Argon", day=1))
        assert line.startswith(
            "[2026-10-01 Thu] x: Google launched Gemini 4 Argon")
        prompt = news_module._SYSTEM_PROMPT
        assert "[2026-10-01 Thu]" in prompt
        assert "2026-09-30" in prompt and "early October" in prompt

    async def test_queries_recent_merge_and_topic_fallback(self):
        self._news_lines_carry_the_weekday_and_the_prompt_resolves_it()
        await _run_steps(
            self._unscoped_search_is_unclassified,
            self._recent_news_is_merged_first_and_its_failure_is_ignored,
            self._zero_results_with_topic_falls_back_to_company_only)

    async def test_classified_lines_links_and_catalog_marks(self):
        await _run_steps(
            self._every_usable_item_is_sent_to_the_classifier,
            self._consent_page_headlines_are_kept_without_links,
            self._consumer_items_are_listed_last_and_not_counted,
            self._lines_from_catalog_outlets_are_marked,
            setup=_install_known_companies)


class _QueuedHttpClient:

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls = 0

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        return self.responses.pop(0)


def _newsapi_response(
        status: int, body: dict[str, Any],
        headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, json=body, headers=headers,
        request=httpx.Request("GET", newsapi_module._EVERYTHING_URL))


_NEWSAPI_OK = {"status": "ok", "articles": []}


def _newsapi_responses(monkeypatch):
    monkeypatch.setattr(settings, "NEWSAPI_API_KEY", "k")
    monkeypatch.setattr(
        newsapi_module, "_rate_limited", newsapi_module._RateLimitState())

    def _install(*responses: httpx.Response) -> _QueuedHttpClient:
        client = _QueuedHttpClient(list(responses))
        monkeypatch.setattr(newsapi_module, "get_http_client", lambda: client)
        return client

    return _install


@pytest.fixture
async def clean_newsapi_usage():
    from shared.redis import get_cache_client

    await get_cache_client(settings).delete(newsapi_module.USAGE_KEY)
    yield
    await get_cache_client(settings).delete(newsapi_module.USAGE_KEY)


async def _newsapi_usage() -> int:
    from shared.redis import get_cache_client

    cache = get_cache_client(settings)
    return int(await cache.zcard(newsapi_module.USAGE_KEY))


class _MemoryCache:

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttl[key] = ex or 0


def _catalog_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "requests": [],
        "cache": _MemoryCache(),
        "status": 200
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(str(request.url))
        if state["status"] != 200:
            return httpx.Response(
                state["status"], json={
                    "status": "error",
                    "code": "unexpectedError"
                })
        return httpx.Response(
            200, json={
                "status": "ok",
                "sources": [{
                    "id": "reuters",
                    "name": "Reuters",
                    "url": "https://www.reuters.com"
                }, {
                    "id": "ap",
                    "name": "AP",
                    "url": "https://apnews.com/"
                }, {
                    "id": "bad",
                    "name": "Bad",
                    "url": ""
                }]
            })

    async def _no_count(resp: httpx.Response) -> None:
        return None

    monkeypatch.setattr(
        newsapi_module, "_catalog_state", newsapi_module._CatalogState())
    monkeypatch.setattr(newsapi_module.settings, "NEWSAPI_API_KEY", "k")
    monkeypatch.setattr(
        newsapi_module, "get_cache_client", lambda s: state["cache"])
    monkeypatch.setattr(
        newsapi_module, "get_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(newsapi_module, "_record_counted_request", _no_count)
    monkeypatch.setattr(newsapi_module._rate_limited, "until", 0.0)
    return state


class TestNewsApiProvider:

    async def _default_search_fields_keep_the_previous_request(
            self, monkeypatch):
        params: list[dict[str, str]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            params.append(dict(request.url.params))
            return httpx.Response(200, json=_NEWSAPI_OK)

        monkeypatch.setattr(
            newsapi_module.settings, "NEWSAPI_API_KEY", "test-key")
        monkeypatch.setattr(
            newsapi_module, "get_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
        monkeypatch.setattr(newsapi_module.settings, "NEWSAPI_SEARCH_IN", "")
        monkeypatch.setattr(
            newsapi_module.settings, "NEWSAPI_SORT_BY", "publishedAt")
        await NewsApiProvider().search_news("AMD")
        assert "searchIn" not in params[0]
        assert params[0]["sortBy"] == "publishedAt"

    async def _a_rate_limit_stops_requests_until_the_cooldown_ends(
            self, monkeypatch):
        newsapi_responses = _newsapi_responses(monkeypatch)
        client = newsapi_responses(
            _newsapi_response(
                429, {
                    "status": "error",
                    "code": "rateLimited",
                    "message": "You have made too many requests recently."
                }), _newsapi_response(200, _NEWSAPI_OK))
        provider = NewsApiProvider()
        with pytest.raises(ProviderRequestError) as first:
            await provider.search_news("AAPL")
        assert "request limit reached (HTTP 429 rateLimited)" in str(
            first.value)
        assert "use web_search" in str(first.value)

        with pytest.raises(ProviderRequestError) as second:
            await provider.search_news("MSFT")
        assert "no NewsAPI request for another 60 min" in str(second.value)
        assert client.calls == 1

        monkeypatch.setattr(newsapi_module._rate_limited, "until", 0.0)
        assert await provider.search_news("KO") == []
        assert client.calls == 2

    async def _only_counted_requests_are_recorded_and_redis_is_optional(
            self, monkeypatch):
        newsapi_responses = _newsapi_responses(monkeypatch)
        newsapi_responses(
            _newsapi_response(200, _NEWSAPI_OK),
            _newsapi_response(
                200, _NEWSAPI_OK, headers={"X-Cached-Result": "true"}),
            _newsapi_response(429, {
                "status": "error",
                "code": "rateLimited"
            }))
        provider = NewsApiProvider()
        await provider.search_news("AAPL")
        await provider.search_news("AAPL")
        with pytest.raises(ProviderRequestError):
            await provider.search_news("MSFT")
        assert await _newsapi_usage() == 1

        monkeypatch.setattr(
            newsapi_module, "_rate_limited", newsapi_module._RateLimitState())

        def _down(settings: Any) -> Any:
            raise ConnectionError("redis down")

        monkeypatch.setattr(newsapi_module, "get_cache_client", _down)
        newsapi_responses(_newsapi_response(200, _NEWSAPI_OK))
        assert await NewsApiProvider().search_news("AAPL") == []

    async def _catalog_is_fetched_once_and_cached_for_a_week(
            self, monkeypatch):
        catalog_env = _catalog_env(monkeypatch)
        catalog = await newsapi_module.source_catalog()
        assert catalog is not None
        assert catalog.hosts == {"reuters.com", "apnews.com"}
        assert len(catalog_env["requests"]) == 1
        assert "top-headlines/sources" in catalog_env["requests"][0]
        cache = catalog_env["cache"]
        assert cache.ttl[newsapi_module.CATALOG_CACHE_KEY] == 7 * 24 * 3600
        await newsapi_module.source_catalog()
        assert len(catalog_env["requests"]) == 1
        newsapi_module._catalog_state.loaded_at = float("-inf")
        again = await newsapi_module.source_catalog()
        assert again == catalog and len(catalog_env["requests"]) == 1

    async def _catalog_failure_is_open_and_not_retried_at_once(
            self, monkeypatch):
        catalog_env = _catalog_env(monkeypatch)
        catalog_env["status"] = 500
        assert await newsapi_module.source_catalog() is None
        assert await newsapi_module.source_catalog() is None
        assert len(catalog_env["requests"]) == 1

    async def _stale_catalog_is_served_while_newsapi_is_rate_limited(
            self, monkeypatch):
        catalog_env = _catalog_env(monkeypatch)
        fetched = datetime.now(UTC) - timedelta(hours=30)
        catalog_env["cache"].store[newsapi_module.CATALOG_CACHE_KEY] = (
            json.dumps({
                "hosts": ["reuters.com"],
                "fetched_at": fetched.isoformat()
            }))
        monkeypatch.setattr(
            newsapi_module._rate_limited, "until", float("inf"))
        with capture_logs() as logs:
            catalog = await newsapi_module.source_catalog()
        assert catalog is not None and catalog.hosts == {"reuters.com"}
        assert len(catalog_env["requests"]) == 0
        assert any(
            e["event"] == "newsapi.source_catalog_stale_served" for e in logs)

    async def test_usage_rate_limit_and_search_fields(
            self, clean_newsapi_usage):
        await _run_steps(
            self._only_counted_requests_are_recorded_and_redis_is_optional,
            self._default_search_fields_keep_the_previous_request,
            self._a_rate_limit_stops_requests_until_the_cooldown_ends)

    async def test_source_catalog_cache_and_failures(self):
        await _run_steps(
            self._catalog_is_fetched_once_and_cached_for_a_week,
            self._catalog_failure_is_open_and_not_retried_at_once,
            self._stale_catalog_is_served_while_newsapi_is_rate_limited)


_GOOGL_TITLES = [
    "Armadin nabs $255.5M to detect vulnerabilities with AI agent swarms",
    "Alphabet vs. Amazon vs. Microsoft: Which Magnificent Seven Stock Is "
    "Better",
    "Waymo says Singapore will be its next international robotaxi city",
    "Hepburn Romanization: How to Read Japanese in the Latin Alphabet",
    "The Genius Logic of the NATO Phonetic Alphabet",
    "Warren Buffett piled into Alphabet to bet big on AI"
]


def _shifted_answer(titles: list[str]) -> list[dict[str, Any]]:
    off_topic = {3, 4}
    answer = [{
        "index": 0,
        "title": _echo(titles[0]),
        "reasoning": "startup funding",
        "subject": "other_company",
        "kind": "company_business"
    }, {
        "index": 1,
        "title": _echo(titles[1]),
        "reasoning": "roundup",
        "subject": "other_company",
        "kind": "opinion_listicle"
    }]
    for i in range(2, len(titles) - 1):
        nxt = i + 1
        answer.append({
            "index": i,
            "title": _echo(titles[nxt]),
            "reasoning": f"about item {nxt}",
            "subject": "none" if nxt in off_topic else "this_company",
            "kind": "company_business"
        })
    return answer


_INTEL_TITLES = [
    "Intel signs a foundry deal today", "Intel names a new finance chief"
]
_CUT_OFF = '[\n  {\n    "index": 0,\n    "title": "Intel signs a'


def _intel_verdicts() -> str:
    return json.dumps([{
        "index": i,
        "title": _echo(t),
        "reasoning": "r",
        "subject": "this_company",
        "kind": "company_business"
    } for i, t in enumerate(_INTEL_TITLES)])


async def _classify_intel(router: _ScriptedRouter) -> list[ClassifiedArticle]:
    return await relevance_module._classify_batch(
        [
            _src_article(t, "https://example.com/" + t[:5])
            for t in _INTEL_TITLES
        ],
        ticker="INTC",
        company_name="Intel",
        llm_router=router  # type: ignore[arg-type]
    )


LQ, RQ = chr(0x2018), chr(0x2019)

_MSFT_TITLES = [
    f"Inside Microsoft{RQ}s big Copilot rethink",
    "Seattle Times and Newsday sue OpenAI and Microsoft for infringement",
    f"{LQ}Xbox is not for sale{RQ} says Microsoft{RQ}s gaming chief",
    "Microsoft is laying off 268 Xbox staffers and Ninja Theory may close"
]


def _msft_answer(broken: int | None = 2) -> str:
    parts = []
    for i, title in enumerate(_MSFT_TITLES):
        echo = _echo(title)
        if i == broken:
            echo = echo.replace(LQ, '"').replace(RQ, '"')
        parts.append(
            f'  {{\n    "index": {i},\n    "title": "{echo}",\n'
            '    "reasoning": "r.",\n    "subject": "this_company",\n'
            '    "kind": "company_business"\n  }')
    return "[\n" + ",\n".join(parts) + "\n]"


class TestNewsRelevance:

    def test_filters_verdict_alignment_and_prompt(self):
        dis = company_aliases("DIS", _LEGAL["DIS"])
        assert dis.strong == ("walt disney", "waltdisney")
        assert company_aliases("AMD", _LEGAL["AMD"]).strong == (
            "advanced micro devices", "advancedmicrodevices")
        assert company_aliases("KO", None).strong == ()

        pre_drops = {
            "package-version": (
                "microsoft-agents-hosting-core 1.8.0",
                "package_version_title"),
            "package-post-release": (
                "dismech-newton 0.1.0.post1", "package_version_title"),
            "untitled": ("(untitled)", "unusable_title"),
            "single-word": ("Disney", "unusable_title")
        }
        for case_id, (title, reason) in pre_drops.items():
            assert precheck_reason(title) == reason, case_id

        url = (
            "https://guce.yahoo.com/consent?brandType=nonEu&done="
            "https%3A%2F%2Ffinance.yahoo.com%2Fnews%2Fxom-keeps-climbing.html")
        assert consent_target_url(url) == (
            "https://finance.yahoo.com/news/xom-keeps-climbing.html")
        assert consent_target_url(_CONSENT) is None

        titles = ["Alpha one two", "Beta three four", "Gamma five six"]
        parsed = [{
            "index": 0,
            "title": "Alpha one two",
            "relevant": True
        }, {
            "index": 0,
            "title": "Alpha one two",
            "relevant": False
        }, {
            "index": 1,
            "title": "Beta three four",
            "relevant": False
        }, {
            "index": 7,
            "title": "Gamma",
            "relevant": True
        }, {
            "index": "2",
            "relevant": True
        }, "junk"]
        trusted, issues = align_verdicts(parsed, titles)
        assert set(trusted) == {1}
        assert issues["duplicate_indices"] == [0]
        assert issues["out_of_range_indices"] == [7]
        assert issues["invalid_entries"] == 2
        assert issues["missing_indices"] == [2]

        titles = ["Alpha one", "Beta two"]
        clean = [{
            "index": 0,
            "relevant": True
        }, {
            "index": 1,
            "relevant": False
        }]
        trusted, issues = align_verdicts(clean, titles)
        assert set(trusted) == {0, 1} and issues == {}
        gappy = [{"index": 0, "relevant": False}]
        trusted, issues = align_verdicts(gappy, titles)
        assert trusted == {} and issues["untrusted_without_echo"] == [0]

        entries = [{
            "index": i,
            "title": t,
            "subject": "this_company",
            "kind": "company_business"
        } for i, t in enumerate(_WEB_TITLES)]
        parsed = read_verdicts(json.dumps({"verdicts": entries}), len(entries))
        assert not parsed.strict
        assert parsed.complete
        self._classifier_prompt_defines_categories_without_examples()

    async def _misaligned_answer_keeps_articles_aligned_one_applies(
            self, monkeypatch):
        articles = [_article(t) for t in _GOOGL_TITLES]
        router = _TitleRouter(_shifted_answer)
        with capture_logs() as logs:
            kept = await relevance_module._classify_batch(
                articles,
                ticker="GOOGL",
                company_name="Alphabet",
                llm_router=router  # type: ignore[arg-type]
            )
        assert [c.article.title for c in kept] == _GOOGL_TITLES[2:]
        assert {c.kind for c in kept} == {"unclassified"}
        events = [
            e for e in logs
            if e["event"] == "news_relevance.verdicts_misaligned"
        ]
        assert events and events[0]["kept_unverified"] == 4
        classified = next(
            e for e in logs if e["event"] == "news_relevance.classified")
        assert [d["title"]
                for d in classified["dropped"]] == (_GOOGL_TITLES[:2])

        router = _TitleRouter(
            _aligned(
                _business_if(
                    lambda t: "Alphabet" in t and "Latin" not in t and "NATO"
                    not in t)))
        kept = await relevance_module._classify_batch(
            articles,
            ticker="GOOGL",
            company_name="Alphabet",
            llm_router=router  # type: ignore[arg-type]
        )
        assert [c.article.title
                for c in kept] == [_GOOGL_TITLES[1], _GOOGL_TITLES[5]]
        assert '"title"' in router.prompts[0]

    async def _stock_move_headline_is_kept_and_prompt_has_no_examples(
            self, monkeypatch):
        _install_known_companies(monkeypatch)
        titles = [
            "The Nasdaq Rose 1.9% in September. Intel Exploded 34%",
            "Going on a Tangent with the Intel 8087's Hybrid CORDIC "
            "Algorithm"
        ]
        verdicts = {
            titles[0]: ("this_company", "stock_market"),
            titles[1]: ("none", "consumer_product")
        }
        router = _TitleRouter(_aligned(lambda t: verdicts[t]))
        kept = await relevance_module.classify_articles(
            [_article(t) for t in titles],
            ticker="INTC",
            llm_router=router  # type: ignore[arg-type]
        )
        assert [c.article.title for c in kept] == [titles[0]]
        prompt = router.prompts[0]
        for example in ("34%", "subsidiary", "retro", "shopping guides",
                        "Waymo"):
            assert example not in prompt

    async def _invalid_verdicts_or_no_company_name_leave_unclassified(
            self, monkeypatch):
        router = _TitleRouter(
            _verdict_fields(
                lambda t: {
                    "subject": "maybe",
                    "kind": "company_business"
                }))
        with capture_logs() as logs:
            out = await relevance_module._classify_batch(
                [_newsapi_article("Coca-Cola raises its dividend again")],
                ticker="KO",
                company_name="Coca Cola",
                llm_router=router  # type: ignore[arg-type]
            )
        assert [c.kind for c in out] == ["unclassified"]
        assert any(
            e["event"] == "news_relevance.invalid_verdicts" for e in logs)

        async def _none(ticker: str) -> None:
            return None

        monkeypatch.setattr(relevance_module, "lookup_company_name", _none)
        router = _TitleRouter(
            _verdict_fields(lambda t: {
                "subject": "none",
                "kind": "not_news"
            }))
        out = await relevance_module.classify_articles(
            [_newsapi_article("A headline of some kind")],
            ticker="ZZZZ",
            llm_router=router  # type: ignore[arg-type]
        )
        assert [c.kind for c in out] == ["unclassified"]
        assert router.calls == []

    async def _unparseable_answer_is_retried_with_a_larger_budget(self):
        router = _ScriptedRouter([_CUT_OFF, _intel_verdicts()])
        with capture_logs() as logs:
            out = await _classify_intel(router)
        assert [c.kind
                for c in out] == ["company_business", "company_business"]
        assert router.max_tokens == [
            relevance_module._VERIFY_MAX_TOKENS,
            relevance_module._RETRY_MAX_TOKENS
        ]
        assert (
            relevance_module._RETRY_MAX_TOKENS
            > relevance_module._VERIFY_MAX_TOKENS)
        events = [e["event"] for e in logs]
        assert "news_relevance.unparseable_retry" in events
        assert "news_relevance.unparseable_retry_recovered" in events
        retry = next(
            e for e in logs
            if e["event"] == "news_relevance.unparseable_retry")
        assert retry["finish_reason"] == "STOP"
        assert retry["answer_tail"].endswith("signs a")

    async def _classifier_call_errors(self):
        cases = {
            "retry-call-error-is-unverified": ([_CUT_OFF,
                                                LLMError("down")],
                                               "unverified"),
            "first-call-error-is-unclassified": ([LLMError("down")],
                                                 "unclassified")
        }
        for case_id, (answers, kind) in cases.items():
            out = await _classify_intel(_ScriptedRouter(answers))
            assert [c.kind for c in out] == [kind, kind], case_id

    async def _invalid_but_complete_answer_is_not_retried(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads(_msft_answer())
        json.loads(_msft_answer(broken=None))
        router = _ScriptedRouter([_msft_answer(broken=None) + "\nDone."])
        with capture_logs() as logs:
            out = await relevance_module._classify_batch(
                [
                    _src_article(t, "https://example.com/" + t[:8])
                    for t in _MSFT_TITLES
                ],
                ticker="MSFT",
                company_name="Microsoft",
                llm_router=router  # type: ignore[arg-type]
            )
        assert router.calls == 1
        assert [c.kind for c in out] == ["company_business"] * 4
        assert any(
            e["event"] == "news_relevance.salvaged_without_retry"
            for e in logs)

    async def test_misaligned_invalid_or_nameless_verdicts(self):
        await _run_steps(
            self._misaligned_answer_keeps_articles_aligned_one_applies,
            self._stock_move_headline_is_kept_and_prompt_has_no_examples,
            self._invalid_verdicts_or_no_company_name_leave_unclassified)

    async def test_unparseable_and_failed_classifier_calls(self):
        await self._unparseable_answer_is_retried_with_a_larger_budget()
        await self._classifier_call_errors()
        await self._invalid_but_complete_answer_is_not_retried()

    def _classifier_prompt_defines_categories_without_examples(self):
        prompt = relevance_module._system_prompt(
            ticker="KO", company_name="Coca Cola")
        for kind in relevance_module.NEWS_KINDS:
            assert prompt.count(f"- {kind}:") == 1
        assert "reference_or_social" not in prompt
        assert '"subject"' in prompt and '"kind"' in prompt
        assert '"title"' in prompt
        assert not re.search(r"\.(?:com|net|org|io)\b|https?://", prompt)
        for name in ("Intel", "Disney", "Amazon", "Waymo", "Berkshire",
                     "Microsoft"):
            assert name not in prompt

        text = " ".join(relevance_module.KIND_DEFINITIONS.values())
        assert not re.search(r"\.(?:com|net|org|io)\b|https?://", text)
        assert not re.search(r"\b[A-Z][a-z]+[A-Z]\w*\b", text)

        prompt = relevance_module._system_prompt(
            ticker="QCOM", company_name="Qualcomm")
        assert '"party"' in prompt
        assert "one of several parties" in prompt
        assert "party" in relevance_module.SUBJECTS
        for name in ("Apple", "CVS", "Microsoft", "Boeing", "Amazon",
                     "Humana"):
            assert name not in prompt

        prompt = relevance_module._system_prompt(
            ticker="MSFT", company_name="Microsoft")
        assert "without any quotation marks" in prompt
        assert (
            "Never put a double quote character inside a string value"
            in prompt)


_WEB_TITLES = [
    "Fed holds rates steady in September",
    "Treasury yields climb after Fed decision",
    "Powell says inflation remains elevated"
]


def _fed_results() -> list[WebSearchResult]:
    return [
        _web_result(t, f"https://site{i}.example/a")
        for i, t in enumerate(_WEB_TITLES)
    ]


def _fed_answer(broken: int | None = 1) -> str:
    entries = []
    for i, title in enumerate(_WEB_TITLES):
        echo = _echo(title)
        if i == broken:
            echo = f'"{echo}" quoted'
        entries.append(
            f'{{"index": {i}, "title": "{echo}", "reasoning": "r", '
            '"relevant": true, "kind": "company_business"}')
    return '{"relevant": [' + ", ".join(entries) + "]}"


class _RecordingTavily(TavilyProvider):

    def __init__(self, results: list[WebSearchResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, int | None]] = []

    async def search(
            self, query: str, *, limit: int = 5,
            news_days: int | None = None) -> list[WebSearchResult]:
        self.calls.append((query, news_days))
        return self.results


class _CannedWebSearch(Tool):
    name = "web_search"
    description = "fake"
    parameters: dict[str, Any] = {}

    def __init__(self, observation: str) -> None:
        self.observation = observation
        self.queries: list[str] = []

    async def run(  # type: ignore[override]
        self,
        query: str,
        limit: int = 5
    ) -> str:
        self.queries.append(query)
        return self.observation


def _install_tavily(monkeypatch, body: dict[str, Any]) -> list[dict]:
    bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    monkeypatch.setattr(tavily.settings, "TAVILY_API_KEY", "k")
    monkeypatch.setattr(
        tavily, "get_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    return bodies


class TestWebSearch:

    async def _tavily_news_mode_dates_and_score_order(self, monkeypatch):
        bodies = _install_tavily(
            monkeypatch, {
                "results": [{
                    "title": "Nvidia news",
                    "url": "https://www.reuters.com/n",
                    "content": "c",
                    "published_date": "Thu, 01 Oct 2026 14:00:00 GMT"
                }]
            })
        results = await TavilyProvider().search(
            "Nvidia company news", news_days=7)
        await TavilyProvider().search("Fed decision")
        assert bodies[0]["topic"] == "news" and bodies[0]["days"] == 7
        assert "topic" not in bodies[1] and "days" not in bodies[1]
        assert results[0].published_at is not None
        assert results[0].published_at.date().isoformat() == "2026-10-01"

        _install_tavily(
            monkeypatch, {
                "results": [{
                    "title": "low",
                    "url": "https://a.example",
                    "content": "c",
                    "score": 0.2
                }, {
                    "title": "none",
                    "url": "https://b.example",
                    "content": "c"
                }, {
                    "title": "high",
                    "url": "https://c.example",
                    "content": "c",
                    "score": 0.9
                }]
            })
        results = await TavilyProvider().search("q")
        assert [r.title for r in results] == ["high", "low", "none"]
        assert [getattr(r, "score", None) for r in results] == [0.9, 0.2, None]

    async def _web_kinds_drop_results_and_the_company_is_named(
            self, known_companies):
        verdicts = {
            "August - Definition and Meaning": (True, "reference_or_social"),
            "Short-Term Energy Outlook: forecast tables": (
                True, "company_business"),
            "Oil price prediction page for Brent": (True, "price_page"),
            "Fed holds rates steady in September": (True, "company_business"),
            "Unrelated business with the same name": (
                False, "company_business"),
            "Package listing page with a version": (True, "not_news"),
            "Fed minutes show a split committee": (True, "nonsense")
        }
        router = _TitleRouter(
            _verdict_fields(
                lambda t: {
                    "relevant": verdicts[t][0],
                    "kind": verdicts[t][1]
                }))
        known_companies.setattr(
            search_relevance_module, "LLMRouter", lambda: router)
        results = [
            _web_result(t, f"https://site{i}.example/p")
            for i, t in enumerate(verdicts)
        ]
        with capture_logs() as logs:
            kept = await search_relevance_module.classify_results(
                "fed decision", results)
        assert [(c.result.title, c.kind) for c in kept] == [
            ("Short-Term Energy Outlook: forecast tables", "company_business"),
            ("Fed holds rates steady in September", "company_business"),
            ("Fed minutes show a split committee", "unclassified")
        ]
        prompt = router.prompts[0]
        assert "- reference_or_social:" in prompt and '"kind"' in prompt
        event = next(
            e for e in logs if e["event"] == "search_relevance.classified")
        assert event["dropped_count"] == 4

        router = _TitleRouter(
            _verdict_fields(
                lambda t: {
                    "relevant": True,
                    "kind": "company_business"
                }))
        known_companies.setattr(
            search_relevance_module, "LLMRouter", lambda: router)
        await search_relevance_module.classify_results(
            "Coca Cola company news", [_web_result("Coca-Cola news story")],
            ticker="KO")
        assert "research on Coca Cola (ticker KO)" in router.prompts[0]

    async def _web_answers_budget_and_unverified_items(self, monkeypatch):
        router = _ScriptedRouter([_fed_answer(broken=None)])
        kept = await search_relevance_module.classify_results(
            "fed decision", _fed_results(),
            llm_router=router)  # type: ignore[arg-type]
        assert router.max_tokens == [4096]
        assert len(kept) == 3
        assert "without any quotation marks" in router.prompts[0]

        router = _ScriptedRouter([_fed_answer(), _fed_answer()])
        monkeypatch.setattr(
            search_relevance_module, "LLMRouter", lambda: router)

        async def _no_catalog() -> None:
            return None

        monkeypatch.setattr(web_tool_module, "source_catalog", _no_catalog)

        class _Provider:

            async def search(self, query: str, *,
                             limit: int = 5) -> list[WebSearchResult]:
                return _fed_results()

        text = await WebSearchTool(provider=_Provider()
                                   ).run("fed")  # type: ignore[arg-type]
        assert "1 unverified" in text.split("\n")[0]
        assert "[unverified] Treasury yields climb" in text
        assert observation_yield(text) == 2

    async def _web_fallback_only_after_a_failed_news_search(
            self, known_companies):
        web = _CannedWebSearch(
            "Web search results for 'MSFT stock news' (3 results):\n"
            "[2026-10-01] Microsoft beats (https://www.reuters.com/a): "
            "text\n"
            "[2026-10-01] Azure grows (https://example.com/b): text\n"
            "[2026-10-01] Same story (https://www.reuters.com/a): text")
        ok = ScratchpadEntry(
            thought="prefetched", action="search_news",
            action_input={"query": "latest news"}, observation=(
                "News results for '(MSFT OR \"Microsoft\")' (13 articles)"),
            prefetched=True)
        assert await prefetch_web_fallback([ok], "MSFT", web) == []
        assert await prefetch_web_fallback([], "MSFT", web) == []
        assert web.queries == []

        router = _TitleRouter(
            _aligned(lambda t: ("this_company", "price_page")))
        known_companies.setattr(
            search_relevance_module, "LLMRouter", lambda: router)
        provider = _RecordingTavily([
            _web_result(
                "NVIDIA Stock Forecast 2026", "https://midforex.example/nvda",
                "text"),
            _web_result(
                "AI sets NVIDIA stock price for October 31, 2026",
                "https://finbold.example/ai-sets-nvidia", "text"),
            _web_result(
                "NVIDIA Corporation (NVDA) Stock Price, News, Quote",
                "https://techi.example/quote/NVDA", "text")
        ])
        web = WebSearchTool(provider=provider, ticker="NVDA", news_days=7)
        failed = ScratchpadEntry(
            thought="prefetched", action="search_news",
            action_input={"query": "x"},
            observation="News search failed for 'x': NewsAPI search failed",
            prefetched=True)
        entries = await prefetch_web_fallback([failed], "NVDA", web,
                                              company_name="Nvidia")
        assert provider.calls == [("Nvidia company news", 7)]
        assert [e.action for e in entries] == ["web_search"]
        assert observation_yield(entries[0].observation) == 0
        assert len(router.calls) == 1 and len(router.calls[0]) == 3

    async def test_tavily_requests_and_answer_budget(self):
        await _run_steps(
            self._tavily_news_mode_dates_and_score_order,
            self._web_answers_budget_and_unverified_items)

    async def test_web_kinds_and_news_fallback(self):
        await _run_steps(
            self._web_kinds_drop_results_and_the_company_is_named,
            self._web_fallback_only_after_a_failed_news_search,
            setup=_install_known_companies)


_LIVE_HEADER = (
    "News results for 'x' (1 articles: 1 company business, 0 stock "
    "market, 0 opinion/listicle, 0 consumer product, 0 unclassified):\n")


def _live_news(line: str) -> ScratchpadEntry:
    return ScratchpadEntry(
        thought="t", action="search_news", action_input={},
        observation=_LIVE_HEADER + line)


def _prior_news_note(ticker: str, note: str) -> ScratchpadEntry:
    return ScratchpadEntry(
        thought="t", action="search_prior_news_notes", action_input={},
        observation=(
            "Prior news note search results for 'q' (1 passages):\n"
            f"Note: ...\n- ({ticker}) [agent:news] (2026-10-02) [PRIOR "
            "AI-WRITTEN NOTE written 2026-10-02 -- unverified lead]: "
            f"{note}"))


_GOOGL_PRIOR_NOTE = (
    "Recent developments for Alphabet are driven by AI announcements and "
    "legal headlines. In early October 2026, Google unveiled its frontier "
    "AI model, and a New York federal judge's ruling lets publishers seek "
    "damages over ad-tech monopolization, while a data center investment "
    "in Finland was announced in September.")

_WEEKDAY_REF = date(2026, 10, 3)


class TestNewsAgent:

    async def test_an_unverified_fallback_is_marked_as_thin(
            self, known_companies):
        monkeypatch = known_companies
        _patch_catalog(monkeypatch, {"reuters.com"})
        monkeypatch.setattr(settings, "AGENT_PREFETCH_ENABLED", True)

        class _Broken:

            async def search_news(self, query: str,
                                  **kw: Any) -> list[NewsArticle]:
                raise ProviderRequestError(
                    "request limit reached (HTTP 429 rateLimited)")

        provider = _RecordingTavily([
            _web_result(
                "Nvidia ships new chips to data centers",
                "https://blog.example/a",
                "Nvidia ships new chips to data centers this week.")
        ])
        monkeypatch.setattr(
            news_tool_module, "get_news_provider", lambda: _Broken())
        monkeypatch.setattr(
            web_tool_module, "get_web_search_provider", lambda: provider)
        monkeypatch.setattr(
            search_relevance_module, "LLMRouter",
            lambda: _TitleRouter(_aligned(_business)))
        finish = _finish(
            "Nvidia ships new chips to data centers this week, per an "
            "unverified blog.")

        class _Scripted:

            async def complete(
                    self, tier: Any, messages: Any, **kw: Any) -> LLMResponse:
                return _response(finish)

        monkeypatch.setattr(loop_module, "LLMRouter", lambda: _Scripted())
        state = AnalysisState(
            analysis_id="a1", user_id="u1", ticker="NVDA",
            analysis_type=AnalysisType.FULL)
        with capture_logs() as logs:
            output = await NewsAgent(role=AgentRole.NEWS).run(state)

        assert provider.calls[0] == (
            "Nvidia company news", settings.NEWS_WEB_FALLBACK_DAYS)
        pad = output.raw_output["scratchpad"]
        assert "assess_source_credibility" not in {e["action"] for e in pad}
        assert output.raw_output["fallback_sources_unverified"] is True
        assert FALLBACK_UNVERIFIED_REASON in (
            output.raw_output["verification_warnings"])
        assert FALLBACK_UNVERIFIED_REASON in output.summary
        assert output.score is not None and output.score <= 0.6
        assert any(
            e["event"] == "news.fallback_sources_unverified" for e in logs)

    def test_finish_validator_and_weekday_checks(self):
        validator = _NewsFinishValidator()
        pad = [
            _prior_news_note("GOOGL", _GOOGL_PRIOR_NOTE),
            _live_news("[2026-10-02 Fri] Src: a")
        ]
        answer = _GOOGL_PRIOR_NOTE + " Sentiment is mixed."
        ratio = prior_news_note_similarity(answer, pad)
        assert ratio is not None
        assert ratio >= settings.PRIOR_NOTE_MAX_SIMILARITY
        with capture_logs() as logs:
            first = validator(answer, None, pad)
        assert first is not None and "Prior-note copy check" in first
        assert any(
            e["event"] == "finish_checks.once_check_rejected"
            and e["check"] == "prior_note_copy" for e in logs)
        assert validator(answer, None, pad) is None

        pad = [
            _prior_news_note(
                "TSLA", "Deliveries fell 2.1% from a year earlier."),
            _live_news(
                "[2026-10-02 Fri] Src: Tesla deliveries 486,532 "
                "units")
        ]
        validator = _NewsFinishValidator()
        answer = "Deliveries fell 2.1% (catalog outlet Electrek)."
        issue = validator(answer, None, pad)
        assert issue is not None
        assert "Internal text check" in issue and "Prior-note check" in issue
        assert validator.rejections == 1
        assert validator(answer, None, pad) is None

        weekday_cases = {
            "wrong-weekday-in-sentence": (
                "approved a four-year contract on Thursday, October 2, 2026",
                True),
            "reference-day-wrong-weekday": (
                "a safety review scheduled for Friday, October 3, 2026", True),
            "right-weekday": ("on Friday, October 2, 2026", False),
            "wrong-weekday-in-brackets": ("October 1, 2026 (Wednesday)", True),
            "right-weekday-in-brackets": ("October 1, 2026 (Thursday)", False),
            "abbreviated-month-in-brackets": (
                "Thursday (Oct. 1, 2026)", False),
            "day-before-month": ("Friday, 3 October 2026", True),
            "iso-date-wrong-weekday": ("2026-10-03 Fri", True),
            "iso-date-right-weekday": ("[2026-10-02 Fri]", False),
            "no-year-right-weekday": ("Monday, September 28", False),
            "no-year-wrong-weekday": ("Tuesday, September 28", True),
            "date-range": ("Thursday, October 1-2, 2026", False),
            "number-is-not-a-date": (
                "On Friday, 17,000 engineers voted", False),
            "impossible-date": ("Sunday, February 29, 2026", False),
            "no-weekday": ("in early October 2026", False)
        }
        for case_id, (text, wrong) in weekday_cases.items():
            got = bool(weekday_date_mismatches(text, _WEEKDAY_REF))
            assert got is wrong, case_id


def test_prompt_wording():
    cases = {
        "news-agent-prompt": (
            news_module._SYSTEM_PROMPT, (
                "[unverified]", "do not count as evidence",
                "without weekday names", "never write them", "catalog=yes"), (
                    "catalog outlet", "assess_source_credibility",
                    "[catalog outlet]", "site:")),
        "news-agent-prompt-names-no-outlet": (
            news_module._SYSTEM_PROMPT.lower(), (), ("reuters", )),
        "macro-agent-prompt": (
            macro_module._SYSTEM_PROMPT,
            ("last policy-rate move", "catalog=yes"),
            ("assess_source_credibility", "[catalog outlet]", "site:")),
        "macro-agent-prompt-names-no-outlet": (
            macro_module._SYSTEM_PROMPT.lower(), (), ("reuters", )),
        "bull-prompt": (
            debate_side._BULL_SYSTEM_PROMPT,
            ("do not present net income as evidence of", ), ()),
        "bear-prompt": (
            debate_side._BEAR_SYSTEM_PROMPT,
            ("do not present net income as evidence of", ), ()),
        "internal-label-message-has-no-outlet-list": (
            INTERNAL_TOKEN_MESSAGE, ("{tokens}", ), ()),
        "prior-note-claims-message": (
            PRIOR_NOTE_CLAIMS_MESSAGE.format(claims='"2.1%"'),
            ("drop it", "search_news"), ()),
        "sentiment-agent-has-no-credibility-tool": (
            inspect.getsource(sentiment_module.SentimentAgent), (),
            ("AssessSourceCredibilityTool", ))
    }
    for case_id, (text, required, forbidden) in cases.items():
        for words in required:
            assert words in text, case_id
        for words in forbidden:
            assert words not in text, case_id


_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class _FixedDatetime(datetime):

    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


async def _amd_name(ticker: str) -> str:
    return "ADVANCED MICRO DEVICES INC"


async def _no_name(ticker: str) -> None:
    return None


def _provider_not_expected():
    raise AssertionError(
        "should not construct a provider when already ingested")


class _DigestNewsApi:

    def __init__(self, articles=None, raises=None):
        self._articles = articles or []
        self._raises = raises

    async def search_news(self, query, *, since=None, limit=8, sort_by=None):
        self.sort_by = sort_by
        if self._raises:
            raise self._raises
        return self._articles


_TOO_SHORT_ARTICLE = NewsArticle(
    title="Brief", source_name="Reuters", url="https://example.com/a",
    published_at=datetime(2026, 8, 1, tzinfo=UTC), summary="Short.")

_AMD_STORIES = {
    "Advanced Micro Devices posts record data center revenue": (
        "this_company", "company_business"),
    "Advanced Micro Devices shares jump after analyst upgrade": (
        "this_company", "stock_market"),
    "Best Advanced Micro Devices laptop deals this week": (
        "this_company", "consumer_product"),
    "Why I keep buying Advanced Micro Devices stock": (
        "this_company", "opinion_listicle")
}


def _amd_story(title: str) -> NewsArticle:
    return NewsArticle(
        title=title, source_name="Reuters",
        url="https://example.com/" + "-".join(title.split()[:4]),
        published_at=datetime(2026, 10, 3, tzinfo=UTC),
        summary="Advanced Micro Devices reported strong results. " * 10)


class _AmdStoriesProvider:

    def __init__(self) -> None:
        self.calls = 0

    async def search_news(self, query: str, **kw: Any) -> list[NewsArticle]:
        self.calls += 1
        return [_amd_story(t) for t in _AMD_STORIES]


class _FakeFredProvider:

    def __init__(self, points_by_indicator=None, raises_for=None):
        self._points_by_indicator = points_by_indicator or {}
        self._raises_for = raises_for or {}

    async def get_indicator(self, series, *, lookback_days=120):
        if series in self._raises_for:
            raise self._raises_for[series]
        return self._points_by_indicator.get(series, [])


class _ContentOnlyResponse:

    def __init__(self, content):
        self.content = content


class _NarrativeRouter:

    def __init__(self, content="A real macro narrative sentence here. " * 12):
        self._content = content

    async def complete(self, tier, messages, **kwargs):
        return _ContentOnlyResponse(self._content)


class _BrokenRouter:

    async def complete(self, *args, **kwargs):
        raise RuntimeError("LLM down")


_CPI = {
    "cpi": [
        MacroDataPoint(
            series_id="X", label="Consumer Price Index", value=310.2, unit="%",
            observed_at=date(2026, 7, 1), source="fred")
    ]
}


class TestNewsDigest:

    async def _no_digest(self, monkeypatch):
        cases = {
            "todays-digest-already-exists": (
                _provider_not_expected,
                {f"AMD_news_{_NOW.date().isoformat()}.txt"}),
            "no-articles": (lambda: _DigestNewsApi([]), set()),
            "too-few-words": (
                lambda: _DigestNewsApi([_TOO_SHORT_ARTICLE]), set()),
            "provider-error-is-not-raised": (
                lambda: _DigestNewsApi(raises=ProviderError("rate limited")),
                set())
        }
        monkeypatch.setattr(news_ingestion, "datetime", _FixedDatetime)
        monkeypatch.setattr(news_ingestion, "lookup_company_name", _amd_name)
        for case_id, (provider_factory, existing) in cases.items():
            monkeypatch.setattr(
                news_ingestion, "NewsApiProvider", provider_factory)
            result = await news_ingestion.generate_news_digest(
                "AMD", already_ingested_filenames=existing)
            assert result is None, case_id

    async def _non_english_articles_never_reach_the_digest(self, monkeypatch):
        kana = "".join(chr(c) for c in range(0x3042, 0x3062))
        japanese = NewsArticle(
            title=f"Mac Studio (M5 Max/Ultra) {kana} 7W",
            source_name="Applech2.com", url="u",
            published_at=datetime(2026, 9, 23, tzinfo=UTC), summary=kana)
        english = [_reuters(f"AMD headline {i}") for i in range(8)]
        seen: list[list[NewsArticle]] = []

        class _Provider:

            async def search_news(
                    self, query, *, since=None, limit=8, sort_by=None):
                return [japanese, *english]

        async def passthrough(articles, *, ticker, llm_router=None):
            seen.append(list(articles))
            return [ClassifiedArticle(a, COMPANY_BUSINESS) for a in articles]

        monkeypatch.setattr(news_ingestion, "NewsApiProvider", _Provider)
        monkeypatch.setattr(relevance_module, "classify_articles", passthrough)
        monkeypatch.setattr(news_ingestion, "lookup_company_name", _amd_name)
        monkeypatch.setattr(news_ingestion, "_MIN_WORD_COUNT", 1)
        digest = await news_ingestion.generate_news_digest(
            "AMD", already_ingested_filenames=set())
        assert digest is not None and digest.article_count == 8
        assert japanese not in seen[0]
        assert "Applech2" not in digest.text

    async def _stores_nothing_on_classifier_failure_then_stores_later(
            self, monkeypatch):
        monkeypatch.setattr(relevance_module, "lookup_company_name", _amd_name)
        monkeypatch.setattr(news_ingestion, "lookup_company_name", _amd_name)
        monkeypatch.setattr(news_ingestion, "_MIN_WORD_COUNT", 1)
        monkeypatch.setattr(settings, "NEWS_RELEVANCE_BATCH_SIZE", 10)
        provider = _AmdStoriesProvider()
        monkeypatch.setattr(
            news_ingestion, "NewsApiProvider", lambda: provider)

        failing = _ScriptedRouter([LLMError("429")])
        monkeypatch.setattr(relevance_module, "LLMRouter", lambda: failing)
        with capture_logs() as logs:
            first = await news_ingestion.generate_news_digest(
                "AMD", already_ingested_filenames=set())
        assert first is None
        assert any(
            e["event"] == "auto_ingestion.news_unverified_skipped"
            for e in logs)

        working = _ScriptedRouter([
            json.dumps([{
                "index": i,
                "title": _echo(t),
                "reasoning": "r",
                "subject": subject,
                "kind": kind
            } for i, (t, (subject, kind)) in enumerate(_AMD_STORIES.items())])
        ])
        monkeypatch.setattr(relevance_module, "LLMRouter", lambda: working)
        second = await news_ingestion.generate_news_digest(
            "AMD", already_ingested_filenames=set())
        assert second is not None
        assert second.article_count == 2
        assert "laptop deals" not in second.text
        assert "keep buying" not in second.text

    async def _without_company_name_no_news_request_is_sent(self, monkeypatch):
        monkeypatch.setattr(news_ingestion, "lookup_company_name", _no_name)
        provider = _AmdStoriesProvider()
        monkeypatch.setattr(
            news_ingestion, "NewsApiProvider", lambda: provider)
        with capture_logs() as logs:
            digest = await news_ingestion.generate_news_digest(
                "AMD", already_ingested_filenames=set())
        assert digest is None and provider.calls == 0
        assert any(
            e["event"] == "auto_ingestion.news_unverified_skipped"
            for e in logs)

    async def test_digest_is_skipped_filtered_or_stored(self):
        await _run_steps(
            self._no_digest, self._non_english_articles_never_reach_the_digest,
            self._stores_nothing_on_classifier_failure_then_stores_later,
            self._without_company_name_no_news_request_is_sent)


class TestMacroReport:

    async def test_generate_macro_report(self, monkeypatch):
        cases = {
            "this-months-report-already-exists": (
                _provider_not_expected, None,
                {f"macro_auto_{_NOW.strftime('%Y-%m')}.txt"}, False),
            "one-failing-indicator-does-not-abort": (
                lambda: _FakeFredProvider(
                    _CPI, raises_for={
                        "fed_funds_rate": ProviderError("rate limited")
                    }), _NarrativeRouter, set(), True),
            "no-indicators-available": (
                lambda: _FakeFredProvider({}), _NarrativeRouter, set(), False),
            "narrative-too-short": (
                lambda: _FakeFredProvider(_CPI),
                lambda: _NarrativeRouter(content="Too short."), set(), False),
            "llm-failure-is-not-raised": (
                lambda: _FakeFredProvider(_CPI), _BrokenRouter, set(), False)
        }
        monkeypatch.setattr(macro_ingestion, "datetime", _FixedDatetime)
        for case_id, (fred, router, existing, expect) in cases.items():
            monkeypatch.setattr(macro_ingestion, "FredProvider", fred)
            if router is not None:
                monkeypatch.setattr(macro_ingestion, "LLMRouter", router)
            result = await macro_ingestion.generate_macro_report(
                already_ingested_filenames=existing)
            assert (result is not None) is expect, case_id


def _dff(values: list[tuple[date, float]]) -> list[MacroDataPoint]:
    return [
        MacroDataPoint(
            series_id="DFF", label="Effective Federal Funds Rate (daily)",
            value=v, unit="%", observed_at=d, source="fred") for d, v in values
    ]


def _hike_series() -> list[MacroDataPoint]:
    values: list[tuple[date, float]] = [(date(2025, 9, 24), 4.09)]
    day = date(2026, 6, 1)
    while day <= date(2026, 9, 24):
        values.append((day, 3.88 if day >= date(2026, 9, 17) else 3.63))
        day += timedelta(days=1)
    return _dff(values)


class _FakeMacro:

    def __init__(self, points: list[MacroDataPoint]) -> None:
        self.points = points

    async def get_indicator(self, series, *, lookback_days=365):
        return self.points


class _ArchiveRetriever:

    def __init__(self, chunks):
        self.chunks = chunks

    async def retrieve(self, query, ticker=None):
        return self.chunks


def _macro_chunk(source: str | None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=str(uuid4()), text="Rates stay restrictive for now.",
        score=1.0, category=DocumentCategory.MACRO_RESEARCH, ticker="AMZN",
        document_id="d", source=source, content_date="2026-09-14")


class TestMacroResearch:

    async def test_policy_rate_moves_and_daily_series(self):
        text = await GetMacroIndicatorsTool(_FakeMacro(_hike_series())
                                            ).run("fed_funds_rate")
        assert text.startswith(
            "Effective Federal Funds Rate (daily) (DFF): latest=3.88% as of "
            "2026-09-24 (down 0.21 over the period, from 4.09%).")
        assert (
            "up 0.25 over the last 30 days (from 3.63% on 2026-08-25)" in text)
        assert (
            "up 0.25 over the last 90 days (from 3.63% on 2026-06-26)" in text)
        assert (
            "Last policy-rate move: up 0.25 on 2026-09-17 "
            "(3.63% -> 3.88%).") in text
        match = macro_module._FRED_LATEST_RE.search(text)
        assert match is not None and match.group("value") == "3.88"

        points = _dff([(date(2026, 9, d), 3.63 if d != 20 else 3.95)
                       for d in range(1, 25)])
        assert last_step_move(points) is None

        points = _dff([(date(2026, 9, d), 3.63) for d in range(1, 25)])
        text = await GetMacroIndicatorsTool(_FakeMacro(points)
                                            ).run("fed_funds_rate")
        assert (
            "Last policy-rate move: none of 0.20 or more since "
            "2026-09-01.") in text
        assert "Recent change" not in text

        cpi = [
            MacroDataPoint(
                series_id="CPIAUCSL", label="CPI", value=v, unit=None,
                observed_at=d, source="fred")
            for d, v in [(date(2025, 8, 1),
                          324.25), (date(2026, 7, 1),
                                    333.0), (date(2026, 8, 1), 334.13)]
        ]
        text = await GetMacroIndicatorsTool(_FakeMacro(cpi)).run("cpi")
        assert "policy-rate" not in text
        assert (
            "up 1.13 over the last 30 days (from 333.00 on 2026-07-01)"
            in text)
        assert _SERIES_ALIASES["fed_funds_rate"][0] == "DFF"
        assert "DFF" in _PERCENT_SERIES_IDS
        assert macro_module._LIVE_SERIES[0].series_id == "DFF"

    async def test_agent_note_guidance_and_recycled_notes(self, monkeypatch):

        def tool(chunks):
            return SearchResearchArchiveTool(
                name="search_prior_macro_notes", description="d",
                category=DocumentCategory.MACRO_RESEARCH, ticker=None,
                retriever=_ArchiveRetriever(chunks))

        with_agent = await tool(
            [_macro_chunk("agent:macro"),
             _macro_chunk(None)]).run("fed")
        assert AGENT_NOTE_GUIDANCE in with_agent
        assert "[agent:macro]" in with_agent
        seed_only = await tool([_macro_chunk(None)]).run("fed")
        assert AGENT_NOTE_GUIDANCE not in seed_only

        import ai.knowledge.archival as archival_module

        def _must_not_run(*args, **kwargs):
            raise AssertionError("a recycled note must not be chunked")

        monkeypatch.setattr(archival_module, "chunk_document", _must_not_run)
        content = " ".join(["macro"] * 60)
        await archival_module.archive_agent_report(
            analysis_id=uuid4(), role=AgentRole.MACRO, ticker="AMD",
            content=content, recycled_share=0.51)
        flagged = AgentOutput(
            role=AgentRole.MACRO, summary="x", score=0.9, raw_output={
                "scratchpad": [],
                "reused_prior_note": True
            })
        assert _archival_recycled_share(flagged) == 1.0
        plain = AgentOutput(
            role=AgentRole.MACRO, summary="x", score=0.9,
            raw_output={"scratchpad": []})
        assert _archival_recycled_share(plain) == 0.0


class _JpmMarket:

    async def get_current_price(self, ticker):
        prices = {"JPM": "343.06", "MSFT": "500.00"}
        return PriceQuote(
            ticker=ticker, price=Decimal(prices[ticker]),
            as_of=datetime.now(UTC), source="fake")


@pytest.fixture
def jpm_env(monkeypatch):

    async def _holdings(portfolio_id):
        return [{"ticker": "MSFT", "quantity": 20.0}]

    async def _cash(portfolio_id):
        return 7699.0

    monkeypatch.setattr(
        sizing_module, "get_market_data_provider", lambda: _JpmMarket())
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _holdings)
    monkeypatch.setattr(sizing_module, "get_portfolio_cash_balance", _cash)
    monkeypatch.setattr(sizing_module.settings, "TIER_MIN_NOTIONAL_USD", 100.0)
    monkeypatch.setattr(
        sizing_module.settings, "TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION", 0.01)


def _tiers(pairs, window=5) -> str:
    return json.dumps({
        "window_days": window,
        "tiers": [{
            "target_price": p,
            "quantity": q
        } for p, q in pairs]
    })


async def _size_jpm(router, committed: Decimal):
    return await generate_position_sizing(
        ticker="JPM", action=RecommendationAction.BUY, confidence=0.8,
        reasoning="r", portfolio_id="p", committed_buy_cash=committed,
        llm_router=router)


_CLEAN_REVIEW = json.dumps({
    "checked_claims": ["close 100.0"],
    "issues": [],
    "retry_roles": {}
})


class _FailingReviewRouter:

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = list(errors)
        self.kwargs: list[dict] = []

    async def complete(self, tier, messages, **kwargs):
        self.kwargs.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return _response(_CLEAN_REVIEW, finish=None)


def _market_state() -> AnalysisState:
    market = AgentOutput(
        role=AgentRole.MARKET, summary="Close 100.0", score=0.9, raw_output={
            "scratchpad": [{
                "thought": "t",
                "action": "get_price_history",
                "action_input": {},
                "observation": "Close 100.0",
                "is_tool_result": True,
                "tool_error": False
            }]
        })
    return AnalysisState(
        analysis_id="a1", user_id="u1", ticker="KO",
        analysis_type=AnalysisType.FULL,
        agent_outputs={AgentRole.MARKET: market})


def _review_sleeps(monkeypatch):
    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    async def _no_free_model(tier):
        return None

    monkeypatch.setattr(reflection_module.asyncio, "sleep", _sleep)
    monkeypatch.setattr(capacity_module, "free_chain_model", _no_free_model)
    monkeypatch.setattr(
        reflection_module.settings, "REFLECTION_REVIEW_DELAYED_RETRY_SECONDS",
        60.0)
    return sleeps


class _FlakyQdrant:

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def upsert(self, **kwargs: Any) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ResponseHandlingException(
                Exception("Server disconnected without sending a response."))


class _Bm25:

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add_many(self, items: list[Any]) -> None:
        self.added.extend(items)


async def _index_one_chunk(monkeypatch, failures: int):
    qdrant, bm25 = _FlakyQdrant(failures), _Bm25()
    monkeypatch.setattr(indexing_module, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(indexing_module, "get_bm25_index", lambda: bm25)
    monkeypatch.setattr(indexing_module, "_UPSERT_RETRY_DELAY_SECONDS", 0.0)
    await indexing_module.index_chunks(
        document_id="d1", ticker="NVDA", category=DocumentCategory.NEWS,
        chunks=[
            Chunk(text="t", chunk_index=0, category=DocumentCategory.NEWS)
        ], embeddings=[[0.1, 0.2]], visibility=ChunkVisibility.system())
    return qdrant, bm25


class TestAnalysisPlumbing:

    async def test_tiers_below_the_minimum_size(self, jpm_env):
        cases = {
            "leftover-budget-below-minimum": ([
                (343.06, 0.0039)
            ], Decimal("7697.66"), "below the minimum tier size of 176.99",
                                              False),
            "all-tiers-too-small": (
                [(343.06, 0.2), (330.0, 0.1)], Decimal(0),
                "every proposed tier was below the minimum tier size", True)
        }
        for case_id, (tiers, committed, note_part, asks) in cases.items():
            router = _ScriptedReviewRouter(_tiers(tiers))
            result, note = await _size_jpm(router, committed)
            assert result is None, case_id
            assert note_part in note, case_id
            assert bool(router.calls) is asks, case_id

        router = _ScriptedReviewRouter(_tiers([(343.06, 1.5), (330.0, 0.1)]))
        result, note = await _size_jpm(router, Decimal(0))
        assert note is not None
        assert note.startswith("Tier sizing (computed in code):")
        assert result is not None
        assert [(t.target_price, t.quantity)
                for t in result.tiers] == [(Decimal("343.06"), Decimal("1.5"))]
        user_text = router.calls[0][1].content
        assert "Minimum size of each BUY tier" in user_text
        assert "176.99" in user_text

    async def _review_is_retried_once_after_a_server_error(self, monkeypatch):
        review_sleeps = _review_sleeps(monkeypatch)
        router = _FailingReviewRouter([LLMServerOverloadedError("503")])
        monkeypatch.setattr(reflection_module, "LLMRouter", lambda: router)
        verdict = await ReflectionAgent().check(_market_state())
        assert verdict.llm_review_ran is True
        assert review_sleeps == [60.0]
        assert len(router.kwargs) == 2

        router = _FailingReviewRouter(
            [LLMRateLimitError("429"),
             LLMRateLimitError("429")])
        monkeypatch.setattr(reflection_module, "LLMRouter", lambda: router)
        verdict = await ReflectionAgent().check(_market_state())
        assert verdict.llm_review_ran is False
        assert verdict.verified is False
        assert len(router.kwargs) == 2

    async def _review_retry_skips_the_wait_when_a_chain_model_is_free(
            self, monkeypatch):
        review_sleeps = _review_sleeps(monkeypatch)

        async def _free(tier):
            return "gemini-3.6-flash"

        monkeypatch.setattr(capacity_module, "free_chain_model", _free)
        router = _FailingReviewRouter([LLMServerOverloadedError("503")])
        monkeypatch.setattr(reflection_module, "LLMRouter", lambda: router)
        with capture_logs() as logs:
            verdict = await ReflectionAgent().check(_market_state())
        assert verdict.llm_review_ran is True
        assert review_sleeps == [] and len(router.kwargs) == 2
        retry = next(
            e for e in logs
            if e["event"] == "reflection.citation_review_delayed_retry")
        assert retry["delay_seconds"] == 0.0
        assert retry["free_model"] == "gemini-3.6-flash"

    async def test_review_retry_after_server_errors(self):
        await _run_steps(
            self._review_is_retried_once_after_a_server_error,
            self._review_retry_skips_the_wait_when_a_chain_model_is_free)

    async def test_upsert_retry_and_stream_gate_limit(self, monkeypatch):
        qdrant, bm25 = await _index_one_chunk(monkeypatch, failures=1)
        assert qdrant.calls == 2 and len(bm25.added) == 1
        with pytest.raises(ResponseHandlingException):
            await _index_one_chunk(monkeypatch, failures=2)

        monkeypatch.setattr(
            settings, "LLM_GOOGLE_LITE_POOL_MAX_WAIT_SECONDS", 30.0)
        monkeypatch.setattr(
            settings, "LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS", 70.0)
        seen: list[float | None] = []

        async def _reserve(
                candidates: Any, min_interval: float,
                **kw: Any) -> GatedReservation:
            seen.append(kw["gate_max_wait_seconds"])
            return GatedReservation(
                key=candidates[0], wait=0.0, gate_wait=45.0, reserved=False)

        class _Client:

            class aio:

                class models:

                    @staticmethod
                    async def generate_content_stream(**kwargs: Any) -> Any:
                        raise AssertionError("must not dispatch")

        pool = google_provider._pool
        monkeypatch.setattr(
            google_provider.request_pacer, "reserve_best_slot_gated", _reserve)
        monkeypatch.setattr(pool, "key_count", lambda: 1)
        monkeypatch.setattr(
            pool, "available_labels_excluding", lambda tried: []
            if tried else ["k1"])
        monkeypatch.setattr(pool, "client_for", lambda label: _Client())
        with capture_logs() as logs, pytest.raises(LLMError):
            async for _ in google_provider.GoogleAdapter().stream(
                [LLMMessage(role="user", content="q")],
                    model="gemini-test-lite"):
                pass
        assert seen == [30.0]
        event = next(
            e for e in logs if e["event"] ==
            "google.pool_pacing_wait_exceeds_threshold_failing_fast")
        assert event["max_wait_seconds"] == 30.0


_REMOVED = [(settings, "NEWS_BLOCKED_DOMAINS"),
            (settings, "NEWS_DOWNWEIGHT_DOMAINS"),
            (settings, "NEWS_DOWNWEIGHT_MAX_ITEMS"),
            (settings, "NEWS_HOMONYM_TICKER_MAX_LEN"),
            (nodes_module.settings, "DATA_AGENT_RATE_LIMIT_RETRY_SECONDS"),
            (filters_module, "is_blocked_domain"),
            (filters_module, "is_ticker_homonym"),
            (filters_module, "non_news_reason"),
            (filters_module, "cap_downweighted"),
            (filters_module, "_GENERIC_NAME_WORDS"),
            (filters_module, "_SHOPPING_TITLE"),
            (filters_module, "_FORECAST_TITLE"),
            (filters_module, "_BOILERPLATE_TEXT"),
            (filters_module, "headline_mentions_company"),
            (relevance_module, "_reasoning_contradicts_relevant"),
            (relevance_module, "_reasoning_affirms_company"),
            (relevance_module, "_keyword_prefilter"),
            (search_relevance_module, "_BLOCKED_DOMAINS"),
            (news_tool_module, "_REPUTABLE_DOMAINS"),
            (news_tool_module, "DOWNWEIGHTED_HEADER")]


def test_removed_names_defaults_and_shared_objects():
    for owner, name in _REMOVED:
        assert not hasattr(owner, name), name
    defaults = {
        "consumer-items-cap": (settings, "NEWS_CONSUMER_MAX_ITEMS", 2),
        "newsapi-searches-titles-and-descriptions": (
            settings, "NEWSAPI_SEARCH_IN", "title,description"),
        "newsapi-sorts-by-relevancy": (
            settings, "NEWSAPI_SORT_BY", "relevancy"),
        "react-output-cap": (loop_module, "_REACT_LOOP_MAX_TOKENS", 4096)
    }
    for case_id, (owner, name, expected) in defaults.items():
        assert getattr(owner, name) == expected, case_id
    assert web_tool_module.LINK_UNAVAILABLE is (
        news_tool_module.LINK_UNAVAILABLE)
    assert relevance_module.headline_names_company is (
        filters_module.headline_names_company)


def _japanese_article() -> NewsArticle:
    kana = "".join(chr(c) for c in range(0x3042, 0x3062))
    return NewsArticle(
        title=f"Mac Studio (M5 Max/Ultra) {kana} 7W",
        source_name="Applech2.com", url="j",
        published_at=datetime(2026, 9, 23, tzinfo=UTC), summary=kana)


async def _must_not_classify(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("must not classify")


class TestCleanArticles:

    async def test_non_english_drop_and_classifier_use(self, monkeypatch):
        assert await relevance_module.classify_articles([], ticker="AMD") == []
        english = _reuters("AMD headline")
        dropped: list[int] = []
        monkeypatch.setattr(
            relevance_module, "classify_articles", _must_not_classify)
        cleaned = await relevance_module.clean_articles(
            [_japanese_article(), english], None,
            on_non_english_dropped=dropped.append)
        assert dropped == [1]
        assert cleaned.english == [english]
        assert cleaned.classified == [
            ClassifiedArticle(english, "unclassified")
        ]

        dropped: list[int] = []
        monkeypatch.setattr(
            relevance_module, "classify_articles", _must_not_classify)
        cleaned = await relevance_module.clean_articles(
            [_japanese_article()], "AMD",
            on_non_english_dropped=dropped.append)
        assert dropped == [1]
        assert cleaned.english == []
        assert cleaned.classified == []

        english = [_reuters("AMD headline 1"), _reuters("AMD headline 2")]
        seen: list[tuple[list[NewsArticle], str]] = []

        async def _classify(articles, *, ticker, llm_router=None):
            seen.append((list(articles), ticker))
            return [ClassifiedArticle(a, COMPANY_BUSINESS) for a in articles]

        dropped: list[int] = []
        monkeypatch.setattr(relevance_module, "classify_articles", _classify)
        cleaned = await relevance_module.clean_articles(
            english, "AMD", on_non_english_dropped=dropped.append)
        assert dropped == []
        assert seen == [(english, "AMD")]
        assert [c.kind for c in cleaned.classified
                ] == [COMPANY_BUSINESS, COMPANY_BUSINESS]


class _KwargsRouter:

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[Any, list[Any], dict[str, Any]]] = []

    async def complete(
            self, tier: Any, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append((tier, list(messages), kwargs))
        return _response(self.answer)


class TestVerifyWithCheapTier:

    async def test_fail_open_one_call_and_misaligned_verdicts(self):
        router = _ScriptedRouter([LLMError("429")])
        with capture_logs() as logs:
            out = await relevance_module.verify_with_cheap_tier(
                router, system_prompt="s", numbered="0. t", titles=["t"],
                log_prefix="probe", query="q")
        assert out is None
        assert [e["event"] for e in logs] == ["probe.verification_unavailable"]

        router = _KwargsRouter(
            json.dumps([{
                "index": 0,
                "title": "AMD beats",
                "kind": "x"
            }]))
        out = await relevance_module.verify_with_cheap_tier(
            router, system_prompt="sys", numbered="0. AMD beats estimates",
            titles=["AMD beats estimates"], log_prefix="probe",
            response_format="json_object")
        assert out is not None
        assert out.unreadable == frozenset()
        assert out.trusted[0]["kind"] == "x"
        [(tier, messages, kwargs)] = router.calls
        assert tier == LLMTier.CHEAP
        assert [(m.role, m.content)
                for m in messages] == [("system", "sys"),
                                       ("user", "0. AMD beats estimates")]
        assert kwargs["response_format"] == "json_object"
        assert kwargs["retries"] == AGENT_LLM_CALL_RETRIES

        router = _KwargsRouter(
            json.dumps([{
                "index": 0,
                "title": "nothing alike",
                "kind": "x"
            }]))
        with capture_logs() as logs:
            out = await relevance_module.verify_with_cheap_tier(
                router, system_prompt="sys", numbered="0. AMD beats estimates",
                titles=["AMD beats estimates"], log_prefix="probe",
                ticker="AMD")
        assert out is not None and out.trusted == {}
        event = next(
            e for e in logs if e["event"] == "probe.verdicts_misaligned")
        assert event["ticker"] == "AMD"
        assert event["total_checked"] == 1
        assert event["kept_unverified"] == 1
