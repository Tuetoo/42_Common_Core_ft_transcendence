import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4
import httpx
import structlog
from ai.providers.base import (NewsArticle, NewsProvider, ProviderQuotaError,
                               ProviderRequestError, ProviderUnavailableError)
from config import settings
from shared.http_client import get_http_client
from shared.redis import get_cache_client

logger = structlog.get_logger(__name__)

_EVERYTHING_URL = "https://newsapi.org/v2/everything"
_SOURCES_URL = "https://newsapi.org/v2/top-headlines/sources"


class NewsApiArticle(NewsArticle):
    source_id: str | None = None


_RATE_LIMIT_CODES = frozenset({"rateLimited", "apiKeyExhausted"})


@dataclass
class _RateLimitState:
    until: float = 0.0
    reason: str = ""


_rate_limited = _RateLimitState()


def _raise_if_rate_limited(query: str) -> None:
    remaining = _rate_limited.until - time.monotonic()
    if remaining <= 0:
        return
    raise ProviderQuotaError(
        f"NewsAPI search failed for query {query!r}: request limit reached "
        f"({_rate_limited.reason}); no NewsAPI request for another "
        f"{remaining / 60:.0f} min")


def _raise_for_error_response(resp: httpx.Response, query: str) -> None:
    code = message = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("code") or "")
        message = str(body.get("message") or "")[:300]
    logger.warning(
        "newsapi.request_failed", query=query, status_code=resp.status_code,
        code=code, message=message)
    detail = f"HTTP {resp.status_code}" + (f" {code}" if code else "")
    if resp.status_code == 429 or code in _RATE_LIMIT_CODES:
        _rate_limited.until = (
            time.monotonic() + settings.NEWSAPI_RATE_LIMIT_COOLDOWN_SECONDS)
        _rate_limited.reason = detail
        logger.warning(
            "newsapi.rate_limited_cooldown_started",
            status_code=resp.status_code, code=code,
            cooldown_seconds=settings.NEWSAPI_RATE_LIMIT_COOLDOWN_SECONDS)
        raise ProviderQuotaError(
            f"NewsAPI search failed for query {query!r}: request limit "
            f"reached ({detail})")
    raise ProviderRequestError(
        f"NewsAPI search failed for query {query!r}: {detail}" +
        (f": {message}" if message else ""))


USAGE_KEY = "ai:newsapi:requests"
USAGE_WINDOW_SECONDS = 24 * 3600


async def _record_counted_request(resp: httpx.Response) -> None:
    if resp.headers.get("X-Cached-Result", "").lower() == "true":
        return
    now = time.time()
    try:
        client = get_cache_client(settings)
        async with client.pipeline(transaction=False) as pipe:
            pipe.zadd(USAGE_KEY, {f"{now:.6f}:{uuid4().hex[:8]}": now})
            pipe.zremrangebyscore(
                USAGE_KEY, "-inf", now - USAGE_WINDOW_SECONDS)
            pipe.expire(USAGE_KEY, USAGE_WINDOW_SECONDS + 3600)
            await pipe.execute()
    except Exception as exc:
        logger.warning("newsapi.usage_count_failed", error=str(exc))


class NewsApiProvider(NewsProvider):

    async def search_news(
            self, query: str, *, since: datetime | None = None,
            limit: int = 20, sort_by: str | None = None) -> list[NewsArticle]:
        if not settings.NEWSAPI_API_KEY:
            raise ProviderUnavailableError(
                "NEWSAPI_API_KEY is not configured -- register a free key at "
                "https://newsapi.org/register")

        params: dict[str, str | int] = {
            "q": query,
            "sortBy": sort_by or settings.NEWSAPI_SORT_BY or "publishedAt",
            "language": "en",
            "pageSize": max(1, min(limit, 100))
        }
        if settings.NEWSAPI_SEARCH_IN:
            params["searchIn"] = settings.NEWSAPI_SEARCH_IN
        if since is not None:
            params["from"] = since.astimezone(UTC).date().isoformat()

        _raise_if_rate_limited(query)
        client = get_http_client()
        try:
            resp = await client.get(
                _EVERYTHING_URL, params=params,
                headers={"X-Api-Key": settings.NEWSAPI_API_KEY})
        except Exception as exc:
            logger.warning(
                "newsapi.request_failed", query=query,
                error=f"{type(exc).__name__}: {exc}")
            raise ProviderRequestError(
                f"NewsAPI search failed for query {query!r}: "
                f"{type(exc).__name__}") from exc
        if resp.status_code >= 400:
            _raise_for_error_response(resp, query)
        await _record_counted_request(resp)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"NewsAPI returned a non-JSON response for query {query!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError(
                f"NewsAPI returned an unexpected response shape for query "
                f"{query!r}")
        if payload.get("status") != "ok":
            raise ProviderRequestError(
                f"NewsAPI returned an error for query {query!r}: "
                f"{payload.get('message')}")

        articles: list[NewsArticle] = []
        for item in (payload.get("articles") or [])[:max(limit, 0)]:
            if not isinstance(item, dict):
                continue
            published_raw = item.get("publishedAt")
            try:
                published_at = datetime.fromisoformat(
                    str(published_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                logger.warning(
                    "newsapi.unparseable_published_at", raw=published_raw)
                continue

            source = item.get("source")
            source = source if isinstance(source, dict) else {}
            source_id = source.get("id")
            articles.append(
                NewsApiArticle(
                    title=item.get("title") or "(untitled)",
                    source_name=str(source.get("name") or "unknown"),
                    source_id=str(source_id) if source_id else None,
                    url=item.get("url", ""), published_at=published_at,
                    summary=item.get("description")))
        return articles


CATALOG_CACHE_KEY = "ai:newsapi:source_catalog"
_CATALOG_TTL_SECONDS = 7 * 24 * 3600
_CATALOG_REFRESH_SECONDS = 24 * 3600.0
_CATALOG_PROCESS_SECONDS = 3600.0
_CATALOG_RETRY_SECONDS = 900.0


def url_host(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".").removeprefix("www.")


@dataclass(frozen=True)
class SourceCatalog:
    hosts: frozenset[str]

    def contains_url(self, url: str) -> bool:
        labels = url_host(url).split(".")
        return any(
            ".".join(labels[i:]) in self.hosts for i in range(len(labels) - 1))


def catalog_flag(
        url: str, *, source_id: str | None,
        catalog: SourceCatalog | None) -> bool | None:
    if source_id:
        return True
    if catalog is None:
        return None
    return catalog.contains_url(url)


@dataclass
class _CatalogState:
    catalog: SourceCatalog | None = None
    loaded_at: float = float("-inf")
    failed_at: float = float("-inf")
    locks: dict[int, asyncio.Lock] = field(default_factory=dict)


_catalog_state = _CatalogState()


def _catalog_lock() -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock = _catalog_state.locks.get(loop_id)
    if lock is None:
        lock = _catalog_state.locks[loop_id] = asyncio.Lock()
    return lock


def _catalog_from_json(raw: str) -> SourceCatalog | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        return None
    return SourceCatalog(hosts=frozenset(str(h) for h in hosts))


def _catalog_age_seconds(raw: str) -> float | None:
    try:
        data = json.loads(raw)
        fetched_at = datetime.fromisoformat(str(data["fetched_at"]))
    except (ValueError, TypeError, KeyError):
        return None
    if fetched_at.tzinfo is None:
        return None
    return (datetime.now(UTC) - fetched_at).total_seconds()


async def _read_cached_catalog() -> tuple[SourceCatalog | None, bool]:
    try:
        raw = await get_cache_client(settings).get(CATALOG_CACHE_KEY)
    except Exception as exc:
        logger.warning(
            "newsapi.source_catalog_cache_read_failed", error=str(exc))
        return None, False
    if not isinstance(raw, str):
        return None, False
    catalog = _catalog_from_json(raw)
    age = _catalog_age_seconds(raw)
    return catalog, age is not None and age < _CATALOG_REFRESH_SECONDS


async def _fetch_catalog() -> SourceCatalog | None:
    if _rate_limited.until > time.monotonic():
        return None
    try:
        resp = await get_http_client().get(
            _SOURCES_URL, params={"language": "en"},
            headers={"X-Api-Key": settings.NEWSAPI_API_KEY})
        if resp.status_code >= 400:
            _raise_for_error_response(resp, "(source catalog)")
        await _record_counted_request(resp)
        payload = resp.json()
    except Exception as exc:
        logger.warning("newsapi.source_catalog_fetch_failed", error=str(exc))
        return None
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        logger.warning(
            "newsapi.source_catalog_fetch_failed", error="no sources list")
        return None
    hosts: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        host = url_host(str(source.get("url") or ""))
        if "." in host:
            hosts.add(host)
    if not hosts:
        logger.warning(
            "newsapi.source_catalog_fetch_failed", error="empty catalog")
        return None
    try:
        await get_cache_client(settings).set(
            CATALOG_CACHE_KEY,
            json.dumps({
                "hosts": sorted(hosts),
                "fetched_at": datetime.now(UTC).isoformat()
            }), ex=_CATALOG_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "newsapi.source_catalog_cache_write_failed", error=str(exc))
    logger.info(
        "newsapi.source_catalog_fetched", sources=len(sources),
        hosts=len(hosts))
    return SourceCatalog(hosts=frozenset(hosts))


async def source_catalog() -> SourceCatalog | None:
    state = _catalog_state
    if not settings.NEWSAPI_API_KEY:
        return None
    if time.monotonic() - state.loaded_at < _CATALOG_PROCESS_SECONDS:
        return state.catalog
    if time.monotonic() - state.failed_at < _CATALOG_RETRY_SECONDS:
        return state.catalog
    try:
        async with _catalog_lock():
            if time.monotonic() - state.loaded_at < _CATALOG_PROCESS_SECONDS:
                return state.catalog
            cached, fresh = await _read_cached_catalog()
            catalog = cached if fresh else await _fetch_catalog()
            if catalog is None and cached is not None:
                logger.info("newsapi.source_catalog_stale_served")
                state.catalog = cached
            if catalog is None:
                state.failed_at = time.monotonic()
                return state.catalog
            state.catalog = catalog
            state.loaded_at = time.monotonic()
            return catalog
    except Exception as exc:
        logger.warning("newsapi.source_catalog_failed", error=str(exc))
        return state.catalog
