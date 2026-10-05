import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import parse_qs, unquote, urlparse
import structlog
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
