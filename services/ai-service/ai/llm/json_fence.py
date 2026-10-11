import json
import re
from dataclasses import dataclass
from typing import Any
import structlog

logger = structlog.get_logger(__name__)

_JSON_CODE_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def strip_json_code_fence(raw: str) -> str:
    text = raw.strip()
    match = _JSON_CODE_FENCE_RE.search(text)
    return match.group(1).strip() if match else text


def parse_json_dict(raw: str, *,
                    log_event: str | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(strip_json_code_fence(raw))
    except json.JSONDecodeError:
        if log_event:
            logger.warning(log_event, raw=raw[:500])
        return None
    return data if isinstance(data, dict) else None


_LIST_KEYS = ("relevant", "results", "items")


def parse_json_list(raw: str, *, log_event: str) -> list[object] | None:
    try:
        data = json.loads(strip_json_code_fence(raw))
    except json.JSONDecodeError:
        logger.warning(log_event, raw=raw[:500])
        return None
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            if isinstance(data.get(key), list):
                return list(data[key])
        return None
    return data if isinstance(data, list) else None


@dataclass(frozen=True)
class JsonObjectParse:
    data: dict[str, Any] | None
    error: str | None = None
    error_pos: int | None = None
    error_line: int | None = None
    error_col: int | None = None
    text: str = ""
    leading_chars: int = 0
    trailing_text: str = ""
    lenient_strings: bool = False


_LENIENT_DECODER = json.JSONDecoder(strict=False)


def _failed(text: str, exc: json.JSONDecodeError) -> JsonObjectParse:
    return JsonObjectParse(
        data=None, error=exc.msg, error_pos=exc.pos, error_line=exc.lineno,
        error_col=exc.colno, text=text)


def _without_trailing_commas(text: str) -> str:
    """Drops a comma that directly precedes a closing brace or bracket.

    Models sometimes end an object with `"last": 1,}`. That comma is the
    only syntax fault seen in real answers that otherwise parse, and a
    re-ask costs a full model call. Only commas outside JSON strings are
    touched, so string content is never changed.
    """
    out: list[str] = []
    in_string = escaped = False
    pending_comma: int | None = None
    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            pending_comma = None
        elif ch in "}]" and pending_comma is not None:
            del out[pending_comma]
            pending_comma = None
        elif ch == ",":
            pending_comma = len(out)
        elif not ch.isspace():
            pending_comma = None
        out.append(ch)
    return "".join(out)


def parse_json_object(raw: str) -> JsonObjectParse:
    text = strip_json_code_fence(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as strict_exc:
        repaired = _without_trailing_commas(text)
        if repaired != text:
            logger.info("json.trailing_comma_removed")
            text = repaired
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                strict_exc = exc
            else:
                if not isinstance(data, dict):
                    return JsonObjectParse(
                        data=None, error="not a JSON object", text=text)
                return JsonObjectParse(data=data, text=text)
        start = text.find("{")
        if start < 0:
            return _failed(text, strict_exc)
        try:
            data, end = _LENIENT_DECODER.raw_decode(text, start)
        except json.JSONDecodeError as exc:
            return _failed(text, exc)
        if not isinstance(data, dict):
            return JsonObjectParse(data=None, error="not a JSON object")
        lenient_strings = True
        try:
            json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
        else:
            lenient_strings = False
        return JsonObjectParse(
            data=data, text=text, leading_chars=start,
            trailing_text=text[end:].strip(), lenient_strings=lenient_strings)
    if not isinstance(data, dict):
        return JsonObjectParse(data=None, error="not a JSON object", text=text)
    return JsonObjectParse(data=data, text=text)
