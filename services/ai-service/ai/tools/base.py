from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
import structlog

logger = structlog.get_logger(__name__)

NON_EVIDENTIAL_TOOLS: frozenset[str] = frozenset({"query_knowledge_graph"})

_SHARE_CLASS_SEPARATORS = str.maketrans({".": "-", "/": "-"})


def normalize_ticker(value: object) -> str:
    return str(value).strip().upper()


def same_ticker(left: object, right: object) -> bool:
    return normalize_ticker(left).translate(
        _SHARE_CLASS_SEPARATORS) == normalize_ticker(right).translate(
            _SHARE_CLASS_SEPARATORS)


def peer_ticker_of(
        action_input: object, analysis_ticker: str | None) -> str | None:
    if analysis_ticker is None or not isinstance(action_input, Mapping):
        return None
    requested = action_input.get("ticker")
    if requested is None:
        return None
    if same_ticker(requested, analysis_ticker):
        return None
    return normalize_ticker(requested)


def peer_ticker_label(peer: str, analysis_ticker: str) -> str:
    analysed = normalize_ticker(analysis_ticker)
    return (
        f"[PEER TICKER {peer}: every figure above is for {peer}, not for "
        f"{analysed}, the company under analysis. It is not evidence about "
        f"{analysed}'s own figures; if you use it, name it as {peer}'s "
        "figure in a comparison.]")


def is_evidence_record(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("is_tool_result") and not record.get("tool_error")
        and (record.get("action") or "") not in NON_EVIDENTIAL_TOOLS
        and not record.get("peer_ticker"))


class Tool(ABC):
    name: str
    description: str
    parameters: ClassVar[dict[str, Any]]
    analysis_ticker: str | None = None

    @abstractmethod
    async def run(self, **kwargs: Any) -> str:
        ...

    def peer_ticker(self, action_input: object) -> str | None:
        return peer_ticker_of(action_input, self.analysis_ticker)

    def label_result(self, ticker: str, text: str) -> str:
        peer = peer_ticker_of({"ticker": ticker}, self.analysis_ticker)
        if peer is None or self.analysis_ticker is None:
            return text
        logger.info(
            "tool.peer_ticker_result_labelled", tool=self.name,
            peer_ticker=peer, analysis_ticker=self.analysis_ticker)
        return f"{text}\n{peer_ticker_label(peer, self.analysis_ticker)}"


def clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, number))


def age_in_days(value: str | datetime) -> int | None:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        try:
            moment = datetime.fromisoformat(value[:10]).replace(tzinfo=UTC)
        except ValueError:
            return None
    return (datetime.now(UTC) - moment).days
