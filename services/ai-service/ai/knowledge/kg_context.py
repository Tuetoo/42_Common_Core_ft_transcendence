import re
from datetime import date
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.knowledge.filing_records import load_filing_records
from database import SessionLocal
from sqlalchemy.ext.asyncio import AsyncSession
from domains.knowledge.repository import (AggregatedRelationRow,
                                          KnowledgeGraphRepository,
                                          TickerNameRepository)

logger = structlog.get_logger(__name__)

_CORPORATE_SUFFIXES: tuple[str, ...] = tuple(
    sorted((
        "& CO", "& CO.", "AND CO", "CO", "CO.", "CORP", "CORP.", "CORPORATION",
        "INC", "INC.", "INCORPORATED", "LTD", "LTD.", "LIMITED", "LLC",
        "L.L.C.", "PLC", "COMPANY", "GROUP"), key=len, reverse=True))


def core_company_name(name: str) -> str:
    normalized = re.sub(r"/[A-Z]{2,3}$", "", name.strip().upper()).rstrip(" ,")
    for suffix in _CORPORATE_SUFFIXES:
        if normalized.endswith(" " + suffix):
            return normalized[:-(len(suffix) + 1)].strip().rstrip(" ,.")
    return normalized


async def resolve_ticker_name(session: AsyncSession, ticker: str) -> str:
    ticker = ticker.upper()
    repo = TickerNameRepository(session)
    try:
        cached = await repo.get(ticker)
    except Exception as exc:
        logger.warning(
            "ticker_names.cache_read_failed", ticker=ticker, error=str(exc))
        return ticker
    if cached is not None:
        return cached

    try:
        from ai.providers.sec_edgar import load_ticker_to_title
        from config import settings

        if not settings.SEC_EDGAR_USER_AGENT:
            logger.warning(
                "ticker_names.user_agent_not_configured", ticker=ticker)
            return ticker

        official_name = (await load_ticker_to_title()).get(ticker) or None
    except Exception as exc:
        logger.warning(
            "ticker_names.sec_lookup_failed", ticker=ticker, error=str(exc))
        return ticker

    if not official_name:
        logger.warning("ticker_names.not_found_in_sec_map", ticker=ticker)
        official_name = ticker

    try:
        async with SessionLocal() as write_session:
            await TickerNameRepository(write_session).create(
                ticker=ticker, official_name=official_name)
            await write_session.commit()
    except Exception as exc:
        logger.warning(
            "ticker_names.cache_write_failed", ticker=ticker, error=str(exc))
        return official_name
    logger.info(
        "ticker_names.resolved_and_cached", ticker=ticker,
        official_name=official_name)
    return official_name


KgRelationTuple = AggregatedRelationRow

_NUMERIC_VALUE_RE = re.compile(r"^-?\$?\d[\d,]*(?:\.\d+)?\s*[%BbMmKk]?$")


def _looks_like_numeric_reading(other_name: str) -> bool:
    return bool(_NUMERIC_VALUE_RE.match(other_name.strip()))


def _tag_superseded_readings(
        relations: list[KgRelationTuple]) -> dict[int, str]:
    groups: dict[tuple[str, str], list[KgRelationTuple]] = {}
    for row in relations:
        other_name, relation_type, direction = row[0], row[1], row[2]
        if _looks_like_numeric_reading(other_name):
            groups.setdefault((relation_type, direction), []).append(row)

    tags: dict[int, str] = {}
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        most_recent = max(group_rows, key=lambda r: r[7])
        for row in group_rows:
            tags[id(row)] = (
                " [MOST RECENT reading]" if row is most_recent else
                " [SUPERSEDED by the MOST RECENT reading]")
    return tags


async def newest_filing_date(ticker: str) -> date | None:
    rows = await load_filing_records(ticker.strip().upper(), limit=1)
    dates = [
        row.filing_date for row in rows if isinstance(row.filing_date, date)
    ]
    return max(dates) if dates else None


async def fetch_kg_relations(
        entity_name: str, ticker_filter: str | None, session: AsyncSession, *,
        viewer_user_id: str | None = None,
        events_expire_before: date | None = None) -> list[KgRelationTuple]:
    repo = KnowledgeGraphRepository(session)
    relations = await repo.find_relations_for_entity_name(
        entity_name, ticker_filter=ticker_filter,
        viewer_user_id=viewer_user_id,
        events_expire_before=events_expire_before)

    if ticker_filter is not None:
        tried = {entity_name.strip().upper()}
        extra_relations: list[KgRelationTuple] = []

        raw_ticker = ticker_filter.strip().upper()
        if raw_ticker not in tried:
            tried.add(raw_ticker)
            extra_relations += await repo.find_relations_for_entity_name(
                ticker_filter, ticker_filter=ticker_filter,
                viewer_user_id=viewer_user_id,
                events_expire_before=events_expire_before)

        canonical_name = await resolve_ticker_name(session, ticker_filter)
        canonical_core = core_company_name(canonical_name)
        if canonical_core not in tried:
            tried.add(canonical_core)
            extra_relations += await repo.find_relations_for_entity_name(
                canonical_core, ticker_filter=ticker_filter,
                viewer_user_id=viewer_user_id,
                events_expire_before=events_expire_before)

        if extra_relations:
            relations = sorted(
                relations + extra_relations, key=lambda r: r[7],
                reverse=True)[:20]

    return relations


def format_kg_relations(
        entity_name: str, relations: list[KgRelationTuple],
        ticker_filter: str | None) -> str:
    scope_note = (
        f" (restricted to ticker={ticker_filter!r})" if ticker_filter else "")
    lines = [
        f"Knowledge graph relations for {entity_name!r}{scope_note} "
        f"({len(relations)} distinct facts found):", KG_PRIOR_NOTE_GUIDANCE
    ]
    tags = _tag_superseded_readings(relations)
    for row in relations:
        (
            other_name, relation_type, direction, corroboration_count,
            confidence, source_categories, first_seen_at,
            last_confirmed_at) = row
        conf_str = (
            f", avg confidence={confidence:.2f}"
            if confidence is not None else "")
        distinct_roles = len({c for c in source_categories.split(", ") if c})
        if corroboration_count == 1:
            corrob_str = (
                f" (dated {last_confirmed_at.date().isoformat()}{conf_str})")
        elif distinct_roles <= 1:
            corrob_str = (
                f" (re-extracted {corroboration_count}x from the same agent "
                f"role's reports, not corroboration, "
                f"{first_seen_at.date().isoformat()} to "
                f"{last_confirmed_at.date().isoformat()}{conf_str})")
        else:
            corrob_str = (
                f" (stated by {distinct_roles} different agent roles, "
                f"{corroboration_count} extractions, "
                f"{first_seen_at.date().isoformat()} to "
                f"{last_confirmed_at.date().isoformat()}{conf_str})")
        source_str = (
            f" [source: {source_categories}] "
            "[agent-derived, not independently sourced]")
        recency_str = tags.get(id(row), "")
        if direction == "->":
            lines.append(
                f"- {entity_name} --[{relation_type}]--> "
                f"{other_name}{corrob_str}{source_str}{recency_str}")
        else:
            lines.append(
                f"- {other_name} --[{relation_type}]--> "
                f"{entity_name}{corrob_str}{source_str}{recency_str}")
    return "\n".join(lines)


KG_PRIOR_NOTE_GUIDANCE = (
    "Note: these relations were extracted from PRIOR AI-WRITTEN NOTES "
    "(earlier agent reports), not from sources. Treat each one as an "
    "unverified lead: confirm any fact, figure, date or status with a fresh "
    "tool result from this run before stating it; if fresh data differs, "
    "the fresh data wins. A lawsuit's, investigation's or deal's status is "
    "the one the newest filing gives.")


def drop_non_english_relations(
        entity_name: str,
        relations: list[KgRelationTuple]) -> list[KgRelationTuple]:
    kept = [
        r for r in relations if non_english_reason(" ".join(r[:3])) is None
    ]
    if len(kept) != len(relations):
        logger.warning(
            "kg_context.non_english_rows_dropped", entity_name=entity_name,
            dropped=len(relations) - len(kept))
    return kept


async def get_kg_context_for_ticker(
        ticker: str, *, viewer_user_id: str | None = None) -> str | None:
    cutoff = await newest_filing_date(ticker)
    async with SessionLocal() as session:
        relations = await fetch_kg_relations(
            ticker, ticker, session, viewer_user_id=viewer_user_id,
            events_expire_before=cutoff)
    relations = drop_non_english_relations(ticker, relations)
    logger.info(
        "kg_context.injected", ticker=ticker, relations=len(relations),
        events_expire_before=cutoff.isoformat() if cutoff else None)
    if not relations:
        return None
    return format_kg_relations(ticker, relations, ticker)


async def get_kg_context_for_portfolio(
        tickers: list[str], *,
        viewer_user_id: str | None = None) -> str | None:
    sections: list[str] = []
    async with SessionLocal() as session:
        for ticker in tickers:
            relations = drop_non_english_relations(
                ticker, await fetch_kg_relations(
                    ticker, ticker, session, viewer_user_id=viewer_user_id,
                    events_expire_before=await newest_filing_date(ticker)))
            if relations:
                sections.append(format_kg_relations(ticker, relations, ticker))
    if not sections:
        return None
    return "\n\n".join(sections)
