from collections.abc import AsyncIterator
from typing import Any, cast
from ai.llm.base import (LLMConfigError, LLMMessage, LLMProviderAdapter,
                         LLMRequestError, LLMResponse, LLMUsage)
from config import settings
from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONObject

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise LLMConfigError("OPENAI_API_KEY is not configured")
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


def _to_openai_messages(
        messages: list[LLMMessage]) -> list[ChatCompletionMessageParam]:
    return cast(
        "list[ChatCompletionMessageParam]", [{
            "role": m.role,
            "content": m.content
        } for m in messages])


class OpenAIAdapter(LLMProviderAdapter):
    name = "openai"

    async def complete(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2, max_tokens: int = 1024,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None) -> LLMResponse:
        client = _get_client()
        timeout = NOT_GIVEN if timeout_seconds is None else timeout_seconds
        try:
            if response_format == "json_object":
                resp = await client.chat.completions.create(
                    model=model, messages=_to_openai_messages(messages),
                    temperature=temperature, max_tokens=max_tokens,
                    response_format=ResponseFormatJSONObject(
                        type="json_object"), timeout=timeout)
            else:
                resp = await client.chat.completions.create(
                    model=model, messages=_to_openai_messages(messages),
                    temperature=temperature, max_tokens=max_tokens,
                    timeout=timeout)
        except Exception as exc:
            raise LLMRequestError(
                f"OpenAI completion failed for model {model!r}") from exc

        choice = resp.choices[0]
        return LLMResponse(
            content=choice.message.content or "", provider=self.name,
            model=resp.model, usage=LLMUsage(
                prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                completion_tokens=(
                    resp.usage.completion_tokens if resp.usage else 0)),
            finish_reason=choice.finish_reason)

    async def stream(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        client = _get_client()
        try:
            stream = await client.chat.completions.create(
                model=model, messages=_to_openai_messages(messages),
                temperature=temperature, max_tokens=max_tokens, stream=True)
            async for chunk in stream:
                delta = (
                    chunk.choices[0].delta.content if chunk.choices else None)
                if delta:
                    yield delta
        except Exception as exc:
            raise LLMRequestError(
                f"OpenAI streaming failed for model {model!r}") from exc
