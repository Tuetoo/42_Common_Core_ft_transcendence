from enum import StrEnum


class RecommendationAction(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionOutcome(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    PENDING = "pending"


class TierSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TierStatus(StrEnum):
    PENDING_ACCEPTANCE = "pending_acceptance"
    ACCEPTED_AWAITING_TRIGGER = "accepted_awaiting_trigger"
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
