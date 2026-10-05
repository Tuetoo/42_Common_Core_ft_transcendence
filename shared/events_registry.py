from enum import StrEnum


class EventName(StrEnum):
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_DELETED = "document.deleted"
    PORTFOLIO_DELETED = "portfolio.deleted"

    DOCUMENT_STATUS_CHANGED = "document.status_changed"
    ANALYSIS_STARTED = "analysis.started"
    ANALYSIS_COMPLETED = "analysis.completed"
    ANALYSIS_FAILED = "analysis.failed"
    RECOMMENDATION_READY = "recommendation.ready"
    DECISION_OUTCOME_EVALUATED = "decision.outcome.evaluated"
    DECISION_RECORDED = "decision.recorded"
    TIER_EXECUTED = "tier.executed"
    TIER_EXECUTION_FAILED = "tier.execution_failed"
    TIER_CANCELLED = "tier.cancelled"
    BACKTEST_COMPLETED = "backtest.completed"
    BACKTEST_FAILED = "backtest.failed"

    ORGANIZATION_MEMBER_JOINED = "organization.member.joined"
    USER_PRESENCE_CHANGED = "user.presence.changed"
    ORGANIZATION_CREATED = "organization.created"
    ORGANIZATION_UPDATED = "organization.updated"
    ORGANIZATION_DELETED = "organization.deleted"
    ORGANIZATION_MEMBER_LEFT = "organization.member.left"
    ORGANIZATION_MEMBER_REMOVED = "organization.member.removed"
    ORGANIZATION_ROLE_CHANGED = "organization.role.changed"
    ORGANIZATION_INVITATION_CREATED = "organization.invitation.created"
    FRIEND_REQUESTED = "friend.requested"
    FRIEND_ACCEPTED = "friend.accepted"
    FRIEND_REMOVED = "friend.removed"
    USER_UPDATED = "user.updated"
    USER_ROLE_CHANGED = "user.role.changed"
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    USER_DELETED = "user.deleted"


EVENT_OWNERS: dict[EventName, str] = {
    EventName.DOCUMENT_UPLOADED: "core-service",
    EventName.DOCUMENT_DELETED: "core-service",
    EventName.PORTFOLIO_DELETED: "core-service",
    EventName.DOCUMENT_STATUS_CHANGED: "ai-service",
    EventName.ANALYSIS_STARTED: "ai-service",
    EventName.ANALYSIS_COMPLETED: "ai-service",
    EventName.ANALYSIS_FAILED: "ai-service",
    EventName.RECOMMENDATION_READY: "ai-service",
    EventName.DECISION_OUTCOME_EVALUATED: "ai-service",
    EventName.DECISION_RECORDED: "ai-service",
    EventName.TIER_EXECUTED: "ai-service",
    EventName.TIER_EXECUTION_FAILED: "ai-service",
    EventName.TIER_CANCELLED: "ai-service",
    EventName.BACKTEST_COMPLETED: "ai-service",
    EventName.BACKTEST_FAILED: "ai-service",
    EventName.ORGANIZATION_MEMBER_JOINED: "auth-service",
    EventName.USER_PRESENCE_CHANGED: "auth-service",
    EventName.ORGANIZATION_CREATED: "auth-service",
    EventName.ORGANIZATION_UPDATED: "auth-service",
    EventName.ORGANIZATION_DELETED: "auth-service",
    EventName.ORGANIZATION_MEMBER_LEFT: "auth-service",
    EventName.ORGANIZATION_MEMBER_REMOVED: "auth-service",
    EventName.ORGANIZATION_ROLE_CHANGED: "auth-service",
    EventName.ORGANIZATION_INVITATION_CREATED: "auth-service",
    EventName.FRIEND_REQUESTED: "auth-service",
    EventName.FRIEND_ACCEPTED: "auth-service",
    EventName.FRIEND_REMOVED: "auth-service",
    EventName.USER_UPDATED: "auth-service",
    EventName.USER_ROLE_CHANGED: "auth-service",
    EventName.USER_DEACTIVATED: "auth-service",
    EventName.USER_REACTIVATED: "auth-service",
    EventName.USER_DELETED: "auth-service"
}
