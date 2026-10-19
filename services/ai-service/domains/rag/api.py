import time
from collections.abc import AsyncIterator
import structlog
from ai.rag.retriever import RetrievedChunk
from ai.rag.visibility import resolve_visibility_scope
from deps import get_rag_user
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from rate_limit import UserRateLimitedError
from domains.rag.qa import (RagAnswerUnavailableError, answer_question,
                            check_rate_limit, retrieve_sources, stream_answer)
from domains.rag.schema import RagAnswer, RagAskRequest
from shared.schemas import APIResponse
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["rag"])


async def _prepare(payload: RagAskRequest, user: CurrentUser, *,
                   stream: bool) -> list[RetrievedChunk]:
    try:
        await check_rate_limit(str(user.user_id))
    except UserRateLimitedError as exc:
        raise exc.http_error() from exc
    logger.info(
        "rag_ask.request", user_id=str(user.user_id), mode=payload.mode,
        question_length=len(payload.question), ticker=payload.ticker,
        categories=[c.value for c in payload.categories or []],
        top_k=payload.top_k, stream=stream)
    scope = await resolve_visibility_scope(str(user.user_id))
    try:
        return await retrieve_sources(payload, scope)
    except Exception as exc:
        logger.warning(
            "rag_ask.retrieval_failed", error_type=type(exc).__name__,
            error=str(exc)[:200])
        raise HTTPException(
            status_code=503, detail=(
                "Document search is temporarily unavailable, try again later"),
            headers={"Retry-After": "30"}) from exc


@router.post("/ask", response_model=APIResponse[RagAnswer])
async def ask(
    payload: RagAskRequest, user: CurrentUser = Depends(get_rag_user)
) -> APIResponse[RagAnswer]:
    started = time.monotonic()
    chunks = await _prepare(payload, user, stream=False)
    try:
        result = await answer_question(payload, chunks, started=started)
    except RagAnswerUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail=str(exc),
            headers={"Retry-After": "30"}) from exc
    logger.info(
        "rag_ask.answered", sources=len(result.citations), model=result.model,
        latency_ms=result.latency_ms, answer_length=len(result.answer))
    return APIResponse(data=result)


@router.post(
    "/ask/stream", response_class=StreamingResponse,
    responses={200: {
        "content": {
            "text/event-stream": {}
        }
    }})
async def ask_stream(
    payload: RagAskRequest, request: Request,
    user: CurrentUser = Depends(get_rag_user)
) -> StreamingResponse:
    started = time.monotonic()
    chunks = await _prepare(payload, user, stream=True)

    async def _events() -> AsyncIterator[str]:
        async for event in stream_answer(
                payload, chunks, started=started,
                is_disconnected=request.is_disconnected):
            yield event.to_sse()

    return StreamingResponse(
        _events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        })
