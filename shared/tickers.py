import re
from typing import Annotated
from pydantic import BeforeValidator

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


def normalize_ticker(value: str) -> str:
    normalized = value.strip().upper()
    if not TICKER_RE.fullmatch(normalized):
        raise ValueError(
            f"Invalid ticker {value!r}: use 1-10 characters of A-Z, 0-9, '.' "
            f"or '-'.")
    return normalized


def _ticker_or_passthrough(value: object) -> object:
    if not isinstance(value, str):
        return value
    return normalize_ticker(value)


def _optional_ticker(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return _ticker_or_passthrough(value)


Ticker = Annotated[str, BeforeValidator(_ticker_or_passthrough)]
OptionalTicker = Annotated[str | None, BeforeValidator(_optional_ticker)]
