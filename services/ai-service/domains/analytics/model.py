from uuid import UUID
from sqlalchemy import ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base, UUIDPKMixin


class RecommendationMetric(Base, UUIDPKMixin):
    __tablename__ = "recommendation_metrics"
    recommendation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("recommendations.id"), nullable=False)
    accuracy_score: Mapped[float | None] = mapped_column(Numeric)

    __table_args__ = (
        UniqueConstraint(
            "recommendation_id",
            name="uq_recommendation_metrics_recommendation_id"), )


class AgentMetric(Base, UUIDPKMixin):
    __tablename__ = "agent_metrics"
    agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=False,
        unique=True)
    avg_score: Mapped[float | None] = mapped_column(Numeric)
    success_rate: Mapped[float | None] = mapped_column(Numeric)
