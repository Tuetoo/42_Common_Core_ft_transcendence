import json
from datetime import date
from typing import Any
import httpx
import structlog
from config import settings
from redis.exceptions import RedisError
from shared.exceptions import UpstreamServiceError
from shared.http_client import get_http_client
from shared.redis import get_cache_client

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 30
_TIMEOUT = 3.0


async def _cache_get(key: str) -> str | bytes | None:
    try:
        cached: str | bytes | None = await get_cache_client(settings).get(key)
        return cached
    except (RedisError, OSError) as exc:
        logger.warning("core_client.cache_get_failed", key=key, error=str(exc))
        return None


async def _cache_set(key: str, value: str, ttl: int) -> None:
    try:
        await get_cache_client(settings).set(key, value, ex=ttl)
    except (RedisError, OSError) as exc:
        logger.warning("core_client.cache_set_failed", key=key, error=str(exc))


async def _get_json(
        path: str, what: str, *, params: dict[str, str] | None = None,
        not_found_none: bool = False) -> Any:
    url = f"{settings.CORE_SERVICE_URL}{path}"
    client = get_http_client(_TIMEOUT)
    try:
        if params is None:
            response = await client.get(url)
        else:
            response = await client.get(url, params=params)
        if not_found_none and response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"core-service unreachable while {what}") from exc
    return response.json()


async def get_portfolio_holdings(portfolio_id: str) -> list[dict[str, Any]]:
    cache_key = f"ai:core_client:holdings:{portfolio_id}"
    cached = await _cache_get(cache_key)
    if cached:
        cached_holdings: list[dict[str, Any]] = json.loads(cached)
        return cached_holdings
    holdings: list[dict[str, Any]] = await _get_json(
        f"/internal/portfolios/{portfolio_id}/holdings",
        f"resolving holdings for {portfolio_id}")
    await _cache_set(cache_key, json.dumps(holdings), _CACHE_TTL_SECONDS)
    return holdings


async def get_portfolio_owner(portfolio_id: str) -> dict[str, Any] | None:
    owner: dict[str, Any] | None = await _get_json(
        f"/internal/portfolios/{portfolio_id}/owner",
        f"resolving owner of {portfolio_id}", not_found_none=True)
    return owner


async def get_portfolio_cash_balance(portfolio_id: str) -> float:
    payload: dict[str, Any] = await _get_json(
        f"/internal/portfolios/{portfolio_id}/cash-balance",
        f"resolving cash balance for {portfolio_id}")
    return float(payload["cash_balance"])


async def get_portfolio_snapshots(
        portfolio_id: str, *, start_date: date | None = None,
        end_date: date | None = None) -> list[dict[str, Any]]:
    params: dict[str, str] = {}
    if start_date is not None:
        params["start_date"] = start_date.isoformat()
    if end_date is not None:
        params["end_date"] = end_date.isoformat()
    snapshots: list[dict[str, Any]] = await _get_json(
        f"/internal/portfolios/{portfolio_id}/snapshots",
        f"resolving snapshots for {portfolio_id}", params=params)
    return snapshots


async def get_portfolio_cash_flows(
        portfolio_id: str, *, start_date: date,
        end_date: date) -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = await _get_json(
        f"/internal/portfolios/{portfolio_id}/cash-flows",
        f"resolving cash flows for {portfolio_id}", params={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat()
        })
    return flows


async def get_tracked_tickers() -> list[str]:
    tickers: list[str] = await _get_json(
        "/internal/tracked-tickers", "resolving tracked tickers")
    return tickers


async def get_existing_document_filenames(
        ticker: str | None, category: str, *,
        system_only: bool = False) -> list[str]:
    params: dict[str, str] = {"category": category}
    if ticker is not None:
        params["ticker"] = ticker
    if system_only:
        params["system_only"] = "true"
    filenames: list[str] = await _get_json(
        "/internal/documents/filenames",
        f"checking existing filenames for {ticker}/{category}", params=params)
    return filenames


async def create_system_document(
        *, filename: str, content: str, category: str, ticker: str | None,
        content_date: str | None = None) -> str:
    url = f"{settings.CORE_SERVICE_URL}/internal/documents"
    try:
        response = await get_http_client(_TIMEOUT).post(
            url, json={
                "filename": filename,
                "content": content,
                "category": category,
                "ticker": ticker,
                "content_date": content_date
            })
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"core-service unreachable while creating document {filename}"
        ) from exc
    document_id: str = response.json()["id"]
    return document_id


class TransactionRejectedError(UpstreamServiceError):

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            f"core-service rejected the transaction ({status_code}): {detail}")


_RETRYABLE_4XX = frozenset({408, 425, 429})


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if body.get("detail"):
            return str(body["detail"])[:500]
    return str(body)[:500]


async def record_transaction(
        portfolio_id: str, *, transaction_type: str, ticker: str,
        quantity: float, price: float, transaction_date: str,
        idempotency_key: str | None = None) -> str:
    url = (
        f"{settings.CORE_SERVICE_URL}/internal/portfolios/{portfolio_id}"
        "/transactions")
    payload: dict[str, Any] = {
        "transaction_type": transaction_type,
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "transaction_date": transaction_date
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    try:
        response = await get_http_client(_TIMEOUT).post(url, json=payload)
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"core-service unreachable while recording a transaction for "
            f"portfolio {portfolio_id}") from exc
    if (400 <= response.status_code < 500
            and response.status_code not in _RETRYABLE_4XX):
        raise TransactionRejectedError(
            response.status_code, _error_detail(response))
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"core-service failed ({response.status_code}) while recording a "
            f"transaction for portfolio {portfolio_id}") from exc
    transaction_id: str = response.json()["id"]
    return transaction_id
