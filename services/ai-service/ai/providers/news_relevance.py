import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import parse_qs, unquote, urlparse
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage,
                         LLMResponse, LLMTier, parse_json_list,
                         strip_json_code_fence)
from ai.llm.router import LLMRouter
from ai.providers.base import NewsArticle
from ai.providers.sec_edgar import load_ticker_to_title
from config import settings
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

SNIPPET_CHARS = 120

_LEGAL_SUFFIX_WORDS = frozenset({
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD",
    "LIMITED", "LLC", "LP", "PLC", "HOLDINGS", "HOLDING", "GROUP", "COM", "SA",
    "NV", "AG", "SE", "THE"
})


@dataclass(frozen=True)
class CompanyAliases:
    ticker: str
    strong: tuple[str, ...]


def core_name_words(legal_name: str) -> list[str]:
    name = re.sub(r"/[A-Za-z]{1,4}\s*$", "", legal_name.strip())
    words = re.findall(r"[A-Za-z0-9&']+(?:[-.][A-Za-z0-9]+)*", name.upper())
    words = [w.replace("-", " ").replace(".", " ") for w in words]
    flat = [part for w in words for part in w.split() if part]
    while len(flat) > 1 and flat[-1] in _LEGAL_SUFFIX_WORDS | {"&", "AND"}:
        flat.pop()
    return flat or [legal_name.strip().upper()]


def company_search_name(legal_name: str) -> str:
    return " ".join(core_name_words(legal_name)).title()


def company_aliases(ticker: str, legal_name: str | None) -> CompanyAliases:
    ticker = ticker.upper()
    if not legal_name or not legal_name.strip():
        return CompanyAliases(ticker=ticker, strong=())
    words = core_name_words(legal_name)
    strong = [" ".join(words).lower()]
    if len(words) > 1 and all(w.isalpha() for w in words):
        strong.append("".join(words).lower())
    return CompanyAliases(ticker=ticker, strong=tuple(strong))


_DOTTED_INITIALS = re.compile(r"(?<![a-z0-9])(?:[a-z]\.\s?){2,}(?=[a-z])")


def _normalize(text: str) -> str:
    text = text.lower().replace("\u2019", "'")
    return re.sub(r"(?<=[a-z0-9])[-\u2010\u2011\u2013](?=[a-z0-9])", " ", text)


def names_alias(text: str, aliases: Sequence[str]) -> bool:
    normalized = _normalize(text)
    collapsed = _DOTTED_INITIALS.sub(
        lambda m: m.group(0).replace(".", "").replace(" ", ""), normalized)
    for alias in aliases:
        pattern = r"\s+".join(re.escape(part) for part in alias.split())
        regex = rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"
        if re.search(regex, normalized) or re.search(regex, collapsed):
            return True
    return False


def headline_names_company(title: str, aliases: CompanyAliases) -> bool:
    return names_alias(title, aliases.strong)


_GENERIC_QUERY_WORDS = frozenset({
    "news", "latest", "recent", "recently", "today", "this", "week", "month",
    "update", "updates", "stock", "stocks", "share", "shares", "price",
    "company", "corporate", "development", "developments", "material",
    "headline", "headlines", "and", "or", "not", "the", "of", "for", "on",
    "in", "about", "to", "a", "an", "with", "from", "inc", "corp",
    "corporation", "report", "reports", "analysis", "current", "market",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december"
})
_MAX_TOPIC_WORDS = 4


def build_ticker_news_query(
        model_query: str, *, ticker: str, company_name: str | None) -> str:
    name_words = set(re.findall(r"[a-z0-9]+", (company_name or "").lower()))
    topic: list[str] = []
    words = [
        part
        for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9&.-]*", model_query)
        for part in word.split("-") if part
    ]
    for word in words:
        lowered = word.lower().strip(".")
        if (len(lowered) < 3 or lowered == ticker.lower()
                or lowered in name_words or lowered in _GENERIC_QUERY_WORDS
                or re.fullmatch(r"\d{4}", lowered)):
            continue
        if lowered not in topic:
            topic.append(lowered)
    entity_parts: list[str] = []
    if len(ticker) >= 2:
        entity_parts.append(ticker.upper())
    if company_name:
        entity_parts.append(f'"{company_name.title()}"')
    entity = " OR ".join(entity_parts) or ticker.upper()
    if not topic:
        return f"({entity})"
    return f"({entity}) AND ({' '.join(topic[:_MAX_TOPIC_WORDS])})"


_PACKAGE_VERSION_TITLE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*\s+v?\d+(?:\.\d+){1,3}(?:[a-z0-9.+-]*)$")
_CONSENT_HOST = re.compile(r"^(?:consent|guce)\.")
_MIN_USABLE_TITLE_WORDS = 3
_PLACEHOLDER_TITLES = frozenset(
    {"(untitled)", "[removed]", "untitled", "removed"})


def is_package_version_title(title: str) -> bool:
    return _PACKAGE_VERSION_TITLE.match(title.strip()) is not None


def is_consent_url(url: str) -> bool:
    return bool(_CONSENT_HOST.match((urlparse(url).hostname or "").lower()))


def has_usable_title(
        title: str, *, min_words: int = _MIN_USABLE_TITLE_WORDS) -> bool:
    stripped = title.strip()
    if stripped.lower() in _PLACEHOLDER_TITLES:
        return False
    return len(re.findall(r"[A-Za-z0-9$%']{2,}",
                          stripped)) >= max(1, min_words)


def precheck_reason(
        title: str, *, min_words: int = _MIN_USABLE_TITLE_WORDS) -> str | None:
    if not has_usable_title(title, min_words=min_words):
        return "unusable_title"
    if is_package_version_title(title):
        return "package_version_title"
    return None


def drop_prechecked(
        items: Sequence[T], *, title_of: Callable[[T], str], log_event: str,
        min_title_words: int = _MIN_USABLE_TITLE_WORDS,
        **log_context: object) -> list[T]:
    kept: list[T] = []
    reasons: Counter[str] = Counter()
    dropped: list[dict[str, str]] = []
    for item in items:
        title = ""
        try:
            title = title_of(item)
            reason = precheck_reason(title, min_words=min_title_words)
        except Exception as exc:
            logger.warning(f"{log_event}_error", error=str(exc), **log_context)
            reason = None
        if reason is None:
            kept.append(item)
            continue
        reasons[reason] += 1
        dropped.append({"title": title[:SNIPPET_CHARS], "reason": reason})
    if dropped:
        logger.info(
            log_event, dropped_count=len(dropped), total=len(items),
            by_reason=dict(reasons), dropped=dropped, **log_context)
    return kept


def consent_target_url(url: str) -> str | None:
    for values in parse_qs(urlparse(url).query).values():
        for value in values:
            candidate = unquote(value).strip()
            parsed = urlparse(candidate)
            if (parsed.scheme in ("http", "https") and parsed.hostname
                    and not is_consent_url(candidate)):
                return candidate
    return None


def relink_consent_items(
        items: Sequence[M], *, title_of: Callable[[M], str], log_event: str,
        **log_context: object) -> list[M]:
    out: list[M] = []
    changed: list[dict[str, str]] = []
    for item in items:
        try:
            url = str(getattr(item, "url", "") or "")
            if not is_consent_url(url):
                out.append(item)
                continue
            target = consent_target_url(url) or ""
            out.append(item.model_copy(update={"url": target}))
            changed.append({
                "title": title_of(item)[:SNIPPET_CHARS],
                "link": "recovered" if target else "unavailable"
            })
        except Exception as exc:
            logger.warning(f"{log_event}_error", error=str(exc), **log_context)
            out.append(item)
    if changed:
        logger.info(
            log_event, count=len(changed), items=changed, **log_context)
    return out


async def lookup_company_name(ticker: str) -> str | None:
    ticker = ticker.upper()
    if not settings.SEC_EDGAR_USER_AGENT:
        logger.info(
            "news_relevance.company_name_lookup_skipped", ticker=ticker,
            reason="SEC_EDGAR_USER_AGENT not set")
        return None
    try:
        titles = await load_ticker_to_title()
    except Exception as exc:
        logger.info(
            "news_relevance.company_name_lookup_failed", ticker=ticker,
            error=str(exc))
        return None
    return titles.get(ticker) or None


async def lookup_search_name(ticker: str) -> str | None:
    legal_name = await lookup_company_name(ticker)
    return company_search_name(legal_name) if legal_name else None


THIS_COMPANY = "this_company"
PARTY = "party"
SUBJECTS = ("this_company", PARTY, "other_company", "none")
KEPT_SUBJECTS = frozenset({THIS_COMPANY, PARTY})

COMPANY_BUSINESS = "company_business"
STOCK_MARKET = "stock_market"
CONSUMER_PRODUCT = "consumer_product"
OPINION_LISTICLE = "opinion_listicle"
PRICE_PAGE = "price_page"
NOT_NEWS = "not_news"
REFERENCE_OR_SOCIAL = "reference_or_social"
UNCLASSIFIED = "unclassified"
UNVERIFIED = "unverified"

KIND_DEFINITIONS: dict[str, str] = {
    COMPANY_BUSINESS: (
        "reporting on a business, an institution or the economy: results, "
        "guidance, deals, management, strategy, operations, workforce, "
        "product or pricing decisions, legal, regulatory or policy matters"),
    STOCK_MARKET: (
        "a company's stock or other securities: price moves, trading, "
        "analyst ratings and price targets, ownership, index changes"),
    CONSUMER_PRODUCT: (
        "content written for consumers: product reviews, buying advice, "
        "deals and discounts, fan, entertainment, travel, food or hobby "
        "content, how-to guides"),
    OPINION_LISTICLE: (
        "opinion or investing commentary, including ranked or listed "
        "stock picks"),
    PRICE_PAGE: (
        "an automatically generated quote, chart or price-prediction page "
        "for a security or asset, with no reported event"),
    NOT_NEWS: (
        "not a news or commentary article: software or package release "
        "pages, file or media release listings, forum posts, product "
        "listings, error, login or consent pages"),
    REFERENCE_OR_SOCIAL: (
        "a dictionary, encyclopedia or calendar page, or a social-media "
        "post or profile")
}
NEWS_KINDS = (
    COMPANY_BUSINESS, STOCK_MARKET, CONSUMER_PRODUCT, OPINION_LISTICLE,
    PRICE_PAGE, NOT_NEWS)
KEPT_KINDS = frozenset({COMPANY_BUSINESS, STOCK_MARKET, OPINION_LISTICLE})
EVIDENCE_KINDS = frozenset({COMPANY_BUSINESS, STOCK_MARKET, UNCLASSIFIED})
CONFIRMED_EVIDENCE_KINDS = EVIDENCE_KINDS - {UNCLASSIFIED}


def kind_definitions_text(kinds: Sequence[str]) -> str:
    return "\n".join(f"- {kind}: {KIND_DEFINITIONS[kind]}" for kind in kinds)


@dataclass(frozen=True)
class ClassifiedArticle:
    article: NewsArticle
    kind: str
    checked: bool = True


_VERIFY_MAX_TOKENS = 4096
_RETRY_MAX_TOKENS = 2 * _VERIFY_MAX_TOKENS

_ECHO_MIN_OVERLAP = 0.6
_ECHO_WORDS = 6


def _title_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _echo_score(echo: list[str], title: str) -> float:
    if not echo:
        return 0.0
    title_words = set(_title_tokens(title))
    return sum(w in title_words for w in echo) / len(echo)


def _echo_matches(echo: str, idx: int, titles: Sequence[str]) -> bool | None:
    words = _title_tokens(echo)[:_ECHO_WORDS]
    if not words:
        return None
    own = _echo_score(words, titles[idx])
    if own < _ECHO_MIN_OVERLAP:
        return False
    return all(
        _echo_score(words, other) <= own for j, other in enumerate(titles)
        if j != idx)


def align_verdicts(
        parsed: Sequence[Any], titles: Sequence[str]
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    n = len(titles)
    by_index: dict[int, list[dict[str, Any]]] = {}
    invalid = 0
    out_of_range: list[Any] = []
    for item in parsed:
        if not isinstance(item, dict):
            invalid += 1
            continue
        idx = item.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            invalid += 1
            continue
        if not 0 <= idx < n:
            out_of_range.append(idx)
            continue
        by_index.setdefault(idx, []).append(item)
    duplicates = sorted(i for i, items in by_index.items() if len(items) > 1)
    missing = [i for i in range(n) if i not in by_index]
    trusted: dict[int, dict[str, Any]] = {}
    mismatched: list[dict[str, Any]] = []
    no_echo: list[int] = []
    for idx, items in by_index.items():
        if len(items) > 1:
            continue
        item = items[0]
        echo = item.get("title")
        verdict = (
            _echo_matches(echo, idx, titles)
            if isinstance(echo, str) else None)
        if verdict is None:
            no_echo.append(idx)
        elif verdict:
            trusted[idx] = item
        else:
            mismatched.append({
                "index": idx,
                "title": titles[idx][:100],
                "echo": str(echo)[:100]
            })
    consistent = not (
        duplicates or missing or out_of_range or invalid or mismatched)
    if consistent:
        for idx in no_echo:
            trusted[idx] = by_index[idx][0]
    issues: dict[str, Any] = {}
    if mismatched:
        issues["mismatched"] = mismatched
    if duplicates:
        issues["duplicate_indices"] = duplicates
    if missing:
        issues["missing_indices"] = missing
    if out_of_range:
        issues["out_of_range_indices"] = out_of_range[:20]
    if invalid:
        issues["invalid_entries"] = invalid
    if no_echo and not consistent:
        issues["untrusted_without_echo"] = sorted(no_echo)
    return trusted, issues


def _system_prompt(*, ticker: str, company_name: str) -> str:
    return (
        "You classify news items for an investment research system. The "
        f"company under analysis is {company_name} (ticker {ticker}).\n\n"
        "For each numbered item give:\n"
        f'- "subject": "this_company" when the main subject of the item is '
        f"{company_name} itself (including its own units, brands, products "
        f'or stock); "party" when {company_name} is one of several parties '
        "the item reports on: a counterparty, customer or supplier in the "
        "reported deal or event, a co-defendant or litigant in a case, one "
        "of the companies a comparison or a sector move names and "
        f"discusses, or the maker of a product at the center of the "
        f'reported event; "other_company" when the main subject is a '
        f"different company, person or organization and {company_name} is "
        "only mentioned in passing (as background or an example, with "
        'nothing reported about it); "none" when the item has no company '
        "as its subject, or when the name or ticker letters mean something "
        "else (an ordinary word, an abbreviation, another name).\n"
        '- "kind", exactly one of:\n'
        f"{kind_definitions_text(NEWS_KINDS)}\n"
        '- "reasoning": one short sentence about that item, written before '
        "you choose subject and kind.\n\n"
        'Respond with exactly one JSON array: [{"index": 0, "title": '
        "\"first five words of that item's title, copied without any "
        'quotation marks", "reasoning": "...", "subject": "...", "kind": '
        '"..."}, ...] -- exactly one entry per numbered item, in the order '
        "given. Never put a double quote character inside a string value "
        "(use single quotes there), so the array stays valid JSON.")


def _verdict(item: dict[str, Any]) -> tuple[str, str] | None:
    subject = str(item.get("subject", "")).strip().lower()
    kind = str(item.get("kind", "")).strip().lower()
    if subject not in SUBJECTS or kind not in NEWS_KINDS:
        return None
    return subject, kind


def _response_shape(response: LLMResponse) -> dict[str, Any]:
    return {
        "finish_reason": response.finish_reason,
        "completion_tokens": response.usage.completion_tokens,
        "thinking_tokens": response.usage.thinking_tokens,
        "answer_chars": len(response.content),
        "answer_tail": response.content[-200:]
    }


@dataclass(frozen=True)
class ParsedVerdicts:
    entries: list[object]
    strict: bool
    unrecovered: frozenset[int]

    @property
    def complete(self) -> bool:
        return not self.unrecovered


def salvage_json_objects(raw: str) -> list[dict[str, Any]]:
    text = strip_json_code_fence(raw)
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        nested = isinstance(obj, dict) and "index" not in obj
        pos = text.find("{", pos + 1 if nested else end)
    return objects


def _entry_indices(entries: Sequence[object], n: int) -> set[int]:
    indices: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < n:
            indices.add(idx)
    return indices


def _parse_error(text: str, exc: json.JSONDecodeError) -> dict[str, Any]:
    return {
        "parse_error": exc.msg,
        "error_pos": exc.pos,
        "error_line": exc.lineno,
        "error_col": exc.colno,
        "error_context": text[max(0, exc.pos - 120):exc.pos + 60]
    }


def read_verdicts(
        raw: str, n: int, *, attempt: int = 1,
        log_prefix: str = "news_relevance",
        **log_context: object) -> ParsedVerdicts:
    text = strip_json_code_fence(raw)
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        error = _parse_error(text, exc)
    else:
        parsed = parse_json_list(
            raw, log_event=f"{log_prefix}.unparseable_response")
        if parsed is not None:
            return ParsedVerdicts(
                entries=list(parsed), strict=True, unrecovered=frozenset(
                    set(range(n)) - _entry_indices(parsed, n)))
        error = {"parse_error": "valid JSON without a verdict list"}
    salvaged: list[object] = list(salvage_json_objects(raw))
    recovered = _entry_indices(salvaged, n)
    logger.warning(
        f"{log_prefix}.unparseable_response", attempt=attempt, raw=raw[:500],
        raw_tail=raw[-200:], salvaged_objects=len(salvaged),
        recovered_items=len(recovered), batch_size=n, **error, **log_context)
    return ParsedVerdicts(
        entries=salvaged, strict=False,
        unrecovered=frozenset(set(range(n)) - recovered))


VerdictRequest = Callable[[int], Awaitable[LLMResponse]]


async def parsed_with_retry(
        request: VerdictRequest, *, titles: Sequence[str],
        log_prefix: str = "news_relevance",
        **log_context: object) -> ParsedVerdicts | LLMError:
    n = len(titles)
    try:
        response = await request(_VERIFY_MAX_TOKENS)
    except LLMError as exc:
        return exc
    first = read_verdicts(
        response.content, n, attempt=1, log_prefix=log_prefix, **log_context)
    if first.strict or first.complete:
        if not first.strict:
            logger.info(
                f"{log_prefix}.salvaged_without_retry", batch_size=n,
                entries=len(first.entries), **log_context)
        return first
    logger.warning(
        f"{log_prefix}.unparseable_retry", batch_size=n,
        max_tokens=_VERIFY_MAX_TOKENS, retry_max_tokens=_RETRY_MAX_TOKENS,
        salvaged_items=n - len(first.unrecovered), **_response_shape(response),
        **log_context)
    try:
        retry = await request(_RETRY_MAX_TOKENS)
    except LLMError as exc:
        logger.warning(
            f"{log_prefix}.unparseable_retry_failed", batch_size=n,
            error=str(exc), kept_unverified=len(first.unrecovered),
            **log_context)
        return first
    second = read_verdicts(
        retry.content, n, attempt=2, log_prefix=log_prefix, **log_context)
    if second.strict:
        logger.info(
            f"{log_prefix}.unparseable_retry_recovered", batch_size=n,
            entries=len(second.entries), **log_context)
        return second
    best = min((first, second), key=lambda p: len(p.unrecovered))
    if best.unrecovered:
        logger.warning(
            f"{log_prefix}.unparseable_after_retry", batch_size=n,
            kept_unverified=len(best.unrecovered),
            salvaged_items=n - len(best.unrecovered),
            used_attempt=1 if best is first else 2, titles=[
                titles[i][:SNIPPET_CHARS] for i in sorted(best.unrecovered)
            ], **_response_shape(retry), **log_context)
    else:
        logger.info(
            f"{log_prefix}.salvaged_after_retry", batch_size=n,
            entries=len(best.entries), **log_context)
    return best


@dataclass(frozen=True)
class CheapVerdicts:
    trusted: dict[int, dict[str, Any]]
    unreadable: frozenset[int]


async def verify_with_cheap_tier(
        llm_router: LLMRouter, *, system_prompt: str, numbered: str,
        titles: Sequence[str], log_prefix: str,
        response_format: str | None = None,
        **log_context: object) -> CheapVerdicts | None:

    async def request(max_tokens: int) -> LLMResponse:
        return await llm_router.complete(
            LLMTier.CHEAP, [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=numbered)
            ], retries=AGENT_LLM_CALL_RETRIES, max_tokens=max_tokens,
            response_format=response_format)

    parsed = await parsed_with_retry(
        request, titles=titles, log_prefix=log_prefix, **log_context)
    if isinstance(parsed, LLMError):
        logger.info(
            f"{log_prefix}.verification_unavailable", error=str(parsed))
        return None
    trusted, issues = align_verdicts(parsed.entries, titles)
    if issues:
        logger.warning(
            f"{log_prefix}.verdicts_misaligned", **log_context,
            total_checked=len(titles),
            kept_unverified=len(titles) - len(trusted), **issues)
    return CheapVerdicts(
        trusted=trusted,
        unreadable=frozenset() if parsed.strict else parsed.unrecovered)


async def _classify_batch(
        articles: list[NewsArticle], *, ticker: str, company_name: str,
        llm_router: LLMRouter) -> list[ClassifiedArticle]:
    verdicts = await verify_with_cheap_tier(
        llm_router,
        system_prompt=_system_prompt(ticker=ticker, company_name=company_name),
        numbered="\n".join(
            f"{i}. Title: {a.title}\n   Summary: {a.summary or '(no summary)'}"
            for i, a in enumerate(articles)),
        titles=[a.title
                for a in articles], log_prefix="news_relevance", ticker=ticker)
    if verdicts is None:
        return [
            ClassifiedArticle(a, UNCLASSIFIED, checked=False) for a in articles
        ]

    aliases = company_aliases(ticker, company_name)
    kept: list[ClassifiedArticle] = []
    dropped: list[dict[str, Any]] = []
    invalid: list[str] = []
    counts: Counter[str] = Counter()
    for i, article in enumerate(articles):
        if i in verdicts.unreadable:
            kept.append(ClassifiedArticle(article, UNVERIFIED))
            counts[UNVERIFIED] += 1
            continue
        item = verdicts.trusted.get(i)
        verdict = _verdict(item) if item is not None else None
        if verdict is None:
            if item is not None:
                invalid.append(article.title[:SNIPPET_CHARS])
            kept.append(ClassifiedArticle(article, UNCLASSIFIED))
            counts[UNCLASSIFIED] += 1
            continue
        subject, kind = verdict
        counts[f"{subject}/{kind}"] += 1
        kept_kind = kind in KEPT_KINDS or kind == CONSUMER_PRODUCT
        if subject in KEPT_SUBJECTS and kept_kind:
            kept.append(ClassifiedArticle(article, kind))
            continue
        dropped.append({
            "title": article.title[:SNIPPET_CHARS],
            "subject": subject,
            "kind": kind,
            "reasoning": (
                str(item.get("reasoning", ""))[:160] if item else ""),
            "title_names_company": headline_names_company(
                article.title, aliases)
        })

    if invalid:
        logger.warning(
            "news_relevance.invalid_verdicts", ticker=ticker,
            count=len(invalid), titles=invalid)
    logger.info(
        "news_relevance.classified", ticker=ticker,
        total_checked=len(articles), by_verdict=dict(counts),
        dropped_count=len(dropped), dropped=dropped)
    return kept


async def _classify_in_batches(
        articles: list[NewsArticle], *, ticker: str, company_name: str,
        llm_router: LLMRouter) -> list[ClassifiedArticle]:
    size = max(1, settings.NEWS_RELEVANCE_BATCH_SIZE)
    batches = [articles[i:i + size] for i in range(0, len(articles), size)]
    results = await asyncio.gather(
        *(
            _classify_batch(
                batch, ticker=ticker, company_name=company_name,
                llm_router=llm_router)
            for batch in batches), return_exceptions=True)
    kept: list[ClassifiedArticle] = []
    for batch, result in zip(batches, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.warning(
                "news_relevance.batch_failed", ticker=ticker,
                batch_size=len(batch), error=str(result))
            kept.extend(
                ClassifiedArticle(a, UNCLASSIFIED, checked=False)
                for a in batch)
        else:
            kept.extend(result)
    return kept


async def classify_articles(
        articles: list[NewsArticle], *, ticker: str,
        llm_router: LLMRouter | None = None) -> list[ClassifiedArticle]:
    if not articles:
        return []
    articles = drop_prechecked(
        articles, title_of=lambda a: a.title,
        log_event="news_relevance.prechecked", ticker=ticker)
    if not articles:
        return []
    articles = relink_consent_items(
        articles, title_of=lambda a: a.title,
        log_event="news_relevance.consent_link_replaced", ticker=ticker)
    legal_name = await lookup_company_name(ticker)
    if legal_name is None:
        logger.info(
            "news_relevance.unclassified_without_company_name", ticker=ticker,
            count=len(articles))
        return [
            ClassifiedArticle(a, UNCLASSIFIED, checked=False) for a in articles
        ]
    return await _classify_in_batches(
        articles, ticker=ticker, company_name=company_search_name(legal_name),
        llm_router=llm_router or LLMRouter())


@dataclass(frozen=True)
class CleanedArticles:
    english: list[NewsArticle]
    classified: list[ClassifiedArticle]


async def clean_articles(
        articles: list[NewsArticle], ticker: str | None, *,
        on_non_english_dropped: Callable[[int], None],
        llm_router: LLMRouter | None = None) -> CleanedArticles:
    english = [
        a for a in articles
        if non_english_reason(f"{a.title}\n{a.summary or ''}") is None
    ]
    if len(english) < len(articles):
        on_non_english_dropped(len(articles) - len(english))
    if ticker is None or not english:
        classified = [ClassifiedArticle(a, UNCLASSIFIED) for a in english]
    else:
        classified = await classify_articles(
            english, ticker=ticker, llm_router=llm_router)
    return CleanedArticles(english=english, classified=classified)


def _item_key(item: ClassifiedArticle) -> str:
    return item.article.url or " ".join(item.article.title.lower().split())


def cap_consumer_items(
        items: Sequence[ClassifiedArticle], *, shown: set[str], max_items: int,
        log_event: str, **log_context: object) -> list[ClassifiedArticle]:
    out: list[ClassifiedArticle] = []
    skipped: list[str] = []
    for item in items:
        if item.kind != CONSUMER_PRODUCT:
            out.append(item)
            continue
        key = _item_key(item)
        if key in shown:
            out.append(item)
        elif len(shown) < max(0, max_items):
            shown.add(key)
            out.append(item)
        else:
            skipped.append(item.article.title[:SNIPPET_CHARS])
    if skipped:
        logger.info(
            log_event, dropped_count=len(skipped), dropped=skipped,
            **log_context)
    return out


def stored_evidence_articles(
        classified: Sequence[ClassifiedArticle], *,
        ticker: str) -> list[NewsArticle] | None:
    unchecked = sum(not item.checked for item in classified)
    if unchecked:
        logger.warning(
            "news_relevance.unchecked_not_stored", ticker=ticker,
            unchecked=unchecked, total=len(classified))
        return None
    kept = [
        item for item in classified if item.kind in CONFIRMED_EVIDENCE_KINDS
    ]
    left_out = Counter(
        item.kind for item in classified
        if item.kind not in CONFIRMED_EVIDENCE_KINDS)
    if left_out:
        logger.info(
            "news_relevance.not_stored", ticker=ticker, by_kind=dict(left_out),
            titles=[
                item.article.title[:SNIPPET_CHARS] for item in classified
                if item.kind not in CONFIRMED_EVIDENCE_KINDS
            ])
    return [item.article for item in kept]
