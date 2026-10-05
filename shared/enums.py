from enum import StrEnum
from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema


class AnalysisType(StrEnum):
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    SENTIMENT = "sentiment"
    RISK = "risk"
    MACRO = "macro"
    NEWS = "news"
    FILING = "filing"
    FULL = "full"


class AnalysisFailureReason(StrEnum):
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SERVICE_RESTART = "service_restart"
    WORKER_ERROR = "worker_error"
    WORKFLOW_ERROR = "workflow_error"
    JUDGE_UNAVAILABLE = "judge_unavailable"
    NO_EVIDENCE = "no_evidence"
    DEBATE_UNAVAILABLE = "debate_unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"


class DocumentCategory(StrEnum):
    FILING = "filing"
    ANALYST_REPORT = "analyst_report"
    EARNINGS_CALL = "earnings_call"
    NEWS = "news"
    MACRO_RESEARCH = "macro_research"
    RISK_ANALYSIS = "risk_analysis"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"

    @classmethod
    def __get_pydantic_json_schema__(
            cls, schema: CoreSchema,
            handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        json_schema = handler.resolve_ref_schema(handler(schema))
        json_schema["description"] = (
            "Processing status of a document: set by ai-service during "
            "RAG ingestion, stored and filterable in core-service.")
        return json_schema


class AssetType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"


class TransactionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    STOCK_SPLIT = "stock_split"
    ADJUSTMENT = "adjustment"


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class NotificationType(StrEnum):
    PORTFOLIO_UPDATED = "portfolio_updated"
    ANALYSIS_STARTED = "analysis_started"
    ANALYSIS_COMPLETED = "analysis_completed"
    RECOMMENDATION_READY = "recommendation_ready"
    ORGANIZATION_INVITE = "organization_invite"

    PORTFOLIO_CREATED = "portfolio_created"
    PORTFOLIO_DELETED = "portfolio_deleted"
    TRANSACTION_CREATED = "transaction_created"
    WATCHLIST_CREATED = "watchlist_created"
    WATCHLIST_UPDATED = "watchlist_updated"
    WATCHLIST_DELETED = "watchlist_deleted"
    API_KEY_CREATED = "api_key_created"
    API_KEY_UPDATED = "api_key_updated"
    API_KEY_REVOKED = "api_key_revoked"

    NOTE_CREATED = "note_created"
    NOTE_UPDATED = "note_updated"
    NOTE_DELETED = "note_deleted"
    NOTE_HIDDEN = "note_hidden"
    COMMENT_CREATED = "comment_created"
    COMMENT_UPDATED = "comment_updated"
    COMMENT_DELETED = "comment_deleted"
    COMMENT_HIDDEN = "comment_hidden"
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_UPDATED = "document_updated"
    DOCUMENT_READY = "document_ready"
    DOCUMENT_FAILED = "document_failed"
    DOCUMENT_DELETED = "document_deleted"

    ORGANIZATION_CREATED = "organization_created"
    ORGANIZATION_UPDATED = "organization_updated"
    ORGANIZATION_DELETED = "organization_deleted"
    ORGANIZATION_MEMBER_JOINED = "organization_member_joined"
    ORGANIZATION_MEMBER_LEFT = "organization_member_left"
    ORGANIZATION_MEMBER_REMOVED = "organization_member_removed"
    ORGANIZATION_ROLE_CHANGED = "organization_role_changed"
    FRIEND_REQUEST = "friend_request"
    FRIEND_ACCEPTED = "friend_accepted"
    FRIEND_REMOVED = "friend_removed"
    ACCOUNT_UPDATED = "account_updated"
    ACCOUNT_ROLE_CHANGED = "account_role_changed"

    ANALYSIS_FAILED = "analysis_failed"
    DECISION_RECORDED = "decision_recorded"
    DECISION_EVALUATED = "decision_evaluated"
    TIER_EXECUTED = "tier_executed"
    TIER_EXECUTION_FAILED = "tier_execution_failed"
    TIER_CANCELLED = "tier_cancelled"
    BACKTEST_COMPLETED = "backtest_completed"
    BACKTEST_FAILED = "backtest_failed"

    @classmethod
    def __get_pydantic_json_schema__(
            cls, schema: CoreSchema,
            handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        json_schema = handler.resolve_ref_schema(handler(schema))
        json_schema["description"] = (
            "Type of a notification. Every create, update and delete a "
            "user performs produces a notification for that user, and "
            "actions that affect someone else also notify that person.")
        return json_schema


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    WEBSOCKET = "websocket"


class OAuthProvider(StrEnum):
    GOOGLE = "google"
    GITHUB = "github"


class RecommendationAction(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class TierSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TierStatus(StrEnum):
    PENDING_ACCEPTANCE = "pending_acceptance"
    ACCEPTED_AWAITING_TRIGGER = "accepted_awaiting_trigger"
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    DECLINED = "declined"
    EXECUTION_FAILED = "execution_failed"


class SystemRole(StrEnum):
    ADMIN = "admin"
    MODERATOR = "moderator"
    USER = "user"
    GUEST = "guest"

    @classmethod
    def __get_pydantic_json_schema__(
            cls, schema: CoreSchema,
            handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        json_schema = handler.resolve_ref_schema(handler(schema))
        json_schema["description"] = (
            "Site-wide roles: `admin` manages users, `moderator` "
            "moderates content and can deactivate users, `user` is the "
            "normal role, `guest` is read-only.")
        return json_schema


class OrganizationRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRole(StrEnum):
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
