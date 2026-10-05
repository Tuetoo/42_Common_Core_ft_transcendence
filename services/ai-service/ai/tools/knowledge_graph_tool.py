from typing import Any, ClassVar
from ai.knowledge.kg_context import (drop_non_english_relations,
                                     fetch_kg_relations, format_kg_relations,
                                     newest_filing_date)
from ai.tools.base import Tool
from database import SessionLocal


class QueryKnowledgeGraphTool(Tool):
    name = "query_knowledge_graph"
    description = (
        "Query the internal knowledge graph for relations already known about "
        "a "
        "named entity (a ticker, company name, person, or other entity a "
        "prior "
        "archived report extracted). action_input: "
        '{"entity_name": "AMD"}. Returns whatever relations already exist, '
        "labeled with which type of report each one came from (fundamental "
        "analysis, risk analysis, news, or macro research) -- weigh a "
        "relation "
        "accordingly (e.g. a supply-chain dependency from a risk report is "
        "more directly relevant to risk exposure than one incidentally "
        "mentioned in a news report). By default this includes relations "
        "found while analyzing OTHER tickers too, if they happen to mention "
        "this entity (real cross-analysis corroboration, e.g. a competitor's "
        "own report mentioning this ticker) -- pass ticker_filter to narrow "
        "to just this ticker's own analysis history instead: "
        '{"entity_name": "AMD", "ticker_filter": "AMD"}. May be empty if '
        "nothing has been extracted about this entity yet, which is expected "
        "and not an error. Note: if you're asking about the ticker you're "
        "already analyzing, check your own task context first -- known facts "
        "about it are usually already provided there; use this tool to look "
        "up a DIFFERENT entity (a competitor, supplier, or other name that "
        "came up during your own reasoning).")
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "entity_name": {
                "type": "string"
            },
            "ticker_filter": {
                "type": "string"
            }
        },
        "required": ["entity_name"],
        "additionalProperties": False
    }

    def __init__(self, *, viewer_user_id: str | None = None) -> None:
        self.viewer_user_id = viewer_user_id or None

    async def run(  # type: ignore[override]
        self, entity_name: str, ticker_filter: str | None = None
    ) -> str:
        try:
            cutoff = await newest_filing_date(ticker_filter or entity_name)
            async with SessionLocal() as session:
                relations = await fetch_kg_relations(
                    entity_name, ticker_filter, session,
                    viewer_user_id=self.viewer_user_id,
                    events_expire_before=cutoff)
        except Exception as exc:
            return f"Knowledge graph query failed for {entity_name!r}: {exc}"

        relations = drop_non_english_relations(entity_name, relations)

        if not relations:
            scope_note = (
                f" (restricted to ticker={ticker_filter!r})"
                if ticker_filter else "")
            return (
                f"No knowledge graph relations found for "
                f"{entity_name!r}{scope_note}. "
                "This may mean nothing has been extracted about this entity "
                "yet, "
                "or the name doesn't exactly match how a prior report's "
                "extraction recorded it (entity matching is exact, not fuzzy, "
                "in this version -- e.g. 'AMD' and 'Advanced Micro Devices' "
                "are "
                "not automatically recognized as the same entity unless a "
                "known-alias mapping already covers the pair).")

        return format_kg_relations(entity_name, relations, ticker_filter)
