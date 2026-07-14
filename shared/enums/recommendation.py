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
