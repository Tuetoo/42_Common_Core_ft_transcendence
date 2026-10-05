from uuid import UUID
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from domains.rag.model import DocumentChunk, EmbeddingJob


class EmbeddingJobRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, document_id: UUID, embedding_model: str,
            status: str) -> EmbeddingJob:
        job = EmbeddingJob(
            document_id=document_id, embedding_model=embedding_model,
            status=status)
        self.session.add(job)
        await self.session.flush()
        return job

    async def set_status(self, job_id: UUID, status: str) -> bool:
        result = await self.session.execute(
            update(EmbeddingJob).where(EmbeddingJob.id == job_id).values(
                status=status))
        return bool(getattr(result, "rowcount", 0))

    async def delete_for_document(self, document_id: UUID) -> int:
        result = await self.session.execute(
            delete(EmbeddingJob).where(
                EmbeddingJob.document_id == document_id))
        return int(getattr(result, "rowcount", 0) or 0)


class DocumentChunkRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_for_document(self, document_id: UUID) -> int:
        result = await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_id == document_id))
        return int(getattr(result, "rowcount", 0) or 0)

    async def bulk_create(
            self, *, document_id: UUID,
            rows: list[tuple[int, int, str]]) -> list[DocumentChunk]:
        chunks = [
            DocumentChunk(
                document_id=document_id, chunk_index=idx,
                token_count=token_count, qdrant_point_id=point_id)
            for idx, token_count, point_id in rows
        ]
        self.session.add_all(chunks)
        await self.session.flush()
        return chunks
