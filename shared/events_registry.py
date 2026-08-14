from enum import StrEnum


class EventName(StrEnum):
    # core -> ai
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_DELETED = "document.deleted"
    # ai -> core
    DOCUMENT_STATUS_CHANGED = "document.status_changed"
    ANALYSIS_STARTED = "analysis.started"
    ANALYSIS_COMPLETED = "analysis.completed"
    ANALYSIS_FAILED = "analysis.failed"
    RECOMMENDATION_READY = "recommendation.ready"
    DECISION_OUTCOME_EVALUATED = "decision.outcome.evaluated"
    # auth -> core/ai
    ORGANIZATION_MEMBER_JOINED = "organization.member.joined"
    USER_PRESENCE_CHANGED = "user.presence.changed"
    # core -> auth
    PORTFOLIO_CREATED = "portfolio.created"


EVENT_OWNERS: dict[EventName, str] = {
    EventName.DOCUMENT_UPLOADED: "core-service",
    EventName.DOCUMENT_DELETED: "core-service",
    EventName.DOCUMENT_STATUS_CHANGED: "ai-service",
    EventName.ANALYSIS_STARTED: "ai-service",
    EventName.ANALYSIS_COMPLETED: "ai-service",
    EventName.ANALYSIS_FAILED: "ai-service",
    EventName.RECOMMENDATION_READY: "ai-service",
    EventName.DECISION_OUTCOME_EVALUATED: "ai-service",
    EventName.ORGANIZATION_MEMBER_JOINED: "auth-service",
    EventName.USER_PRESENCE_CHANGED: "auth-service",
    EventName.PORTFOLIO_CREATED: "core-service",
}
