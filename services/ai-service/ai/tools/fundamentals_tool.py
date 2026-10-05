from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
import structlog
from ai.providers.base import (FundamentalDataProvider, FundamentalsSnapshot,
                               ProviderError)
from ai.providers.registry import get_fundamental_data_providers
from ai.providers.sec_edgar import latest_quarter_payload
from ai.tools.base import Tool

logger = structlog.get_logger(__name__)

QuarterLookup = Callable[[str], Awaitable[dict[str, Any] | None]]

_SEC_EDGAR_SOURCE = "sec_edgar"
_YAHOO_SOURCE = "yahoo_finance"
_PERIOD_LABELS: dict[tuple[str, str], str] = {
    **{(_SEC_EDGAR_SOURCE, f): "annual, latest FY"
       for f in ("gross_margin", "operating_margin", "net_margin")},
    **{(_YAHOO_SOURCE, f): "TTM"
       for f in ("gross_margin", "operating_margin", "net_margin")}
}


class GetFundamentalsTool(Tool):
    name = "get_fundamentals"
    description = (
        "Get valuation ratios (P/E, P/B) and margins/revenue growth for a "
        'ticker. action_input: {"ticker": "AAPL"}')
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string"
            }
        },
        "required": ["ticker"],
        "additionalProperties": False
    }

    def __init__(
            self, providers: list[FundamentalDataProvider] | None = None,
            quarter_lookup: QuarterLookup | None = None, *,
            analysis_ticker: str | None = None) -> None:
        self.analysis_ticker = analysis_ticker
        self.providers = (
            providers
            if providers is not None else get_fundamental_data_providers())
        self._quarter_lookup = quarter_lookup or latest_quarter_payload

    async def latest_quarter(self, ticker: str) -> dict[str, Any] | None:
        try:
            return await self._quarter_lookup(ticker)
        except Exception as exc:
            logger.warning(
                "fundamentals_tool.latest_quarter_failed", ticker=ticker,
                error=str(exc))
            return None

    async def run(self, ticker: str) -> str:  # type: ignore[override]
        return self.label_result(ticker, await self._fundamentals(ticker))

    async def _fundamentals(self, ticker: str) -> str:
        errors: list[str] = []
        snap: FundamentalsSnapshot | None = None
        sources: list[str] = []
        field_sources: dict[str, str] = {}
        mergeable_fields = (
            "pe_ratio", "forward_pe", "pb_ratio", "revenue_growth_yoy",
            "gross_margin", "operating_margin", "net_margin")

        for provider in self.providers:
            try:
                result = await provider.get_fundamentals(ticker)
            except ProviderError as exc:
                errors.append(f"{provider.__class__.__name__}: {exc}")
                continue

            if snap is None:
                snap = result
                sources.append(result.source)
                field_sources.update(
                    dict.fromkeys((
                        f for f in mergeable_fields
                        if getattr(result, f) is not None), result.source))
                continue

            filled_any = False
            for field in mergeable_fields:
                if (getattr(snap, field) is None
                        and getattr(result, field) is not None):
                    setattr(snap, field, getattr(result, field))
                    field_sources[field] = result.source
                    filled_any = True
            if filled_any:
                sources.append(result.source)
            snap.unavailable_notes.extend(result.unavailable_notes)

        quarter = await self.latest_quarter(ticker)
        quarter_line = f"\n{quarter['text']}" if quarter else ""

        if snap is None:
            return (
                f"Fundamentals lookup failed for {ticker!r} from all "
                f"{len(self.providers)} provider(s): {'; '.join(errors)}"
            ) + quarter_line

        _NOTE_FIELD_PREFIXES = {
            "gross margin:": "gross_margin",
            "operating margin:": "operating_margin",
            "net margin:": "net_margin"
        }
        snap.unavailable_notes = [
            note for note in snap.unavailable_notes if not any(
                note.startswith(prefix) and getattr(snap, field) is not None
                for prefix, field in _NOTE_FIELD_PREFIXES.items())
        ]
        if len(sources) > 1:
            snap.source = "+".join(dict.fromkeys(sources))

        _POST_MERGE_SANITY_EPSILON = 0.005
        same_period = field_sources.get("gross_margin") == field_sources.get(
            "operating_margin")
        if (snap.gross_margin is not None and snap.operating_margin is not None
                and (snap.gross_margin <= snap.operating_margin +
                     _POST_MERGE_SANITY_EPSILON if same_period else
                     snap.gross_margin < snap.operating_margin)):
            snap.unavailable_notes.append(
                f"gross margin: computed value ({snap.gross_margin:.1%}) "
                "discarded -- came out lower than, or equal to, operating "
                f"margin ({snap.operating_margin:.1%}) after merging every "
                "provider's data, which should not be possible (operating "
                "margin can never reach gross margin unless operating "
                "expenses are exactly zero) and indicates the underlying "
                "figures were not really comparable (common for financial-"
                "sector tickers, where gross margin often isn't a "
                "meaningful GAAP concept)")
            snap.gross_margin = None

        return self._format(snap, field_sources) + quarter_line

    @staticmethod
    def _format(
            snap: FundamentalsSnapshot,
            field_sources: dict[str, str] | None = None) -> str:
        field_sources = field_sources or {}

        def _period(field: str) -> str:
            label = _PERIOD_LABELS.get((field_sources.get(field, ""), field))
            return f" ({label})" if label else ""

        parts = [
            f"Fundamentals for {snap.ticker!r} (source: {snap.source}, "
            f"as of {snap.as_of.date()}):"
        ]
        if snap.pe_ratio is not None:
            parts.append(f"P/E={snap.pe_ratio:.2f}")
        if snap.forward_pe is not None:
            parts.append(f"forward P/E={snap.forward_pe:.2f}")
        if snap.pb_ratio is not None:
            parts.append(f"P/B={snap.pb_ratio:.2f}")
        if snap.revenue_growth_yoy is not None:
            growth_label = (
                "quarterly, latest quarter vs year-ago quarter"
                if field_sources.get("revenue_growth_yoy") == _YAHOO_SOURCE
                else "annual, latest FY vs prior FY")
            parts.append(
                f"revenue growth YoY ({growth_label})="
                f"{snap.revenue_growth_yoy:.1%}")
        if snap.gross_margin is not None:
            parts.append(
                f"gross "
                f"margin{_period('gross_margin')}={snap.gross_margin:.1%}")
        if snap.operating_margin is not None:
            parts.append(
                f"operating margin{_period('operating_margin')}="
                f"{snap.operating_margin:.1%}")
        if snap.net_margin is not None:
            parts.append(
                f"net margin{_period('net_margin')}={snap.net_margin:.1%}")
        if len(parts) == 1:
            parts.append("(no ratios available from this source)")
        if snap.unavailable_notes:
            parts.append("| " + "; ".join(snap.unavailable_notes))
        return " ".join(parts)
