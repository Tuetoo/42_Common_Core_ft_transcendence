from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from uuid import UUID
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from shared.config import BaseServiceSettings
from shared.http_client import get_http_client

logger = structlog.get_logger(__name__)

DELETED_USERS_BATCH_SIZE = 500
_TIMEOUT_SECONDS = 10.0


async def _ask_auth_in_batches(
        settings: BaseServiceSettings, path: str, ids: Iterable[UUID], *,
        request_key: str, answer_key: str) -> set[UUID]:
    candidates = sorted(set(ids), key=str)
    url = f"{settings.AUTH_SERVICE_URL}{path}"
    found: set[UUID] = set()
    for start in range(0, len(candidates), DELETED_USERS_BATCH_SIZE):
        batch = candidates[start:start + DELETED_USERS_BATCH_SIZE]
        response = await get_http_client(_TIMEOUT_SECONDS).post(
            url, json={request_key: [str(item) for item in batch]})
        response.raise_for_status()
        found.update(UUID(str(item)) for item in response.json()[answer_key])
    return found


async def deleted_user_ids(
        settings: BaseServiceSettings, user_ids: Iterable[UUID]) -> set[UUID]:
    return await _ask_auth_in_batches(
        settings, "/internal/users/deleted", user_ids,
        request_key="user_ids", answer_key="deleted_ids")


async def missing_organization_ids(
        settings: BaseServiceSettings,
        organization_ids: Iterable[UUID]) -> set[UUID]:
    return await _ask_auth_in_batches(
        settings, "/internal/organizations/missing", organization_ids,
        request_key="organization_ids", answer_key="missing_ids")


CleanupStep = Callable[[AsyncSession], Awaitable[int]]


async def run_cleanup_steps(
        session_factory: async_sessionmaker[AsyncSession], scope: str,
        scope_id: UUID, steps: dict[str, CleanupStep], *,
        strict: bool = False) -> dict[str, int]:
    removed: dict[str, int] = {}
    for name, step in steps.items():
        try:
            async with session_factory() as session:
                removed[name] = await step(session)
        except Exception:
            logger.exception(
                f"{scope}_data_cleanup_failed", scope_id=str(scope_id),
                step=name)
    logger.info(f"{scope}_data_cleaned", scope_id=str(scope_id), **removed)
    failed = sorted(set(steps) - set(removed))
    if strict and failed:
        raise RuntimeError(f"{scope} cleanup steps failed: {failed}")
    return removed


@dataclass(frozen=True)
class SweepResult:
    candidates: int
    deleted: int
    cleaned: int
    failed: int
    lookup_failed: bool = False
    refused: bool = False


async def clean_each(
        ids: Iterable[UUID], cleanup: Callable[[UUID], Awaitable[object]], *,
        scope: str) -> tuple[int, int]:
    cleaned = failed = 0
    for scope_id in sorted(ids, key=str):
        try:
            await cleanup(scope_id)
        except Exception:
            failed += 1
            logger.exception(
                f"deleted_{scope}_sweep_item_failed", scope_id=str(scope_id))
        else:
            cleaned += 1
    return cleaned, failed


def sweep_steps(
        prefix: str, result: SweepResult, *,
        brake: bool = False) -> dict[str, tuple[int, int]]:
    steps = {
        prefix: (result.deleted, result.failed),
        f"{prefix}_lookup": (1, int(result.lookup_failed))
    }
    if brake:
        steps[f"{prefix}_safety_brake"] = (1, int(result.refused))
    return steps


def log_sweep(scope: str, result: SweepResult) -> SweepResult:
    logger.info(
        f"deleted_{scope}_sweep_completed", candidates=result.candidates,
        deleted=result.deleted, cleaned=result.cleaned, failed=result.failed,
        lookup_failed=result.lookup_failed, refused=result.refused)
    return result


async def run_deleted_user_sweep(
        settings: BaseServiceSettings, candidates: Iterable[UUID],
        cleanup: Callable[[UUID], Awaitable[object]]) -> SweepResult:
    owners = set(candidates)
    try:
        deleted = await deleted_user_ids(settings, owners)
    except Exception:
        logger.exception(
            "deleted_user_sweep_lookup_failed", candidates=len(owners))
        return log_sweep(
            "user",
            SweepResult(
                candidates=len(owners), deleted=0, cleaned=0, failed=0,
                lookup_failed=True))
    cleaned, failed = await clean_each(deleted, cleanup, scope="user")
    return log_sweep(
        "user",
        SweepResult(
            candidates=len(owners), deleted=len(deleted), cleaned=cleaned,
            failed=failed))
