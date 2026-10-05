from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import structlog
from ai.ingestion.source_failures import report_source_failure
from ai.llm.base import AGENT_LLM_CALL_RETRIES, LLMMessage, LLMTier
from ai.llm.router import LLMRouter
from ai.providers.base import (MacroDataPoint, ProviderError,
                               ProviderUnavailableError)
from ai.providers.fred import FredProvider
from ai.tools.macro_tool import (YOY_INDEX_SERIES_IDS, YOY_MIN_LOOKBACK_DAYS,
                                 point_on_or_before)

logger = structlog.get_logger(__name__)

_INDICATORS = (
    "fed_funds_rate", "cpi", "unemployment_rate", "gdp", "10y_treasury_yield")
_MIN_WORD_COUNT = 60

_PROMPT_TEMPLATE = (
    "You are a macroeconomic research analyst. Using ONLY the real "
    "indicator values listed below (do not invent any numbers not given "
    "here), write a concise 2-3 paragraph macro research note summarizing "
    "the current U.S. macroeconomic backdrop for an equity research "
    "audience.\n\n{facts}")


def _fact_line(points: list[MacroDataPoint]) -> str:
    latest = points[-1]
    as_of = latest.observed_at.isoformat()
    if latest.series_id not in YOY_INDEX_SERIES_IDS:
        unit = latest.unit or ""
        return (
            f"{latest.label}: {latest.value}{unit} as of {as_of} "
            f"(source: {latest.source})")
    ref = point_on_or_before(
        points[:-1], latest.observed_at - timedelta(days=365))
    if ref is None or ref.value == 0:
        logger.warning(
            "auto_ingestion.macro_year_over_year_unavailable",
            series_id=latest.series_id,
            first_observation=points[0].observed_at.isoformat())
        return (
            f"{latest.label}: index level {latest.value} as of {as_of}; "
            f"this is an index level, not an inflation rate, and the "
            f"year-over-year change is not available "
            f"(source: {latest.source})")
    change_pct = (latest.value / ref.value - 1) * 100
    return (
        f"{latest.label}: year-over-year inflation {change_pct:.2f}% as of "
        f"{as_of} (index {latest.value} versus {ref.value} on "
        f"{ref.observed_at.isoformat()}; source: {latest.source})")


@dataclass
class MacroReport:
    filename: str
    text: str
    content_date: str | None = None


async def generate_macro_report(
        *, already_ingested_filenames: set[str]) -> MacroReport | None:
    month_key = datetime.now(UTC).strftime("%Y-%m")
    filename = f"macro_auto_{month_key}.txt"
    if filename in already_ingested_filenames:
        return None

    provider = FredProvider()
    fact_lines: list[str] = []
    fetch_failures = 0
    for indicator in _INDICATORS:
        try:
            points = await provider.get_indicator(
                indicator, lookback_days=YOY_MIN_LOOKBACK_DAYS)
        except ProviderUnavailableError:
            raise
        except ProviderError:
            logger.exception(
                "auto_ingestion.macro_indicator_fetch_failed",
                indicator=indicator)
            fetch_failures += 1
            continue
        if not points:
            logger.warning(
                "auto_ingestion.macro_indicator_empty", indicator=indicator,
                lookback_days=YOY_MIN_LOOKBACK_DAYS)
            continue
        fact_lines.append(_fact_line(points))

    if not fact_lines:
        logger.warning("auto_ingestion.no_macro_data_available")
        if fetch_failures:
            report_source_failure("fred")
        return None

    prompt = _PROMPT_TEMPLATE.format(facts="\n".join(fact_lines))
    router = LLMRouter()
    try:
        response = await router.complete(
            LLMTier.CHEAP, [LLMMessage(role="user", content=prompt)],
            retries=AGENT_LLM_CALL_RETRIES)
    except Exception:
        logger.exception("auto_ingestion.macro_narrative_generation_failed")
        report_source_failure("llm")
        return None

    text = response.content.strip()
    if len(text.split()) < _MIN_WORD_COUNT:
        logger.warning(
            "auto_ingestion.macro_narrative_too_short",
            word_count=len(text.split()))
        return None

    return MacroReport(
        filename=filename, text=text,
        content_date=datetime.now(UTC).date().isoformat())
