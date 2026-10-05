import re
import structlog
from database import SessionLocal
from sqlalchemy.ext.asyncio import AsyncSession
from domains.knowledge.repository import TickerNameRepository

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
