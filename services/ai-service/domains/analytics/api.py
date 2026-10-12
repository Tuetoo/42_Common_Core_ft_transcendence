from datetime import date
from typing import Literal
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analytics.dashboard import (AnalyticsDashboard, DashboardFilters,
                                         DashboardInputError, Dataset,
                                         Interval, Metric, Scope,
                                         resolve_range)
from domains.analytics.repository import AnalyticsRepository
from domains.analytics.schema import (AgentPerformanceRead,
                                      AnalyticsOverviewRead,
                                      AnalyticsTimeseriesRead)
from shared.enums import RecommendationAction, WorkflowStatus
from shared.exceptions import ValidationAppError
from shared.permissions import Permission
from shared.schemas import APIResponse, normalize_ticker
from shared.security import CurrentUser, ensure_permission

router = APIRouter(tags=["analytics"])


@router.get(
    "/agents", response_model=APIResponse[list[AgentPerformanceRead]],
    description="Global agent statistics. Any signed-in user may read them.")
async def get_agent_analytics(
        db: AsyncSession = Depends(get_db, scope="function"),
        user: CurrentUser = Depends(get_current_user)
) -> APIResponse[list[AgentPerformanceRead]]:
    metrics = await AnalyticsRepository(db).list_agent_metrics()
    return APIResponse(
        data=[AgentPerformanceRead.model_validate(m) for m in metrics])


def _filters(
        date_from: date | None, date_to: date | None, ticker: str | None,
        action: RecommendationAction | None,
        status: WorkflowStatus | None) -> DashboardFilters:
    try:
        resolved_from, resolved_to = resolve_range(date_from, date_to)
    except DashboardInputError as exc:
        raise ValidationAppError(str(exc)) from exc
    try:
        normalized_ticker = (
            normalize_ticker(ticker) if ticker and ticker.strip() else None)
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from exc
    return DashboardFilters(
        date_from=resolved_from, date_to=resolved_to, ticker=normalized_ticker
        or None, action=action,
        status=status.value if status is not None else None)


def _dashboard(
        db: AsyncSession, user: CurrentUser,
        scope: Scope) -> AnalyticsDashboard:
    if scope == "all":
        ensure_permission(user, Permission.SITE_STATS)
    return AnalyticsDashboard(db, user, scope)


@router.get(
    "/overview", response_model=APIResponse[AnalyticsOverviewRead],
    description=(
        "Counts by status, action distribution, average confidence, accuracy "
        "(recommendations scored by the daily review), per-agent and "
        "per-ticker "
        "breakdowns over the date range (default: the last 90 days)."))
async def get_overview(
        date_from: date | None = Query(default=None, alias="from"),
        date_to: date | None = Query(default=None, alias="to"),
        ticker: str | None = Query(default=None, max_length=10),
        action: RecommendationAction | None = Query(default=None),
        status: WorkflowStatus | None = Query(default=None),
        scope: Literal["mine", "all"] = Query(default="mine"),
        db: AsyncSession = Depends(get_db, scope="function"),
        user: CurrentUser = Depends(get_current_user)
) -> APIResponse[AnalyticsOverviewRead]:
    filters = _filters(date_from, date_to, ticker, action, status)
    result = await _dashboard(db, user, scope).overview(filters)
    return APIResponse(data=result)


@router.get(
    "/timeseries", response_model=APIResponse[AnalyticsTimeseriesRead],
    description=(
        "One point per day or week (UTC; weeks start Monday), gaps filled "
        "(analyses: value 0; means: value null, count 0)."))
async def get_timeseries(
        metric: Literal["analyses", "accuracy",
                        "confidence"] = Query(default="analyses"),
        interval: Literal["day", "week"] = Query(default="day"),
        date_from: date | None = Query(default=None, alias="from"),
        date_to: date | None = Query(default=None, alias="to"),
        ticker: str | None = Query(default=None, max_length=10),
        action: RecommendationAction | None = Query(default=None),
        status: WorkflowStatus | None = Query(default=None),
        scope: Literal["mine", "all"] = Query(default="mine"),
        db: AsyncSession = Depends(get_db, scope="function"),
        user: CurrentUser = Depends(get_current_user)
) -> APIResponse[AnalyticsTimeseriesRead]:
    filters = _filters(date_from, date_to, ticker, action, status)
    metric_value: Metric = metric
    interval_value: Interval = interval
    result = await _dashboard(db, user, scope).timeseries(
        filters, metric=metric_value, interval=interval_value)
    return APIResponse(data=result)


@router.get(
    "/export.csv", response_class=Response,
    responses={200: {
        "content": {
            "text/csv": {}
        }
    }}, description=(
        "The selected dataset as CSV (UTF-8, header row, newest first, at "
        "most "
        "50000 rows; `X-Export-Truncated: true` when cut)."))
async def export_csv(
        dataset: Literal["analyses", "recommendations",
                         "decisions"] = Query(default="analyses"),
        date_from: date | None = Query(default=None, alias="from"),
        date_to: date | None = Query(default=None, alias="to"),
        ticker: str | None = Query(default=None, max_length=10),
        action: RecommendationAction | None = Query(default=None),
        status: WorkflowStatus | None = Query(default=None),
        scope: Literal["mine", "all"] = Query(default="mine"),
        db: AsyncSession = Depends(get_db, scope="function"),
        user: CurrentUser = Depends(get_current_user)) -> Response:
    filters = _filters(date_from, date_to, ticker, action, status)
    dataset_value: Dataset = dataset
    text, truncated = await _dashboard(db, user, scope).export_csv(
        filters, dataset=dataset_value)
    filename = (
        f"alphamind_{dataset}_{filters.date_from.isoformat()}_"
        f"{filters.date_to.isoformat()}.csv")
    return Response(
        content=text, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Truncated": "true" if truncated else "false"
        })
