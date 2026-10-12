from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from pydantic import ConfigDict, Field, field_validator
from shared.enums import AgentRole, AnalysisType
from shared.schemas import BaseSchema, IDMixin, Ticker

RESEARCH_ROLES = tuple(
    role for role in AgentRole
    if role not in (AgentRole.PORTFOLIO, AgentRole.BULL, AgentRole.BEAR))


class AnalysisCreate(BaseSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Body of `POST /api/v1/analyses`. `analysis_type` and "
                "`requested_agents` are optional and steer the supervisor "
                "agent "
                "directly; a given `requested_agents` list must name at "
                "least one research agent. `user_id` is ignored on this "
                "route: the owner is always the caller.")
        })

    ticker: Ticker = Field(min_length=1, max_length=10)
    portfolio_id: UUID | None = None
    user_id: UUID | None = None
    analysis_type: AnalysisType = AnalysisType.FULL
    requested_agents: list[AgentRole] | None = None

    @field_validator("requested_agents")
    @classmethod
    def _names_a_research_agent(
            cls, value: list[AgentRole] | None) -> list[AgentRole] | None:
        if value is not None and not any(r in RESEARCH_ROLES for r in value):
            raise ValueError(
                "requested_agents must name at least one research agent: " +
                ", ".join(role.value for role in RESEARCH_ROLES))
        return value


class AnalysisRead(BaseSchema, IDMixin):
    ticker: str
    status: str
    user_id: UUID | None
    portfolio_id: UUID | None
    created_at: datetime
    failure_reason: str | None = None


class AgentResultRead(BaseSchema):
    score: float | None
    summary: str | None
    output: dict[str, Any] | None


class AgentRunRead(BaseSchema, IDMixin):
    agent_name: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    result: AgentResultRead | None


class DebateArgumentRead(BaseSchema):
    round_number: int
    side: str
    content: str
    citations: list[str] | None


class DebateRead(BaseSchema, IDMixin):
    arguments: list[DebateArgumentRead]


class ReflectionRead(BaseSchema, IDMixin):
    round_number: int
    verified: bool
    issues: list[str] | None
    retry_agents: dict[str, str] | None


class RecommendationTierRead(BaseSchema, IDMixin):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "One priced and sized tier, so a user can see exactly what "
                '"followed" authorises before accepting it. '
                "`window_expires_at` is the parent recommendation's (tiers "
                "share one window).")
        })

    recommendation_id: UUID
    side: str
    target_price: Decimal
    quantity: Decimal
    status: str
    window_expires_at: datetime | None
    triggered_at: datetime | None
    triggered_price: Decimal | None
    created_at: datetime


class RecommendationRead(BaseSchema, IDMixin):
    analysis_id: UUID
    ticker: str
    recommendation: str
    confidence: float
    reasoning: str
    confidence_adjustment_note: str | None
    tier_generation_note: str | None
    debate_converged: bool
    created_at: datetime
    window_expires_at: datetime | None = None
    accepted_at: datetime | None = None
    tiers: list[RecommendationTierRead] = Field(default_factory=list)


class AnalysisDetailRead(AnalysisRead):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Full detail of one analysis: every agent run, the debate "
                "transcript, the reflection history and the final "
                "recommendation,"
                " in one response.")
        })

    agent_runs: list[AgentRunRead]
    debate: DebateRead | None
    reflections: list[ReflectionRead]
    recommendation: RecommendationRead | None
