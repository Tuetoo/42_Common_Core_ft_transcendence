from enum import StrEnum


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
