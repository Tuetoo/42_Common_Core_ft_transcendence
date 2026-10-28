from collections.abc import AsyncIterator
from typing import Any, cast
import openai
from ai.llm.base import (LLMConfigError, LLMCreditExhaustedError, LLMError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMProviderAdapter, LLMProviderTimeoutError,
                         LLMRateLimitError, LLMRequestError, LLMResponse,
                         LLMServerOverloadedError, LLMUsage)
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
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0)
    return _client


_RATE_LIMIT_CODE = 429
_QUOTA_ERROR_CODE = "insufficient_quota"


def _mapped_error(exc: Exception, *, model: str, action: str) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, openai.APITimeoutError):
        return LLMProviderTimeoutError(
            f"OpenAI {action} for model {model!r} timed out")
    detail = (
        f"OpenAI {action} failed for model {model!r}: "
        f"{type(exc).__name__}: {str(exc)[:200]}")
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code == _RATE_LIMIT_CODE and exc.code == _QUOTA_ERROR_CODE:
            return LLMCreditExhaustedError(
                "The OpenAI API quota is used up (insufficient_quota); no "
                f"call to {model!r} can succeed until credit is added or "
                "LLM_DEFAULT_PROVIDER is switched.")
        if code == _RATE_LIMIT_CODE:
            return LLMRateLimitError(detail, requests_sent=1)
        if code >= 500:
            return LLMServerOverloadedError(detail, requests_sent=1)
        if code >= 400:
            return LLMInvalidRequestError(detail)
    return LLMRequestError(detail)


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
            raise _mapped_error(
                exc, model=model, action="completion") from exc

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
            raise _mapped_error(
                exc, model=model, action="streaming") from exc
