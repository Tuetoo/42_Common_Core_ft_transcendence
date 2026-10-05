from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
import structlog
from ai.llm.router import LLMRouter
from ai.providers.base import WebSearchResult
from ai.providers.news_filters import (SNIPPET_CHARS, company_search_name,
                                       drop_prechecked, relink_consent_items)
from ai.providers.news_relevance import (NEWS_KINDS, NOT_NEWS, PRICE_PAGE,
                                         REFERENCE_OR_SOCIAL, UNCLASSIFIED,
                                         UNVERIFIED, kind_definitions_text,
                                         lookup_company_name,
                                         verify_with_cheap_tier)
from ai.providers.newsapi import url_host

logger = structlog.get_logger(__name__)

WEB_KINDS = (*NEWS_KINDS, REFERENCE_OR_SOCIAL)
_DROPPED_KINDS = frozenset({PRICE_PAGE, NOT_NEWS, REFERENCE_OR_SOCIAL})


@dataclass(frozen=True)
class ClassifiedResult:
    result: WebSearchResult
    kind: str


def _system_prompt(query: str, subject: str | None) -> str:
    context = (
        f" The search is part of research on {subject}." if subject else "")
    return (
        f"The search query was: {query!r}.{context}\n\n"
        "For each numbered search result give:\n"
        '- "reasoning": one short sentence about that result, written first;\n'
        '- "relevant": true when the result really addresses what the query '
        "was looking for, false when it only shares a keyword while being "
        "about something else (an ordinary word, an unrelated business or "
        "person with the same name, a generic page that never addresses the "
        "query's subject);\n"
        '- "kind", exactly one of:\n'
        f"{kind_definitions_text(WEB_KINDS)}\n\n"
        'Respond with ONLY a JSON object whose "relevant" key holds one '
        'object per result, in the order given: {"relevant": [{"index": '
        '0, "title": "first five words of that result\'s title, copied '
        'without any quotation marks", "reasoning": "...", "relevant": '
        'true, "kind": "..."}, ...]}. No text outside the JSON object. '
        "Never put a double quote character inside a string value (use "
        "single quotes there), so the object stays valid JSON.")


def _verdict(item: dict[str, Any]) -> tuple[bool, str] | None:
    relevant = item.get("relevant")
    if isinstance(relevant, str):
        relevant = {"true": True, "false": False}.get(relevant.strip().lower())
    kind = str(item.get("kind", "")).strip().lower()
    if not isinstance(relevant, bool) or kind not in WEB_KINDS:
        return None
    return relevant, kind


async def _classify_results(
        query: str, results: list[WebSearchResult], llm_router: LLMRouter,
        subject: str | None) -> list[ClassifiedResult]:
    verdicts = await verify_with_cheap_tier(
        llm_router, system_prompt=_system_prompt(query, subject),
        numbered="\n".join(
            f"{i}. {r.title} ({url_host(r.url)}): {r.snippet}"
            for i, r in enumerate(results)), titles=[r.title for r in results],
        log_prefix="search_relevance", response_format="json_object",
        query=query)
    if verdicts is None:
        return [ClassifiedResult(r, UNCLASSIFIED) for r in results]
    kept: list[ClassifiedResult] = []
    dropped: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for i, result in enumerate(results):
        if i in verdicts.unreadable:
            kept.append(ClassifiedResult(result, UNVERIFIED))
            counts[UNVERIFIED] += 1
            continue
        item = verdicts.trusted.get(i)
        verdict = _verdict(item) if item is not None else None
        if verdict is None:
            kept.append(ClassifiedResult(result, UNCLASSIFIED))
            counts[UNCLASSIFIED] += 1
            continue
        relevant, kind = verdict
        counts[f"{'relevant' if relevant else 'irrelevant'}/{kind}"] += 1
        if relevant and kind not in _DROPPED_KINDS:
            kept.append(ClassifiedResult(result, kind))
            continue
        dropped.append({
            "title": result.title[:SNIPPET_CHARS],
            "relevant": relevant,
            "kind": kind,
            "reasoning": (
                str(item.get("reasoning", ""))[:160] if item else "")
        })
    logger.info(
        "search_relevance.classified", query=query, total_checked=len(results),
        by_verdict=dict(counts), dropped_count=len(dropped), dropped=dropped)
    return kept


async def classify_results(
        query: str, results: Sequence[WebSearchResult], *,
        llm_router: LLMRouter | None = None,
        ticker: str | None = None) -> list[ClassifiedResult]:
    if not results:
        return []
    kept = drop_prechecked(
        list(results), title_of=lambda r: r.title,
        log_event="search_relevance.prechecked", min_title_words=1,
        query=query, ticker=ticker)
    if not kept:
        return []
    kept = relink_consent_items(
        kept, title_of=lambda r: r.title,
        log_event="search_relevance.consent_link_replaced", query=query,
        ticker=ticker)
    subject: str | None = None
    if ticker is not None:
        legal_name = await lookup_company_name(ticker)
        name = company_search_name(legal_name) if legal_name else None
        subject = f"{name} (ticker {ticker})" if name else f"ticker {ticker}"
    return await _classify_results(
        query, kept, llm_router or LLMRouter(), subject)
