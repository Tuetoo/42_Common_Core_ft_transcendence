from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from shared.schemas.refs import AnalysisRef, RecommendationRef
from database import get_db

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/analyses/{analysis_id}", response_model=AnalysisRef)
async def get_analysis_ref(analysis_id: UUID,
                           db: AsyncSession = Depends(get_db)) -> AnalysisRef:
    # TODO
    raise NotImplementedError


@router.get("/recommendations/latest", response_model=RecommendationRef)
async def get_latest_recommendation(
    ticker: str = Query(...), db: AsyncSession = Depends(get_db)
        ) -> RecommendationRef:
    # TODO
    raise NotImplementedError
