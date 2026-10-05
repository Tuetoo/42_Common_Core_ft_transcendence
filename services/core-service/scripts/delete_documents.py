import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from config import settings
from database import SessionLocal, engine
from outbox import OutboxEntry, publish_now
from sqlalchemy.ext.asyncio import AsyncSession
from domains.research.model import Document
from domains.research.repository import DocumentRepository
from domains.research.service import delete_document_row
from shared.redis import close_redis_connections
from shared.storage import delete_object

DeleteObjectFn = Callable[..., Awaitable[None]]
PublishFn = Callable[[list[OutboxEntry]], Awaitable[None]]


@dataclass
class Plan:
    documents: list[Document] = field(default_factory=list)
    not_found: list[UUID] = field(default_factory=list)


@dataclass
class Result:
    deleted: list[UUID] = field(default_factory=list)
    failed: list[tuple[UUID, str]] = field(default_factory=list)


def parse_ids(raw: Sequence[str]) -> list[UUID]:
    seen: dict[UUID, None] = {}
    for value in raw:
        seen.setdefault(UUID(value.strip()), None)
    return list(seen)


async def build_plan(session: AsyncSession, ids: Sequence[UUID]) -> Plan:
    repo = DocumentRepository(session)
    plan = Plan()
    for document_id in ids:
        document = await repo.get_by_id(document_id)
        if document is None:
            plan.not_found.append(document_id)
        else:
            plan.documents.append(document)
    return plan


def describe(document: Document) -> str:
    uploaded = (
        f"{document.uploaded_at:%Y-%m-%d %H:%M}"
        if document.uploaded_at else "-")
    return (
        f"{document.id}  {document.category}/{document.ticker or '-'}  "
        f"uploaded={uploaded}  owner={document.owner_id}\n"
        f"   filename: {document.filename}\n"
        f"   object:   {settings.S3_BUCKET_DOCUMENTS}/{document.storage_url}")


async def apply_plan(
        session: AsyncSession, plan: Plan, *,
        delete_object_fn: DeleteObjectFn = delete_object,
        publish_fn: PublishFn = publish_now) -> Result:
    repo = DocumentRepository(session)
    result = Result()
    document_ids = [document.id for document in plan.documents]
    for document_id in document_ids:
        try:
            document = await repo.get_by_id(document_id)
            if document is None:
                result.failed.append((document_id, "no longer exists"))
                continue
            storage_key, entry = await delete_document_row(session, document)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            result.failed.append((document_id, f"{type(exc).__name__}: {exc}"))
            continue
        await publish_fn([entry])
        result.deleted.append(document_id)
        try:
            await delete_object_fn(
                settings, bucket=settings.S3_BUCKET_DOCUMENTS, key=storage_key)
        except Exception as exc:
            result.failed.append((
                document_id,
                f"deleted, but the stored file {storage_key!r} could not "
                f"be "
                f"removed ({type(exc).__name__}: {exc}); it is an orphan "
                f"object"))
    return result


def print_plan(plan: Plan) -> None:
    print(f"1. Documents to delete: {len(plan.documents)}")
    for document in plan.documents:
        print(describe(document))
    if plan.not_found:
        print(
            f"\n2. Not found (already deleted or wrong id): "
            f"{len(plan.not_found)}")
        for document_id in plan.not_found:
            print(f"   {document_id}")


def print_result(result: Result) -> None:
    print(
        f"\nDeleted: {len(result.deleted)} "
        "(DOCUMENT_DELETED queued in the outbox for each)")
    for document_id in result.deleted:
        print(f"   {document_id}")
    if result.failed:
        print(f"\nFailed: {len(result.failed)}")
        for document_id, reason in result.failed:
            print(f"   {document_id}: {reason}")


DESCRIPTION = (
    "Operator script: delete core documents by id, the same way\n"
    "`ResearchService.delete_document` does, without the owner check.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--id", dest="ids", action="append", required=True,
        help="core.documents id; repeat for several")
    parser.add_argument(
        "--confirm", action="store_true",
        help="apply the deletion (default: preview only)")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ids = parse_ids(args.ids)
    except ValueError as exc:
        print(f"Invalid id: {exc}")
        return 2

    try:
        async with SessionLocal() as session:
            plan = await build_plan(session, ids)
            print_plan(plan)
            if not args.confirm:
                print("\nPreview only. Re-run with --confirm to delete.")
                return 0
            if not plan.documents:
                print("\nNothing to delete.")
                return 0
            result = await apply_plan(session, plan)
            print_result(result)
            return 1 if result.failed else 0
    finally:
        await close_redis_connections()
        await engine.dispose()


def _run() -> Any:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _run()
