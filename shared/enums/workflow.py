from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    MARKET = "market"
    NEWS = "news"
    SENTIMENT = "sentiment"
    FILING = "filing"
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    RISK = "risk"
    MACRO = "macro"
    PORTFOLIO = "portfolio"
    BULL = "bull"
    BEAR = "bear"
    JUDGE = "judge"
    REFLECTION = "reflection"
