from datetime import date, datetime
from typing import Any, cast
from uuid import UUID
from sqlalchemy import Date
from sqlalchemy import cast as sql_cast
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from domains.analysis.model import Analysis
from domains.knowledge.model import (AgentReport, KgEntity, KgRelation,
                                     TickerName)

_RawRelationRow = tuple[str, str, float | None, str, datetime, str]

AggregatedRelationRow = tuple[str, str, str, int, float | None, str, datetime,
                              datetime]


class AgentReportRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, report_id: UUID, analysis_id: UUID, agent_role: str,
            ticker: str | None, category: str, content: str) -> AgentReport:
        report = AgentReport(
            id=report_id, analysis_id=analysis_id, agent_role=agent_role,
            ticker=ticker, category=category, content=content)
        self.session.add(report)
        await self.session.flush()
        return report

    async def list_recent_by_category(
            self, category: str, *, ticker: str | None = None,
            limit: int = 5) -> list[AgentReport]:
        query = select(AgentReport).where(AgentReport.category == category)
        if ticker is not None:
            query = query.where(AgentReport.ticker == ticker)
        query = query.order_by(AgentReport.created_at.desc()).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def delete(self, report_id: UUID) -> None:
        await self.session.execute(
            delete(AgentReport).where(AgentReport.id == report_id))


class KnowledgeGraphRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create_entity(
            self, *, name: str, entity_type: str) -> KgEntity:
        normalized_name = name.strip().upper()
        existing = await self.session.scalar(
            select(KgEntity).where(
                KgEntity.normalized_name == normalized_name,
                KgEntity.entity_type == entity_type))
        if existing is not None:
            return existing

        entity = KgEntity(
            name=name.strip(), normalized_name=normalized_name,
            entity_type=entity_type)
        try:
            async with self.session.begin_nested():
                self.session.add(entity)
                await self.session.flush()
        except IntegrityError:
            existing = await self.session.scalar(
                select(KgEntity).where(
                    KgEntity.normalized_name == normalized_name,
                    KgEntity.entity_type == entity_type))
            if existing is not None:
                found_entity: KgEntity = existing
                return found_entity
            raise
        return entity

    async def create_relation(
            self, *, source_entity_id: UUID, target_entity_id: UUID,
            relation_type: str, source_report_id: UUID,
            confidence: float | None, kind: str | None = None,
            as_of: date | None = None) -> KgRelation:
        relation = KgRelation(
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id, relation_type=relation_type,
            source_report_id=source_report_id, confidence=confidence,
            kind=kind, as_of=as_of)
        self.session.add(relation)
        await self.session.flush()
        return relation

    async def delete_relations_for_report(self, report_id: UUID) -> int:
        result = await self.session.execute(
            delete(KgRelation).where(KgRelation.source_report_id == report_id))
        return cast(CursorResult[Any], result).rowcount or 0

    async def find_relations_for_entity_name(
        self, name: str, *, ticker_filter: str | None = None, limit: int = 20,
        viewer_user_id: str | UUID | None = None,
        events_expire_before: date | None = None
    ) -> list[AggregatedRelationRow]:
        normalized_name = name.strip().upper()
        viewer = _viewer_uuid(viewer_user_id)
        entity_ids = list((
            await self.session.scalars(
                select(KgEntity.id).where(
                    KgEntity.normalized_name == normalized_name))).all())
        if not entity_ids:
            return []

        source_entity = aliased(KgEntity)
        target_entity = aliased(KgEntity)

        as_source_query = (
            select(
                target_entity.name, KgRelation.relation_type,
                KgRelation.confidence, AgentReport.category,
                KgRelation.created_at).join(
                    target_entity,
                    KgRelation.target_entity_id == target_entity.id).join(
                        AgentReport,
                        KgRelation.source_report_id == AgentReport.id).where(
                            KgRelation.source_entity_id.in_(entity_ids)))
        as_target_query = (
            select(
                source_entity.name, KgRelation.relation_type,
                KgRelation.confidence, AgentReport.category,
                KgRelation.created_at).join(
                    source_entity,
                    KgRelation.source_entity_id == source_entity.id).join(
                        AgentReport,
                        KgRelation.source_report_id == AgentReport.id).where(
                            KgRelation.target_entity_id.in_(entity_ids)))
        visible = (
            Analysis.user_id.is_(None) if viewer is None else or_(
                Analysis.user_id.is_(None), Analysis.user_id == viewer))
        as_source_query = as_source_query.outerjoin(
            Analysis, AgentReport.analysis_id == Analysis.id).where(visible)
        as_target_query = as_target_query.outerjoin(
            Analysis, AgentReport.analysis_id == Analysis.id).where(visible)
        if ticker_filter is not None:
            as_source_query = as_source_query.where(
                AgentReport.ticker == ticker_filter)
            as_target_query = as_target_query.where(
                AgentReport.ticker == ticker_filter)
        if events_expire_before is not None:
            extracted = sql_cast(KgRelation.created_at, Date)
            current = or_(
                KgRelation.kind.is_distinct_from("event"),
                func.least(
                    func.coalesce(KgRelation.as_of, extracted), extracted)
                >= events_expire_before)
            as_source_query = as_source_query.where(current)
            as_target_query = as_target_query.where(current)

        as_source = await self.session.execute(
            as_source_query.order_by(KgRelation.created_at.desc()))
        as_target = await self.session.execute(
            as_target_query.order_by(KgRelation.created_at.desc()))

        raw_rows: list[_RawRelationRow] = [(
            other_name, relation_type, confidence, source_category, created_at,
            "->") for (
                other_name, relation_type, confidence, source_category,
                created_at) in as_source] + [(
                    other_name, relation_type, confidence, source_category,
                    created_at, "<-") for (
                        other_name, relation_type, confidence, source_category,
                        created_at) in as_target]
        return _aggregate_relation_rows(raw_rows, limit=limit)


def _viewer_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _aggregate_relation_rows(rows: list[_RawRelationRow], *,
                             limit: int) -> list[AggregatedRelationRow]:
    groups: dict[tuple[str, str, str], list[_RawRelationRow]] = {}
    for row in rows:
        (
            other_name, relation_type, _confidence, _source_category,
            _created_at, direction) = row
        groups.setdefault((other_name, relation_type, direction),
                          []).append(row)

    aggregated: list[AggregatedRelationRow] = []
    for (other_name, relation_type, direction), group_rows in groups.items():
        confidences = [r[2] for r in group_rows if r[2] is not None]
        confidence_avg = (
            sum(confidences) / len(confidences) if confidences else None)
        source_categories = ", ".join(sorted({r[3] for r in group_rows}))
        timestamps = [r[4] for r in group_rows]
        aggregated.append((
            other_name, relation_type,
            direction, len(group_rows), confidence_avg, source_categories,
            min(timestamps), max(timestamps)))

    aggregated.sort(key=lambda row: row[7], reverse=True)
    return aggregated[:limit]


class TickerNameRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, ticker: str) -> str | None:
        result = await self.session.execute(
            select(TickerName.official_name).where(
                TickerName.ticker == ticker.upper()))
        return result.scalar_one_or_none()

    async def create(self, *, ticker: str, official_name: str) -> TickerName:
        row = TickerName(ticker=ticker.upper(), official_name=official_name)
        self.session.add(row)
        await self.session.flush()
        return row
