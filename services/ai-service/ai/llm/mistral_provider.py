"""Mistral adapter (`mistralai` v1+ SDK). Closest of the 4 to
OpenAI's shape -- `role`/`content` messages, `choices[0].message`,
`usage.prompt_tokens`/`completion_tokens` -- so this file is the
shortest of the four.

**Key pooling, added 2026-08-06, extended to 5 slots 2026-08-14, then
to 7 slots 2026-08-24**:
the team's rate-limit problem is per-account (each of Mistral's real
RPS/TPM ceilings, see `ai/llm/router.py`'s comments, is scoped to one
account/key), and each real key here needs to be under its own
genuinely separate Mistral account to help at all -- a same-account
key adds zero extra throughput, since the limit it hits is the
account's, not that specific key string's. `_MistralKeyPool`
round-robins across whichever of `MISTRAL_API_KEY`/`_2` through `_7`
are actually configured (works with any subset -- see `config.py`'s
comment) and takes a key out of rotation for a cooldown window after
it 429s, rather than continuing to hammer an account that's already
rate-limited. This is throughput multiplication, not just redundancy:
round-robining a given call rate across N keys means each individual
account only ever sees roughly 1/N of total calls, so N accounts can
collectively sustain roughly N times the aggregate call rate before
any one of them hits its own per-account ceiling -- `ai/llm/
router.py::_min_interval_seconds` divides its pacing interval by
`mistral_key_count()` for exactly this reason, so the system's pacing
math scales automatically as more keys get configured, no separate
code change needed per key added.

A note on why this is safe to build this way but IS a real
compliance question the team should actually answer, not just assume:
these are 3 people's own individually-provisioned keys under their
own accounts (not fabricated/shared credentials), which is a
meaningfully different situation from ban-evasion tactics like
multi-accounting to farm one free tier repeatedly -- but Mistral's own
Terms of Service may or may not explicitly address pooling multiple
personal accounts' capacity for one shared application, and that's
not something to assume either way from first principles. Check
Mistral's current ToS/acceptable-use policy before relying on this in
anything beyond this school project.

**Untested with 2+ real keys as of 2026-08-06** -- built and verified
with fakes (round-robin selection, cooldown-skipping, all-cooling-down
fallback) since only `MISTRAL_API_KEY` has a real value so far; the
2-key and 3-key rotation paths should get a real run once a teammate's
key is actually pasted into `MISTRAL_API_KEY_2`/`_3`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from ai.llm.base import (
    LLMConfigError,
    LLMMessage,
    LLMProviderAdapter,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponse,
    LLMUsage,
)
from config import settings
from mistralai.client import Mistral
from mistralai.client.errors.sdkerror import SDKError
from mistralai.client.models import ResponseFormatTypedDict

logger = structlog.get_logger(__name__)

# How long a key sits out of rotation after it 429s. Mistral's error
# response doesn't reliably expose a Retry-After we could key this
# off of instead (SDKError's raw_response wasn't observed carrying
# one in real testing this session) -- 60s matches the top of
# ai/llm/base.py::RATE_LIMIT_BACKOFF_SCHEDULE, a real, already-
# calibrated number for "how long can a Mistral 429 realistically
# take to clear", rather than a fresh guess.
_COOLDOWN_SECONDS = 60.0


class _MistralKeyPool:
    """Round-robins calls across whichever of `MISTRAL_API_KEY`/`_2`/
    `_3` are actually configured. Not thread-safety-hardened beyond
    what a single asyncio event loop already gives call-order
    guarantees for -- `next_client()` reading and writing
    `_round_robin_index` between two `await`-free lines is atomic
    under asyncio's cooperative scheduling, no explicit lock needed.
    """

    def __init__(self) -> None:
        self._clients: dict[str, Mistral] = {}
        self._cooldown_until: dict[str, float] = {}
        self._round_robin_index = 0

    def _ensure_built(self) -> None:
        if self._clients:
            return
        for label, key in self._configured_keys():
            self._clients[label] = Mistral(api_key=key)
        if not self._clients:
            raise LLMConfigError("MISTRAL_API_KEY is not configured")

    @staticmethod
    def _configured_keys() -> list[tuple[str, str]]:
        candidates = [
            ("primary", settings.MISTRAL_API_KEY),
            ("key_2", settings.MISTRAL_API_KEY_2),
            ("key_3", settings.MISTRAL_API_KEY_3),
            ("key_4", settings.MISTRAL_API_KEY_4),
            ("key_5", settings.MISTRAL_API_KEY_5),
            ("key_6", settings.MISTRAL_API_KEY_6),
            ("key_7", settings.MISTRAL_API_KEY_7),
        ]
        return [(label, key) for label, key in candidates if key]

    def key_count(self) -> int:
        """Deliberately does NOT go through `_ensure_built()`/
        `self._clients` -- those cache actual `Mistral()` SDK client
        objects (expensive to construct, correctly reused across
        calls in production), but `key_count()` just needs a fresh
        count of however many keys are *currently* configured, same
        "read settings fresh every call" convention `ai/llm/
        router.py::_tier_model_map` already uses so tests can
        monkeypatch `config.settings` and see it reflected without a
        module reload. Coupling this to the cached client dict would
        make `mistral_key_count()` (called on every real request via
        `_min_interval_seconds`) silently freeze at whatever count was
        true the first time ANY call built the pool -- wrong in tests
        (settings get monkeypatched per test) and still not what's
        wanted in production either if a teammate's key ever gets
        added without a full restart.
        """
        return len(self._configured_keys())

    def next_client(self) -> tuple[str, Mistral]:
        """Round-robin among keys not currently cooling down. If
        every configured key is cooling down (all of them 429'd
        recently), falls through to the one whose cooldown expires
        soonest rather than raising -- still worth trying; the 429
        that put it in cooldown may already have cleared by the time
        this specific call reaches Mistral, and the alternative
        (refusing to even try) helps nobody.
        """
        self._ensure_built()
        labels = list(self._clients.keys())
        now = time.monotonic()

        available = [lbl for lbl in labels if self._cooldown_until.get(lbl, 0.0) <= now]
        if available:
            pool = available
        else:
            pool = [min(labels, key=lambda lbl: self._cooldown_until.get(lbl, 0.0))]

        chosen = pool[self._round_robin_index % len(pool)]
        self._round_robin_index += 1
        return chosen, self._clients[chosen]

    def next_client_excluding(
        self, excluded: set[str]
    ) -> tuple[str, Mistral] | None:
        """Real, deliberate addition 2026-08-15/16, per Jenny's own
        explicit direction: a real 402 ("Check your subscription")
        should immediately fail over to the NEXT real key in the pool
        within the SAME real call, not sit in a cooldown waiting to be
        retried later -- unlike a real 429, a real 402 reflects a real
        account/billing state that won't clear itself on any real
        short timescale, so there's real, immediate value in trying a
        different key right away rather than waiting at all.

        Real, confirmed bug found and fixed 2026-08-16, via a real,
        full pytest run (`tests/ai-service/test_embedding_pool.py`'s
        own real `test_a_429_on_one_key_moves_to_the_next_pooled_key`):
        this method's own earlier real version deliberately never
        touched `_cooldown_until` when SELECTING a candidate -- correct
        for the real 402 case this was built for, but `ai/rag/
        embedding.py`'s own real retry loop calls this SAME method for
        every real attempt, 429s included, since a 429 there still
        calls `mark_rate_limited` first. That earlier real version
        never consulted the real cooldown state it had just written,
        so a real key marked rate-limited a moment ago could still be
        re-selected on the very next real attempt -- a real,
        confirmed regression. Now excludes both the real explicit
        `excluded` set (for 402's own real "already tried this call"
        state) AND any real key still in `_cooldown_until` (for 429's
        own real cooldown, written by `mark_rate_limited`) -- real,
        unified selection logic that correctly serves both real
        callers.

        Falls back to the least-recently-excluded real cooling-down
        key (rather than returning `None`) if every real remaining key
        is in a real cooldown but NONE are in `excluded` -- mirrors
        `next_client`'s own real fallback logic above; still worth a
        real attempt over refusing outright, since a real cooldown may
        already have cleared server-side by the time this specific
        real call actually reaches Mistral.

        Returns `None` only once every real configured key is in the
        real `excluded` set itself (the caller's own real signal that
        it has now tried every real key at least once THIS call).
        """
        self._ensure_built()
        now = time.monotonic()
        candidates = [lbl for lbl in self._clients if lbl not in excluded]
        if not candidates:
            return None

        not_cooling_down = [
            lbl for lbl in candidates
            if self._cooldown_until.get(lbl, 0.0) <= now
        ]
        pool = not_cooling_down if not_cooling_down else candidates
        chosen = pool[self._round_robin_index % len(pool)]
        self._round_robin_index += 1
        return chosen, self._clients[chosen]

    def mark_rate_limited(
        self, label: str, cooldown_seconds: float = _COOLDOWN_SECONDS
    ) -> None:
        self._cooldown_until[label] = time.monotonic() + cooldown_seconds


_pool = _MistralKeyPool()

# Generous but genuinely bounded -- real completions (any tier) have
# always finished well under this in every real run this whole
# project has seen; this exists purely to bound how long a silently-
# hung connection (a dropped Wi-Fi mid-request, not a real 429 or any
# other actual error response) can block before the existing retry
# logic gets a chance to run at all. See this constant's use in
# `complete()` below for the full story. Public (not `_`-prefixed) --
# `ai/rag/embedding.py` imports and reuses this same value for its own
# embedding calls, rather than each maintaining its own separate
# guess at the same number. This is the real DEFAULT only, as of
# 2026-08-17 -- `complete()`'s own `timeout_seconds` parameter lets a
# specific real caller with an unusually large prompt (`ai/knowledge/
# filing_summary.py`) request a longer real wait without changing this
# default for every other, typically much smaller, real call site.
REQUEST_TIMEOUT_SECONDS = 60.0

# Real, deliberate local copy of `ai/knowledge/filing_summary.
# py::_CHARS_PER_TOKEN_ESTIMATE` -- same real, single-data-point
# derivation (see that constant's own docstring for the full "why"),
# duplicated here rather than imported so this provider-layer module
# doesn't take on a reverse dependency on a knowledge-layer module two
# layers above it in the real call graph (filing_summary.py calls INTO
# this file via the router, never the other way around). Only used for
# the real, client-side-only estimate logged on a real timeout above --
# re-derive both copies together if a future real batch run produces
# better (raw_chars, real_prompt_tokens) data to calibrate against.
_CHARS_PER_TOKEN_ESTIMATE = 5.14


def mistral_key_count() -> int:
    """Read by `ai/llm/router.py::_min_interval_seconds` so pacing
    scales automatically with however many keys are actually
    configured -- see this module's own docstring for why dividing
    the pacing interval by the key count is the right math.
    """
    return _pool.key_count()


def get_pooled_mistral_client() -> tuple[str, Mistral]:
    """Public accessor for `_pool.next_client()` -- added 2026-08-08
    so other Mistral-calling code outside this module (`ai/rag/
    embedding.py`, confirmed for real to have been maintaining its
    own entirely separate, single-key-only client, silently never
    benefiting from however many keys got configured here) can share
    the SAME pool instead of each call site growing its own copy of
    this same round-robin/cooldown logic. See `mark_pooled_client_
    rate_limited` below for the matching other half.
    """
    return _pool.next_client()


def mark_pooled_client_rate_limited(
    key_label: str, cooldown_seconds: float = _COOLDOWN_SECONDS
) -> None:
    """Public accessor for `_pool.mark_rate_limited()` -- see `get_
    pooled_mistral_client`'s own docstring for why this needs to be
    exposed alongside it. For real 429s only -- a real 402 should use
    `next_client_excluding_pooled` below instead (immediate failover,
    no cooldown; see that function's own docstring for why).
    """
    _pool.mark_rate_limited(key_label, cooldown_seconds)


def next_client_excluding_pooled(
    excluded: set[str],
) -> tuple[str, Mistral] | None:
    """Public accessor for `_pool.next_client_excluding()` -- see that
    method's own docstring for the full real reasoning (real,
    immediate 402 failover with no persistent cooldown state).
    """
    return _pool.next_client_excluding(excluded)


def _to_mistral_messages(messages: list[LLMMessage]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _extract_text_content(content: object) -> str:
    """`AssistantMessage.content` is typed as `str | list[<content
    chunk types>] | None` by the SDK -- richer than the plain-text
    completions this adapter actually asks for, but not something the
    SDK's types let us assume away. A plain str is the overwhelmingly
    common case; the list branch defensively extracts whatever text
    chunks are present rather than silently dropping non-str content.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            chunk.text for chunk in content
            if hasattr(chunk, "text") and isinstance(chunk.text, str)
        )
    return str(content)


class MistralAdapter(LLMProviderAdapter):
    name = "mistral"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        response_format: str | None = None,
        # Real, deliberate addition 2026-08-17, per Jenny's own direct
        # request -- see `ai/llm/base.py`'s own comment for the full
        # real "why". `None` (the default, used by every real call site
        # except `ai/knowledge/filing_summary.py`) preserves the exact
        # existing real behavior (`REQUEST_TIMEOUT_SECONDS`, 60s).
        timeout_seconds: float | None = None,
        # Accepted for interface compliance only (see `ai/llm/base.py`'s
        # own comment) -- deliberately not implemented/used here.
        # Mistral's own `response_format={"type": "json_object"}` below
        # already guarantees valid JSON at the API level (no observed
        # real failure mode this would address for this provider), and
        # per Jenny's standing instruction this file's own real,
        # working Mistral logic is not to be touched while Mistral
        # access is restricted -- only left able to keep compiling
        # against the shared 4-provider interface.
        response_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        # Real fix 2026-08-11: Mistral's own API supports
        # `response_format={"type": "json_object"}`, which guarantees
        # the response is valid JSON (confirmed via Mistral's own
        # docs) -- genuinely eliminates the "not valid JSON at all"
        # failure mode at the API level for this provider, rather
        # than relying on prompt instructions alone and catching the
        # failure after the fact.
        mistral_response_format: ResponseFormatTypedDict | None = (
            {"type": "json_object"} if response_format == "json_object" else None
        )
        real_timeout = (
            timeout_seconds if timeout_seconds is not None
            else REQUEST_TIMEOUT_SECONDS
        )

        # Real, deliberate restructure 2026-08-15/16, per Jenny's own
        # explicit direction: a real 402 immediately fails over to the
        # next real key in the SAME call rather than raising right
        # away -- see `next_client_excluding`'s own docstring for the
        # full real reasoning. Bounded by the real, current key count
        # so this can never loop forever even if every real key is
        # genuinely out of subscription.
        tried_labels: set[str] = set()
        last_exc: SDKError | None = None
        for _ in range(_pool.key_count()):
            selection = _pool.next_client_excluding(tried_labels)
            if selection is None:
                break
            key_label, client = selection
            tried_labels.add(key_label)
            try:
                # Real gap found 2026-08-08: a dropped/unstable network
                # connection (confirmed for real -- Jenny's own Wi-Fi
                # cutting out mid-run) can leave this call waiting
                # indefinitely with no response and no error at all --
                # neither a 429 nor any other exception, just silence,
                # since nothing here previously bounded how long to
                # wait. `asyncio.wait_for` turns that into a real,
                # bounded `TimeoutError` after `real_timeout` (this
                # call's own real timeout -- `REQUEST_TIMEOUT_SECONDS`
                # unless a caller like `ai/knowledge/filing_summary.py`
                # requested a longer one)
                # -- caught by its own dedicated `except TimeoutError`
                # branch just below (real fix 2026-08-17: originally
                # this fell through to the generic `except Exception`
                # branch, which produces a real, blank, uninformative
                # message for this specific exception type -- see that
                # branch's own comment for the full story), which
                # converts it into a retryable `LLMRequestError` that
                # `ai/llm/router.py::LLMRouter.complete`'s existing
                # retry loop already knows how to handle (it retries on
                # ANY `LLMRequestError`, not just rate-limit errors
                # specifically -- see that loop's own comment) -- no
                # retry-logic changes needed, only giving a hang an
                # actual endpoint to hang until.
                resp = await asyncio.wait_for(
                    client.chat.complete_async(
                        model=model,
                        messages=_to_mistral_messages(messages),  # type: ignore[arg-type]
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=mistral_response_format,
                    ),
                    timeout=real_timeout,
                )
            except TimeoutError as exc:
                # Real, confirmed logging gap found and fixed 2026-08-17
                # -- Python's built-in `TimeoutError` (what
                # `asyncio.wait_for` raises above on the real timeout
                # this except clause exists for) takes no real message
                # argument, so `str(exc)` is always an empty string.
                # Before this fix, a real timeout fell through to the
                # generic `except Exception` below, which builds its
                # message as f"...: {exc}" -- producing a real, logged
                # line ending in an empty, blank "(key=key_2): " with
                # zero indication of WHAT actually went wrong. Confirmed
                # the hard way: a real JPM 10-Q test run (`llm_router.
                # retrying`, 2026-08-17) showed exactly this blank
                # `error=""` field, indistinguishable in the log from a
                # dozen other possible real failure modes. This
                # dedicated branch runs BEFORE the generic one so a real
                # timeout gets its own real, specific, non-empty message
                # instead of silently falling through to the generic
                # (and, for this one specific exception type, useless)
                # `{exc}` interpolation.
                #
                # Real, deliberate addition 2026-08-17, per Jenny's own
                # direct question: a real TIMEOUT never gets a response
                # back from Mistral at all, so there is no real `usage`
                # object to report -- unlike `mistral.completion_
                # succeeded` below, which already logs the real
                # Mistral-reported `prompt_tokens`/`completion_tokens`
                # on every real success. What CAN be reported here is
                # the real SIZE of what was actually sent -- computed
                # client-side, before any real response, using the same
                # real chars-per-token estimate `ai/knowledge/filing_
                # summary.py::_CHARS_PER_TOKEN_ESTIMATE` was calibrated
                # against (see that constant's own docstring for the
                # real single-data-point derivation) -- explicitly
                # labeled as an ESTIMATE, not a real Mistral-reported
                # figure, so it's never confused with the real numbers
                # `mistral.completion_succeeded` logs on success.
                prompt_chars = sum(len(m.content) for m in messages)
                estimated_prompt_tokens = round(
                    prompt_chars / _CHARS_PER_TOKEN_ESTIMATE
                )
                logger.warning(
                    "mistral.request_timed_out",
                    model=model, key=key_label,
                    timeout_seconds=real_timeout,
                    prompt_chars=prompt_chars,
                    estimated_prompt_tokens=estimated_prompt_tokens,
                )
                raise LLMRequestError(
                    f"Mistral completion timed out after "
                    f"{real_timeout:.0f}s for model {model!r} "
                    f"(key={key_label}) -- no response received within "
                    f"the real, bounded wait; likely a real network "
                    f"stall or a genuinely slow real completion, not an "
                    f"error Mistral itself returned. Real request size: "
                    f"~{prompt_chars:,} chars sent, an ESTIMATED "
                    f"~{estimated_prompt_tokens:,} prompt tokens (client-"
                    f"side estimate only -- Mistral never returned real "
                    f"usage for a timed-out call)."
                ) from exc
            except SDKError as exc:
                last_exc = exc
                if exc.raw_response.status_code == 429:
                    _pool.mark_rate_limited(key_label)
                    raise LLMRateLimitError(
                        f"Mistral rate-limited model {model!r} (key={key_label})"
                    ) from exc
                if exc.raw_response.status_code == 402:
                    # Real, immediate failover -- no cooldown, no
                    # wait. Loop continues to the next real key.
                    logger.warning(
                        "mistral.subscription_error_failing_over",
                        model=model, key=key_label,
                        tried_count=len(tried_labels),
                    )
                    continue
                # Real, confirmed logging gap found and fixed
                # 2026-08-15 -- `SDKError`'s own real `__init__`
                # (mistralai>=1.9, `ai/llm/mistral_provider.py`'s own
                # pinned version) already builds a real, detailed
                # message including the actual response body (status
                # code, content-type, real error body text) -- but this
                # line was discarding all of that and replacing it with
                # a fixed, generic template, so every real caller that
                # logs `str(exc)` (e.g. `ai/knowledge/filing_summary.
                # py`'s own `filing_summary.llm_call_failed` warning)
                # only ever saw "Mistral completion failed for model
                # 'x'" with zero real detail about WHY. Confirmed the
                # hard way: a real batch run had ALL 5 of JPM's real
                # filings (by far the largest raw text of the 11 real
                # tickers checked, JPM's real 10-K alone over 1.6M real
                # characters) fail with a real 400 (not a 429 -- a
                # genuinely different, non-retryable failure mode) on
                # every real attempt, and the existing log gave no way
                # to tell whether that was a real context-length limit,
                # a real malformed-request issue, or something else --
                # `str(exc)` now flows through instead of being thrown
                # away, so the next time this happens, the real reason
                # is actually visible.
                raise LLMRequestError(
                    f"Mistral completion failed for model {model!r} "
                    f"(key={key_label}): {exc}"
                ) from exc
            except Exception as exc:
                raise LLMRequestError(
                    f"Mistral completion failed for model {model!r} "
                    f"(key={key_label}): {exc}"
                ) from exc
            else:
                choice = resp.choices[0]
                if choice.message is None:
                    raise LLMRequestError(
                        f"Mistral returned a completion with no message "
                        f"for model {model!r}"
                    )
                usage = LLMUsage(
                    prompt_tokens=(
                        resp.usage.prompt_tokens if resp.usage else None
                    ) or 0,
                    completion_tokens=(
                        resp.usage.completion_tokens if resp.usage else None
                    ) or 0,
                )
                # Real, deliberate addition 2026-08-15/16, per Jenny's
                # own explicit direction: previously ONLY a real
                # failure ever logged which real pooled key was
                # involved -- a real successful call left zero real
                # trace of which key actually served it, making it
                # genuinely impossible to see from logs alone why one
                # real key's own usage might be climbing much faster
                # than another's (exactly the real question Jenny had
                # about her own two real Mistral accounts' very
                # different real token totals).
                logger.info(
                    "mistral.completion_succeeded", model=model, key=key_label,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    finish_reason=choice.finish_reason,
                )
                return LLMResponse(
                    content=_extract_text_content(choice.message.content),
                    provider=self.name,
                    model=resp.model,
                    usage=usage,
                    finish_reason=choice.finish_reason,
                )

        # Real, confirmed bug found 2026-09-06, via a real `make
        # test-ai` run in an environment with zero Mistral keys
        # configured: `for _ in range(_pool.key_count())` above skips
        # its body entirely when `key_count()` is 0, so `tried_labels`
        # stays empty and `last_exc` stays `None` -- the loop never
        # actually attempts a single real call, yet execution falls
        # straight through to the line below, which used to
        # unconditionally claim "every real pooled key (0) returned
        # 402" and log a real, fabricated `": None"` exception tail.
        # `_ensure_built()` (called inside `next_client_excluding`)
        # already raises `LLMConfigError` for exactly this "zero keys
        # configured" case -- but never gets the chance to, since the
        # `for` loop's own body (where that call lives) never runs at
        # all when the range is empty. Confirmed real-world impact:
        # `ai/llm/router.py`'s retry loop only catches `LLMRequestError`
        # (not `LLMConfigError`, its sibling, see `ai/llm/base.py`), so
        # this generic-looking `LLMRequestError` was being retried 4
        # times with real exponential backoff, repeated for every
        # single one of ~15+ real LLM call sites across a full graph
        # run, turning an instant, obvious "no key configured" case
        # into a genuinely misleading near-2-hour test run before it
        # ever surfaced as a real (and, in that specific run,
        # completely unrelated) `InvalidUpdateError`.
        if not tried_labels:
            raise LLMConfigError(
                f"Mistral completion for model {model!r} failed: no "
                "Mistral API key is configured (MISTRAL_API_KEY / _2 "
                "through _7 are all unset or blank) -- zero keys were "
                "available to even attempt a real call. This is a "
                "configuration problem, not a rate limit or a real "
                "402 from Mistral -- no retry will help."
            )

        # Every real configured key 402'd on this call.
        raise LLMRequestError(
            f"Mistral completion failed for model {model!r}: every "
            f"real pooled key ({len(tried_labels)}) returned 402 "
            f"(check subscriptions at "
            f"https://admin.mistral.ai/subscription): {last_exc}"
        ) from last_exc

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        # Real, deliberate restructure 2026-08-15/16, matching
        # `complete` above -- but ONLY covers establishing the real
        # stream connection itself (before any real content has been
        # yielded). Once `async for event in event_stream` below
        # starts, a real failure is NOT retried with a different key
        # here -- a real partial stream can't be safely resumed or
        # restarted (this project's own established principle, see
        # `ai/llm/router.py`'s own "No retry on streaming" comment) --
        # so a real 402 mid-stream still just raises, same as before.
        tried_labels: set[str] = set()
        last_exc: SDKError | None = None
        event_stream = None
        for _ in range(_pool.key_count()):
            selection = _pool.next_client_excluding(tried_labels)
            if selection is None:
                break
            key_label, client = selection
            tried_labels.add(key_label)
            try:
                event_stream = await client.chat.stream_async(
                    model=model,
                    messages=_to_mistral_messages(messages),  # type: ignore[arg-type]
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                break
            except SDKError as exc:
                last_exc = exc
                if exc.raw_response.status_code == 429:
                    _pool.mark_rate_limited(key_label)
                    raise LLMRequestError(
                        f"Mistral streaming failed for model {model!r} "
                        f"(key={key_label}): {exc}"
                    ) from exc
                if exc.raw_response.status_code == 402:
                    logger.warning(
                        "mistral.subscription_error_failing_over",
                        model=model, key=key_label,
                        tried_count=len(tried_labels),
                    )
                    continue
                raise LLMRequestError(
                    f"Mistral streaming failed for model {model!r} "
                    f"(key={key_label}): {exc}"
                ) from exc
            except Exception as exc:
                raise LLMRequestError(
                    f"Mistral streaming failed for model {model!r} "
                    f"(key={key_label}): {exc}"
                ) from exc

        if event_stream is None:
            # Same real bug, same fix as `complete()` above (see that
            # method's own comment for the full incident) -- zero
            # configured keys means `tried_labels` stays empty here
            # too, and this branch would otherwise fabricate a "402"
            # that was never actually received.
            if not tried_labels:
                raise LLMConfigError(
                    f"Mistral streaming for model {model!r} failed: no "
                    "Mistral API key is configured (MISTRAL_API_KEY / "
                    "_2 through _7 are all unset or blank) -- zero "
                    "keys were available to even attempt a real call."
                )
            raise LLMRequestError(
                f"Mistral streaming failed for model {model!r}: every "
                f"real pooled key ({len(tried_labels)}) returned 402 "
                f"(check subscriptions at "
                f"https://admin.mistral.ai/subscription): {last_exc}"
            ) from last_exc

        # Same real per-call key-visibility fix as `complete` above --
        # see that method's own comment for the full real reasoning.
        # Logged once the real connection is established, not per
        # real chunk (that would be excessive), and real usage isn't
        # known yet for a real stream, unlike `complete`.
        logger.info("mistral.stream_started", model=model, key=key_label)

        try:
            async for event in event_stream:
                delta = event.data.choices[0].delta.content
                if delta:
                    yield delta
        except SDKError as exc:
            if exc.raw_response.status_code == 429:
                _pool.mark_rate_limited(key_label)
            raise LLMRequestError(
                f"Mistral streaming failed for model {model!r} "
                f"(key={key_label}): {exc}"
            ) from exc
        except Exception as exc:
            raise LLMRequestError(
                f"Mistral streaming failed for model {model!r} "
                f"(key={key_label}): {exc}"
            ) from exc