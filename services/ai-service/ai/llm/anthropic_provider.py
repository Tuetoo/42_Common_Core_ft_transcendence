import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any, cast
import anthropic
import structlog
from ai.llm.base import (LLMConfigError, LLMCreditExhaustedError, LLMError,
                         LLMInvalidRequestError, LLMMessage,
                         LLMOutputExhaustedError, LLMProviderAdapter,
                         LLMProviderTimeoutError, LLMRateLimitError,
                         LLMRequestError, LLMResponse,
                         LLMServerOverloadedError, LLMUsage,
                         strip_json_code_fence)
from anthropic import AsyncAnthropic, omit
from anthropic.types import Message, MessageParam, OutputConfigParam, Usage
from config import settings

logger = structlog.get_logger(__name__)

_client: AsyncAnthropic | None = None
_schema_levels: dict[str, str] = {}

_INVALID_REQUEST_CODES = frozenset({400, 404, 413, 422})
_RATE_LIMIT_CODE = 429
_OVERLOADED_CODE = 529
_AUTH_ERROR_CODES = frozenset({401, 403})
_CREDIT_ERROR_TYPE = "billing_error"
_CREDIT_MESSAGE_MARKER = "credit balance"

_DROPPED_SCHEMA_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minLength", "maxLength", "maxItems", "pattern",
    "minProperties", "maxProperties", "uniqueItems", "patternProperties",
    "propertyNames"
})
_SUPPORTED_STRING_FORMATS = frozenset({
    "date-time", "time", "date", "duration", "email", "hostname", "uri",
    "ipv4", "ipv6", "uuid"
})
_MAX_OPTIONAL_PARAMETERS = 24
_MAX_UNION_PARAMETERS = 16

_ADAPTIVE = "adaptive"
_SCHEMA_TOO_COMPLEX_MARKER = "schema is too complex"
_STOP_MAX_TOKENS = "max_tokens"
_FULL_SCHEMA = "full"
_REDUCED_SCHEMA = "reduced"
_NO_SCHEMA = "none"
_EPHEMERAL_CACHE = {"type": "ephemeral"}
_MAX_MESSAGE_BREAKPOINTS = 3
_MAX_BREAKPOINTS = 4


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise LLMConfigError("ANTHROPIC_API_KEY is not configured")
        _client = AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
    return _client


def _segmented_content(
        message: LLMMessage, *, breakpoint: bool) -> list[dict[str, Any]]:
    texts: list[str] = []
    pending = ""
    for text in message.cache_segments or ():
        if text.strip():
            texts.append(pending + text)
            pending = ""
        elif texts:
            texts[-1] += text
        else:
            pending += text
    blocks: list[dict[str, Any]] = [{
        "type": "text",
        "text": text
    } for text in texts]
    if breakpoint:
        blocks[-1]["cache_control"] = _EPHEMERAL_CACHE
    return blocks


def _turn_content(
        message: LLMMessage, *,
        breakpoint: bool) -> str | list[dict[str, Any]]:
    if (not settings.LLM_ANTHROPIC_PROMPT_CACHING
            or not settings.LLM_ANTHROPIC_CACHE_SCRATCHPAD
            or message.cache_segments is None
            or not message.content.strip()):
        return message.content
    return _segmented_content(message, breakpoint=breakpoint)


def _split_system_and_turns(
        messages: list[LLMMessage], *,
        max_message_breakpoints: int = _MAX_MESSAGE_BREAKPOINTS
) -> tuple[str, list[MessageParam]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    others = [m for m in messages if m.role != "system"]
    hinted = [i for i, m in enumerate(others) if m.cache_segments is not None]
    marked = (
        set(hinted[-max_message_breakpoints:])
        if max_message_breakpoints > 0 else set())
    turns = cast(
        "list[MessageParam]", [{
            "role": m.role,
            "content": _turn_content(m, breakpoint=i in marked)
        } for i, m in enumerate(others)])
    return "\n\n".join(system_parts), turns


# With LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST, the evidence moves to the first
# system block. Its heading stays in the user message, so the message points
# there; without the pointer the model reads the heading as empty evidence.
_EVIDENCE_BLOCK_LABEL = "Evidence for this analysis:\n"
_EVIDENCE_POINTER = (
    "(given in the first system block, under \"Evidence for this analysis\")")


def _hoisted_evidence(
        messages: list[LLMMessage]) -> tuple[str | None, list[LLMMessage]]:
    if not (settings.LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST
            and settings.LLM_ANTHROPIC_PROMPT_CACHING):
        return None, messages
    for index, message in enumerate(messages):
        evidence = message.shared_evidence
        if message.role != "user" or evidence is None:
            continue
        if not evidence.strip() or not message.content.replace(
                evidence, "", 1).strip():
            return None, messages
        rest = message.content.replace(evidence, _EVIDENCE_POINTER, 1)
        moved = message.model_copy(
            update={
                "content": rest,
                "shared_evidence": None
            })
        return _EVIDENCE_BLOCK_LABEL + evidence, [
            *messages[:index], moved, *messages[index + 1:]
        ]
    return None, messages


def _system_param(system: str, evidence: str | None = None) -> Any:
    if evidence is not None:
        return [{
            "type": "text",
            "text": text,
            "cache_control": _EPHEMERAL_CACHE
        } for text in (evidence, system) if text]
    if not system:
        return omit
    if not settings.LLM_ANTHROPIC_PROMPT_CACHING:
        return system
    return [{
        "type": "text",
        "text": system,
        "cache_control": _EPHEMERAL_CACHE
    }]


def _convert_node(node: Any) -> Any:
    if isinstance(node, list):
        return [_convert_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED_SCHEMA_KEYWORDS:
            continue
        if key == "minItems" and not (isinstance(value, int) and value <= 1):
            continue
        if key == "format" and value not in _SUPPORTED_STRING_FORMATS:
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(
                value, dict):
            out[key] = {k: _convert_node(v) for k, v in value.items()}
        else:
            out[key] = _convert_node(value)
    node_type = out.get("type")
    is_object = node_type == "object" or (
        isinstance(node_type, list) and "object" in node_type)
    if is_object or "properties" in out:
        out["additionalProperties"] = False
    return out


def _ref_name(ref: Any) -> str | None:
    if not isinstance(ref, str):
        return None
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return None


def _refs_in(node: Any) -> set[str]:
    if isinstance(node, list):
        return set().union(*(_refs_in(item) for item in node))
    if not isinstance(node, dict):
        return set()
    found = set().union(*(_refs_in(v) for v in node.values()))
    name = _ref_name(node.get("$ref"))
    return found | {name} if name else found


def _is_recursive(schema: dict[str, Any]) -> bool:
    defs: dict[str, Any] = {
        **schema.get("definitions", {}),
        **schema.get("$defs", {})
    }
    graph = {name: _refs_in(body) for name, body in defs.items()}

    def _reaches(start: str, target: str, seen: set[str]) -> bool:
        for nxt in graph.get(start, set()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                if _reaches(nxt, target, seen):
                    return True
        return False

    return any(_reaches(name, name, set()) for name in graph)


def _parameter_counts(node: Any) -> tuple[int, int]:
    if isinstance(node, list):
        counts = [_parameter_counts(item) for item in node]
        return sum(c[0] for c in counts), sum(c[1] for c in counts)
    if not isinstance(node, dict):
        return 0, 0
    optional = union = 0
    properties = node.get("properties")
    if isinstance(properties, dict):
        required = set(node.get("required", []))
        optional += sum(1 for name in properties if name not in required)
        union += sum(
            1 for prop in properties.values() if isinstance(prop, dict) and (
                "anyOf" in prop or isinstance(prop.get("type"), list)))
    for value in node.values():
        child_optional, child_union = _parameter_counts(value)
        optional += child_optional
        union += child_union
    return optional, union


def anthropic_output_schema(
        schema: dict[str, Any]) -> dict[str, Any] | None:
    if _is_recursive(schema):
        return None
    converted: dict[str, Any] = _convert_node(schema)
    optional, union = _parameter_counts(converted)
    if optional > _MAX_OPTIONAL_PARAMETERS or union > _MAX_UNION_PARAMETERS:
        return None
    return converted


def _is_object_arm(node: Any) -> bool:
    return isinstance(node, dict) and (
        node.get("type") == "object" or "properties" in node)


def _merged_object_arm(arms: list[dict[str, Any]]) -> dict[str, Any]:
    variants: dict[str, list[Any]] = {}
    for arm in arms:
        for name, prop in (arm.get("properties") or {}).items():
            seen = variants.setdefault(name, [])
            if prop not in seen:
                seen.append(prop)
    return {
        "type": "object",
        "properties": {
            name: found[0] if len(found) == 1 else {"anyOf": found}
            for name, found in variants.items()
        },
        "additionalProperties": False
    }


def reduced_output_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [reduced_output_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: reduced_output_schema(value) for key, value in node.items()}
    arms = out.get("anyOf")
    if isinstance(arms, list):
        objects = [arm for arm in arms if _is_object_arm(arm)]
        if len(objects) > 1:
            out["anyOf"] = [
                _merged_object_arm(objects),
                *(arm for arm in arms if not _is_object_arm(arm))
            ]
    return out


def _output_config(
        response_schema: dict[str, Any] | None,
        model: str) -> OutputConfigParam | None:
    config: OutputConfigParam = {}
    if settings.LLM_ANTHROPIC_EFFORT:
        config["effort"] = settings.LLM_ANTHROPIC_EFFORT
    if response_schema:
        converted = anthropic_output_schema(response_schema)
        if converted is None:
            logger.warning(
                "anthropic.response_schema_not_sent", model=model,
                reason="recursive or over the structured output limits")
        else:
            config["format"] = {"type": "json_schema", "schema": converted}
    return config or None


def _thinking_mode(model: str) -> str:
    if model == settings.LLM_ANTHROPIC_MODEL_CHEAP:
        return settings.LLM_ANTHROPIC_THINKING_CHEAP
    if model == settings.LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY:
        return settings.LLM_ANTHROPIC_THINKING_HIGHEST_ACCURACY
    return _ADAPTIVE


def _is_credit_exhausted(exc: anthropic.APIStatusError) -> bool:
    return (
        exc.type == _CREDIT_ERROR_TYPE
        or _CREDIT_MESSAGE_MARKER in str(exc.message).lower())


def _status_error(exc: anthropic.APIStatusError, *, model: str,
                  action: str, thinking: str) -> LLMError:
    detail = f"Anthropic {action} failed for model {model!r}: {exc.message}"
    code = exc.status_code
    if _is_credit_exhausted(exc):
        return LLMCreditExhaustedError(
            "The Anthropic API credit is used up (credit balance too low); "
            f"no call to {model!r} can succeed until credit is added or "
            "LLM_DEFAULT_PROVIDER is switched.")
    if code == _RATE_LIMIT_CODE or exc.type == "rate_limit_error":
        return LLMRateLimitError(detail, requests_sent=1)
    if (code == _OVERLOADED_CODE or code >= 500
            or exc.type in ("overloaded_error", "api_error")):
        return LLMServerOverloadedError(detail, requests_sent=1)
    if code in _AUTH_ERROR_CODES or exc.type in ("authentication_error",
                                                 "permission_error"):
        return LLMConfigError(f"{detail} (check ANTHROPIC_API_KEY)")
    if code in _INVALID_REQUEST_CODES or exc.type in ("invalid_request_error",
                                                      "not_found_error"):
        if thinking != _ADAPTIVE and "thinking" in str(exc.message).lower():
            return LLMInvalidRequestError(
                f"Anthropic rejected thinking type {thinking!r} for model "
                f"{model!r}; set the tier's LLM_ANTHROPIC_THINKING_* setting "
                f"to {_ADAPTIVE!r}. {detail}")
        return LLMInvalidRequestError(detail)
    return LLMRequestError(detail)


def _mapped_error(
        exc: Exception, *, model: str, action: str,
        thinking: str) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMProviderTimeoutError(
            f"Anthropic {action} for model {model!r} timed out")
    if isinstance(exc, anthropic.APIStatusError):
        return _status_error(
            exc, model=model, action=action, thinking=thinking)
    return LLMRequestError(
        f"Anthropic {action} failed for model {model!r}: "
        f"{type(exc).__name__}: {str(exc)[:200]}")


def _log_failure(exc: Exception, error: LLMError, *, model: str,
                 event: str) -> None:
    logger.warning(
        event, model=model, error_type=type(error).__name__,
        status_code=getattr(exc, "status_code", None),
        api_error_type=getattr(exc, "type", None),
        request_id=getattr(exc, "request_id", None), raw_error=str(exc)[:500])


def _thinking_tokens(usage: Usage) -> int | None:
    details = usage.output_tokens_details
    return details.thinking_tokens if details is not None else None


def _prompt_tokens(usage: Usage) -> int:
    return (
        usage.input_tokens + (usage.cache_read_input_tokens or 0) +
        (usage.cache_creation_input_tokens or 0))


def _log_success(
        event: str, *, model: str, message: Message, started: float,
        evidence_first: bool = False, **fields: object) -> None:
    if evidence_first:
        fields["evidence_first"] = True
    usage = message.usage
    thinking_tokens = _thinking_tokens(usage)
    logger.info(
        event, model=model, prompt_tokens=_prompt_tokens(usage),
        uncached_prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens - (thinking_tokens or 0),
        thinking_tokens=thinking_tokens, finish_reason=message.stop_reason,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_creation_tokens=usage.cache_creation_input_tokens or 0,
        latency_ms=round((time.monotonic() - started) * 1000), **fields)


def _thinking_exhausted(
        message: Message, *, model: str, has_text: bool,
        max_tokens: int) -> LLMOutputExhaustedError | None:
    if has_text or message.stop_reason != _STOP_MAX_TOKENS:
        return None
    thinking_tokens = _thinking_tokens(message.usage)
    logger.warning(
        "anthropic.thinking_exhausted_max_tokens", model=model,
        max_tokens=max_tokens, thinking_tokens=thinking_tokens,
        output_tokens=message.usage.output_tokens)
    return LLMOutputExhaustedError(
        f"Anthropic model {model!r} used its whole max_tokens ({max_tokens}) "
        f"before writing any text (thinking tokens: {thinking_tokens})")


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _normalized_json(text: str, *, model: str) -> str:
    if _parses(text):
        return text
    spans = sorted((start, text[start:end + 1])
                   for start, end in ((text.find(o), text.rfind(c))
                                      for o, c in (("{", "}"), ("[", "]")))
                   if 0 <= start < end)
    for candidate in [strip_json_code_fence(text), *(s for _, s in spans)]:
        if candidate != text and _parses(candidate):
            logger.info(
                "anthropic.json_normalized", model=model,
                chars_dropped=len(text) - len(candidate))
            return candidate
    return text


def _schema_hash(kwargs: dict[str, Any]) -> str | None:
    config = kwargs["output_config"] or {}
    if "format" not in config:
        return None
    canonical = json.dumps(config["format"]["schema"], sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _without_format(kwargs: dict[str, Any]) -> dict[str, Any]:
    config = {
        k: v
        for k, v in (kwargs["output_config"] or {}).items() if k != "format"
    }
    return {**kwargs, "output_config": config or omit}


def _with_schema(kwargs: dict[str, Any],
                 schema: dict[str, Any]) -> dict[str, Any]:
    config = {
        **kwargs["output_config"], "format": {
            "type": "json_schema",
            "schema": schema
        }
    }
    return {**kwargs, "output_config": config}


def _schema_attempts(
        kwargs: dict[str, Any],
        schema_hash: str | None) -> list[tuple[str, dict[str, Any]]]:
    if schema_hash is None:
        return [(_NO_SCHEMA, kwargs)]
    full = kwargs["output_config"]["format"]["schema"]
    reduced = reduced_output_schema(full)
    attempts = {_FULL_SCHEMA: kwargs}
    if reduced != full:
        attempts[_REDUCED_SCHEMA] = _with_schema(kwargs, reduced)
    attempts[_NO_SCHEMA] = _without_format(kwargs)
    levels = list(attempts)
    start = levels.index(_schema_levels.get(schema_hash, _FULL_SCHEMA))
    return [(level, attempts[level]) for level in levels[start:]]


async def _create_with_schema_levels(
        client: AsyncAnthropic, kwargs: dict[str, Any], *,
        model: str) -> tuple[Message, dict[str, Any]]:
    schema_hash = _schema_hash(kwargs)
    attempts = _schema_attempts(kwargs, schema_hash)
    for index, (level, attempt) in enumerate(attempts):
        if schema_hash is not None and level == _NO_SCHEMA:
            logger.warning(
                "anthropic.response_schema_skipped", model=model,
                schema_hash=schema_hash)
        try:
            resp: Message = await client.messages.create(**attempt)
        except Exception as exc:
            if index == len(attempts) - 1 or not _is_schema_too_complex(exc):
                raise
            logger.warning(
                "anthropic.schema_too_complex_fallback", model=model,
                schema_hash=schema_hash, rejected_level=level,
                next_level=attempts[index + 1][0])
            continue
        if schema_hash is not None:
            _schema_levels[schema_hash] = level
        return resp, attempt
    raise AssertionError("unreachable")


def _is_schema_too_complex(exc: Exception) -> bool:
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return (
        exc.type == "invalid_request_error" and isinstance(message, str)
        and _SCHEMA_TOO_COMPLEX_MARKER in message.lower())


def _request_kwargs(
        messages: list[LLMMessage], *, evidence: str | None, model: str,
        temperature: float, max_tokens: int, timeout_seconds: float | None,
        output_config: OutputConfigParam | None,
        thinking: str) -> dict[str, Any]:
    """Build the request; `messages` and `evidence` come from
    `_hoisted_evidence`, which runs once per request."""
    system, turns = _split_system_and_turns(
        messages, max_message_breakpoints=min(
            _MAX_MESSAGE_BREAKPOINTS,
            _MAX_BREAKPOINTS - (evidence is not None) - any(
                m.role == "system" and m.content for m in messages)))
    return {
        "model": model,
        "system": _system_param(system, evidence),
        "messages": turns,
        "max_tokens": max_tokens + (
            settings.LLM_ANTHROPIC_THINKING_HEADROOM_TOKENS
            if thinking == _ADAPTIVE else 0),
        "thinking": {
            "type": thinking
        },
        "temperature": (
            temperature if settings.LLM_ANTHROPIC_SEND_TEMPERATURE else omit),
        "output_config": output_config or omit,
        "timeout": (
            timeout_seconds if timeout_seconds is not None else
            settings.LLM_ANTHROPIC_REQUEST_TIMEOUT_SECONDS)
    }


class AnthropicAdapter(LLMProviderAdapter):
    name = "anthropic"

    async def complete(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2, max_tokens: int = 1024,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None) -> LLMResponse:
        client = _get_client()
        thinking = _thinking_mode(model)
        evidence, messages = _hoisted_evidence(messages)
        kwargs = _request_kwargs(
            messages, evidence=evidence, model=model,
            temperature=temperature, max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            output_config=_output_config(
                response_schema
                if response_format == "json_object" else None, model),
            thinking=thinking)
        started = time.monotonic()
        try:
            resp, kwargs = await _create_with_schema_levels(
                client, kwargs, model=model)
        except Exception as exc:
            error = _mapped_error(
                exc, model=model, action="completion", thinking=thinking)
            _log_failure(
                exc, error, model=model, event="anthropic.request_failed")
            raise error from exc
        _log_success(
            "anthropic.completion_succeeded", model=model, message=resp,
            started=started,
            schema_sent="format" in (kwargs["output_config"] or {}),
            evidence_first=evidence is not None)
        content = "".join(
            block.text for block in resp.content if block.type == "text")
        exhausted = _thinking_exhausted(
            resp, model=model, has_text=bool(content),
            max_tokens=kwargs["max_tokens"])
        if exhausted is not None:
            raise exhausted
        if (response_format == "json_object"
                and "format" not in (kwargs["output_config"] or {})):
            content = _normalized_json(content, model=model)
        thinking_tokens = _thinking_tokens(resp.usage)
        return LLMResponse(
            content=content, provider=self.name, model=resp.model,
            usage=LLMUsage(
                prompt_tokens=_prompt_tokens(resp.usage),
                completion_tokens=(
                    resp.usage.output_tokens - (thinking_tokens or 0)),
                thinking_tokens=thinking_tokens),
            finish_reason=resp.stop_reason)

    async def stream(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        client = _get_client()
        thinking = _thinking_mode(model)
        evidence, messages = _hoisted_evidence(messages)
        kwargs = _request_kwargs(
            messages, evidence=evidence, model=model,
            temperature=temperature, max_tokens=max_tokens,
            timeout_seconds=None,
            output_config=_output_config(None, model), thinking=thinking)
        started = time.monotonic()
        has_text = False
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    has_text = has_text or bool(text)
                    yield text
                final = await stream.get_final_message()
        except Exception as exc:
            error = _mapped_error(
                exc, model=model, action="streaming", thinking=thinking)
            _log_failure(
                exc, error, model=model, event="anthropic.stream_failed")
            raise error from exc
        _log_success(
            "anthropic.stream_completed", model=model, message=final,
            started=started, evidence_first=evidence is not None)
        exhausted = _thinking_exhausted(
            final, model=model, has_text=has_text,
            max_tokens=kwargs["max_tokens"])
        if exhausted is not None:
            raise exhausted
