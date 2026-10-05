import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID
from deps import accessible_portfolio_ids
from sqlalchemy import ColumnElement, and_, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from domains.analysis.model import (Agent, AgentResult, AgentRun, Analysis,
                                    Recommendation)
from domains.analytics.model import RecommendationMetric
from domains.analytics.schema import (AccuracyRead, AgentBreakdownRead,
                                      AnalyticsOverviewRead,
                                      AnalyticsTimeseriesRead, DateRangeRead,
                                      TickerBreakdownRead, TimeseriesPointRead)
from domains.decision.model import DecisionOutcome, InvestmentDecision
from shared.enums import RecommendationAction
from shared.security import CurrentUser

MAX_RANGE_DAYS = 731
DEFAULT_RANGE_DAYS = 90
EXPORT_ROW_LIMIT = 50_000

Scope = Literal["mine", "all"]
Metric = Literal["analyses", "accuracy", "confidence"]
Interval = Literal["day", "week"]
Dataset = Literal["analyses", "recommendations", "decisions"]


class DashboardInputError(ValueError):
    pass


@dataclass(frozen=True)
class DashboardFilters:
    date_from: date
    date_to: date
    ticker: str | None = None
    action: RecommendationAction | None = None
    status: str | None = None

    @property
    def start(self) -> datetime:
        return datetime.combine(self.date_from, time.min, tzinfo=UTC)

    @property
    def end_exclusive(self) -> datetime:
        return datetime.combine(
            self.date_to + timedelta(days=1), time.min, tzinfo=UTC)


def resolve_range(
        date_from: date | None, date_to: date | None, *,
        today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(UTC).date()
    resolved_to = date_to or today
    resolved_from = date_from or (
        resolved_to - timedelta(days=DEFAULT_RANGE_DAYS))
    if resolved_from > resolved_to:
        raise DashboardInputError("'from' must be on or before 'to'")
    if (resolved_to - resolved_from).days > MAX_RANGE_DAYS:
        raise DashboardInputError("the date range may span at most 2 years")
    return resolved_from, resolved_to


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _float(value: Any) -> float | None:
    return round(float(value), 4) if value is not None else None


def _mean(total: float, count: int) -> float | None:
    return _float(total / count) if count else None


class AnalyticsDashboard:

    def __init__(
            self, session: AsyncSession, user: CurrentUser,
            scope: Scope) -> None:
        self.session = session
        self.user = user
        self.scope = scope

    async def _scope_condition(
            self, filters: DashboardFilters) -> ColumnElement[bool]:
        if self.scope == "all":
            return true()
        candidate_ids = ((
            await self.session.execute(
                select(Analysis.portfolio_id).where(
                    Analysis.user_id.is_(None),
                    Analysis.portfolio_id.is_not(None), Analysis.created_at
                    >= filters.start, Analysis.created_at
                    < filters.end_exclusive).distinct())).scalars().all())
        allowed = await accessible_portfolio_ids(
            self.user, [pid for pid in candidate_ids if pid is not None])
        system_clause: ColumnElement[bool] = (
            and_(
                Analysis.user_id.is_(None), Analysis.portfolio_id.in_(allowed))
            if allowed else false())
        return or_(Analysis.user_id == self.user.user_id, system_clause)

    async def _analysis_conditions(
            self, filters: DashboardFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [
            await self._scope_condition(filters), Analysis.created_at
            >= filters.start, Analysis.created_at < filters.end_exclusive
        ]
        if filters.ticker:
            conditions.append(Analysis.ticker == filters.ticker)
        if filters.status:
            conditions.append(Analysis.status == filters.status)
        if filters.action is not None:
            rec = aliased(Recommendation)
            conditions.append(
                select(rec.id).where(
                    rec.analysis_id == Analysis.id,
                    rec.recommendation == filters.action.value).correlate(
                        Analysis).exists())
        return conditions

    def _range(self, filters: DashboardFilters) -> DateRangeRead:
        return DateRangeRead(
            date_from=filters.date_from, date_to=filters.date_to)

    async def overview(
            self, filters: DashboardFilters) -> AnalyticsOverviewRead:
        conditions = await self._analysis_conditions(filters)
        status_rows = (
            await self.session.execute(
                select(Analysis.ticker, Analysis.status,
                       func.count()).where(*conditions).group_by(
                           Analysis.ticker, Analysis.status))).all()
        rec_rows = (
            await self.session.execute(
                select(
                    Analysis.ticker, Recommendation.recommendation,
                    func.count(Recommendation.id),
                    func.sum(Recommendation.confidence),
                    func.count(RecommendationMetric.accuracy_score),
                    func.sum(RecommendationMetric.accuracy_score)).join(
                        Analysis,
                        Recommendation.analysis_id == Analysis.id).outerjoin(
                            RecommendationMetric,
                            RecommendationMetric.recommendation_id ==
                            Recommendation.id).where(*conditions).group_by(
                                Analysis.ticker,
                                Recommendation.recommendation))).all()
        agent_rows = (
            await self.session.execute(
                select(
                    Agent.name, func.count(AgentRun.id),
                    func.count(
                        AgentRun.id).filter(AgentRun.status == "completed"),
                    func.count(
                        AgentRun.id).filter(AgentRun.status == "failed"),
                    func.avg(AgentResult.score),
                    func.avg(AgentRun.duration_ms)).join(
                        AgentRun, AgentRun.agent_id == Agent.id).join(
                            Analysis,
                            AgentRun.analysis_id == Analysis.id).outerjoin(
                                AgentResult,
                                AgentResult.agent_run_id == AgentRun.id).where(
                                    *conditions).group_by(Agent.name).order_by(
                                        Agent.name))).all()

        status_counts: dict[str, int] = defaultdict(int)
        per_ticker: dict[str, dict[str, Any]] = {}

        def _ticker(name: str) -> dict[str, Any]:
            return per_ticker.setdefault(
                name, {
                    "analyses": 0,
                    "completed": 0,
                    "failed": 0,
                    "actions": defaultdict(int),
                    "conf_sum": 0.0,
                    "recs": 0,
                    "evaluated": 0,
                    "acc_sum": 0.0
                })

        for ticker, status, count in status_rows:
            status_counts[str(status)] += int(count)
            bucket = _ticker(ticker)
            bucket["analyses"] += int(count)
            if status in ("completed", "failed"):
                bucket[status] += int(count)

        actions: dict[str, int] = {a.value: 0 for a in RecommendationAction}
        total_recs, conf_sum, evaluated, acc_sum = 0, 0.0, 0, 0.0
        for ticker, action, count, conf, n_eval, acc in rec_rows:
            bucket = _ticker(ticker)
            actions[str(action)] = actions.get(str(action), 0) + int(count)
            bucket["actions"][str(action)] += int(count)
            bucket["recs"] += int(count)
            bucket["conf_sum"] += float(conf or 0)
            bucket["evaluated"] += int(n_eval)
            bucket["acc_sum"] += float(acc or 0)
            total_recs += int(count)
            conf_sum += float(conf or 0)
            evaluated += int(n_eval)
            acc_sum += float(acc or 0)

        by_ticker = [
            TickerBreakdownRead(
                ticker=name, analyses=b["analyses"], completed=b["completed"],
                failed=b["failed"], actions=dict(b["actions"]),
                average_confidence=_mean(b["conf_sum"], b["recs"]),
                accuracy=AccuracyRead(
                    evaluated=b["evaluated"],
                    rate=_mean(b["acc_sum"], b["evaluated"])))
            for name, b in sorted(
                per_ticker.items(), key=lambda item:
                (-item[1]["analyses"], item[0]))
        ]
        by_agent = [
            AgentBreakdownRead(
                agent=str(name), runs=int(runs), completed=int(done),
                failed=int(failed), success_rate=_ratio(int(done), int(runs)),
                avg_score=_float(avg_score),
                avg_duration_ms=_float(avg_duration))
            for name, runs, done, failed, avg_score, avg_duration in agent_rows
        ]
        return AnalyticsOverviewRead(
            range=self._range(filters), scope=self.scope,
            total_analyses=sum(status_counts.values()),
            status_counts=dict(status_counts), recommendations=total_recs,
            action_distribution=actions,
            average_confidence=_mean(conf_sum,
                                     total_recs), accuracy=AccuracyRead(
                                         evaluated=evaluated,
                                         rate=_mean(acc_sum, evaluated)),
            by_agent=by_agent, by_ticker=by_ticker)

    async def timeseries(
            self, filters: DashboardFilters, *, metric: Metric,
            interval: Interval) -> AnalyticsTimeseriesRead:
        conditions = await self._analysis_conditions(filters)
        period = func.date_trunc(
            interval, func.timezone("UTC",
                                    Analysis.created_at)).label("period")
        stmt: Any
        if metric == "analyses":
            stmt = select(
                period, func.count(Analysis.id),
                func.count(Analysis.id)).where(*conditions)
        elif metric == "confidence":
            stmt = (
                select(
                    period, func.avg(Recommendation.confidence),
                    func.count(Recommendation.id)).join(
                        Analysis,
                        Recommendation.analysis_id == Analysis.id).where(
                            *conditions))
        else:
            stmt = (
                select(
                    period, func.avg(RecommendationMetric.accuracy_score),
                    func.count(RecommendationMetric.id)).join(
                        Recommendation, RecommendationMetric.recommendation_id
                        == Recommendation.id).join(
                            Analysis,
                            Recommendation.analysis_id == Analysis.id).where(
                                *conditions))
        rows = (
            await self.session.execute(stmt.group_by(period).order_by(period)
                                       )).all()
        found = {
            (row[0].date() if isinstance(row[0], datetime) else row[0]): row
            for row in rows
        }
        points: list[TimeseriesPointRead] = []
        for start in _periods(filters.date_from, filters.date_to, interval):
            row = found.get(start)
            if row is None:
                value: float | None = 0.0 if metric == "analyses" else None
                points.append(
                    TimeseriesPointRead(
                        period_start=start, value=value, count=0))
                continue
            points.append(
                TimeseriesPointRead(
                    period_start=start, value=(
                        float(row[1]) if metric == "analyses" else _float(
                            row[1])), count=int(row[2])))
        return AnalyticsTimeseriesRead(
            metric=metric, interval=interval, range=self._range(filters),
            scope=self.scope, points=points)

    async def export_csv(self, filters: DashboardFilters, *,
                         dataset: Dataset) -> tuple[str, bool]:
        if dataset == "decisions":
            header, rows = await self._decision_rows(filters)
        elif dataset == "recommendations":
            header, rows = await self._recommendation_rows(filters)
        else:
            header, rows = await self._analysis_rows(filters)
        truncated = len(rows) > EXPORT_ROW_LIMIT
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(header)
        for row in rows[:EXPORT_ROW_LIMIT]:
            writer.writerow([_csv_cell(value) for value in row])
        return buffer.getvalue(), truncated

    async def _analysis_rows(
            self,
            filters: DashboardFilters) -> tuple[list[str], list[list[Any]]]:
        conditions = await self._analysis_conditions(filters)
        latest_rec = select(
            Recommendation.analysis_id, Recommendation.recommendation,
            Recommendation.confidence,
            func.row_number().over(
                partition_by=Recommendation.analysis_id,
                order_by=Recommendation.created_at.desc()).label(
                    "rn")).subquery()
        result = (
            await self.session.execute(
                select(
                    Analysis.id, Analysis.created_at, Analysis.ticker,
                    Analysis.status, Analysis.failure_reason,
                    Analysis.portfolio_id, Analysis.user_id,
                    latest_rec.c.recommendation,
                    latest_rec.c.confidence).outerjoin(
                        latest_rec,
                        and_(
                            latest_rec.c.analysis_id == Analysis.id,
                            latest_rec.c.rn == 1)).where(*conditions).order_by(
                                Analysis.created_at.desc()).limit(
                                    EXPORT_ROW_LIMIT + 1))).all()
        header = [
            "analysis_id", "created_at", "ticker", "status", "failure_reason",
            "portfolio_id", "owner", "action", "confidence"
        ]
        rows = [[
            row[0],
            _iso(row[1]), row[2], row[3], row[4], row[5] or "",
            "system" if row[6] is None else "user", row[7] or "",
            "" if row[8] is None else row[8]
        ] for row in result]
        return header, rows

    async def _recommendation_rows(
            self,
            filters: DashboardFilters) -> tuple[list[str], list[list[Any]]]:
        conditions = await self._analysis_conditions(filters)
        rec_conditions = list(conditions)
        if filters.action is not None:
            rec_conditions.append(
                Recommendation.recommendation == filters.action.value)
        result = (
            await self.session.execute(
                select(
                    Recommendation.id, Recommendation.analysis_id,
                    Recommendation.created_at, Analysis.ticker,
                    Recommendation.recommendation, Recommendation.confidence,
                    RecommendationMetric.accuracy_score,
                    Recommendation.debate_converged,
                    Recommendation.window_expires_at,
                    Recommendation.accepted_at, Analysis.portfolio_id).join(
                        Analysis,
                        Recommendation.analysis_id == Analysis.id).outerjoin(
                            RecommendationMetric,
                            RecommendationMetric.recommendation_id ==
                            Recommendation.id).where(*rec_conditions).order_by(
                                Recommendation.created_at.desc()).limit(
                                    EXPORT_ROW_LIMIT + 1))).all()
        header = [
            "recommendation_id", "analysis_id", "created_at", "ticker",
            "action", "confidence", "accuracy_score", "debate_converged",
            "window_expires_at", "accepted_at", "portfolio_id"
        ]
        rows = [[
            row[0], row[1],
            _iso(row[2]), row[3], row[4], row[5],
            "" if row[6] is None else float(row[6]), row[7],
            _iso(row[8]),
            _iso(row[9]), row[10] or ""
        ] for row in result]
        return header, rows

    async def _decision_rows(
            self,
            filters: DashboardFilters) -> tuple[list[str], list[list[Any]]]:
        conditions: list[ColumnElement[bool]] = [
            InvestmentDecision.created_at >= filters.start,
            InvestmentDecision.created_at < filters.end_exclusive
        ]
        if self.scope != "all":
            conditions.append(InvestmentDecision.user_id == self.user.user_id)
        if filters.ticker:
            conditions.append(InvestmentDecision.ticker == filters.ticker)
        if filters.action is not None:
            conditions.append(
                Recommendation.recommendation == filters.action.value)
        latest_eval = select(
            DecisionOutcome.decision_id, DecisionOutcome.strategy_return,
            DecisionOutcome.benchmark_return, DecisionOutcome.outperformed,
            func.row_number().over(
                partition_by=DecisionOutcome.decision_id,
                order_by=DecisionOutcome.evaluated_at.desc().nulls_last(
                )).label("rn")).subquery()
        result = (
            await self.session.execute(
                select(
                    InvestmentDecision.id, InvestmentDecision.created_at,
                    InvestmentDecision.ticker, InvestmentDecision.action_taken,
                    Recommendation.recommendation,
                    InvestmentDecision.recommendation_id,
                    InvestmentDecision.portfolio_id, InvestmentDecision.notes,
                    latest_eval.c.strategy_return,
                    latest_eval.c.benchmark_return,
                    latest_eval.c.outperformed).join(
                        Recommendation, InvestmentDecision.recommendation_id ==
                        Recommendation.id).
                outerjoin(
                    latest_eval,
                    and_(
                        latest_eval.c.decision_id == InvestmentDecision.id,
                        latest_eval.c.rn == 1)).where(*conditions).order_by(
                            InvestmentDecision.created_at.desc()).limit(
                                EXPORT_ROW_LIMIT + 1))).all()
        header = [
            "decision_id", "created_at", "ticker", "action_taken",
            "recommended_action", "recommendation_id", "portfolio_id", "notes",
            "strategy_return", "benchmark_return", "outperformed"
        ]
        rows = [[
            row[0],
            _iso(row[1]), row[2], row[3], row[4], row[5], row[6] or "", row[7]
            or "", "" if row[8] is None else float(row[8]),
            "" if row[9] is None else float(row[9]),
            "" if row[10] is None else row[10]
        ] for row in result]
        return header, rows


def _periods(date_from: date, date_to: date, interval: Interval) -> list[date]:
    if interval == "day":
        days = (date_to - date_from).days + 1
        return [date_from + timedelta(days=i) for i in range(days)]
    start = date_from - timedelta(days=date_from.weekday())
    out: list[date] = []
    while start <= date_to:
        out.append(start)
        start += timedelta(days=7)
    return out


def _iso(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
