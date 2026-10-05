from collections.abc import AsyncIterator
from typing import Any, cast
from ai.llm.base import (LLMConfigError, LLMMessage, LLMProviderAdapter,
                         LLMRequestError, LLMResponse, LLMUsage)
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from config import settings

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise LLMConfigError("ANTHROPIC_API_KEY is not configured")
        _client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _split_system_and_turns(
        messages: list[LLMMessage]) -> tuple[str, list[MessageParam]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    turns = cast(
        "list[MessageParam]", [{
            "role": m.role,
            "content": m.content
        } for m in messages if m.role != "system"])
    return "\n\n".join(system_parts), turns


class AnthropicAdapter(LLMProviderAdapter):
    name = "anthropic"

    async def complete(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2, max_tokens: int = 1024,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None) -> LLMResponse:
        client = _get_client()
        system, turns = _split_system_and_turns(messages)
        try:
            resp = await client.messages.create(
                model=model, system=system, messages=turns,
                temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:
            raise LLMRequestError(
                f"Anthropic completion failed for model {model!r}") from exc

        content = "".join(
            block.text for block in resp.content if block.type == "text")
        return LLMResponse(
            content=content, provider=self.name, model=resp.model,
            usage=LLMUsage(
                prompt_tokens=resp.usage.input_tokens,
                completion_tokens=resp.usage.output_tokens),
            finish_reason=resp.stop_reason)

    async def stream(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        client = _get_client()
        system, turns = _split_system_and_turns(messages)
        try:
            async with client.messages.stream(model=model, system=system,
                                              messages=turns,
                                              temperature=temperature,
                                              max_tokens=max_tokens) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as exc:
            raise LLMRequestError(
                f"Anthropic streaming failed for model {model!r}") from exc
