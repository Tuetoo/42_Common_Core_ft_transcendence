from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID
import structlog
from config import settings
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from domains.auth.model import User
from shared.redis import get_events_client
from shared.user_state import UserState, store_user_state, user_state_key

logger = structlog.get_logger(__name__)

STARTUP_BATCH_SIZE = 500
_KEY_PREFIX = user_state_key("")


class _StateFields(Protocol):

    @property
    def system_role(self) -> str:
        ...

    @property
    def is_active(self) -> bool:
        ...

    @property
    def deleted_at(self) -> datetime | None:
        ...


def state_of(user: _StateFields) -> UserState:
    return UserState(
        role=user.system_role, is_active=bool(user.is_active)
        and user.deleted_at is None)


async def mirror_user(user: User) -> None:
    await store_user_state(settings, user.id, state_of(user))


async def mirror_user_safely(user: User) -> None:
    try:
        await mirror_user(user)
    except Exception as exc:
        logger.warning(
            "user_state.mirror_failed", user_id=str(user.id), error=str(exc))


async def commit_with_state(
        session: AsyncSession, user: User, previous: UserState) -> None:
    await session.flush()
    await mirror_user(user)
    try:
        await session.commit()
    except Exception:
        try:
            await store_user_state(settings, user.id, previous)
        except Exception as exc:
            logger.error(
                "user_state.restore_failed", user_id=str(user.id),
                error=str(exc))
        raise


async def mirror_all_users(
        session_factory: async_sessionmaker[AsyncSession],
        batch_size: int = STARTUP_BATCH_SIZE) -> int:
    client = get_events_client(settings)
    written: set[str] = set()
    last_id: UUID | None = None
    async with session_factory() as session:
        while True:
            stmt: Any = _state_rows().order_by(User.id).limit(batch_size)
            if last_id is not None:
                stmt = stmt.where(User.id > last_id)
            rows = (await session.execute(stmt)).all()
            if not rows:
                break
            await _write_states(client, rows)
            written.update(str(row.id) for row in rows)
            last_id = rows[-1][0]
            if len(rows) < batch_size:
                break
        removed = 0
        async for keys in _scan_batches(client, batch_size):
            stale = [
                key for key in keys
                if key.removeprefix(_KEY_PREFIX) not in written
            ]
            if stale:
                removed += await _drop_unknown(client, session, stale)
    if removed:
        logger.info("user_state.startup_sync_removed", keys=removed)
    return len(written)


def _state_rows() -> Any:
    return select(
        User.id, User.system_role, User.is_active, User.deleted_at)


async def _write_states(client: Redis, rows: Sequence[Any]) -> None:
    async with client.pipeline(transaction=False) as pipe:
        for row in rows:
            pipe.set(user_state_key(row.id), state_of(row).to_json())
        await pipe.execute()


async def _scan_batches(
        client: Redis, batch_size: int) -> AsyncIterator[list[str]]:
    batch: list[str] = []
    async for key in client.scan_iter(
            match=f"{_KEY_PREFIX}*", count=batch_size):
        batch.append(str(key))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _drop_unknown(
        client: Redis, session: AsyncSession, keys: list[str]) -> int:
    ids: dict[UUID, str] = {}
    for key in keys:
        try:
            ids[UUID(key.removeprefix(_KEY_PREFIX))] = key
        except ValueError:
            continue
    if not ids:
        return 0
    rows = (await session.execute(
        _state_rows().where(User.id.in_(list(ids))))).all()
    await _write_states(client, rows)
    for row in rows:
        ids.pop(row.id, None)
    if not ids:
        return 0
    return int(await client.delete(*ids.values()))


async def mirror_all_users_safely(
        session_factory: async_sessionmaker[AsyncSession]) -> None:
    try:
        count = await mirror_all_users(session_factory)
        logger.info("user_state.startup_sync_done", users=count)
    except Exception as exc:
        logger.error("user_state.startup_sync_failed", error=str(exc))
