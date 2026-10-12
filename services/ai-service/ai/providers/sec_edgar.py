import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
import httpx
import structlog
from ai.providers.base import (FundamentalDataProvider, FundamentalsSnapshot,
                               ProviderError, ProviderRequestError,
                               ProviderUnavailableError, call_with_retry,
                               is_retryable_http_error)
from config import settings
from shared.http_client import get_http_client

logger = structlog.get_logger(__name__)

_SOURCE = "sec_edgar"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANY_FACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"

_REVENUE_TAGS = (
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
_GROSS_PROFIT_TAGS = ("GrossProfit", )
_COST_OF_REVENUE_TAGS = (
    "CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold")
_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss", )
_NET_INCOME_TAGS = ("NetIncomeLoss", )

_MAX_PERIOD_MISALIGNMENT_DAYS = 60

_ticker_to_cik_cache: dict[str, int] | None = None
_ticker_map_day: date | None = None
_ticker_to_title_cache: dict[str, str] | None = None
_ticker_map_lock = asyncio.Lock()

_last_request_at = 0.0


async def _throttle() -> None:
    global _last_request_at
    rate = settings.SEC_EDGAR_MAX_REQUESTS_PER_SECOND
    interval = 1.0 / rate if rate > 0 else 0.0
    now = time.monotonic()
    wait = _last_request_at + interval - now
    _last_request_at = max(now, _last_request_at + interval)
    if wait > 0:
        await asyncio.sleep(wait)


async def _sec_get_json(url: str, *, op_name: str, what: str) -> Any:
    client = get_http_client()
    headers = _headers()

    async def _fetch() -> httpx.Response:
        await _throttle()
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp

    try:
        resp = await call_with_retry(
            _fetch, op_name=op_name, should_retry=is_retryable_http_error)
    except Exception as exc:
        raise ProviderRequestError(f"{what} failed") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderRequestError(
            f"{what}: response was not valid JSON") from exc


def _headers() -> dict[str, str]:
    if not settings.SEC_EDGAR_USER_AGENT:
        raise ProviderUnavailableError(
            "SEC_EDGAR_USER_AGENT is not configured -- SEC requires a "
            "descriptive User-Agent ('Company Name contact@domain.com') "
            "on every request.")
    return {"User-Agent": settings.SEC_EDGAR_USER_AGENT}


async def _load_ticker_to_cik() -> dict[str, int]:
    global _ticker_to_cik_cache, _ticker_to_title_cache, _ticker_map_day
    if _ticker_to_cik_cache is not None and _ticker_map_day == _today():
        return _ticker_to_cik_cache

    async with _ticker_map_lock:
        if _ticker_to_cik_cache is not None and _ticker_map_day == _today():
            return _ticker_to_cik_cache
        data = await _sec_get_json(
            _TICKER_MAP_URL, op_name="sec_edgar.ticker_map",
            what="SEC ticker->CIK mapping fetch")

        try:
            entries = [e for e in data.values() if isinstance(e, dict)]
            cik_map: dict[str, int] = {}
            title_map: dict[str, str] = {}
            for entry in entries:
                symbol = str(entry["ticker"]).upper()
                if symbol in cik_map:
                    continue
                cik_map[symbol] = int(entry["cik_str"])
                title_map[symbol] = str(entry.get("title") or "")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ProviderRequestError(
                "SEC ticker->CIK mapping response could not be parsed"
            ) from exc
        _ticker_to_title_cache = title_map
        _ticker_to_cik_cache = cik_map
        _ticker_map_day = _today()
        return _ticker_to_cik_cache


async def load_ticker_to_title() -> dict[str, str]:
    await _load_ticker_to_cik()
    return _ticker_to_title_cache or {}


def _first_available_concept(
        facts: dict[str, Any],
        tags: tuple[str, ...]) -> list[dict[str, Any]] | None:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    best_facts: list[dict[str, Any]] | None = None
    best_end: str = ""
    for tag in tags:
        concept = us_gaap.get(tag)
        if not concept or not concept.get("units", {}).get("USD"):
            continue
        usd_facts: list[dict[str, Any]] = concept["units"]["USD"]
        latest_end = _most_recent_valid_annual_end(usd_facts)
        if latest_end is not None and latest_end > best_end:
            best_end = latest_end
            best_facts = usd_facts
    return best_facts


def _is_annual_10k_fact(fact: dict[str, Any]) -> bool:
    if fact.get("form") != "10-K" or fact.get("fp") != "FY":
        return False
    if "end" not in fact or "start" not in fact:
        return False
    try:
        duration_days = (
            date.fromisoformat(fact["end"]) -
            date.fromisoformat(fact["start"])).days
    except ValueError:
        return False
    return 350 <= duration_days <= 380


def _most_recent_valid_annual_end(
        facts_usd: list[dict[str, Any]]) -> str | None:
    best: str | None = None
    for fact in facts_usd:
        if _is_annual_10k_fact(fact) and (best is None or fact["end"] > best):
            best = fact["end"]
    return best


def _latest_two_annual_values(
        facts_usd: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    by_period: dict[str, dict[str, Any]] = {}
    for fact in facts_usd:
        if not _is_annual_10k_fact(fact):
            continue
        period_end = fact["end"]
        existing = by_period.get(period_end)
        if existing is None or fact.get("filed", "") > existing.get("filed",
                                                                    ""):
            by_period[period_end] = fact

    ordered = sorted(by_period.items(), key=lambda kv: kv[0], reverse=True)
    latest = ordered[0][1]["val"] if len(ordered) >= 1 else None
    prior = ordered[1][1]["val"] if len(ordered) >= 2 else None
    return latest, prior


def _aligned_component_value(
        component_facts: list[dict[str, Any]] | None,
        reference_period_end: str | None) -> tuple[float | None, str | None]:
    if component_facts is None:
        return None, "not_disclosed"
    if reference_period_end is None:
        return None, "not_disclosed"
    component_end = _most_recent_valid_annual_end(component_facts)
    if component_end is None:
        return None, "not_disclosed"
    misalignment_days = abs((
        date.fromisoformat(reference_period_end) -
        date.fromisoformat(component_end)).days)
    if misalignment_days > _MAX_PERIOD_MISALIGNMENT_DAYS:
        return None, "stale"
    latest_value, _ = _latest_two_annual_values(component_facts)
    return latest_value, None


async def _cik_for(ticker: str) -> int:
    ticker_map = await _load_ticker_to_cik()
    cik = ticker_map.get(ticker.upper())
    if cik is None:
        raise ProviderRequestError(f"no SEC CIK found for ticker {ticker!r}")
    return cik


async def _company_facts(ticker: str) -> dict[str, Any]:
    cik = await _cik_for(ticker)
    facts = await _sec_get_json(
        _COMPANY_FACTS_URL.format(cik=cik),
        op_name=f"sec_edgar.company_facts[{ticker}]",
        what=f"SEC company facts lookup for {ticker}")
    if not isinstance(facts, dict):
        raise ProviderRequestError(
            f"SEC company facts response for {ticker} had an unexpected shape")
    return facts


class SecEdgarProvider(FundamentalDataProvider):

    async def get_fundamentals(self, ticker: str) -> FundamentalsSnapshot:
        facts = await _company_facts(ticker)
        _store_quarter_results(
            ticker, _quarter_results_from_facts(facts, ticker))

        revenue_facts = _first_available_concept(facts, _REVENUE_TAGS)
        gross_profit_facts = _first_available_concept(
            facts, _GROSS_PROFIT_TAGS)
        cost_of_revenue_facts = _first_available_concept(
            facts, _COST_OF_REVENUE_TAGS)
        operating_income_facts = _first_available_concept(
            facts, _OPERATING_INCOME_TAGS)
        net_income_facts = _first_available_concept(facts, _NET_INCOME_TAGS)

        revenue_latest, revenue_prior = (
            _latest_two_annual_values(revenue_facts) if revenue_facts else
            (None, None))
        revenue_period_end = (
            _most_recent_valid_annual_end(revenue_facts)
            if revenue_facts else None)

        revenue_growth_yoy = None
        if revenue_latest is not None and revenue_prior:
            revenue_growth_yoy = (
                revenue_latest - revenue_prior) / revenue_prior

        unavailable_notes: list[str] = []

        def _stale_or_missing_note(field: str, reason: str) -> str:
            if reason == "stale":
                return (
                    f"{field}: not available for the latest reported period "
                    "(the underlying filed figure is for a materially older "
                    "fiscal period than the latest revenue figure, so they "
                    "were not divided together)")
            return f"{field}: not disclosed separately by this filer"

        gross_latest, gross_reason = _aligned_component_value(
            gross_profit_facts, revenue_period_end)
        if gross_latest is None:
            cost_latest, cost_reason = _aligned_component_value(
                cost_of_revenue_facts, revenue_period_end)
            if cost_latest is not None and revenue_latest is not None:
                gross_latest = revenue_latest - cost_latest
            else:
                combined_reason = (
                    "stale" if "stale" in (gross_reason,
                                           cost_reason) else "not_disclosed")
                unavailable_notes.append(
                    _stale_or_missing_note("gross margin", combined_reason))

        operating_latest, operating_reason = _aligned_component_value(
            operating_income_facts, revenue_period_end)
        if operating_latest is None:
            unavailable_notes.append(
                _stale_or_missing_note(
                    "operating margin", operating_reason or "not_disclosed"))

        net_latest, net_reason = _aligned_component_value(
            net_income_facts, revenue_period_end)
        if net_latest is None:
            unavailable_notes.append(
                _stale_or_missing_note(
                    "net margin", net_reason or "not_disclosed"))

        def _margin(numerator: float | None) -> float | None:
            if numerator is None or not revenue_latest:
                return None
            return numerator / revenue_latest

        gross_margin = _margin(gross_latest)
        operating_margin = _margin(operating_latest)
        net_margin = _margin(net_latest)

        _SANITY_EPSILON = 0.005
        if (gross_margin is not None and operating_margin is not None
                and gross_margin < operating_margin - _SANITY_EPSILON):
            unavailable_notes.append(
                f"gross margin: computed value ({gross_margin:.1%}) discarded "
                f"-- came out lower than operating margin "
                f"({operating_margin:.1%}), "
                "which should not be possible and indicates the underlying "
                "figures were not really comparable")
            gross_margin = None

        if (operating_margin is not None and net_margin is not None
                and operating_margin < net_margin - _SANITY_EPSILON):
            fiscal_year = (
                f" for the fiscal year ended {revenue_period_end}"
                if revenue_period_end else " for the latest fiscal year")
            unavailable_notes.append(
                f"note: operating margin ({operating_margin:.1%}) is below "
                f"net margin ({net_margin:.1%}){fiscal_year} -- can be "
                "legitimate (e.g. one-time gains, tax adjustments) but worth "
                "double-checking against the filing directly")

        return FundamentalsSnapshot(
            ticker=ticker, revenue_growth_yoy=revenue_growth_yoy,
            gross_margin=gross_margin, operating_margin=operating_margin,
            net_margin=net_margin, as_of=datetime.now(UTC), source=_SOURCE,
            unavailable_notes=unavailable_notes)


@dataclass(frozen=True)
class FilingPeriod:
    form: str
    filing_date: str
    report_date: str
    accession: str


@dataclass
class _Submissions:
    day: date
    rows: list[FilingPeriod]
    pages: list[tuple[str, str, str]]


_submissions_cache: dict[str, _Submissions] = {}


def _today() -> date:
    return datetime.now(UTC).date()


def _rows_from_columns(columns: Any) -> list[FilingPeriod]:
    if not isinstance(columns, dict):
        return []
    forms = columns.get("form") or []
    filed = columns.get("filingDate") or []
    reports = columns.get("reportDate") or []
    accessions = columns.get("accessionNumber") or []
    rows: list[FilingPeriod] = []
    for form, filing_date, report_date, accession in zip(forms, filed, reports,
                                                         accessions,
                                                         strict=False):
        try:
            date.fromisoformat(str(report_date))
            date.fromisoformat(str(filing_date))
        except ValueError:
            continue
        rows.append(
            FilingPeriod(
                form=str(form), filing_date=str(filing_date),
                report_date=str(report_date), accession=str(accession)))
    return rows


async def _submissions(ticker: str) -> _Submissions:
    key = ticker.upper()
    cached = _submissions_cache.get(key)
    if cached is not None and cached.day == _today():
        return cached
    cik = await _cik_for(key)
    data = await _sec_get_json(
        _SUBMISSIONS_URL.format(cik=cik),
        op_name=f"sec_edgar.submissions[{key}]",
        what=f"SEC submissions lookup for {key}")
    if not isinstance(data, dict):
        raise ProviderRequestError(
            f"SEC submissions response for {key} had an unexpected shape")
    filings = data.get("filings") or {}
    pages = [(
        str(page.get("name")), str(page.get("filingFrom")),
        str(page.get("filingTo"))) for page in (filings.get("files") or [])
             if isinstance(page, dict) and page.get("name")]
    entry = _Submissions(
        day=_today(), rows=_rows_from_columns(filings.get("recent")),
        pages=pages)
    _submissions_cache[key] = entry
    return entry


def _match(
        rows: list[FilingPeriod], form: str,
        filing_date: str) -> FilingPeriod | None:
    for row in rows:
        if row.form == form and row.filing_date == filing_date:
            return row
    return None


async def filing_period(
        ticker: str, form: str, filing_date: str) -> FilingPeriod | None:
    try:
        subs = await _submissions(ticker)
        found = _match(subs.rows, form, filing_date)
        if found is not None:
            return found
        for page in list(subs.pages):
            name, first, last = page
            if not (first <= filing_date <= last) or page not in subs.pages:
                continue
            data = await _sec_get_json(
                _SUBMISSIONS_PAGE_URL.format(name=name),
                op_name=f"sec_edgar.submissions_page[{ticker.upper()}]",
                what=f"SEC submissions page {name}")
            if page not in subs.pages:
                continue
            subs.rows.extend(_rows_from_columns(data))
            subs.pages.remove(page)
        return _match(subs.rows, form, filing_date)
    except ProviderError as exc:
        logger.info(
            "sec_edgar.filing_period_unavailable", ticker=ticker, form=form,
            filing_date=filing_date, error=str(exc))
        return None


_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100
_YEAR_AGO_MIN_DAYS = 350
_YEAR_AGO_MAX_DAYS = 380
_YEAR_DAYS = 364
_Q_REVENUE_TAGS = (*_REVENUE_TAGS, "RevenuesNetOfInterestExpense")
_Q_NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
_NONOPERATING_TAGS = ("NonoperatingIncomeExpense", )
_INCOME_TAX_TAGS = ("IncomeTaxExpenseBenefit", )
_RD_EXPENSE_TAGS = ("ResearchAndDevelopmentExpense", )
_SGA_EXPENSE_TAGS = ("SellingGeneralAndAdministrativeExpense", )
_POLICY_BENEFITS_TAGS = ("PolicyholderBenefitsAndClaimsIncurredNet", )


@dataclass(frozen=True)
class XbrlItem:
    key: str
    label: str
    current: float | None
    prior: float | None
    signed: bool = False
    derived: bool = False
    prior_derived: bool = False
    computed: bool = False


@dataclass(frozen=True)
class _QuarterValue:
    start: date
    end: date
    value: float
    form: str
    filed: str
    derived: bool


@dataclass(frozen=True)
class QuarterResults:
    ticker: str
    form: str
    filed: str
    period_start: str
    period_end: str
    prior_period_end: str | None
    derived: bool
    revenue: float | None
    revenue_prior: float | None
    operating_income: float | None
    operating_income_prior: float | None
    net_income: float | None
    net_income_prior: float | None
    nonoperating: float | None
    income_tax: float | None
    newer_filing: str | None = None
    year_to_date: "YearToDate | None" = None
    gross_margin_pct: float | None = None
    gross_margin_pct_prior: float | None = None
    rd_expense: float | None = None
    rd_expense_prior: float | None = None
    sga_expense: float | None = None
    sga_expense_prior: float | None = None
    below_operating: tuple[XbrlItem, ...] = ()
    cash_flow: tuple[XbrlItem, ...] = ()
    cash_flow_period: str | None = None


@dataclass(frozen=True)
class YearToDate:
    period_start: str
    period_end: str
    months: int
    prior_period_start: str | None
    prior_period_end: str | None
    revenue: float | None
    revenue_prior: float | None
    operating_income: float | None
    operating_income_prior: float | None
    net_income: float | None
    net_income_prior: float | None
    below_operating: tuple[XbrlItem, ...] = ()


def _periodic_form(form: str) -> bool:
    return form.startswith(("10-Q", "10-K"))


def _duration_facts(
    facts_usd: list[dict[str,
                         Any]]) -> dict[tuple[date, date], dict[str, Any]]:
    by_period: dict[tuple[date, date], dict[str, Any]] = {}
    for fact in facts_usd:
        if not _periodic_form(str(fact.get("form", ""))):
            continue
        try:
            start = date.fromisoformat(str(fact["start"]))
            end = date.fromisoformat(str(fact["end"]))
            float(fact["val"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        existing = by_period.get((start, end))
        filed = str(fact.get("filed", ""))
        if existing is None or filed > str(existing.get("filed", "")):
            by_period[(start, end)] = fact
    return by_period


def _quarter_values(
        facts_usd: list[dict[str, Any]]) -> dict[date, _QuarterValue]:
    periods = _duration_facts(facts_usd)
    out: dict[date, _QuarterValue] = {}
    for (start, end), fact in periods.items():
        if _QUARTER_MIN_DAYS <= (end - start).days <= _QUARTER_MAX_DAYS:
            current = out.get(end)
            if current is None or fact.get("filed", "") > current.filed:
                out[end] = _QuarterValue(
                    start, end, float(fact["val"]), str(fact.get("form", "")),
                    str(fact.get("filed", "")), False)
    by_start: dict[date, list[tuple[date, dict[str, Any]]]] = {}
    for (start, end), fact in periods.items():
        by_start.setdefault(start, []).append((end, fact))
    for start, items in by_start.items():
        items.sort(key=lambda item: item[0])
        for (end1, fact1), (end2, fact2) in pairwise(items):
            if end2 in out:
                continue
            if (end1 - start).days < _QUARTER_MIN_DAYS:
                continue
            if (not _QUARTER_MIN_DAYS <=
                    (end2 - end1).days <= _QUARTER_MAX_DAYS):
                continue
            out[end2] = _QuarterValue(
                end1 + timedelta(days=1), end2,
                float(fact2["val"]) - float(fact1["val"]),
                str(fact2.get("form", "")), str(fact2.get("filed", "")), True)
    return out


def _tag_maps(us_gaap: dict[str, Any],
              tags: tuple[str, ...]) -> list[dict[date, _QuarterValue]]:
    maps: list[dict[date, _QuarterValue]] = []
    for tag in tags:
        usd = (us_gaap.get(tag) or {}).get("units", {}).get("USD")
        if isinstance(usd, list):
            values = _quarter_values(usd)
            if values:
                maps.append(values)
    return maps


def _current_and_prior(
        maps: list[dict[date, _QuarterValue]],
        end: date) -> tuple[_QuarterValue | None, _QuarterValue | None]:
    ordered = sorted(maps, key=lambda m: end not in m)
    current = next((m[end] for m in ordered if end in m), None)
    prior: _QuarterValue | None = None
    for values in ordered:
        candidates = [
            v for e, v in values.items()
            if _YEAR_AGO_MIN_DAYS <= (end - e).days <= _YEAR_AGO_MAX_DAYS
        ]
        if candidates:
            prior = min(
                candidates, key=lambda v: abs((end - v.end).days - _YEAR_DAYS))
            break
    return current, prior


def _quarter_results_from_facts(
        facts: dict[str, Any], ticker: str) -> QuarterResults | None:
    try:
        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        if not isinstance(us_gaap, dict):
            return None
        revenue = _tag_maps(us_gaap, _Q_REVENUE_TAGS)
        operating = _tag_maps(us_gaap, _OPERATING_INCOME_TAGS)
        net = _tag_maps(us_gaap, _Q_NET_INCOME_TAGS)
        ends = [
            e for maps in (revenue, operating, net) for m in maps for e in m
        ]
        if not ends:
            return None
        end = max(ends)
        rev_cur, rev_prior = _current_and_prior(revenue, end)
        op_cur, op_prior = _current_and_prior(operating, end)
        net_cur, net_prior = _current_and_prior(net, end)
        nonop_cur, _ = _current_and_prior(
            _tag_maps(us_gaap, _NONOPERATING_TAGS), end)
        tax_cur, _ = _current_and_prior(
            _tag_maps(us_gaap, _INCOME_TAX_TAGS), end)
        source = net_cur or rev_cur or op_cur
        if source is None:
            return None
        prior_any = net_prior or rev_prior or op_prior
        costs = _quarter_costs(us_gaap, end, rev_cur, rev_prior)
        ytd = (
            _year_to_date(us_gaap, end)
            if source.form.startswith("10-Q") else None)
        prior_end = prior_any.end if prior_any else None
        cash_flow, cash_flow_period = _cash_flow_for(
            us_gaap, form=source.form, start=source.start, end=end,
            prior_end=prior_end, ytd=ytd)
        return QuarterResults(
            ticker=ticker.upper(), form=source.form, filed=source.filed,
            period_start=source.start.isoformat(), period_end=end.isoformat(),
            prior_period_end=prior_any.end.isoformat() if prior_any else None,
            derived=source.derived, revenue=rev_cur.value if rev_cur else None,
            revenue_prior=rev_prior.value if rev_prior else None,
            operating_income=op_cur.value if op_cur else None,
            operating_income_prior=op_prior.value if op_prior else None,
            net_income=net_cur.value if net_cur else None,
            net_income_prior=net_prior.value if net_prior else None,
            nonoperating=nonop_cur.value if nonop_cur else None,
            income_tax=tax_cur.value if tax_cur else None, year_to_date=ytd,
            gross_margin_pct=costs.get("gross_margin_pct"),
            gross_margin_pct_prior=costs.get("gross_margin_pct_prior"),
            rd_expense=costs.get("rd_expense"),
            rd_expense_prior=costs.get("rd_expense_prior"),
            sga_expense=costs.get("sga_expense"),
            sga_expense_prior=costs.get("sga_expense_prior"),
            below_operating=_below_operating_items(
                us_gaap, _quarter_lookup(end, prior_end)), cash_flow=cash_flow,
            cash_flow_period=cash_flow_period)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("sec_edgar.quarter_results_failed", error=str(exc))
        return None


def _gross_profit(
        us_gaap: dict[str, Any], revenue: _QuarterValue) -> float | None:
    insurer = any(
        revenue.end in values
        for values in _tag_maps(us_gaap, _POLICY_BENEFITS_TAGS))
    for tags in (_GROSS_PROFIT_TAGS, _COST_OF_REVENUE_TAGS):
        if insurer and tags is _COST_OF_REVENUE_TAGS:
            logger.info(
                "sec_edgar.gross_margin_skipped_policy_benefits",
                period_end=revenue.end.isoformat())
            return None
        for values in _tag_maps(us_gaap, tags):
            value = values.get(revenue.end)
            if value is not None:
                return (
                    value.value if tags is _GROSS_PROFIT_TAGS else
                    (revenue.value - value.value))
    return None


def _margin_pct(
        gross: float | None, revenue: _QuarterValue | None) -> float | None:
    if gross is None or revenue is None or revenue.value <= 0:
        return None
    return round(gross / revenue.value * 100, 1)


def _quarter_costs(
        us_gaap: dict[str, Any], end: date, rev_cur: _QuarterValue | None,
        rev_prior: _QuarterValue | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    try:
        out["gross_margin_pct"] = _margin_pct(
            _gross_profit(us_gaap, rev_cur) if rev_cur else None, rev_cur)
        out["gross_margin_pct_prior"] = (
            _margin_pct(
                _gross_profit(us_gaap, rev_prior) if rev_prior else None,
                rev_prior) if out["gross_margin_pct"] is not None else None)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("sec_edgar.gross_margin_failed", error=str(exc))
    for name, tags in (("rd_expense", _RD_EXPENSE_TAGS), ("sga_expense",
                                                          _SGA_EXPENSE_TAGS)):
        try:
            current, prior = _current_and_prior(_tag_maps(us_gaap, tags), end)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "sec_edgar.cost_metric_failed", metric=name, error=str(exc))
            continue
        out[name] = current.value if current else None
        out[f"{name}_prior"] = prior.value if current and prior else None
    return out


_XbrlGroup = tuple[str, bool, tuple[tuple[str, str], ...]]
_PRETAX_KEY = "pretax_income"
_NONOPERATING_KEY = "nonoperating_total"
_BELOW_OPERATING_GROUPS: tuple[_XbrlGroup, ...] = (
    (
        _PRETAX_KEY, True, ((
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
            "ExtraordinaryItemsNoncontrollingInterest", "pretax income"), (
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                "MinorityInterestAndIncomeLossFromEquityMethodInvestments",
                "pretax income"))), (
                    _NONOPERATING_KEY, True, ((
                        "NonoperatingIncomeExpense",
                        "non-operating income (expense), total"), )), (
                            "other_nonoperating", True, ((
                                "OtherNonoperatingIncomeExpense",
                                "other non-operating income (expense)"), )),
    (
        "equity_securities_gains",
        True, ((
            "EquitySecuritiesFvNiGainLoss",
            "gains (losses) on equity securities"),
               (
                   "EquitySecuritiesFvNiUnrealizedGainLoss",
                   "unrealized gains (losses) on equity securities"),
               ("GainLossOnInvestments", "gains (losses) on investments"))),
    (
        "equity_upward_adjustments", False, ((
            "EquitySecuritiesWithoutReadilyDeterminableFairValue"
            "UpwardPrice"
            "AdjustmentAnnualAmount",
            "upward adjustments of equity securities without a readily "
            "determinable fair value (gain)"), )),
    (
        "equity_downward_adjustments", False, ((
            "EquitySecuritiesWithoutReadilyDeterminableFairValue"
            "DownwardPrice"
            "AdjustmentAnnualAmount",
            "downward adjustments of equity securities without a readily "
            "determinable fair value (loss)"), )), (
                "impairment", False, (
                    (
                        "GoodwillAndIntangibleAssetImpairment",
                        "goodwill and intangible asset impairment (charge)"),
                    ("GoodwillImpairmentLoss", "goodwill impairment (charge)"),
                    ("AssetImpairmentCharges", "asset impairment charges"), (
                        "ImpairmentOfIntangibleAssetsExcludingGoodwill",
                        "intangible asset impairment (charge)"))),
    (
        "restructuring", False,
        (("RestructuringCharges", "restructuring charges"), (
            "RestructuringSettlementAndImpairmentProvisions",
            "restructuring, settlement and impairment provisions (charge)"))),
    (
        "income_tax", False,
        (("IncomeTaxExpenseBenefit", "income tax expense (benefit)"), )))
_NONOPERATING_COMPUTED_LABEL = (
    "non-operating total (computed: pretax income minus operating income)")
_OCF_KEY = "operating_cash_flow"
_CAPEX_KEY = "capital_expenditures"
_FREE_CASH_FLOW_KEY = "free_cash_flow"
_FREE_CASH_FLOW_LABEL = (
    "free cash flow (computed: operating cash flow minus capital expenditures)"
)
_AFTER_RETURNS_KEY = "free_cash_flow_after_returns"
_RETURN_KEYS = ("share_repurchases", "dividends_paid")
_RETURN_NAMES = {
    "share_repurchases": "buybacks",
    "dividends_paid": "dividends"
}
_RETURN_LABELS = {
    "share_repurchases": "share repurchases",
    "dividends_paid": "dividends paid"
}
_CASH_FLOW_GROUPS: tuple[_XbrlGroup, ...] = (
    (
        _OCF_KEY, False,
        (("NetCashProvidedByUsedInOperatingActivities",
          "operating cash flow"), )), (
              _CAPEX_KEY, False,
              ((
                  "PaymentsToAcquirePropertyPlantAndEquipment",
                  "capital expenditures"),
               ("PaymentsToAcquireProductiveAssets", "capital expenditures"))),
    (
        "share_repurchases", False,
        (("PaymentsForRepurchaseOfCommonStock", "share repurchases"), )), (
            "dividends_paid", False,
            (("PaymentsOfDividendsCommonStock", "dividends paid"),
             ("PaymentsOfDividends", "dividends paid"),
             ("PaymentsOfOrdinaryDividends", "dividends paid"))),
    (
        "long_term_debt_issued", False,
        (("ProceedsFromIssuanceOfLongTermDebt", "long-term debt issued"),
         ("ProceedsFromIssuanceOfDebt", "debt issued"),
         ("ProceedsFromDebtNetOfIssuanceCosts", "debt issued (proceeds "
          "after issuance costs, before repayments)"))), (
              "long_term_debt_repaid", False,
              (("RepaymentsOfLongTermDebt", "long-term debt repaid"),
               ("RepaymentsOfDebt", "debt repaid"))),
    (
        "net_change_in_cash", True, ((
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
            "PeriodIncreaseDecreaseIncludingExchangeRateEffect",
            "net change in cash, cash equivalents and restricted cash"), (
                "CashAndCashEquivalentsPeriodIncreaseDecrease",
                "net change in cash and cash equivalents"))))

_Found = tuple[float, bool]
_Lookup = Callable[[list[dict[str, Any]]], tuple[_Found | None, _Found | None]]


def _quarter_lookup(end: date, prior_end: date | None) -> _Lookup:

    def look(usd: list[dict[str, Any]]) -> tuple[_Found | None, _Found | None]:
        values = _quarter_values(usd)
        current = values.get(end)
        prior = values.get(prior_end) if prior_end is not None else None
        return ((current.value, current.derived) if current else None,
                (prior.value, prior.derived) if prior else None)

    return look


def _exact_lookup(
        current: tuple[date, date],
        prior: tuple[date, date] | None) -> _Lookup:

    def look(usd: list[dict[str, Any]]) -> tuple[_Found | None, _Found | None]:
        facts = _duration_facts(usd)
        now = facts.get(current)
        before = facts.get(prior) if prior is not None else None
        return ((float(now["val"]), False) if now else None,
                (float(before["val"]), False) if before else None)

    return look


def _span_lookup(
        end: date, prior_end: date | None, min_days: int,
        max_days: int) -> _Lookup:

    def pick(
            facts: dict[tuple[date, date], dict[str, Any]],
            at: date) -> _Found | None:
        spans = [(e - s, fact) for (s, e), fact in facts.items()
                 if e == at and min_days <= (e - s).days <= max_days]
        if not spans:
            return None
        return float(max(spans, key=lambda item: item[0])[1]["val"]), False

    def look(usd: list[dict[str, Any]]) -> tuple[_Found | None, _Found | None]:
        facts = _duration_facts(usd)
        return pick(
            facts,
            end), (pick(facts, prior_end) if prior_end is not None else None)

    return look


def _tag_values(us_gaap: dict[str, Any], tag: str,
                lookup: _Lookup) -> tuple[_Found | None, _Found | None]:
    usd = (us_gaap.get(tag) or {}).get("units", {}).get("USD")
    return lookup(usd) if isinstance(usd, list) else (None, None)


def _group_item(
        us_gaap: dict[str, Any], group: _XbrlGroup,
        lookup: _Lookup) -> XbrlItem | None:
    key, signed, tags = group
    found = [(label, *_tag_values(us_gaap, tag, lookup))
             for tag, label in tags]
    for index in (1, 2):
        for row in found:
            value = row[index]
            if isinstance(value, tuple) and value[0] != 0:
                label, current, prior = row
                return XbrlItem(
                    key=key, label=str(label),
                    current=current[0] if isinstance(current, tuple) else None,
                    prior=prior[0] if isinstance(prior, tuple) else None,
                    signed=signed, derived=isinstance(current, tuple)
                    and current[1], prior_derived=isinstance(prior, tuple)
                    and prior[1])
    return None


def _difference(a: float | None, b: float | None) -> float | None:
    return a - b if a is not None and b is not None else None


def _below_operating_items(us_gaap: dict[str, Any],
                           lookup: _Lookup) -> tuple[XbrlItem, ...]:
    try:
        items = [
            item for group in _BELOW_OPERATING_GROUPS
            if (item := _group_item(us_gaap, group, lookup)) is not None
        ]
        pretax = next((i for i in items if i.key == _PRETAX_KEY), None)
        operating = _group_item(
            us_gaap, ("operating", True, (("OperatingIncomeLoss", ""), )),
            lookup)
        if pretax is not None and operating is not None:
            current = _difference(pretax.current, operating.current)
            prior = _difference(pretax.prior, operating.prior)
            if current is not None or prior is not None:
                items = [i for i in items if i.key != _NONOPERATING_KEY]
                items.insert(
                    1,
                    XbrlItem(
                        key=_NONOPERATING_KEY,
                        label=_NONOPERATING_COMPUTED_LABEL, current=current,
                        prior=prior, signed=True, derived=pretax.derived
                        or operating.derived,
                        prior_derived=pretax.prior_derived
                        or operating.prior_derived, computed=True))
        return tuple(items)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("sec_edgar.below_operating_failed", error=str(exc))
        return ()


def _cash_flow_items(us_gaap: dict[str, Any],
                     lookup: _Lookup) -> tuple[XbrlItem, ...]:
    try:
        items = [
            item for group in _CASH_FLOW_GROUPS
            if (item := _group_item(us_gaap, group, lookup)) is not None
        ]
        by_key = {item.key: item for item in items}
        ocf, capex = by_key.get(_OCF_KEY), by_key.get(_CAPEX_KEY)
        if ocf is not None and capex is not None:
            current = _difference(ocf.current, capex.current)
            prior = _difference(ocf.prior, capex.prior)
            if current is not None or prior is not None:
                items.insert(
                    items.index(capex) + 1,
                    XbrlItem(
                        key=_FREE_CASH_FLOW_KEY, label=_FREE_CASH_FLOW_LABEL,
                        current=current, prior=prior, signed=True,
                        computed=True))
        return _with_after_returns(items)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("sec_edgar.cash_flow_failed", error=str(exc))
        return ()


def _after_returns(
        fcf: float | None, returns: list[float | None]) -> float | None:
    if fcf is None or any(value is None for value in returns):
        return None
    return fcf - sum(value for value in returns if value is not None)


def _with_after_returns(items: list[XbrlItem]) -> tuple[XbrlItem, ...]:
    by_key = {item.key: item for item in items}
    fcf = by_key.get(_FREE_CASH_FLOW_KEY)
    returns = [by_key[key] for key in _RETURN_KEYS if key in by_key]
    if fcf is None or not returns:
        return tuple(items)
    current = _after_returns(fcf.current, [r.current for r in returns])
    prior = _after_returns(fcf.prior, [r.prior for r in returns])
    if current is None and prior is None:
        return tuple(items)
    minus = " minus ".join(r.label for r in returns)
    after = " and ".join(_RETURN_NAMES[r.key] for r in returns)
    missing = "".join(
        f", no {_RETURN_LABELS[key]} line in the data" for key in _RETURN_KEYS
        if key not in by_key)
    items.insert(
        max(items.index(r) for r in returns) + 1,
        XbrlItem(
            key=_AFTER_RETURNS_KEY, label=(
                f"free cash flow after {after} (computed: free cash flow "
                f"minus {minus}{missing})"), current=current, prior=prior,
            signed=True, computed=True))
    return tuple(items)


def _cash_flow_for(
        us_gaap: dict[str, Any], *, form: str, start: date, end: date,
        prior_end: date | None,
        ytd: YearToDate | None) -> tuple[tuple[XbrlItem, ...], str | None]:
    compared = (
        f", compared with the same period ended {prior_end}"
        if prior_end else "")
    if ytd is not None:
        prior = ((
            date.fromisoformat(ytd.prior_period_start),
            date.fromisoformat(ytd.prior_period_end))
                 if ytd.prior_period_start and ytd.prior_period_end else None)
        lookup = _exact_lookup((
            date.fromisoformat(
                ytd.period_start), date.fromisoformat(ytd.period_end)), prior)
        label = (
            f"year to date, {ytd.months} months {ytd.period_start} to "
            f"{ytd.period_end}" + (
                f", compared with {ytd.prior_period_start} to "
                f"{ytd.prior_period_end}" if prior else ""))
    elif form.startswith("10-K"):
        lookup = _span_lookup(
            end, prior_end, _YEAR_AGO_MIN_DAYS, _YEAR_AGO_MAX_DAYS)
        label = f"fiscal year ended {end}{compared}"
    else:
        lookup = _span_lookup(
            end, prior_end, _QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS)
        label = f"quarter {start} to {end}{compared}"
    items = _cash_flow_items(us_gaap, lookup)
    return items, (label if items else None)


_FISCAL_START_TOLERANCE_DAYS = 7
_YTD_LENGTH_TOLERANCE_DAYS = 10


@dataclass(frozen=True)
class _Period:
    start: date
    end: date
    value: float
    fiscal_period: str = ""


def _group_periods(us_gaap: dict[str, Any],
                   tags: tuple[str, ...]) -> list[list[_Period]]:
    groups: list[list[_Period]] = []
    for tag in tags:
        usd = (us_gaap.get(tag) or {}).get("units", {}).get("USD")
        if not isinstance(usd, list):
            continue
        periods = [
            _Period(start, end, float(fact["val"]), str(fact.get("fp") or ""))
            for (start, end), fact in _duration_facts(usd).items()
        ]
        if periods:
            groups.append(periods)
    return groups


def _fiscal_year_ends(groups: list[list[_Period]]) -> list[date]:
    return [
        p.end for periods in groups for p in periods
        if _YEAR_AGO_MIN_DAYS <= (p.end - p.start).days <= _YEAR_AGO_MAX_DAYS
    ]


def _starts_fiscal_year(start: date, year_ends: list[date]) -> bool:
    return any(
        0 < (start - end).days <= 1 + _FISCAL_START_TOLERANCE_DAYS
        for end in year_ends)


_YTD_FISCAL_PERIODS = ("Q2", "Q3")


def _is_year_to_date(period: _Period, year_ends: list[date]) -> bool:
    return _starts_fiscal_year(
        period.start,
        year_ends) or (period.fiscal_period in _YTD_FISCAL_PERIODS)


def _ytd_pair(groups: list[list[_Period]], end: date,
              year_ends: list[date]) -> tuple[_Period | None, _Period | None]:
    for periods in groups:
        current = max((
            p for p in periods
            if p.end == end and _QUARTER_MAX_DAYS < (p.end - p.start).days <
            _YEAR_AGO_MIN_DAYS and _is_year_to_date(p, year_ends)),
                      key=lambda p: (p.end - p.start).days, default=None)
        if current is None:
            continue
        length = (current.end - current.start).days
        prior = min((
            p for p in periods
            if _YEAR_AGO_MIN_DAYS <= (end - p.end).days <= _YEAR_AGO_MAX_DAYS
            and abs((p.end - p.start).days -
                    length) <= _YTD_LENGTH_TOLERANCE_DAYS), key=lambda p: abs(
                        (end - p.end).days - _YEAR_DAYS), default=None)
        return current, prior
    return None, None


def _year_to_date(us_gaap: dict[str, Any], end: date) -> YearToDate | None:
    groups = {
        name: _group_periods(us_gaap, tags)
        for name, tags in (("revenue", _Q_REVENUE_TAGS),
                           ("operating", _OPERATING_INCOME_TAGS),
                           ("net", _Q_NET_INCOME_TAGS))
    }
    year_ends = _fiscal_year_ends([g for gs in groups.values() for g in gs])
    pairs = {name: _ytd_pair(g, end, year_ends) for name, g in groups.items()}
    for first, second in pairs.values():
        if first is not None and second is not None:
            current, prior = first, second
            break
    else:
        logger.info(
            "sec_edgar.year_to_date_unavailable", period_end=end.isoformat(),
            fiscal_year_ends=len(year_ends), metrics={
                name: (
                    "no tag" if not groups[name] else (
                        "no year-to-date fact"
                        if pair[0] is None else "no prior year to date"))
                for name, pair in pairs.items()
            })
        return None

    def value(name: str, index: int) -> float | None:
        period = pairs[name][index]
        return period.value if period else None

    days = (current.end - current.start).days + 1
    return YearToDate(
        period_start=current.start.isoformat(),
        period_end=current.end.isoformat(), months=round(days / 30.44),
        prior_period_start=prior.start.isoformat(),
        prior_period_end=prior.end.isoformat(), revenue=value("revenue", 0),
        revenue_prior=value("revenue",
                            1), operating_income=value("operating", 0),
        operating_income_prior=value("operating",
                                     1), net_income=value("net", 0),
        net_income_prior=value("net",
                               1), below_operating=_below_operating_items(
                                   us_gaap,
                                   _exact_lookup((current.start, current.end),
                                                 (prior.start, prior.end))))


_quarter_cache: dict[str, tuple[date, QuarterResults | None]] = {}


def _store_quarter_results(
        ticker: str, results: QuarterResults | None) -> None:
    _quarter_cache[ticker.upper()] = (_today(), results)


async def latest_quarter_results(ticker: str) -> QuarterResults | None:
    key = ticker.upper()
    cached = _quarter_cache.get(key)
    if cached is not None and cached[0] == _today():
        if cached[1] is not None and cached[1].newer_filing is None:
            checked = await _with_newer_filing(cached[1])
            _store_quarter_results(key, checked)
            return checked
        return cached[1]
    try:
        facts = await _company_facts(key)
    except ProviderError as exc:
        logger.info(
            "sec_edgar.latest_quarter_unavailable", ticker=key, error=str(exc))
        return None
    results = _quarter_results_from_facts(facts, key)
    if results is not None:
        results = await _with_newer_filing(results)
    _store_quarter_results(key, results)
    return results


async def _with_newer_filing(results: QuarterResults) -> QuarterResults:
    try:
        subs = await _submissions(results.ticker)
    except ProviderError:
        return results
    newer = [
        row for row in subs.rows
        if row.form in ("10-Q", "10-K") and row.filing_date > results.filed
    ]
    if not newer:
        return results
    newest = max(newer, key=lambda row: row.filing_date)
    return replace(
        results, newer_filing=f"{newest.form} filed {newest.filing_date}")


def _yoy_pct(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    if (current < 0) != (prior < 0):
        return None
    return round((current - prior) / abs(prior) * 100, 1)


def _usd(value: float, *, signed: bool = False) -> str:
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    size = abs(value)
    if size >= 1e9:
        return f"{sign}${size / 1e9:.2f}B"
    if size >= 1e6:
        return f"{sign}${size / 1e6:.0f}M"
    return f"{sign}${size:,.0f}"


def _metric_text(
        name: str, current: float | None, prior: float | None, *,
        prior_label: str = "year-ago quarter") -> str:
    if current is None:
        return f"{name}: not reported by the filer"
    if prior is None:
        return f"{name} {_usd(current)} ({prior_label} not reported)"
    pct = _yoy_pct(current, prior)
    if pct is not None and current < 0 and prior < 0:
        change = (
            f"loss {'narrowed' if current > prior else 'widened'} "
            f"{abs(pct):.0f}%")
    elif pct is not None:
        change = f"{pct:+.0f}%"
    elif prior == 0:
        change = "year-ago value zero"
    else:
        change = "swung to a loss" if current < 0 else "swung to a profit"
    return f"{name} {_usd(current)} vs {_usd(prior)} ({change})"


def quarter_results_payload(q: QuarterResults) -> dict[str, Any]:
    compared = (
        f", compared with the quarter ended {q.prior_period_end}"
        if q.prior_period_end else "")
    derived = (
        "; quarter derived from year-to-date figures" if q.derived else "")
    title = (
        f"Latest quarter in SEC XBRL -- the newer {q.newer_filing} is not in "
        "XBRL yet" if q.newer_filing else "Latest quarter")
    head = (
        f"{title} (SEC XBRL, {q.form} filed {q.filed}, quarter "
        f"{q.period_start} to {q.period_end}{compared}{derived}): ")
    line = head + "; ".join([
        _metric_text("revenue", q.revenue, q.revenue_prior),
        _metric_text(
            "operating income", q.operating_income, q.operating_income_prior),
        _metric_text("net income", q.net_income, q.net_income_prior)
    ])
    costs_line = _costs_text(q)
    if costs_line:
        line += "." + costs_line
    net_minus_operating = (
        q.net_income - q.operating_income if q.net_income is not None
        and q.operating_income is not None else None)
    text = line
    if net_minus_operating is not None:
        parts = []
        if q.nonoperating is not None and not q.below_operating:
            parts.append(
                f"nonoperating income/expense "
                f"{_usd(q.nonoperating, signed=True)}")
        if q.income_tax is not None and not q.below_operating:
            parts.append(f"income tax expense {_usd(q.income_tax)}")
        components = f" ({'; '.join(parts)})" if parts else ""
        text += (
            f"\nNet income minus operating income: "
            f"{_usd(net_minus_operating, signed=True)}{components}")
    below = _items_text(q.below_operating)
    if below:
        compared = (
            f", compared with the quarter ended {q.prior_period_end}"
            if q.prior_period_end else "")
        text += (
            f"\nBelow operating income (SEC XBRL, quarter ended {q.period_end}"
            f"{compared}): {below}.")
    ytd_line = _year_to_date_text(q.year_to_date)
    if ytd_line:
        text += "\n" + ytd_line
    ytd_below = (
        _items_text(q.year_to_date.below_operating) if q.year_to_date else "")
    if ytd_below and q.year_to_date is not None:
        text += (
            f"\nBelow operating income, year to date ({q.year_to_date.months} "
            f"months ended {q.year_to_date.period_end}, compared with the "
            f"period "
            f"ended {q.year_to_date.prior_period_end}): {ytd_below}.")
    cash = _items_text(q.cash_flow)
    if cash:
        text += f"\nCash flow (SEC XBRL, {q.cash_flow_period}): {cash}."
    shown = (
        *q.below_operating, *q.cash_flow,
        *(q.year_to_date.below_operating if q.year_to_date else ()))
    if shown:
        text += (
            "\n" + _ITEMS_NOTE + (
                " " + _DERIVED_NOTE if any(
                    i.derived or i.prior_derived for i in shown) else ""))
    return {
        "text": text,
        "form": q.form,
        "filed": q.filed,
        "period_start": q.period_start,
        "period_end": q.period_end,
        "revenue": q.revenue,
        "revenue_prior": q.revenue_prior,
        "revenue_yoy_pct": _yoy_pct(q.revenue, q.revenue_prior),
        "operating_income": q.operating_income,
        "operating_income_prior": q.operating_income_prior,
        "operating_income_yoy_pct": _yoy_pct(
            q.operating_income, q.operating_income_prior),
        "net_income": q.net_income,
        "net_income_prior": q.net_income_prior,
        "net_income_yoy_pct": _yoy_pct(q.net_income, q.net_income_prior),
        "net_minus_operating": net_minus_operating,
        "newer_filing": q.newer_filing,
        "year_to_date": _year_to_date_payload(q.year_to_date),
        **_costs_payload(q),
        **({
            "below_operating": _items_payload(q.below_operating)
        } if q.below_operating else {}),
        **({
            "cash_flow": {
                "period": q.cash_flow_period,
                "items": _items_payload(q.cash_flow)
            }
        } if q.cash_flow else {})
    }


_ITEMS_NOTE = (
    "Only items the filer tags with these standard XBRL concepts are listed "
    "(an item tagged only with a company-specific concept is not). Items can "
    "overlap (one charge can carry several concepts, e.g. an impairment also "
    "tagged as restructuring): never add them; cite a single item or the "
    "total.")
_EQUITY_GAINS_KEY = "equity_securities_gains"
_EQUITY_ADJUSTMENT_KEYS = frozenset(
    {"equity_upward_adjustments", "equity_downward_adjustments"})


def _within(part: XbrlItem, total: XbrlItem) -> bool:
    pairs = [(a, b) for a, b in ((part.current, total.current),
                                 (part.prior, total.prior))
             if a is not None and b is not None]
    return bool(pairs) and all(abs(a) <= abs(b) for a, b in pairs)


_DERIVED_NOTE = "* = derived as the difference of two year-to-date facts."


def _item_value(value: float, *, signed: bool, derived: bool) -> str:
    return _usd(value, signed=signed) + ("*" if derived else "")


def _items_text(items: tuple[XbrlItem, ...]) -> str:
    parts: list[str] = []
    gains = next((i for i in items if i.key == _EQUITY_GAINS_KEY), None)
    for item in items:
        current = (
            _item_value(
                item.current, signed=item.signed, derived=item.derived)
            if item.current is not None else "not tagged")
        label, included = item.label, ""
        if (gains is not None and item.key in _EQUITY_ADJUSTMENT_KEYS
                and _within(item, gains)):
            label = f"of which {label}"
            included = f" (included in the {gains.label} above)"
        if item.prior is None:
            parts.append(
                f"{label} {current} (comparison period not tagged){included}")
            continue
        prior = _item_value(
            item.prior, signed=item.signed, derived=item.prior_derived)
        parts.append(f"{label} {current} vs {prior}{included}")
    return "; ".join(parts)


def _items_payload(items: tuple[XbrlItem, ...]) -> list[dict[str, Any]]:
    return [{
        "key": item.key,
        "label": item.label,
        "current": item.current,
        "prior": item.prior,
        "derived": item.derived,
        "prior_derived": item.prior_derived,
        "computed": item.computed
    } for item in items]


def _costs_text(q: QuarterResults) -> str:
    parts: list[str] = []
    if q.gross_margin_pct is not None:
        prior = (
            f" vs {q.gross_margin_pct_prior:.1f}%"
            if q.gross_margin_pct_prior is not None else "")
        parts.append(f"gross margin {q.gross_margin_pct:.1f}%{prior}")
    if q.rd_expense is not None:
        parts.append(_metric_text("R&D", q.rd_expense, q.rd_expense_prior))
    if q.sga_expense is not None:
        parts.append(_metric_text("SG&A", q.sga_expense, q.sga_expense_prior))
    return f" Costs: {'; '.join(parts)}." if parts else ""


def _costs_payload(q: QuarterResults) -> dict[str, float]:
    values: dict[str, float | None] = {
        "gross_margin_pct": q.gross_margin_pct,
        "gross_margin_pct_prior": q.gross_margin_pct_prior,
        "rd_expense": q.rd_expense,
        "rd_expense_prior": q.rd_expense_prior,
        "rd_expense_yoy_pct": _yoy_pct(q.rd_expense, q.rd_expense_prior),
        "sga_expense": q.sga_expense,
        "sga_expense_prior": q.sga_expense_prior,
        "sga_expense_yoy_pct": _yoy_pct(q.sga_expense, q.sga_expense_prior)
    }
    return {key: value for key, value in values.items() if value is not None}


def _year_to_date_text(ytd: YearToDate | None) -> str:
    if ytd is None:
        return ""
    compared = (
        f", compared with {ytd.prior_period_start} to {ytd.prior_period_end}"
        if ytd.prior_period_start and ytd.prior_period_end else "")
    label = "prior year to date"
    return (
        f"Year to date ({ytd.months} months {ytd.period_start} to "
        f"{ytd.period_end}{compared}): " + "; ".join([
            _metric_text(
                "revenue", ytd.revenue, ytd.revenue_prior, prior_label=label),
            _metric_text(
                "operating income", ytd.operating_income,
                ytd.operating_income_prior, prior_label=label),
            _metric_text(
                "net income", ytd.net_income, ytd.net_income_prior,
                prior_label=label)
        ]))


def _year_to_date_payload(ytd: YearToDate | None) -> dict[str, Any] | None:
    if ytd is None:
        return None
    return {
        "period_start": ytd.period_start,
        "period_end": ytd.period_end,
        "months": ytd.months,
        "prior_period_start": ytd.prior_period_start,
        "prior_period_end": ytd.prior_period_end,
        "revenue": ytd.revenue,
        "revenue_prior": ytd.revenue_prior,
        "revenue_yoy_pct": _yoy_pct(ytd.revenue, ytd.revenue_prior),
        "operating_income": ytd.operating_income,
        "operating_income_prior": ytd.operating_income_prior,
        "operating_income_yoy_pct": _yoy_pct(
            ytd.operating_income, ytd.operating_income_prior),
        "net_income": ytd.net_income,
        "net_income_prior": ytd.net_income_prior,
        "net_income_yoy_pct": _yoy_pct(ytd.net_income, ytd.net_income_prior),
        **({
            "below_operating": _items_payload(ytd.below_operating)
        } if ytd.below_operating else {})
    }


async def latest_quarter_payload(ticker: str) -> dict[str, Any] | None:
    results = await latest_quarter_results(ticker)
    return quarter_results_payload(results) if results is not None else None


def forget_filing_caches(ticker: str) -> None:
    _submissions_cache.pop(ticker.upper(), None)
    _quarter_cache.pop(ticker.upper(), None)


def _reset_caches_for_tests() -> None:
    _submissions_cache.clear()
    _quarter_cache.clear()
