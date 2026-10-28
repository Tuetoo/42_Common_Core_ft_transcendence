import bisect
import calendar
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol
import structlog
from config import settings

logger = structlog.get_logger(__name__)

RECORDS_MARKER = "=== FILING RECORDS JSON ==="

LEGAL_STATUSES = (
    "pending", "dismissed", "settled", "judgment", "appeal", "other")
TRANSACTION_TYPES = (
    "acquisition", "divestiture", "investment", "financing", "guarantee",
    "other")
TRANSACTION_STATUSES = ("announced", "pending", "completed", "terminated")

CATEGORIES: dict[str, tuple[str, str]] = {
    "one_off_items": ("O", "unusual item"),
    "guidance": ("G", "guidance"),
    "transactions": ("T", "transaction"),
    "legal_matters": ("L", "legal matter")
}
_LETTER_TO_CATEGORY = {letter: key for key, (letter, _) in CATEGORIES.items()}
_TITLE_FIELDS = {
    "one_off_items": "description",
    "guidance": "metric",
    "transactions": "name",
    "legal_matters": "name"
}

_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "one_off_items": (("description", "amount_text", "quote", "period_end"),
                      ()),
    "guidance": (("metric", "value_text", "quote"), ("period_covered", )),
    "transactions": (("name", "type", "status", "quote"), ("date", "detail")),
    "legal_matters": (("name", "status", "quote"),
                      ("status_date", "detail", "exposure_text"))
}
_ENUMS: dict[tuple[str, str],
             tuple[str,
                   ...]] = {("legal_matters", "status"): LEGAL_STATUSES,
                            ("transactions", "type"): TRANSACTION_TYPES,
                            ("transactions", "status"): TRANSACTION_STATUSES}
_DATE_FIELDS = {("legal_matters", "status_date"), ("transactions", "date")}
_AMOUNT_FIELDS = {("one_off_items", "amount_text"),
                  ("legal_matters", "exposure_text")}
_AMOUNTS_IN_QUOTE = {("one_off_items", "amount_text")}
_UNIT_CHECK_FIELDS = _AMOUNT_FIELDS | {("guidance", "value_text")}
_UNIT_REQUIRED_MIN = 10.0
OWN_PERIOD_MONTHS = {"10-Q": 3, "10-K": 12}
_PERIOD_END_TOLERANCE_DAYS = 7
QUOTE_MIN_CHARS = 25
_QUOTE_PART_MIN_CHARS = 12

_RECORDS_SCHEMA = (
    f"one line containing exactly {RECORDS_MARKER} and then one JSON object "
    "(no code fence, nothing after it) with these four keys, each a list "
    "(an empty list when the filing has none):\n"
    '- "one_off_items": [{"description": an unusual item: an amount '
    "recorded in the period's income statement -- a charge, gain or loss -- "
    "that is material to results (impairments, restructuring, litigation "
    "charges, gains or losses on divestitures or on investments, "
    "remeasurement gains or losses, unusual tax charges or benefits); it may "
    "recur in later periods. Never a balance as of a date (a fair value, a "
    "reserve, an accrual or an accumulated gain at the period end is not an "
    "income-statement amount), and never a recurring charge the company "
    "records every period as part of its normal results (amortization of "
    "acquired intangibles, depreciation, routine stock-based compensation): "
    "those are not unusual items. A material gain or loss on equity "
    "investments, derivatives or other remeasurements IS an unusual item "
    "even when such a line appears every period -- list the largest ones "
    'first, "amount_text": its amount (a loss or charge the filing writes '
    'in parentheses keeps a minus sign, e.g. "-$1,199 million"), '
    '"period_end": the last day of the period it was recorded in '
    '("YYYY-MM-DD"), "period_months": the length in months (3, 6, 9 or 12) '
    "of the period that the quote itself states for this amount -- an amount "
    "the quote gives for the first half, the six or nine months or the year "
    "to date is 6 or 9, not 3, even in a quarterly report, "
    '"quote": a sentence of the filing that states this amount and its '
    "period; use a table row only when no sentence states it, and then "
    "include the row's period header words in the quote when the filing "
    "places them on the same line (the quote must still be found in the "
    "filing word for word)}]. List only items recorded in this filing's own "
    "period -- the three months ended on the period end of a 10-Q, the "
    "fiscal year of a 10-K -- not year-to-date or prior-year figures.\n"
    '- "guidance": [{"metric": what the company guides -- a financial target '
    "or a dated operating target (a production or delivery rate, a "
    "delivery, certification or launch date), "
    '"value_text": the guided value as the filing states it, '
    '"period_covered": the period the guidance is for, "quote": the sentence '
    "that states it}]\n"
    '- "transactions": [{"name": the acquisition, divestiture, investment in '
    "another company, material financing (debt or equity issued, a new "
    "credit facility) or guarantee given to another party, "
    '"type": one of "acquisition", "divestiture", "investment", "financing", '
    '"guarantee", "other", "status": one of "announced", "pending", '
    '"completed", "terminated" (as of this filing), "date": the date of that '
    'status ("YYYY-MM-DD", or "YYYY-MM" / "YYYY" when the filing gives no '
    'day; required for "completed" and "terminated": take it from the '
    'filing; otherwise null when the filing gives none), "quote": the '
    'sentence of this filing that states that status, "detail": one '
    "sentence}]. List a transaction only when it was announced, changed "
    "status or was still open during this filing's own period -- not one "
    "completed in an earlier period\n"
    '- "legal_matters": [{"name": short name of a material lawsuit, '
    "investigation, regulatory proceeding or dispute with a tax authority -- "
    'the most material first, "status": one of "pending" (no decision yet), '
    '"appeal" (a decision was appealed and the appeal is not decided), '
    '"judgment" (decided, no appeal stated), "settled", "dismissed" (every '
    'claim dismissed; a partial dismissal is "pending"), "other", as of this '
    'filing, "status_date": the date this filing states for the latest '
    'event that has already happened ("YYYY-MM-DD", or "YYYY-MM" / "YYYY" '
    "when the filing gives no day) -- only a date the filing writes for "
    "that event; never the period end of this report when the filing gives "
    "no event date (then null), and never a scheduled future hearing or "
    'trial date; or null, "quote": the sentence of this filing that states '
    'that status (when "exposure_text" is set, also the sentence that '
    'states that amount, joined with "..."), "detail": one sentence, '
    '"exposure_text": the amount claimed, sought, assessed or awarded '
    "against the company in this matter -- the largest such amount the "
    'filing states (e.g. "$14 billion") -- never an amount the company has '
    "already accrued, paid, escrowed or reserved, never a settlement or "
    "penalty already paid, and never a valuation of an asset; when a later "
    "decision reduced, vacated or replaced an amount, the amount in force "
    "now; null when the filing states no such amount}]\n"
    "Read the whole filing, including the notes on other income (expense), "
    "investments, impairments, restructuring and income taxes, the outlook "
    "in management's discussion, and the notes on acquisitions, debt and "
    'legal proceedings. Every "quote" is copied word for word from this '
    'filing (at least eight words; use "..." only to skip words inside it); '
    "a record whose quote is not in the filing is discarded. Write every "
    "amount (amount_text, value_text, exposure_text and amounts in other "
    "fields) with its $ sign and its scale word, with the number as the "
    "filing writes it, not rounded: for a figure from a table, write the "
    'table value with the table\'s unit (1,423 in a table "in millions" is '
    '"$1,423 million", never a bare "$1,423"); a full-dollar amount takes a '
    'scale word too ($1,250,000 is "$1.25 million"); a per-share amount '
    'keeps its form and its "per share". A record whose amount_text, '
    "value_text or exposure_text states $10 or more without a scale word "
    '(thousand, million, billion) or "per share" is discarded. Do not '
    "compute new amounts or percentages: a record with an amount that "
    "cannot be found in the filing is discarded.")
RECORDS_ONLY_PROMPT = (
    "Read this SEC filing. Do not summarize it. Respond with " +
    _RECORDS_SCHEMA)


def parse_filing_filename(filename: str) -> tuple[str, str, str] | None:
    stem = filename.removesuffix(".txt")
    parts = stem.rsplit("_", 2)
    if len(parts) != 3 or not all(parts):
        return None
    try:
        date.fromisoformat(parts[2])
    except ValueError:
        return None
    return parts[0], parts[1], parts[2]


def split_summary_and_records(text: str) -> tuple[str, dict[str, Any] | None]:
    index = text.rfind(RECORDS_MARKER)
    if index < 0:
        return text.strip(), None
    summary = text[:index].strip()
    block = text[index + len(RECORDS_MARKER):]
    start, end = block.find("{"), block.rfind("}")
    if start < 0 or end <= start:
        return summary, None
    try:
        data = json.loads(block[start:end + 1])
    except ValueError:
        return summary, None
    return summary, data if isinstance(data, dict) else None


_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT_SCALE: dict[str, float] = {
    "trillion": 1e12,
    "tn": 1e12,
    "t": 1e12,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mn": 1e6,
    "mm": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3
}
_AMOUNT_RE = re.compile(
    rf"(?P<cur>\$|US\$|USD\s?)?\s?(?P<open>\(\s?)?(?P<num>{_NUMBER})"
    r"(?(open)\s?\))\s?"
    r"(?P<unit>%|percent\b|per\s+(?:[a-z]+\s+)?share\b"
    r"|trillion\b|billion\b|million\b|thousand\b"
    r"|tn\b|bn\b|mn\b|mm\b|[TBMK]\b)?", re.IGNORECASE)


def _scale_unit(match: re.Match[str]) -> str:
    unit = (match.group("unit") or "").lower()
    return "" if unit.endswith("share") else unit


@dataclass(frozen=True)
class StatedAmount:
    low: float
    high: float
    percent: bool
    digits: int


_TABLE_MATCH_MIN_DIGITS = 3
_TABLE_SCALES = (1.0, 1e3, 1e6)


def _decimals(number: str) -> int:
    return len(number.split(".", 1)[1]) if "." in number else 0


def _significant_digits(number: str) -> int:
    return len(number.replace(",", "").replace(".", "").lstrip("0"))


def stated_amounts(text: str) -> list[StatedAmount]:
    amounts: list[StatedAmount] = []
    for match in _AMOUNT_RE.finditer(text):
        cur, unit = match.group("cur"), _scale_unit(match)
        if not cur and not match.group("unit"):
            continue
        number = match.group("num")
        value = float(number.replace(",", ""))
        percent = unit in ("%", "percent")
        scale = 1.0 if percent or not unit else _UNIT_SCALE[unit]
        half = 0.5 * 10**-_decimals(number)
        amounts.append(
            StatedAmount((value - half) * scale, (value + half) * scale,
                         percent, _significant_digits(number)))
    return amounts


def _is_bare_year(number: str) -> bool:
    return (
        len(number) == 4 and number.isdigit() and 1900 <= int(number) <= 2100)


def _first_in(values: list[float], low: float, high: float) -> bool:
    index = bisect.bisect_left(values, low)
    return index < len(values) and values[index] <= high


class RawNumbers:

    def __init__(self, raw_text: str) -> None:
        with_unit: set[float] = set()
        percents: set[float] = set()
        bare: set[float] = set()
        for match in _AMOUNT_RE.finditer(raw_text):
            number = match.group("num")
            value = float(number.replace(",", ""))
            unit = _scale_unit(match)
            if unit in ("%", "percent"):
                percents.add(value)
            elif unit:
                with_unit.add(value * _UNIT_SCALE[unit])
            if not unit and (match.group("cur") or match.group("unit")):
                with_unit.add(value)
            if not unit and (match.group("cur") or "," in number
                             or "." in number or
                             (len(number) >= 3 and not _is_bare_year(number))):
                bare.add(value)
        self._with_unit = sorted(with_unit)
        self._percents = sorted(percents)
        self._bare = sorted(bare)

    def contains(self, amount: StatedAmount) -> bool:
        if amount.percent:
            return _first_in(self._percents, amount.low, amount.high)
        if _first_in(self._with_unit, amount.low, amount.high):
            return True
        if amount.digits < _TABLE_MATCH_MIN_DIGITS:
            return False
        return any(
            _first_in(self._bare, amount.low / scale, amount.high / scale)
            for scale in _TABLE_SCALES)


_RANGE_GAP_RE = re.compile(
    r"\s*(?:-|\u2013|\u2014|to|and|or)\s*", re.IGNORECASE)


def amount_without_unit(text: str) -> bool:
    matches = list(_AMOUNT_RE.finditer(text))
    for index, match in enumerate(matches):
        if not match.group("cur") or match.group("unit"):
            continue
        if float(match.group("num").replace(",", "")) < _UNIT_REQUIRED_MIN:
            continue
        following = matches[index + 1] if index + 1 < len(matches) else None
        if (following is not None and following.group("unit")
                and following.group("unit").lower() not in ("%", "percent") and
                _RANGE_GAP_RE.fullmatch(text[match.end():following.start()])):
            continue
        return True
    return False


_ISO_PARTIAL_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _valid_date(value: str) -> bool:
    if not _ISO_PARTIAL_RE.match(value):
        return False
    parts = [int(p) for p in value.split("-")]
    try:
        date(
            parts[0], parts[1] if len(parts) > 1 else 1,
            parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return False
    return True


_SQUASH_RE = re.compile(r"[^a-z0-9]+")
_ELLIPSIS_RE = re.compile(r"\.\.\.|\u2026")


def squash(text: str) -> str:
    return _SQUASH_RE.sub("", text.lower())


def _quote_states_date(quote: str, day: date) -> bool:
    month = calendar.month_name[day.month]
    forms = (
        f"{month} {day.day}, {day.year}", f"{day.day} {month} {day.year}",
        day.isoformat(), f"{day.month}/{day.day}/{day.year}")
    squashed = squash(quote)
    return any(squash(form) in squashed for form in forms)


def quote_in_filing(quote: str, squashed_filing: str) -> bool:
    parts = [squash(p) for p in _ELLIPSIS_RE.split(quote)]
    long_parts = [p for p in parts if len(p) >= _QUOTE_PART_MIN_CHARS]
    if not long_parts or sum(len(p) for p in parts) < QUOTE_MIN_CHARS:
        return False
    return all(p in squashed_filing for p in long_parts)


@dataclass
class _Context:
    raw: RawNumbers
    squashed: str
    form: str | None
    filing_date: str | None
    report_date: date | None
    counts: dict[str, int]

    def note(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1


def period_months_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _one_off_period_issue(
        item: Any, record: dict[str, str | None], ctx: _Context) -> str | None:
    months = period_months_value(item.get("period_months"))
    if months not in (3, 6, 9, 12):
        return "bad_period_months"
    record["period_months"] = str(months)
    period_end = str(record.get("period_end") or "")
    try:
        end = date.fromisoformat(period_end)
    except ValueError:
        return "bad_period_end"
    expected = OWN_PERIOD_MONTHS.get(ctx.form or "")
    if expected is not None and months != expected:
        return "not_own_period"
    if (ctx.report_date is not None
            and abs((end - ctx.report_date).days)
            > _PERIOD_END_TOLERANCE_DAYS):
        return "not_own_period"
    return None


def _ended_before_own_period(
        record: dict[str, str | None], ctx: _Context) -> bool:
    months = OWN_PERIOD_MONTHS.get(ctx.form or "")
    value = record.get("date")
    if (record.get("status") not in ("completed", "terminated")
            or months is None or ctx.report_date is None or not value):
        return False
    parts = [int(p) for p in value.split("-")]
    latest = date(
        parts[0], parts[1] if len(parts) > 1 else 12,
        parts[2] if len(parts) > 2 else 28)
    start = ctx.report_date - timedelta(
        days=months * 31 + _PERIOD_END_TOLERANCE_DAYS)
    return latest < start


def _clean_record(
        category: str, item: Any,
        ctx: _Context) -> tuple[dict[str, str | None] | None, str | None]:
    if not isinstance(item, dict):
        return None, "not_an_object"
    required, optional = _FIELDS[category]
    record: dict[str, str | None] = {}
    for name in required:
        value = item.get(name)
        if not isinstance(value, str) or not value.strip():
            return None, f"missing_{name}"
        record[name] = value.strip()
    for name in optional:
        value = item.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            record[name] = None
        elif isinstance(value, str):
            record[name] = value.strip()
        else:
            return None, f"bad_{name}"
    for (cat, name), allowed in _ENUMS.items():
        if (cat == category
                and str(record.get(name) or "").lower() not in allowed):
            return None, f"bad_{name}"
        if cat == category:
            record[name] = str(record[name]).lower()
    for cat, name in _DATE_FIELDS:
        value = record.get(name) if cat == category else None
        if value is None:
            continue
        if not _valid_date(value):
            return None, f"bad_{name}"
        if ctx.filing_date and value > ctx.filing_date[:len(value)]:
            record[name] = None
            ctx.note("future_date_cleared")
    if (category == "legal_matters" and ctx.report_date is not None
            and record.get("status_date") == ctx.report_date.isoformat()):
        if _quote_states_date(str(item.get("quote") or ""), ctx.report_date):
            ctx.note("status_date_is_period_end_kept")
        else:
            record["status_date"] = None
            ctx.note("status_date_is_period_end_cleared")
    if not quote_in_filing(str(record["quote"]), ctx.squashed):
        return None, "quote_not_in_filing"
    for cat, name in sorted(_AMOUNT_FIELDS):
        value = record.get(name) if cat == category else None
        if not value or stated_amounts(value):
            continue
        if name in required:
            return None, "amount_unreadable"
        record[name] = None
        ctx.note("amount_unreadable_cleared")
    if any(amount_without_unit(str(record.get(name) or ""))
           for cat, name in _UNIT_CHECK_FIELDS if cat == category):
        return None, "amount_without_unit"
    if (category == "transactions"
            and record.get("status") in ("completed", "terminated")
            and not record.get("date")):
        return None, "missing_date"
    if category == "transactions" and _ended_before_own_period(record, ctx):
        return None, "not_own_period"
    if category == "one_off_items":
        issue = _one_off_period_issue(item, record, ctx)
        if issue is not None:
            return None, issue
    quote_names = [name for cat, name in _AMOUNTS_IN_QUOTE if cat == category]
    if quote_names:
        quote_numbers = RawNumbers(str(record["quote"]))
        if not all(quote_numbers.contains(a) for name in quote_names
                   for a in stated_amounts(str(record.get(name) or ""))):
            return None, "amount_not_in_quote"
    if category == "legal_matters" and record.get("exposure_text"):
        quote_numbers = RawNumbers(str(record["quote"]))
        if not all(quote_numbers.contains(a)
                   for a in stated_amounts(str(record["exposure_text"]))):
            ctx.note("exposure_not_in_quote")
    text = " ".join(v for k, v in record.items() if v and k != "quote")
    if not all(ctx.raw.contains(a) for a in stated_amounts(text)):
        return None, "amount_not_in_filing"
    return record, None


def missing_categories(data: dict[str, Any] | None) -> list[str]:
    if data is None:
        return []
    return [key for key in CATEGORIES if not isinstance(data.get(key), list)]


_DROPPED_TITLE_CHARS = 120


def _dropped_title(category: str, item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    title = " ".join(str(item.get(_TITLE_FIELDS[category]) or "").split())
    return title[:_DROPPED_TITLE_CHARS]


def validate_records(
        data: dict[str, Any] | None, raw_text: str, *, ticker: str = "",
        filename: str = "", form: str | None = None,
        filing_date: str | None = None, report_date: str | None = None,
        served_by: str = "") -> dict[str, list[dict[str, str | None]]] | None:
    if data is None:
        return None
    report: date | None = None
    if report_date:
        try:
            report = date.fromisoformat(report_date)
        except ValueError:
            report = None
    ctx = _Context(
        raw=RawNumbers(raw_text), squashed=squash(raw_text), form=form,
        filing_date=filing_date, report_date=report, counts={})
    dropped: dict[str, int] = {}
    kept: dict[str, list[dict[str, str | None]]] = {}
    for category in CATEGORIES:
        items = data.get(category)
        kept[category] = []
        if not isinstance(items, list):
            ctx.note(f"missing_category:{category}")
            continue
        for item in items:
            record, reason = _clean_record(category, item, ctx)
            if record is not None:
                kept[category].append(record)
            elif reason is not None:
                dropped[reason] = dropped.get(reason, 0) + 1
                logger.info(
                    "filing_records.dropped", ticker=ticker, filename=filename,
                    category=category, title=_dropped_title(category, item),
                    reason=reason, served_by=served_by)
    logger.info(
        "filing_records.validated", ticker=ticker, filename=filename,
        served_by=served_by, kept={
            k: len(v)
            for k, v in kept.items()
        }, dropped=dropped, adjusted=ctx.counts)
    return kept


class FilingRecordRow(Protocol):

    @property
    def filename(self) -> str:
        ...

    @property
    def form(self) -> str:
        ...

    @property
    def filing_date(self) -> date:
        ...

    @property
    def report_date(self) -> date | None:
        ...

    @property
    def records(self) -> dict[str, Any]:
        ...


def ordered_rows(rows: Sequence[FilingRecordRow]) -> list[FilingRecordRow]:
    return sorted(
        rows, key=lambda r: (r.filing_date, r.filename), reverse=True)


def record_items(records: dict[str, Any] | None,
                 category: str) -> list[dict[str, Any]]:
    items = (records or {}).get(category)
    return ([i for i in items
             if isinstance(i, dict)] if isinstance(items, list) else [])


def legal_record_count(rows: Sequence[FilingRecordRow]) -> int:
    return sum(
        len(record_items(row.records, "legal_matters")) for row in rows)


_QUOTE_SHOWN_CHARS = 200


def quote_evidence(item: dict[str, Any]) -> str:
    quote = " ".join(str(item.get("quote") or "").split())
    if not quote:
        return ""
    if len(quote) > _QUOTE_SHOWN_CHARS:
        quote = quote[:_QUOTE_SHOWN_CHARS].rstrip() + "..."
    return f' Filing text: "{quote}"'


def _record_line(record_id: str, category: str, item: dict[str, Any]) -> str:
    return display_amounts(
        _record_text(record_id, category, item) + quote_evidence(item))


_DOLLAR_SPACE_RE = re.compile(r"\$\s+(?=\d)")


def display_amounts(text: str) -> str:
    return _DOLLAR_SPACE_RE.sub("$", text)


def unusual_item_period(item: dict[str, Any]) -> str:
    if item.get("period_end"):
        months = item.get("period_months")
        return (
            f" ({months} months ended {item['period_end']})"
            if months else f" (period ended {item['period_end']})")
    if item.get("period"):
        return f" ({item['period']})"
    return ""


AMOUNT_STATED_LABEL = "Amount stated (claimed, sought, assessed or awarded)"


def _record_text(record_id: str, category: str, item: dict[str, Any]) -> str:

    def opt(name: str) -> str:
        return str(item.get(name) or "")

    if category == "legal_matters":
        when = f" ({opt('status_date')})" if opt("status_date") else ""
        detail = f" {opt('detail')}" if opt("detail") else ""
        exposure = (
            f" {AMOUNT_STATED_LABEL}: {opt('exposure_text')}."
            if opt("exposure_text") else "")
        return (
            f"  {record_id} legal matter: {opt('name')} -- status: "
            f"{opt('status')}{when}.{detail}{exposure}")
    if category == "transactions":
        when = f" ({opt('date')})" if opt("date") else ""
        detail = f" {opt('detail')}" if opt("detail") else ""
        return (
            f"  {record_id} transaction: {opt('name')} ({opt('type')}) -- "
            f"status: "
            f"{opt('status')}{when}.{detail}")
    if category == "guidance":
        covers = (
            f" (period covered: {opt('period_covered')})"
            if opt("period_covered") else "")
        return (
            f"  {record_id} guidance: {opt('metric')}: "
            f"{opt('value_text')}{covers}")
    return (
        f"  {record_id} unusual item: {opt('description')}: "
        f"{opt('amount_text')}{unusual_item_period(item)}")


def render_filing_records(rows: Sequence[FilingRecordRow]) -> str:
    if not rows:
        return ""
    lines = [
        "Filing records (extracted automatically from each filing by an LLM; "
        "each record's quote and amounts are checked against the filing text, "
        "but its status and the choice of records are the model's reading and "
        "can be wrong or incomplete -- confirm a status in the filing "
        "passages "
        "before relying on it; an unusual item is an amount recorded in that "
        "period's results and may recur; a legal matter's amount stated is "
        "what is claimed, sought, assessed or awarded, not a loss the company "
        "has recorded; newest filing first):"
    ]
    for index, row in enumerate(ordered_rows(rows), start=1):
        report = (
            f", period of report {row.report_date.isoformat()}"
            if row.report_date else ", period of report not available")
        recency = "NEWEST" if index == 1 else "OLDER"
        lines.append(
            f"F{index} = {row.form} filed {row.filing_date.isoformat()}"
            f"{report} [{recency}]")
        count = 0
        for category, (letter, _) in CATEGORIES.items():
            items = record_items(row.records, category)
            for number, item in enumerate(items, start=1):
                lines.append(
                    _record_line(f"F{index}.{letter}{number}", category, item))
                count += 1
        if count == 0:
            lines.append("  (no records)")
    return "\n".join(lines)


def record_title(record: dict[str, Any]) -> str:
    category = str(record.get("category") or "")
    if category not in CATEGORIES:
        return ""
    title = " ".join(str(record.get(_TITLE_FIELDS[category]) or "").split())
    return f"{CATEGORIES[category][1]}: {title}" if title else ""


RECORD_ID_PATTERN = r"F(\d+)\.([A-Z])(\d+)"
_RECORD_ID_RE = re.compile(RECORD_ID_PATTERN)


def is_record_id(text: str) -> bool:
    return _RECORD_ID_RE.fullmatch(text.upper()) is not None


def record_by_id(rows: Sequence[FilingRecordRow],
                 record_id: str) -> dict[str, Any] | None:
    match = _RECORD_ID_RE.fullmatch(record_id.strip().upper())
    if match is None:
        return None
    filing_index, letter, number = (
        int(match.group(1)), match.group(2), int(match.group(3)))
    category = _LETTER_TO_CATEGORY.get(letter)
    ordered = ordered_rows(rows)
    if category is None or not 1 <= filing_index <= len(ordered):
        return None
    row = ordered[filing_index - 1]
    items = record_items(row.records, category)
    if not 1 <= number <= len(items):
        return None
    return {
        **items[number - 1], "id": f"F{filing_index}.{letter}{number}",
        "category": category,
        "filing_index": filing_index,
        "filename": row.filename,
        "form": row.form,
        "filing_date": row.filing_date.isoformat(),
        "report_date": (
            row.report_date.isoformat() if row.report_date else None)
    }


async def load_filing_records(ticker: str,
                              limit: int | None = None) -> list[Any]:
    from database import SessionLocal
    from domains.filings.repository import FilingRecordRepository

    try:
        async with SessionLocal() as session:
            return list(
                await FilingRecordRepository(session).latest_for_ticker(
                    ticker, limit or settings.FILING_RECORDS_MAX_FILINGS))
    except Exception as exc:
        logger.warning(
            "filing_records.load_failed", ticker=ticker, error=str(exc))
        return []


async def store_filing_records(
        *, ticker: str, filename: str, form: str, filing_date: str,
        report_date: str | None, records: dict[str, Any]) -> bool:
    from database import SessionLocal
    from domains.filings.repository import FilingRecordRepository

    try:
        report = date.fromisoformat(report_date) if report_date else None
        async with SessionLocal() as session:
            await FilingRecordRepository(session).upsert(
                ticker=ticker, filename=filename, form=form,
                filing_date=date.fromisoformat(filing_date),
                report_date=report, records=records)
            await session.commit()
    except Exception as exc:
        logger.warning(
            "filing_records.store_failed", ticker=ticker, filename=filename,
            error=str(exc))
        return False
    logger.info(
        "filing_records.stored", ticker=ticker, filename=filename, counts={
            k: len(v)
            for k, v in records.items() if isinstance(v, list)
        })
    return True
