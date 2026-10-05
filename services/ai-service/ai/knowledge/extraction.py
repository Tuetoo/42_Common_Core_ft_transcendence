import json
import math
from datetime import date
import structlog
from ai.llm.base import AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage, LLMTier
from ai.llm.json_fence import strip_json_code_fence
from ai.llm.router import LLMRouter
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)

logger = structlog.get_logger(__name__)

_ENTITY_TYPES = (
    "company", "person", "institution", "economic_indicator", "event",
    "concept", "product", "position", "country")

_MAX_TRIPLES = 15
KG_KINDS = ("stable", "event")
_EXTRACTION_MAX_TOKENS = 2048

_SYSTEM_PROMPT = (
    "Extract a small knowledge graph from the report below: a list of "
    "(source entity, relation, target entity) triples that capture its key "
    'factual claims. Use short, canonical entity names (e.g. "AMD", not '
    '"the company" or "Advanced Micro Devices Inc." if the report itself '
    'consistently says "AMD") -- consistent naming matters more than '
    "completeness, since two different names for the same real-world thing "
    "become two different graph nodes. "
    f"entity_type must be one of: {', '.join(_ENTITY_TYPES)}. "
    'relation should be a short verb phrase (e.g. "raised_rates_by", '
    '"affects", "reported_revenue_of"), not a full sentence. '
    'kind is "stable" for a lasting relationship (a competitor, a supplier, '
    'a customer, a product, a role) or "event" for a fact whose status or '
    "value can change (a lawsuit, investigation or regulatory action and its "
    "status, a deal, guidance, a metric reading). as_of is the date the "
    'report states the fact as of, as "YYYY-MM-DD", or null when it gives '
    "none. "
    f"Return at most {_MAX_TRIPLES} triples -- prioritize the most important "
    "claims, not every mentioned fact. Return ONLY a JSON array, no "
    "commentary, in this exact shape: "
    '[{"source": "...", "source_type": "...", "relation": "...", '
    '"target": "...", "target_type": "...", "confidence": 0.0-1.0, '
    '"kind": "stable" or "event", "as_of": "YYYY-MM-DD" or null}]. '
    "If the report contains nothing worth extracting, return [].")


class KgTriple(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=200)
    source_type: str
    relation: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=200)
    target_type: str
    confidence: float = 0.5
    kind: str | None = None
    as_of: date | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in KG_KINDS:
            return value.strip().lower()
        return None

    @field_validator("as_of", mode="before")
    @classmethod
    def _iso_date(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return value if isinstance(value, date) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> object:
        if value is None:
            return 0.5
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return value
        try:
            number = float(value)
        except ValueError:
            return value
        if not math.isfinite(number):
            raise ValueError("confidence must be a finite number")
        return min(1.0, max(0.0, number))


def _parse_response(raw: str) -> list[KgTriple]:
    text = strip_json_code_fence(raw)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("kg_extraction.unparseable_response", raw=raw[:500])
        return []

    if not isinstance(data, list):
        logger.warning("kg_extraction.response_not_a_list", raw=raw[:500])
        return []

    triples: list[KgTriple] = []
    for item in data[:_MAX_TRIPLES]:
        if isinstance(item, dict):
            recovered_fields = []
            if "source_type" not in item:
                item = {**item, "source_type": "concept"}
                recovered_fields.append("source_type")
            if "target_type" not in item:
                item = {**item, "target_type": "concept"}
                recovered_fields.append("target_type")
            if recovered_fields:
                logger.warning(
                    "kg_extraction.recovered_missing_type_field",
                    fields=recovered_fields, item=item)

        try:
            triple = KgTriple.model_validate(item)
        except ValidationError:
            logger.warning("kg_extraction.malformed_triple", item=item)
            continue
        if (triple.source_type not in _ENTITY_TYPES
                or triple.target_type not in _ENTITY_TYPES):
            logger.warning("kg_extraction.unknown_entity_type", item=item)
            continue
        triples.append(triple)
    return triples


async def extract_entities_relations(
        report_text: str, *, llm_router: LLMRouter | None = None,
        llm_tier: LLMTier = LLMTier.CHEAP, ticker: str | None = None,
        canonical_name: str | None = None) -> list[KgTriple]:
    router = llm_router or LLMRouter()
    user_content = report_text
    if ticker is not None and canonical_name is not None:
        user_content = (
            f"This report is specifically about ticker {ticker}" + (
                f", official name {canonical_name}"
                if canonical_name.upper() != ticker.upper() else "") +
            f'. Use "{ticker}" as the entity name for this company '
            "specifically, regardless of which name form the report text "
            "below actually uses.\n\n"
            f"{report_text}")
    try:
        response = await router.complete(
            llm_tier, [
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(role="user", content=user_content)
            ], temperature=0.0, retries=AGENT_LLM_CALL_RETRIES,
            max_tokens=_EXTRACTION_MAX_TOKENS)
    except LLMError as exc:
        logger.info("kg_extraction.unavailable", error=str(exc))
        return []

    return _parse_response(response.content)
