from enum import StrEnum


class NotificationType(StrEnum):
    PORTFOLIO_UPDATED = "portfolio_updated"
    ANALYSIS_STARTED = "analysis_started"
    ANALYSIS_COMPLETED = "analysis_completed"
    RECOMMENDATION_READY = "recommendation_ready"
    ORGANIZATION_INVITE = "organization_invite"
    SYSTEM = "system"


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    WEBSOCKET = "websocket"
