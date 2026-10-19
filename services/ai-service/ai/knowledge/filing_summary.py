from dataclasses import dataclass
from typing import Any
import structlog
from ai.knowledge.filing_records import (RECORDS_ONLY_PROMPT,
                                         missing_categories,
                                         split_summary_and_records,
                                         validate_records)
from ai.llm.base import AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage, LLMTier
from ai.llm.router import LLMRouter
from config import settings

logger = structlog.get_logger(__name__)

FALLBACK_SUMMARY_PREFIX = (
    "[Real LLM summarization unavailable -- showing a raw excerpt "
    "instead, may include non-substantive content such as filing "
    "metadata.]")


def is_fallback_summary(text: str) -> bool:
    return text.startswith(FALLBACK_SUMMARY_PREFIX)


@dataclass(frozen=True)
class FilingSummary:
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class ExtractedRecords:
    records: dict[str, Any]
    served_by: str


_SUMMARY_MAX_TOKENS = 12288
_RECORDS_MAX_TOKENS = 10240
_RETRY_MAX_TOKENS = 16384


def hit_output_limit(finish_reason: str | None) -> bool:
    reason = (finish_reason or "").upper()
    return "MAX_TOKENS" in reason or reason.endswith("LENGTH")


class FilingTooLargeError(Exception):
    pass


_FILING_SUMMARY_TIMEOUT_SECONDS = 300.0

_TRUNCATION_STAIRCASE: list[tuple[int, float]] = [(1_200_000, 1.0),
                                                  (1_600_000, 0.8),
                                                  (1_800_000, 0.7),
                                                  (2_100_000, 0.6),
                                                  (2_500_000, 0.5)]

RECORDS_MAX_CHARS = 900_000


def records_keep_fraction(size: int) -> float:
    return 1.0 if size <= RECORDS_MAX_CHARS else RECORDS_MAX_CHARS / size


def _complete_records_block(
        block: dict[str, Any] | None, *, ticker: str, form: str,
        filing_date: str | None) -> dict[str, Any] | None:
    missing = missing_categories(block)
    if not missing:
        return block
    logger.warning(
        "filing_summary.records_incomplete", ticker=ticker, form=form,
        filing_date=filing_date, missing_categories=missing)
    return None


def _apply_truncation_staircase(
        combined_text: str, *, ticker: str, form: str,
        filing_date: str | None) -> str:
    real_length = len(combined_text)
    for ceiling, keep_fraction in _TRUNCATION_STAIRCASE:
        if real_length < ceiling:
            if keep_fraction >= 1.0:
                return combined_text
            cut_at = int(real_length * keep_fraction)
            logger.warning(
                "filing_summary.staircase_truncated", ticker=ticker, form=form,
                filing_date=filing_date, real_length=real_length,
                keep_fraction=keep_fraction, cut_at=cut_at)
            return combined_text[:cut_at]
    raise FilingTooLargeError(
        f"{ticker} {form} ({filing_date}): combined real text is "
        f"{real_length:,} real characters, exceeding every real tier "
        f"in the staircase (largest ceiling: "
        f"{_TRUNCATION_STAIRCASE[-1][0]:,}). Skipping this real "
        f"document rather than attempting an unvalidated, more "
        f"aggressive real truncation.")


_CAPITAL_ALLOCATION_BULLET = (
    "- Capital allocation and commitments: capital expenditures in the "
    "period and any capital-expenditure outlook the filing states (with the "
    "period it covers); share repurchases and dividends in the period (say "
    "so when there were none); cash and debt at the period end; new debt "
    "issued; commitments and guarantees with their amounts (leases not yet "
    "commenced, purchase obligations, guarantees of another party's debt); "
    "and what explains the gap between operating income and net income "
    "(the tax rate, non-operating gains or losses), with the real figures.\n")

_GROUNDING_RULE = (
    "Hard rule: every specific number or fact in your summary must be "
    "traceable to the real text below. Do not fill gaps with general "
    "knowledge about this company or typical figures for companies like "
    "it -- if something isn't in the text, leave it out. A shorter, fully "
    "grounded summary is correct; a longer one padded with plausible-"
    "sounding detail not actually in the text is not, regardless of how "
    "well-written it reads.\n\n"
    "Respond with the summary (a few real paragraphs), no preamble. ")

_10K_SYSTEM_PROMPT = (
    "Summarize this real 10-K (annual report) filing for use by investment "
    "research agents. Extract and preserve, in your own concise words:\n"
    "- Core financial results: revenue, gross margin, operating income, net "
    "income, EPS, and how each compares to the prior year, with the real "
    "figures.\n"
    "- Segment/regional performance -- if the filing includes a segment or "
    "regional breakdown table (e.g. Americas, Europe, Greater China, "
    "Japan, Rest of Asia Pacific, or a different real set for this "
    "company), report the real figure/change for EVERY real segment "
    "listed, not just the 2-3 largest or most notable ones -- a segment "
    "table with 5 real rows should produce a summary that mentions all 5, "
    "not a subset.\n"
    "- Risk factors -- COVERAGE MATTERS AS MUCH AS DEPTH HERE. A real 10-K "
    "typically discloses risk under several genuinely distinct categories "
    "(e.g. macroeconomic/trade, competition, product/manufacturing, supply "
    "chain and third-party dependencies, intellectual property, workforce/"
    "culture, distribution/channel, information technology and data "
    "security, legal and regulatory, business strategy/M&A -- the real "
    "categories present in THIS filing may differ, use what's actually "
    "there, not this list). First identify every real, distinct risk "
    "category the filing actually discusses, then write one or two "
    "sentences of REAL, SPECIFIC detail (not a generic label) for EACH one "
    "you find -- do not stop after covering just the 2-3 most prominent-"
    "sounding risks and skip the rest; a summary that thoroughly covers "
    "one risk while omitting five other real, distinct categories the "
    "filing discloses is a real, meaningful gap, not an acceptable "
    "trade-off for concision.\n"
    "- Legal proceedings and other material regulatory/macro developments "
    "-- same completeness principle: if the filing describes multiple "
    "real, distinct legal matters or regulatory/macro developments (e.g. "
    "several separate lawsuits, investigations, or rulings), cover each "
    "one with its own real, specific outcome or status, not just the "
    "single most prominent one. Real, specific status details matter here "
    "(e.g. a fine amount, an injunction, a referral for further "
    "proceedings, a court ruling) -- don't flatten a case with a real, "
    "material recent development down to a generic one-line mention. If "
    "the filing states a real, aggregate figure for accrued legal/"
    "regulatory liabilities or similar total exposure (not just the "
    "individual case amounts), include that real aggregate number too -- "
    "it's a distinct, material data point on its own, not redundant with "
    "the individual cases.\n"
    "- Major recent events, new products, or one-time transactions -- "
    "real 10-Ks often include a section specifically devoted to this "
    '(sometimes literally titled something like "Key Recent Events" or '
    "similar, sometimes folded into the MD&A overview) listing real, "
    "concrete developments: completed or announced acquisitions/"
    "divestitures, major financing or investment transactions (e.g. a "
    "real strategic investment from or into another named company), new "
    "product or platform launches or their production status, plant/"
    "facility openings. If the filing has a real section like this, cover "
    "EVERY real item it lists, not just the ones that also show up "
    "elsewhere in the financial figures -- a real item can be "
    "significant (e.g. a named strategic investor, a next-generation "
    "product announcement) even when it hasn't yet moved this period's "
    "reported financial results.\n"
    "- Management's own discussion of what drove results (MD&A) -- real, "
    "specific drivers, not generic commentary.\n" +
    _CAPITAL_ALLOCATION_BULLET + "\n"
    "You do NOT need to preserve routine accounting-footnote detail "
    "(fair-value hierarchy breakdowns, debt instrument specifics, "
    "share-based compensation vesting schedules) unless a specific figure "
    "there is itself materially significant -- summarize these at most in "
    "one line, don't reproduce the detail. This word-budget saved on "
    "footnote minutiae is exactly what should go toward genuinely covering "
    "every real segment, risk category, and material development instead.\n\n"
    + _GROUNDING_RULE +
    "For each legal matter, say its status as of this filing; label any "
    "guidance with the period it covers.")

_10Q_SYSTEM_PROMPT = (
    "Summarize this real 10-Q (quarterly report) filing for use by "
    "investment research agents. Extract and preserve, in your own concise "
    "words:\n"
    "- This quarter's financial results: revenue, gross margin, operating "
    "income, net income, EPS, and how each compares to the same quarter "
    "last year, with the real figures.\n"
    "- Segment/regional performance -- if the filing includes a segment or "
    "regional breakdown table, report the real figure/change for EVERY "
    "real segment listed, not just the 2-3 largest or most notable ones -- "
    "a segment table with 5 real rows should produce a summary that "
    "mentions all 5, not a subset.\n"
    "- Management's own discussion of what drove this quarter's results "
    "(MD&A) -- real, specific drivers, not generic commentary. This "
    "includes real production/operational status updates for named "
    "products or programs (e.g. a specific vehicle or platform moving "
    "from construction to pilot production to full production) even when "
    "they haven't yet moved this quarter's reported financial figures --"
    " a real status change on a named product is itself a material "
    "MD&A fact, not something to skip just because the segment revenue "
    "discussion covers the numbers separately.\n" +
    _CAPITAL_ALLOCATION_BULLET +
    "- Legal proceedings and other material regulatory/macro developments "
    "disclosed this quarter -- if the filing describes multiple real, "
    "distinct items (e.g. several separate lawsuits, investigations, "
    "rulings, or macro/trade developments), cover each one with its real, "
    "specific outcome or status, not just the single most prominent one. "
    "Real, specific status details matter (e.g. a fine amount, a court "
    "ruling, a referral for further proceedings) -- don't flatten a case "
    "or development with a real, material update down to a generic "
    "one-line mention.\n\n"
    "Real, important structural note about 10-Q risk factors: a 10-Q's own "
    "Risk Factors section (if present) typically covers ONLY material "
    "CHANGES since the prior 10-K/10-Q, not the company's complete risk "
    "profile -- the rest is incorporated by reference, not restated. If "
    "this filing includes a Risk Factors section, summarize it explicitly "
    'as "new or updated risk factors this quarter," not as if it were '
    "the company's full risk profile -- do not imply completeness the "
    "real filing itself doesn't claim. Coverage still matters here too: if "
    "the section discusses several real, distinct new/updated risk "
    "categories, cover each one briefly rather than elaborating on only "
    "the first or most prominent-sounding one and dropping the rest.\n\n"
    "You do NOT need to preserve routine accounting-footnote detail unless "
    "a specific figure there is itself materially significant -- summarize "
    "these at most in one line, don't reproduce the detail.\n\n" +
    _GROUNDING_RULE +
    "For each legal matter, say its status as of this filing (e.g. "
    "pending, dismissed on <date>, settled on <date>); label any guidance "
    "with the period it covers.")

_AMENDMENT_INSTRUCTION = (
    "A real amendment was filed against the original filing above (see "
    "the two labeled sections in the text you were given). For any topic "
    "the amendment actually discusses, use the amendment's real, "
    "corrected content -- it supersedes the original on that topic. For "
    "everything else the amendment doesn't touch (a real amendment often "
    "covers only one specific item, e.g. just the MD&A, while genuinely "
    "not including Risk Factors, Legal Proceedings, or other real "
    "sections at all), the original filing remains the real, "
    "authoritative source -- its absence from the amendment does not "
    "mean it no longer applies. Do not treat the amendment's narrower "
    "real scope as if the original's other real content had been "
    "withdrawn or was never true.")


async def summarize_filing(
        raw_text: str, *, form: str, ticker: str,
        filing_date: str | None = None, amendment_text: str | None = None,
        llm_router: LLMRouter | None = None) -> FilingSummary:
    router = llm_router or LLMRouter()
    if form not in ("10-K", "10-Q"):
        logger.warning(
            "filing_summary.unknown_form_type", form=form, ticker=ticker)
    system_prompt = (
        _10Q_SYSTEM_PROMPT if form == "10-Q" else _10K_SYSTEM_PROMPT)

    if amendment_text is not None:
        combined_text = (
            f"=== ORIGINAL FILING ===\n{raw_text}\n\n"
            f"=== REAL AMENDMENT FILED AGAINST THE ABOVE "
            f"(corrects specific item(s) only; does not necessarily "
            f"cover the whole original filing) ===\n{amendment_text}")
        system_prompt = system_prompt + "\n\n" + _AMENDMENT_INSTRUCTION
    else:
        combined_text = raw_text

    combined_text = _apply_truncation_staircase(
        combined_text, ticker=ticker, form=form, filing_date=filing_date)

    async def _generate(max_tokens: int) -> tuple[str, str | None] | None:
        user_content = combined_text
        try:
            response = await router.complete(
                LLMTier(settings.FILING_LLM_TIER), [
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=user_content)
                ], temperature=0.0, retries=AGENT_LLM_CALL_RETRIES,
                max_tokens=max_tokens,
                timeout_seconds=_FILING_SUMMARY_TIMEOUT_SECONDS)
        except LLMError as exc:
            logger.warning(
                "filing_summary.llm_call_failed", ticker=ticker, form=form,
                error=str(exc))
            return None
        return response.content.strip(), response.finish_reason

    generated = await _generate(_SUMMARY_MAX_TOKENS)
    truncated = False
    if generated is not None and hit_output_limit(generated[1]):
        logger.warning(
            "filing_summary.output_limit_retry", ticker=ticker, form=form,
            filing_date=filing_date, finish_reason=generated[1])
        retried = await _generate(_RETRY_MAX_TOKENS)
        if retried is not None:
            generated = retried
        truncated = hit_output_limit(generated[1])
        if truncated:
            logger.warning(
                "filing_summary.output_truncated", ticker=ticker, form=form,
                filing_date=filing_date, finish_reason=generated[1])
    raw_output = generated[0] if generated is not None else None
    if raw_output is None:
        fallback = " ".join(raw_text.split()[:1200])
        logger.warning(
            "filing_summary.using_fallback_excerpt", ticker=ticker, form=form)
        return FilingSummary(f"{FALLBACK_SUMMARY_PREFIX} {fallback}")

    return FilingSummary(raw_output, truncated)


async def extract_filing_records(
        raw_text: str, *, form: str, ticker: str, filing_date: str,
        amendment_text: str | None = None, llm_router: LLMRouter | None = None,
        tier: LLMTier | None = None,
        report_date: str | None = None) -> ExtractedRecords | None:
    router = llm_router or LLMRouter()
    combined = (
        raw_text if amendment_text is None else (
            f"=== ORIGINAL FILING ===\n{raw_text}\n\n=== AMENDMENT FILED "
            f"AGAINST "
            f"THE ABOVE (supersedes it on the topics it covers) "
            f"===\n{amendment_text}"))
    original_chars = len(combined)
    if original_chars > RECORDS_MAX_CHARS:
        keep = records_keep_fraction(original_chars)
        combined = combined[:RECORDS_MAX_CHARS]
        logger.warning(
            "filing_summary.records_input_truncated", ticker=ticker, form=form,
            filing_date=filing_date, real_length=original_chars,
            keep_fraction=round(keep, 3), cut_at=RECORDS_MAX_CHARS)
    logger.info(
        "filing_summary.records_call_input", ticker=ticker, form=form,
        filing_date=filing_date, original_chars=original_chars,
        sent_chars=len(combined))

    async def _call(max_tokens: int) -> tuple[str, str | None, str] | None:
        try:
            response = await router.complete(
                tier or LLMTier(settings.FILING_RECORDS_BACKFILL_LLM_TIER), [
                    LLMMessage(role="system", content=RECORDS_ONLY_PROMPT),
                    LLMMessage(role="user", content=combined)
                ], temperature=0.0, retries=AGENT_LLM_CALL_RETRIES,
                max_tokens=max_tokens,
                timeout_seconds=_FILING_SUMMARY_TIMEOUT_SECONDS)
        except LLMError as exc:
            logger.warning(
                "filing_summary.records_call_failed", ticker=ticker, form=form,
                filing_date=filing_date, error=str(exc))
            return None
        served_by = f"{response.provider}/{response.model}"
        logger.info(
            "filing_summary.records_served_by", ticker=ticker, form=form,
            filing_date=filing_date, served_by=served_by,
            finish_reason=response.finish_reason)
        return response.content, response.finish_reason, served_by

    result = await _call(_RECORDS_MAX_TOKENS)
    if result is not None and hit_output_limit(result[1]):
        logger.warning(
            "filing_summary.output_limit_retry", ticker=ticker, form=form,
            filing_date=filing_date, finish_reason=result[1],
            records_only=True)
        result = await _call(_RETRY_MAX_TOKENS)
    if result is None:
        return None
    content, finish_reason, served_by = result
    _, block = split_summary_and_records(content)
    if block is None:
        logger.warning(
            "filing_summary.records_block_missing", ticker=ticker, form=form,
            filing_date=filing_date, finish_reason=finish_reason,
            served_by=served_by)
        return None
    if (_complete_records_block(block, ticker=ticker, form=form,
                                filing_date=filing_date) is None):
        return None
    records = validate_records(
        block, (
            raw_text
            if amendment_text is None else f"{raw_text}\n{amendment_text}"),
        ticker=ticker, filename=f"{ticker}_{form}_{filing_date}", form=form,
        filing_date=filing_date, report_date=report_date, served_by=served_by)
    if records is None:
        return None
    return ExtractedRecords(records, served_by)
