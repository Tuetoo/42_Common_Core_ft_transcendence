import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock
from uuid import uuid4

_AI_SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(_AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE_DIR))

DESCRIPTION = (
    "Owner-run probe: does Anthropic prompt caching reuse the ReAct "
    "scratchpad prefix? Sends a 3-turn ReAct-style prompt twice per turn "
    "to the CHEAP and HIGHEST_ACCURACY models with max_tokens=16; costs "
    "cents.")
_MAX_TOKENS = 16
_TURNS = 3
_OBSERVATION_LINES = 60


def _observation(turn: int) -> str:
    return "\n".join(
        f"row {turn}.{i}: close {100 + turn + i / 100:.2f}, volume "
        f"{1_000_000 + 37 * i}, note: synthetic probe data line {i}"
        for i in range(_OBSERVATION_LINES))


def probe_request(nonce: str, turns: int) -> tuple[list[Any], dict[str, Any]]:
    from ai.agents.loop import ReActAgentLoop, ScratchpadEntry
    from ai.tools.base import Tool
    from shared.enums import AgentRole

    class _ProbeTool(Tool):
        name = "get_price_history"
        description = "Daily closes for a ticker (probe stand-in)."
        parameters: ClassVar[dict[str, Any]] = {
            "type": "object",
            "properties": {}
        }

        async def run(self, **kwargs: Any) -> str:
            return ""

    loop = ReActAgentLoop(
        role=AgentRole.MARKET, tools=[_ProbeTool()],
        system_prompt=f"You are a market data agent (probe run {nonce}).",
        max_iterations=turns + 2, llm_router=mock.Mock())
    scratchpad = [
        ScratchpadEntry(
            thought=f"Step {i}: read more history.",
            action="get_price_history",
            action_input={"ticker": "AAPL", "page": i},
            observation=_observation(i)) for i in range(turns - 1)
    ]
    messages = loop._build_messages(
        "Summarise AAPL's recent price action.", None, scratchpad)
    return messages, loop._response_schema


def check(turn: int, repeat: bool, usage: Any,
          previous_cached: int) -> tuple[bool, str]:
    read = usage.cache_read_input_tokens or 0
    written = usage.cache_creation_input_tokens or 0
    if repeat:
        ok = written == 0 and read >= previous_cached
        return ok, "whole prompt read from cache, nothing written"
    if turn == 1:
        ok = written > 0
        return ok, "prefix written (system read if cached earlier)"
    ok = read >= previous_cached and written > 0
    return ok, (
        f"previous turn's {previous_cached} tokens read, newest entry "
        "written")


class _Recorder:

    def __init__(self, messages: Any) -> None:
        self._messages = messages
        self.last: Any = None
        self.sent: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.sent = kwargs
        self.last = await self._messages.create(**kwargs)
        return self.last


async def _probe_model(client: Any, model: str, nonce: str) -> int:
    import ai.llm.anthropic_provider as provider
    from ai.llm.base import LLMError

    recorder = _Recorder(client.messages)
    spent = 0
    cached = 0
    print(f"\n{model}")
    print(f"{'turn':<5}{'call':<8}{'input':>8}{'write':>8}{'read':>8}  check")
    with mock.patch.object(provider, "_client",
                           mock.Mock(messages=recorder)):
        for turn in range(1, _TURNS + 1):
            messages, schema = probe_request(nonce, turn)
            user = messages[-1]
            assert "".join(user.cache_segments) == user.content
            for repeat in (False, True):
                try:
                    await provider.AnthropicAdapter().complete(
                        messages, model=model, max_tokens=_MAX_TOKENS,
                        response_format="json_object", response_schema=schema)
                except LLMError as exc:
                    if recorder.last is None:
                        raise
                    print(f"      ({type(exc).__name__}; usage still read)")
                blocks = recorder.sent["messages"][-1]["content"]
                assert "".join(b["text"] for b in blocks) == user.content
                usage = recorder.last.usage
                ok, expected = check(turn, repeat, usage, cached)
                read = usage.cache_read_input_tokens or 0
                written = usage.cache_creation_input_tokens or 0
                print(
                    f"{turn:<5}{'repeat' if repeat else 'first':<8}"
                    f"{usage.input_tokens:>8}{written:>8}{read:>8}  "
                    f"{'OK' if ok else 'UNEXPECTED'}: {expected}", flush=True)
                spent += usage.input_tokens + written + read + (
                    usage.output_tokens)
                cached = read + written
                recorder.last = None
    return spent


async def _same_tokens_as_one_string(client: Any, model: str,
                                     nonce: str) -> None:
    import ai.llm.anthropic_provider as provider

    messages, _ = probe_request(nonce, _TURNS)
    user = messages[-1]
    # The adapter's own block builder, so the verdict covers what it sends.
    blocks = provider._segmented_content(user, breakpoint=False)
    counts = []
    for content in (user.content, blocks):
        result = await client.messages.count_tokens(
            model=model, system=messages[0].content,
            messages=[{"role": "user", "content": content}])
        counts.append(result.input_tokens)
    verdict = "same" if counts[0] == counts[1] else "DIFFERENT"
    print(f"\ncount_tokens on {model}: one string {counts[0]}, "
          f"{len(blocks)} blocks {counts[1]} ({verdict})")


async def main() -> None:
    argparse.ArgumentParser(description=DESCRIPTION).parse_args()
    from anthropic import AsyncAnthropic
    from config import settings

    for switch in ("LLM_ANTHROPIC_PROMPT_CACHING",
                   "LLM_ANTHROPIC_CACHE_SCRATCHPAD"):
        if not getattr(settings, switch):
            print(
                f"{switch} is false, so the scratchpad is sent as one "
                "string and not cached; nothing to probe.", file=sys.stderr)
            sys.exit(1)
    if not settings.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
    nonce = uuid4().hex[:12]
    print(f"probe run {nonce}, {_TURNS} turns, max_tokens={_MAX_TOKENS}, "
          "thinking headroom 0")
    total = 0
    with mock.patch.object(settings,
                           "LLM_ANTHROPIC_THINKING_HEADROOM_TOKENS", 0):
        for model in (settings.LLM_ANTHROPIC_MODEL_CHEAP,
                      settings.LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY):
            total += await _probe_model(client, model, nonce)
            await _same_tokens_as_one_string(client, model, nonce)
    print(f"\nTotal tokens: {total}")


if __name__ == "__main__":
    asyncio.run(main())
