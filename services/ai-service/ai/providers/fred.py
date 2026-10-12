from datetime import date, timedelta
import httpx
import structlog
from ai.providers.base import (MacroDataPoint, MacroDataProvider,
                               ProviderRequestError, ProviderUnavailableError,
                               call_with_retry, is_retryable_http_error)
from config import settings
from shared.http_client import get_http_client

logger = structlog.get_logger(__name__)

_SOURCE = "fred"
_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

_SERIES_ALIASES: dict[str, tuple[str, str]] = {
    "fed_funds_rate": ("DFF", "Effective Federal Funds Rate (daily)"),
    "cpi": ("CPIAUCSL", "Consumer Price Index (All Urban Consumers)"),
    "unemployment_rate": ("UNRATE", "Civilian Unemployment Rate"),
    "gdp": ("GDP", "Gross Domestic Product"),
    "10y_treasury_yield": ("DGS10", "10-Year Treasury Constant Maturity Rate"),
    "vix": ("VIXCLS", "CBOE Volatility Index")
}

_PERCENT_SERIES_IDS = frozenset({"DFF", "FEDFUNDS", "UNRATE", "DGS10"})

_FRED_TIMEOUT_SECONDS = 15.0
_MAX_ERROR_TEXT = 300


def _describe_error(exc: BaseException) -> str:
    text = str(exc) or repr(exc)
    if settings.FRED_API_KEY:
        text = text.replace(settings.FRED_API_KEY, "***")
    return f"{type(exc).__name__}: {text[:_MAX_ERROR_TEXT]}"


class FredProvider(MacroDataProvider):

    async def get_indicator(self, series: str, *,
                            lookback_days: int = 365) -> list[MacroDataPoint]:
        if not settings.FRED_API_KEY:
            raise ProviderUnavailableError(
                "FRED_API_KEY is not configured -- register a free key at "
                "https://fred.stlouisfed.org/docs/api/api_key.html")

        series_id, label = _SERIES_ALIASES.get(
            series.lower(), (series, series))
        start = (date.today() - timedelta(days=lookback_days)).isoformat()

        client = get_http_client()

        async def _fetch() -> httpx.Response:
            resp = await client.get(
                _OBSERVATIONS_URL, params={
                    "series_id": series_id,
                    "api_key": settings.FRED_API_KEY,
                    "file_type": "json",
                    "observation_start": start
                }, timeout=_FRED_TIMEOUT_SECONDS)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as status_exc:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} from FRED for series "
                    f"{series_id}", request=status_exc.request,
                    response=status_exc.response) from None
            return resp

        try:
            resp = await call_with_retry(
                _fetch, op_name=f"fred.observations[{series_id}]",
                should_retry=is_retryable_http_error)
        except Exception as exc:
            detail = _describe_error(exc)
            logger.warning(
                "fred.lookup_failed", series_id=series_id, error=detail)
            raise ProviderRequestError(
                f"FRED lookup failed for series {series_id!r}: {detail}"
            ) from exc

        try:
            payload = resp.json()
            points: list[MacroDataPoint] = []
            for obs in payload.get("observations", []):
                if obs.get("value") in (None, "."):
                    continue
                points.append(
                    MacroDataPoint(
                        series_id=series_id, label=label,
                        value=float(obs["value"]),
                        unit="%" if series_id in _PERCENT_SERIES_IDS else None,
                        observed_at=date.fromisoformat(obs["date"]),
                        source=_SOURCE))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            detail = _describe_error(exc)
            logger.warning(
                "fred.unparseable_response", series_id=series_id, error=detail)
            raise ProviderRequestError(
                "FRED returned an unparseable response for series "
                f"{series_id!r}: {detail}") from exc
        return points
