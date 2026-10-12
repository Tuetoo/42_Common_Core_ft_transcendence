import contextlib
import time
import redis.asyncio as redis
import structlog
from config import settings
from prometheus_client import Gauge
from shared.logging import get_or_create
from shared.redis import get_cache_client

logger = structlog.get_logger(__name__)

SCHEDULED_JOBS = frozenset({
    "market-open", "market-close", "tier-monitoring", "review-run",
    "rag-ingest", "logs-cleanup", "backup", "purge-unverified",
    "notification-retention"
})
_REDIS_KEY = "core:job_last_success"
_SKIPPED_KEY = "core:market_close_skipped"
_SINCE_KEY = "core:job_tracked_since"

JOB_LAST_SUCCESS = get_or_create(
    Gauge, "alphamind_job_last_success_timestamp_seconds",
    "Unix time of the last successful run of a scheduled job.",
    ("scheduled_job", ))
MARKET_CLOSE_SKIPPED = get_or_create(
    Gauge, "alphamind_market_close_portfolios_skipped",
    "Portfolios the last market-close run left without a snapshot "
    "(a holding had no close for the session).")


async def record_job_success(job: str, *, at: float | None = None) -> float:
    moment = time.time() if at is None else at
    JOB_LAST_SUCCESS.labels(scheduled_job=job).set(moment)
    try:
        await get_cache_client(settings).hset(_REDIS_KEY, job, str(moment))
    except Exception as exc:
        logger.warning("job_health_store_failed", job=job, error=str(exc))
    return moment


async def record_market_close_skipped(count: int) -> None:
    MARKET_CLOSE_SKIPPED.set(count)
    try:
        await get_cache_client(settings).set(_SKIPPED_KEY, str(count))
    except Exception as exc:
        logger.warning("market_close_skipped_store_failed", error=str(exc))


async def _tracked_since(cache: redis.Redis, job: str, start: float) -> float:
    try:
        await cache.hsetnx(_SINCE_KEY, job, str(start))
        raw = await cache.hget(_SINCE_KEY, job)
    except Exception as exc:
        logger.warning(
            "job_health_baseline_store_failed", job=job, error=str(exc))
        return start
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return float(text)
    except ValueError:
        return start


async def load_job_health(*, started_at: float | None = None) -> None:
    start = time.time() if started_at is None else started_at
    cache = get_cache_client(settings)
    try:
        skipped = await cache.get(_SKIPPED_KEY)
    except Exception as exc:
        logger.warning("market_close_skipped_load_failed", error=str(exc))
        skipped = None
    if skipped is not None:
        raw = skipped.decode() if isinstance(skipped, bytes) else str(skipped)
        with contextlib.suppress(ValueError):
            MARKET_CLOSE_SKIPPED.set(int(raw))
    loaded: set[str] = set()
    readable = True
    try:
        stored = await cache.hgetall(_REDIS_KEY)
    except Exception as exc:
        logger.warning("job_health_load_failed", error=str(exc))
        stored = {}
        readable = False
    for raw_job, raw_moment in stored.items():
        job = raw_job.decode() if isinstance(raw_job, bytes) else str(raw_job)
        moment = (
            raw_moment.decode()
            if isinstance(raw_moment, bytes) else raw_moment)
        if job in SCHEDULED_JOBS:
            try:
                JOB_LAST_SUCCESS.labels(scheduled_job=job).set(float(moment))
            except ValueError:
                continue
            loaded.add(job)
    for job in sorted(SCHEDULED_JOBS - loaded):
        since = (
            await _tracked_since(cache, job, start) if readable else start)
        JOB_LAST_SUCCESS.labels(scheduled_job=job).set(since)
        logger.info("scheduled_job_no_success_yet", job=job, since=since)
