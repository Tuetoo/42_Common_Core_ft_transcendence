import argparse
import asyncio
import sys
from typing import Any
from unittest import mock
from uuid import uuid4


DESCRIPTION = (
    "Owner-run probe: does Anthropic accept each agent's ReAct step schema "
    "(full and reduced)? Sends max_tokens=16 requests; costs cents.")
_PROMPT = "Reply with the smallest valid JSON object."
_MAX_TOKENS = 16
_SETUP_TIMEOUT_SECONDS = 60.0


class _Captured(Exception):

    def __init__(self, schema: dict[str, Any]) -> None:
        super().__init__("schema captured")
        self.schema = schema


async def step_schemas() -> dict[str, dict[str, Any]]:
    from ai.agents.loop import ReActAgentLoop
    from ai.graph.nodes import AGENT_REGISTRY, _build_agent
    from ai.graph.state import AnalysisState
    from config import settings
    from shared.enums import AgentRole, AnalysisType

    async def _capture(self: ReActAgentLoop, *args: Any,
                       **kwargs: Any) -> None:
        raise _Captured(self._response_schema)

    schemas: dict[str, dict[str, Any]] = {}
    with mock.patch.object(ReActAgentLoop, "run", _capture), \
            mock.patch.object(settings, "AGENT_PREFETCH_ENABLED", False):
        for role in [*AGENT_REGISTRY, AgentRole.PORTFOLIO]:
            state = AnalysisState(
                analysis_id=str(uuid4()), user_id=str(uuid4()),
                portfolio_id=str(uuid4()), ticker="AAPL",
                analysis_type=AnalysisType.TECHNICAL)
            print(f"capturing {role.value} ...", flush=True)
            try:
                await asyncio.wait_for(
                    _build_agent(role, state).run(state),
                    timeout=_SETUP_TIMEOUT_SECONDS)
            except _Captured as captured:
                schemas[role.value] = captured.schema
            except Exception as exc:
                print(f"  {role.value}: not captured ({exc!r:.120})",
                      flush=True)
    return schemas


async def _send(client: Any, model: str,
                schema: dict[str, Any]) -> tuple[str, int]:
    import anthropic

    try:
        resp = await client.messages.create(
            model=model, max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": _PROMPT}],
            output_config={"format": {"type": "json_schema",
                                      "schema": schema}})
    except anthropic.APIStatusError as exc:
        return f"rejected ({exc.status_code}: {str(exc.message)[:80]})", 0
    usage = resp.usage
    return "accepted", usage.input_tokens + usage.output_tokens


async def main() -> None:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--model", default=None,
        help="model ID (default: LLM_ANTHROPIC_MODEL_CHEAP)")
    args = parser.parse_args()

    from ai.llm.anthropic_provider import (anthropic_output_schema,
                                           reduced_output_schema)
    from anthropic import AsyncAnthropic
    from config import settings

    if not settings.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    model = args.model or settings.LLM_ANTHROPIC_MODEL_CHEAP
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
    total = 0
    print(f"{model}, max_tokens={_MAX_TOKENS}\n", flush=True)
    for role, schema in (await step_schemas()).items():
        full = anthropic_output_schema(schema)
        if full is None:
            print(f"{role:<12} full: not sent (over the local limits)",
                  flush=True)
            continue
        full_status, full_tokens = await _send(client, model, full)
        reduced = reduced_output_schema(full)
        if reduced == full:
            reduced_status, reduced_tokens = "same as full", 0
        else:
            reduced_status, reduced_tokens = await _send(
                client, model, reduced)
        total += full_tokens + reduced_tokens
        print(
            f"{role:<12} full: {full_status:<10} reduced: "
            f"{reduced_status:<12} tokens: {full_tokens + reduced_tokens}",
            flush=True)
    print(f"\nTotal tokens: {total}")


if __name__ == "__main__":
    asyncio.run(main())
