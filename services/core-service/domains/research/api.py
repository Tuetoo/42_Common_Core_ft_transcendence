from uuid import UUID
from datetime import datetime
from database import get_db
from deps import get_current_user
from fastapi import (APIRouter, Depends, Form, Query, Response, UploadFile,
                     status, BackgroundTasks)
from sqlalchemy.ext.asyncio import AsyncSession
from domains.research.schema import (
    DocumentRead, ResearchCommentCreate, ResearchCommentRead,
    ResearchNoteCreate, ResearchNoteFilters, ResearchNoteRead,
    ResearchNoteUpdate)
from domains.research.service import ResearchService
from shared.enums.document import DocumentCategory
from shared.schemas.base import (APIResponse, PaginatedResponse,
                                 PaginationParams)
from shared.security import CurrentUser

router = APIRouter(tags=["research"])


@router.post("/notes", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[ResearchNoteRead])
async def create_note(data: ResearchNoteCreate,
                      current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)
                      ) -> APIResponse[ResearchNoteRead]:
    result = await ResearchService(db).create_note(current_user.user_id, data)
    return APIResponse(data=result)


@router.get("/notes", response_model=PaginatedResponse[ResearchNoteRead])
async def search_notes(
    q: str | None = None, tag: str | None = None,
    author_id: UUID | None = None, organization_id: UUID | None = None,
    sort: str = Query(default="-created_at"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> PaginatedResponse[ResearchNoteRead]:
    filters = ResearchNoteFilters(
        q=q, tag=tag, author_id=author_id, organization_id=organization_id,
        sort=sort, page=page, page_size=page_size)
    items, total = await ResearchService(db).search_notes(
        current_user.user_id, filters)
    return PaginatedResponse(items=items, total=total, page=page,
                             page_size=page_size)


@router.get("/notes/{note_id}", response_model=APIResponse[ResearchNoteRead])
async def get_note(note_id: UUID,
                   current_user: CurrentUser = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)
                   ) -> APIResponse[ResearchNoteRead]:
    result = await ResearchService(db).get_note(note_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch("/notes/{note_id}", response_model=APIResponse[ResearchNoteRead])
async def update_note(note_id: UUID, data: ResearchNoteUpdate,
                      current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)
                      ) -> APIResponse[ResearchNoteRead]:
    result = await ResearchService(db).update_note(
        note_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: UUID,
                      current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> None:
    await ResearchService(db).delete_note(note_id, current_user.user_id)


@router.post("/notes/{note_id}/comments", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[ResearchCommentRead])
async def add_comment(note_id: UUID, data: ResearchCommentCreate,
                      current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)
                      ) -> APIResponse[ResearchCommentRead]:
    result = await ResearchService(db).add_comment(
        note_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.post("/documents", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[DocumentRead])
async def upload_document(
    background_tasks: BackgroundTasks, file: UploadFile,
    category: str = Form(...), ticker: str | None = Form(default=None),
    organization_id: UUID | None = Form(default=None),
    content_date: datetime | None = Form(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[DocumentRead]:
    content = await file.read()
    result = await ResearchService(db).upload_document(
        owner_id=current_user.user_id, organization_id=organization_id,
        filename=file.filename or "upload", content=content,
        category=category, ticker=ticker, content_date=content_date,
        background_tasks=background_tasks)
    return APIResponse(data=result)


@router.get("/documents", response_model=PaginatedResponse[DocumentRead])
async def list_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ticker: str | None = Query(default=None),
    category: DocumentCategory | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> PaginatedResponse[DocumentRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await ResearchService(db).list_documents(
        current_user.user_id, pagination=pagination, ticker=ticker,
        category=category)
    return PaginatedResponse(
        items=items, total=total, page=pagination.page,
        page_size=pagination.page_size)


@router.get("/documents/{document_id}",
            response_model=APIResponse[DocumentRead])
async def get_document(
    document_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[DocumentRead]:
    result = await ResearchService(db).get_document(
        document_id, current_user.user_id)
    return APIResponse(data=result)


@router.get("/documents/{document_id}/content")
async def download_document_content(
    document_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Response:
    service = ResearchService(db)
    metadata = await service.get_document(document_id, current_user.user_id)
    content = await service.download_document(
        document_id, current_user.user_id)
    media_type = {"pdf": "application/pdf", "txt": "text/plain",
                  "md": "text/markdown"}.get(metadata.document_type,
                                             "application/octet-stream")
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition":
                             f'inline; filename="{metadata.filename}"'})


@router.delete("/documents/{document_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await ResearchService(db).delete_document(
        document_id, current_user.user_id, background_tasks=background_tasks)
