import json
import re
import unicodedata
from collections import Counter
from typing import Any
from ai.llm.base import strip_json_code_fence

_PERCENTAGE_RE = re.compile(r"\d+(?:\.\d+)?%")


def extract_reported_percentages(*texts: str | None) -> set[str]:
    percentages: set[str] = set()
    for text in texts:
        if text:
            percentages.update(_PERCENTAGE_RE.findall(text))
    return percentages


def final_answer_has_unverifiable_percentages(
        final_answer: str, trusted: set[str]) -> list[str]:
    return [
        pct for pct in _PERCENTAGE_RE.findall(final_answer)
        if pct not in trusted
    ]


def wrap_verification_warning(reason: str, final_answer: str) -> str:
    return f"[VERIFICATION WARNING: {reason}]\n\n{final_answer}"


_NON_LATIN_MIN_LETTERS = 5
_NON_LATIN_MIN_RATIO = 0.02
_NON_LATIN_MIN_RUN = 2
_IGNORED_LETTERS = frozenset({"\u00b5", "\u00aa", "\u00ba"})

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _script_of(letter: str) -> str:
    name = unicodedata.name(letter, "")
    return name.split(" ", 1)[0] if name else "UNNAMED"


def non_english_reason(text: str) -> str | None:
    if not text:
        return None
    letters = 0
    non_latin = 0
    scripts: Counter[str] = Counter()
    run: list[str] = []

    def _flush_run() -> None:
        nonlocal non_latin
        if len(run) >= _NON_LATIN_MIN_RUN:
            non_latin += len(run)
            for letter in run:
                scripts[_script_of(letter)] += 1
        run.clear()

    for ch in text:
        category = unicodedata.category(ch)
        if category.startswith("M"):
            continue
        if not category.startswith("L") or ch in _IGNORED_LETTERS:
            _flush_run()
            continue
        letters += 1
        if unicodedata.name(ch, "").startswith("LATIN"):
            _flush_run()
        else:
            run.append(ch)
    _flush_run()
    if letters == 0 or non_latin < _NON_LATIN_MIN_LETTERS:
        return None
    ratio = non_latin / letters
    if ratio < _NON_LATIN_MIN_RATIO:
        return None
    top_scripts = ", ".join(name for name, _ in scripts.most_common(3))
    return (
        f"non-English text: {non_latin} of {letters} letters "
        f"({ratio:.1%}) are non-Latin script ({top_scripts}); "
        "responses must be written in English")


def _collect_json_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_json_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_json_strings(item, out)


def non_english_reason_in_output(content: str) -> str | None:
    if not content:
        return None
    stripped = content.strip()
    is_one_fence = (
        stripped.startswith("```") and stripped.endswith("```")
        and stripped.count("```") == 2)
    candidate = strip_json_code_fence(stripped) if is_one_fence else stripped
    if candidate[:1] in ("{", "["):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            strings: list[str] = []
            _collect_json_strings(parsed, strings)
            return non_english_reason("\n".join(strings))
    decoded = _UNICODE_ESCAPE_RE.sub(
        lambda m: chr(int(m.group(1), 16)), content)
    return non_english_reason(decoded)
