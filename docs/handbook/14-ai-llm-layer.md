# 14. The LLM layer (providers, router, keys, retries)

> Every AI step in AlphaMind (an agent's next move, a debate argument, the
> Judge's verdict) is a call to a large language model (LLM). The LLM
> layer in `services/ai-service/ai/llm/` turns "give me a cheap answer"
> or "give me the best answer" into a concrete model and a concrete API
> key, and it decides what to do when Google says "too many requests"
> (429), "overloaded" (503), or says nothing at all (timeout). The live
> provider is Google Gemini on free-tier quotas, which are small and
> shared. Most of the code in this chapter exists because those quotas
> and Google's peak-hour overloads made whole analyses fail in September
> 2026. This chapter explains the moving parts, then follows one call
> through all of them.

## 1. The big picture

```
 caller (agent loop, Bull, Bear, Judge, Reflection, RAG, ...)
     |  llm_router.complete(tier, messages, retries=..., ...)
     v
 +--------------------------- LLMRouter (ai/llm/router.py) ---------------------------+
 |  tier -> model           CHEAP  -> gemini-3.5-flash-lite  (fallback 3.1-flash-lite) |
 |                          HIGHEST_ACCURACY -> 3.8-flash -> 3.7 -> 3.6 -> 3.5-flash   |
 |  Lite concurrency cap (2 requests in flight)                                       |
 |  skip models known to be exhausted / overloaded / timed out                        |
 |  same-model retries with backoff (limited per error type)                          |
 |  walk the fallback chain, then one "rescue" or one cooldown retry                  |
 |  reject non-English output                                                         |
 +-------------------------------------------------------------------------------------+
     |  adapter.complete(model=...)
     v
 +-------------------- GoogleAdapter (ai/llm/google_provider.py) ----------------------+
 |  key pool: GOOGLE_API_KEY, GOOGLE_API_KEY_2 .. _10 (one Google Cloud project each)  |
 |  RequestPacer: reserve the earliest free (model, key) slot; Lite pool-wide gate     |
 |  per request: timeout, then classify the answer                                     |
 |     429 -> bench this key (65 s, or until Pacific midnight) and try another key      |
 |     503 -> HIGHEST: give up at once and mark the model; Lite: try 2 more keys        |
 |     timeout -> HIGHEST: skip this model for 300 s                                   |
 |  thinking config per model; usage, finish_reason, latency in the log               |
 +-------------------------------------------------------------------------------------+
     |
     v
  Google Generative Language API (google-genai SDK)
```

There are two layers on purpose. The **adapter** knows about keys and
HTTP answers for one model. The **router** knows about tiers, model
chains and which models to avoid right now. The adapter tries the keys of
one model; only when it gives up does the router move to another model.

## 2. Concepts you need

| Term | Meaning in this chapter |
|---|---|
| Tier | A quality/cost class a caller asks for: `CHEAP` or `HIGHEST_ACCURACY` (`ai/llm/base.py::LLMTier`). The router maps it to a model. |
| Lite model | Google's smaller "Flash-Lite" models. The code decides "Lite" by the substring `lite` in the model name (`google_is_lite_model`). Higher free quota, used by the nine data agents. |
| Flash chain | The non-Lite models for `HIGHEST_ACCURACY`, in quality order: 3.8, 3.7, 3.6, 3.5 Flash. Used by Bull, Bear, Judge, Reflection's review, the filing summary and records calls at ingestion, and position sizing. |
| Adapter | A class that speaks one vendor's API and returns a normalised `LLMResponse` (`content`, `provider`, `model`, `usage`, `finish_reason`, `rate_limited`). |
| Key pool | Up to ten Google API keys. Gemini's free limits count per Google Cloud **project**, so each key must come from a separate project to add capacity. |
| RPM / RPD / TPM | Requests per minute, requests per day, tokens per minute: the three free-tier limits. |
| 429 | HTTP "Too Many Requests": a quota on our side ran out (per minute or per day). |
| 503 | HTTP "Service Unavailable": Google's servers are overloaded for that model ("high demand"). Not our quota, but it still counts against the daily request limit. |
| Timeout | No answer within the request time limit; the adapter cancels the request with `asyncio.wait_for`. Google sends no status code. |
| Pacing | Spacing request starts so we never send faster than the quota allows. Proactive, unlike retries. |
| Cooldown | A period during which a key (or a whole model) is not used because it just failed. |
| Fallback chain | The ordered list of other models the router tries after the first one fails for availability reasons. |
| Backoff | Waiting before a retry, often longer each time. Jitter is a small random change of that wait so parallel callers do not retry in the same instant. |
| Thinking tokens | Hidden reasoning tokens some Gemini models spend before writing the answer. They come out of the same `max_output_tokens` budget. |
| `finish_reason` | Why the model stopped: `STOP` (normal), `MAX_TOKENS` (budget used up), `MALFORMED_RESPONSE`, and others. |
| JSON mode | Asking the model for a JSON object (`response_format="json_object"`, sent to Gemini as `response_mime_type="application/json"`, optionally with a JSON schema). |
| Code fence | Markdown wrapping like ```` ```json ... ``` ````. Gemini sometimes adds it even in JSON mode. |

## 3. How it works in AlphaMind

### 3.1 Provider adapters

`ai/llm/router.py::_DEFAULT_PROVIDERS` builds one adapter per vendor:

| Adapter | File | Status |
|---|---|---|
| `GoogleAdapter` | `ai/llm/google_provider.py` | **Live provider.** `LLM_DEFAULT_PROVIDER = "google"` (`config.py`). Key pool, pacing, 429/503/timeout handling, thinking config. Its key pool is also used for Gemini embeddings (section 3.14). |
| `MistralAdapter` | `ai/llm/mistral_provider.py` | Complete and selectable (`LLM_DEFAULT_PROVIDER=mistral`). Own pool of up to 7 keys (`MISTRAL_API_KEY`, `_2` .. `_7`) with round-robin and paced by the router. The owner keeps this file unchanged; it is the one file `make lint` excludes from flake8. |
| `OpenAIAdapter` | `ai/llm/openai_provider.py` | Simple single-key adapter (Chat Completions). Wraps every error as a plain `LLMRequestError`. Not used by default. |
| `AnthropicAdapter` | `ai/llm/anthropic_provider.py` | Simple single-key adapter. Not used by default. |

All adapters implement `LLMProviderAdapter` (`ai/llm/base.py`): a list of
`LLMMessage` (`system`/`user`/`assistant`) in, one `LLMResponse` out.
Gemini needs two conversions (`_split_system_and_contents`): system
messages go into a separate `system_instruction`, and the assistant role
is called `model`.

Why Google: the Mistral free plan gives no API access to the chat
models AlphaMind needs, and Mistral's embedding endpoint answered
"backend out of capacity" to about 75% of requests when it was measured
in September 2026. Gemini's free tier covers both chat and embeddings
(`ai/rag/embedding.py`).

### 3.2 Tiers and models

`ai/llm/router.py::_tier_model_map` reads the models from settings on
every call, and `_tier_fallback_model_map` builds the chains:

| Tier | First model (setting) | Fallback chain (settings, in order) |
|---|---|---|
| `CHEAP` | `gemini-3.5-flash-lite` (`LLM_GOOGLE_MODEL_CHEAP`) | `gemini-3.1-flash-lite` (`LLM_GOOGLE_MODEL_CHEAP_FALLBACK`) |
| `HIGHEST_ACCURACY` | `gemini-3.8-flash` (`LLM_GOOGLE_MODEL_HIGHEST_ACCURACY`) | `gemini-3.7-flash` (`..._FALLBACK`), `gemini-3.6-flash` (`..._FALLBACK_2`), `gemini-3.5-flash` (`..._FALLBACK_3`) |

An empty setting removes that step. The last model of a chain is called
the **terminal** model; it has nothing after it.

Who uses which tier (from the call sites):

- `CHEAP` (Lite): every data agent's ReAct loop (`ai/agents/loop.py`),
  Sentiment's label classifier, the news relevance classifier
  (`ai/providers/news_relevance.py`), the web-search relevance classifier
  (`ai/tools/search_relevance.py`), query expansion, knowledge-graph
  extraction, the RAG assistant (`RAG_ASK_LLM_TIER`), user-content
  sentiment, decision reviews, the monthly macro note
  (`ai/ingestion/macro_ingestion.py`), position sizing's second try, and
  the filing-records backfill (`scripts/backfill_filing_records.py`,
  tier `FILING_RECORDS_BACKFILL_LLM_TIER`, `cheap` by default, overridable
  with `--tier`).
- `HIGHEST_ACCURACY` (Flash chain): Bull and Bear arguments, the Judge,
  Reflection's citation review, position sizing's first try, and the two
  filing calls made at ingestion (`ai/ingestion/sec_edgar_filings.py`):
  the filing summary and the separate records-only call
  (`ai/knowledge/filing_summary.py`, chapter 18).

How much can we send? The quotas are not set by AlphaMind. They are
Google's published free-tier limits as shown on the Google AI Studio
rate-limit page on 2026-09-08, and Google can change them:

- the two Lite models: 15 RPM, 250,000 TPM, 500 RPD per project;
- the four Flash models: 5 RPM, 250,000 TPM, 20 RPD per project;
- Pro models: not available on this account.

The code never reads these numbers. It has its own pacing numbers,
derived from them with a safety margin (section 3.4): one request per
Lite model and key every 5.0 s (12 RPM), one request per Flash model and
key every 13.5 s (about 4.4 RPM), and at most one Lite request every
3.0 s across the whole pool.
There is no daily counter in code. A per-day 429 from Google is the only
signal that a daily quota is used up (section 3.7). Capacity check:
4 chain models x 10 projects x 20 RPD = 800 Flash
requests per day at most. With the 9 keys in use, each Flash model has
about 180 requests per day.

Input tokens are limited too: 250,000 per minute per key on the free
tier. Only the filing calls come near it. The records-only call cuts its
input to `RECORDS_MAX_CHARS` (900,000 characters) for this reason: the
quota check behaves like about 4 characters per token, and requests of
about 980,000 characters were rejected while 869,446 passed (chapter 18).

### 3.3 The key pool

`_GoogleKeyPool._configured_keys()` reads `GOOGLE_API_KEY` (label
`primary`) and `GOOGLE_API_KEY_2` .. `GOOGLE_API_KEY_10` (labels `key_2` ..
`key_10`) and keeps only the non-empty ones. Each label gets its own
`genai.Client`. If none is set, a call raises `LLMConfigError`.

There is no paid-key slot. Enabling billing turns a whole Google
project into a paid project, so a paid key would need its own wiring
(its own pacing numbers and quota handling).

The pool only says which keys exist. Which key a request uses is decided
by the pacer (next section), never by round-robin.

### 3.4 Request pacing

`ai/llm/base.py::RequestPacer` keeps one table: for each key (a tuple),
the monotonic time when the next request may start. The singleton
`request_pacer` is shared by every call in the process. The pacer never
sleeps itself; it returns a wait and the caller sleeps.

For each request attempt `GoogleAdapter.complete` and `stream` call
`_reserve_or_fail_fast`, which calls `_reserve_dispatch_slot(model,
candidates)` and so `reserve_best_slot_gated` under one lock:

1. Among the candidate timelines `("google", model, label)` for every key
   not yet tried in this call, pick the one that frees up earliest and
   reserve it: its next start moves forward by the model's interval.
   - Lite models: `LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS = 5.0` (12 RPM
     per project, about 20% under the real 15 RPM).
   - Flash models: `LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS =
     13.5` (about 4.4 RPM, under the real 5 RPM).
   - The interval is **not** divided by the number of keys: each project
     has its own timeline.
2. For Lite models only, also reserve a slot on the shared pool timeline
   `("google", "pool", "lite")`, spaced
   `LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS = 3.0` apart, across
   all Lite models and keys.
3. If the per-key wait would exceed one interval, or the pool wait would
   exceed `lite_pool_gate_max_wait_seconds()`, nothing is reserved
   (`GatedReservation.reserved` is false). `_reserve_or_fail_fast` logs
   `google.pacing_wait_exceeds_threshold_failing_fast` (per-key wait) or
   `google.pool_pacing_wait_exceeds_threshold_failing_fast` (pool wait),
   and the adapter stops trying keys.
   - The pool limit is `LLM_GOOGLE_LITE_POOL_MAX_WAIT_SECONDS` (30 s)
     only when the router's Lite cooldown retry (section 3.7) is off.
   - While that retry is on (the default), the limit is at least the
     retry's default wait, `GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS`
     (60 s).
   - `GoogleAdapter.stream` passes `delayed_retry_follows=False` and keeps
     30 s, because nothing retries a stream.
   - Reason: the pool wait is pure queue depth. A call that fails fast
     there cannot be served by the CHEAP fallback model (same gate, same
     wait), and the router's cooldown retry would then sleep about 60 s
     anyway. In one batch four calls failed fast at 30.4-30.9 s and then
     slept 60-63 s. Keeping the place in the queue is faster.

On top of pacing, the router lets at most
`LLM_GOOGLE_LITE_MAX_CONCURRENT_REQUESTS = 2` Lite requests be in flight
at once (`_google_lite_concurrency_semaphore`, `_concurrency_gate`). A
**semaphore** is a counter of free places: a caller takes one before it
goes on and gives it back afterwards; when none is free, it waits. The
gate is a `ConcurrencyGate` (`ai/llm/base.py`); `_concurrency_gate`
in `router.py` returns one with the Lite semaphore for Google CHEAP
calls and one without a semaphore (a no-op) for everything else.
`LLMRouter.complete` enters it for the whole call, but the gate holds one
of the two Lite slots only while a request is being sent. Whenever the
call only waits, it gives the slot back and queues for a slot again
before its next request:

- In the router, through `gate.released()`: the retry backoff, the delay
  before the terminal-server-error retry, the Lite cooldown retry's wait,
  and the router's own pacing wait.
- In the Google adapter, through `released_while_waiting()`: the per-key
  pacing sleep (`if effective_wait > 0`) in both `GoogleAdapter.complete`
  and `GoogleAdapter.stream`.

The adapter does not receive the gate as an argument. When the router
enters the gate, the gate stores itself in a **context variable**
(Python's `contextvars`: a value that belongs to the current async task
and the code it awaits). `released_while_waiting()` reads it from there.
When no gate is set (for example a call through `LLMRouter.stream`, which
does not use the gate), it does nothing. Reason: if the slot were held
for the whole call, one call sleeping up to about 73 s in the Lite
cooldown retry would block one of the two slots for every other agent. Flash calls are not capped
this way. Mistral is paced at the router instead
(`_min_interval_seconds`: 15.0 s CHEAP and 22.0 s HIGHEST, divided by the
number of Mistral keys).

Cooldowns use the same table: a 429 calls `mark_unavailable_until(key,
until)`, which pushes that key's next start into the future. One table
means one clock; an earlier design with a second cooldown dictionary let
two callers book the same key (section 5).

### 3.5 What the adapter does with each answer

`GoogleAdapter.complete()` loops over the pool (at most one attempt per
key per call). For each attempt it reserves a slot, sleeps the wait (with
the Lite slot given back, `released_while_waiting()`),
skips the key if it was benched during the wait
(`google.key_marked_unavailable_during_wait_skipping`; the test is the
key's mark count, `RequestPacer.mark_count`, which only
`mark_unavailable_until` changes, so other callers queuing on the same
key never cause a skip), sends the request
with a timeout, and classifies the result:

| Result | Action | Limit per call |
|---|---|---|
| Success | Log `google.completion_succeeded` with `model`, `key`, token counts, `thinking_tokens`, `finish_reason`, `latency_ms`. Reset the model's 503 streak, record "last success" time (Flash only), clear the 503 marker. | - |
| 429 | Classify per day or per minute (`_classify_quota_violation`: "perday" in the error text means day). Bench this key for this model: 65 s (`_MINUTE_COOLDOWN_SECONDS`) or until the next midnight in America/Los_Angeles plus 60 s (`_seconds_until_pacific_midnight`). Log `google.quota_exceeded_failing_over`. Try another key. | After the 3rd 429 (`_MAX_QUOTA_EXCEEDED_RETRIES = 2`), raise `LLMRateLimitError` (`google.quota_exceeded_giving_up_early`). |
| 503 / other 5xx | Log `google.server_error_failing_over`. No per-key cooldown (it is not a per-project problem). | Flash: give up at the first one (`_MAX_SERVER_ERROR_RETRIES = 0`); record the model's overload streak and marker (section 3.6). Lite: give up at the 3rd (`_MAX_SERVER_ERROR_RETRIES_LITE = 2`). Raise `LLMServerOverloadedError` (`google.server_error_giving_up_early`). |
| Timeout | Log `google.request_timed_out`. For a Flash model on its default timeout: mark the model for `LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS = 300.0` and raise `LLMRequestTimeoutError`. Otherwise (Lite, or a caller-supplied timeout) raise `LLMProviderTimeoutError`. | Immediately. |
| 400 / 404 | `LLMInvalidRequestError`: the request itself is wrong; never retried. | Immediately. |
| Other 4xx, or an unexpected exception | `LLMRequestError` (`google.request_failed_non_retryable` or `google.request_failed_unexpected_error`); no other key is tried. The router may still retry it. | Immediately. |
| `MALFORMED_RESPONSE` finish | Try another key (`google.malformed_response_retrying`). | After the 3rd, raise `LLMRequestError`. |
| Other finish reason than `STOP` | Logged as `google.non_stop_finish_reason` with the token counts; the response is still returned. | - |

If every key was tried or the pacer failed fast, the adapter logs
`google.pool_exhausted_for_model` and raises `LLMServerOverloadedError`
(when the last failure was a 503) or `LLMRateLimitError`. Every
`LLMRateLimitError` the adapter raises, from `complete` and from `stream`,
carries `requests_sent`: the number of requests this call really sent to
Google. 0 means "nothing was even sent, every key was cooling down". A
skip error that the router builds itself (a model skipped as exhausted,
overloaded or timed out, section 3.10) sent nothing either, but carries
`requests_sent=None` because no adapter call was made.

The exception family matters, because the router branches on it
(`ai/llm/base.py`):

```
LLMError
 +-- LLMConfigError                 no key / unknown provider (never retried)
 +-- LLMNonEnglishOutputError       answer not in English (never retried by the router)
 +-- LLMRequestError                generic request failure
      +-- LLMInvalidRequestError    400/404
      +-- LLMProviderTimeoutError   Lite or caller-supplied timeout
      +-- LLMRateLimitError         429 (quota)  -> triggers the fallback chain
           +-- LLMServerOverloadedError    503 give-up, or a skipped overloaded model
                +-- LLMRequestTimeoutError Flash timeout on the default limit
```

### 3.6 503 overload handling

A 503 means Google is overloaded for that model, for everyone. Trying
other keys rarely helps, and each 503 still uses one of the 20 daily
requests. So for Flash models:

1. The adapter gives up on the first 503 of a call.
2. It increments the model's **overload streak**
   (`("google", "server_error_streak", model)`) and sets the **confirmed
   overloaded marker** `("google", "server_error", model)` for an
   escalating time: `_server_error_cooldown_for_streak` gives
   `LLM_GOOGLE_SERVER_ERROR_COOLDOWN_SECONDS * 3^(streak-1)`, capped at
   `LLM_GOOGLE_SERVER_ERROR_COOLDOWN_MAX_SECONDS`: **20 s, 60 s, 180 s,
   then 300 s**.
3. While the marker is set, `google_model_confirmed_overloaded(model)` is
   true and the router skips that model (it moves straight to the next
   chain model).
4. One successful completion of that model resets the streak, clears
   the marker (`google.server_error_cooldown_cleared_on_success`) and,
   for a Flash model, stamps its last-success time. `complete` and a
   `stream` that ran to its end share this bookkeeping
   (`google_provider._record_success`).

Lite models never get the marker; their 503s use the extra keys and then
the Lite fallback model.

Two callers are special: the **Judge** and **Reflection's review** pass
`ignore_server_error_cooldown=True`. For them the marker is ignored, so
each chain model gets one real try, unless the model's streak is 2 or
more (`_IGNORE_COOLDOWN_HONOUR_MIN_STREAK = 2`). Why: Bull and Bear
run seconds before the Judge and often set markers on several chain
models; honouring those markers would make the Judge skip most of its
chain on the strength of one 503 each.

`LLM_GOOGLE_DEBATE_PRIMARY_EXTRA_503_KEYS` (default 0) can give Judge,
Bull and Bear extra keys on the first chain model after a 503. It is 0
because with 1 the extra key answered only about 23% of the time and
spent most of 3.8's daily quota.

### 3.7 429 handling: minute versus day

A 429 benches one key for one model, in the pacer table:

- **Per-minute quota**: 65 s, after which the key is usable again.
- **Per-day quota**: until midnight Pacific time plus 60 s. Gemini's
  daily quota resets at midnight in America/Los_Angeles (Google's
  rate-limit documentation). That is 03:00
  New York and usually 08:00 London.

When the classification is unsure it says "minute", because benching a
key until midnight for a 60-second problem costs much more than one
extra 429.

The pacer table lives in process memory. After a restart every key looks
fresh, so the first call on a day-exhausted (model, key) meets one more
429 before that key is benched until Pacific midnight. There is no
persisted quota table; one extra 429 per key after a restart is accepted. Capacity of one Flash
model is about 9 keys x 20 requests per day, and 503s and timeouts count
against it. A 15-ticker batch uses about 45 requests on 3.8, and a
records re-extraction of 32 filings on HIGHEST about 32 plus fallbacks.

The router reads the same table to decide whether a model is worth
trying at all. `google_model_pool_exhausted(model)` is true when the
earliest key frees up more than `_DAY_SCALE_WAIT_THRESHOLD_SECONDS` (65 +
120 = 185 s) from now, or when at least 70% of the keys
(`_POOL_EXHAUSTION_FRACTION_THRESHOLD = 0.7`) are benched that long.

For **Lite** 429s the router does not do short same-model retries
(section 3.9). After the Lite fallback model also fails, it makes **one
cooldown retry**: `_google_lite_cooldown_wait` returns the time until the
earliest key's cooldown ends (60 s if none is recorded) plus up to 3 s of
jitter. The retry happens only if all of these are true: the caller
allowed retries (`retries > 0`), the first error was a real 429 (not a
503), the wait is at most
`LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS = 70.0`, and the cooldown is
not a per-day one. Log events: `llm_router.lite_cooldown_retry`, then
`..._succeeded` or `..._failed`. Measured in the logs of 2026-09-24 to
2026-09-30: a Lite model answered again within 60 s of its pool running
out in 61% of cases.

### 3.8 Timeouts

The adapter picks the request timeout with `_default_request_timeout`
unless the caller passes `timeout_seconds` (an explicit caller timeout
always wins):

| Model | Timeout | Setting |
|---|---|---|
| Lite models | 60 s | `google_provider.REQUEST_TIMEOUT_SECONDS` |
| Flash models (non-terminal) | 30 s | `LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS` |
| `gemini-3.7-flash` (non-terminal) | 60 s | `LLM_GOOGLE_MODEL_REQUEST_TIMEOUT_SECONDS = {"gemini-3.7-flash": 60.0}` |
| The terminal chain model (`terminal_chain_model=True`, set by the router) and every rescue attempt | 90 s | `LLM_GOOGLE_HIGHEST_TERMINAL_REQUEST_TIMEOUT_SECONDS` |
| Filing summary and records-only calls | 300 s (caller-supplied) | `ai/knowledge/filing_summary.py::_FILING_SUMMARY_TIMEOUT_SECONDS` |

The measurements behind these numbers (logs of 2026-09-25 to
2026-10-01):

- Successful 3.8 and 3.6 calls have a p99 latency of about 15 s, so
  30 s is enough for Flash on the Judge/Bull/Bear critical path.
- 3.7 has a long tail (p50 5.3 s, p90 28.9 s, p95 45.9 s, max 56.2 s)
  and caused 27 of 50 timeouts; with a 60 s limit, 4 of its 25 requests
  that ran past 30 s still answered. So 3.7 gets 60 s.
- A 30 s timeout on the terminal model 3.5 has nothing behind it: losing
  it costs a debate argument or all position tiers. So the terminal
  model gets 90 s.

A Flash timeout on the default limit also marks the model as "timed out"
for 300 s (`google_model_recently_timed_out`). Every caller skips it during
that time, even the Judge; the last chain model is still always tried.
A Flash stream that does not start within its timeout sets the same
marker (`google_provider._record_timeout_cooldown`, shared by `complete`
and `stream`) and raises `LLMProviderTimeoutError`.

### 3.9 The router's retry policy

`LLMRouter.complete(tier, messages, retries=1, ...)` has a default of 1
retry, but every agent and debate call passes
`retries=AGENT_LLM_CALL_RETRIES = 4` (`ai/llm/base.py`).
`router_retry_limit(exc, ...)` then lowers it per error type:

| Error | Same-model retries |
|---|---|
| `LLMInvalidRequestError` (400/404) | 0 |
| Google Lite `LLMRateLimitError` (a 429, or a 503 give-up, because `LLMServerOverloadedError` is a subclass of it) | 0 (the fallback model and the cooldown retry take over) |
| Google Lite `LLMProviderTimeoutError` | at most 1 (`_GOOGLE_LITE_TIMEOUT_RETRIES`) |
| Everything else | the caller's `retries` |

The wait before a retry is `rate_limit_backoff_seconds(attempt)` for
rate-limit errors: the schedule `[1, 2, 4, 8, 60]` seconds
(`RATE_LIMIT_BACKOFF_SCHEDULE`) with +/-20% jitter; with 4 retries only 1,
2, 4 and 8 s are used. Other errors wait `0.5 s x (attempt + 1)`.

For Google with a fallback chain, a rate-limit failure **breaks out of the
retries early** (`llm_router.confirmed_exhausted_mid_retry_loop_skipping_remaining_attempts`)
when the adapter sent nothing, the pool is exhausted, the model has a
503 marker or a timeout marker, or (Judge/Reflection) the error was a
503. In practice a Flash 503 or timeout moves to the next model at once.
Measured in the logs of 2026-09-24 to 2026-09-30: of 184 retries after
a cooled-down pool 157 met the same cooldown again, and of 117 retries
after a Lite 503 only 1 succeeded.

### 3.10 The fallback chain and the HIGHEST rescue attempt

If the last error is in the `LLMRateLimitError` family (429, 503,
Flash timeout) and the tier has a chain, the router walks it. For each
chain model:

- Skip it (`llm_router.skipping_known_exhausted_model`, with
  `chain_step` and a `reason`) if its pool is day-exhausted, or, except
  for the last step, if it has a 503 marker or a timeout marker.
- The last step is skipped for day quota only if some earlier chain model
  still has quota (`any_chain_model_with_quota`).

`tier_model_chain(tier)` returns the primary model followed by its chain
for the default provider. The router and Reflection's retry gate
(chapter 16, section 3.10) use the same chain and the same
`any_chain_model_with_quota` check, so the gate never drops a retry that
the router could still serve with a fallback model.
- Otherwise call it once (`llm_router.falling_back_to_alternate_model`);
  the last step gets the 90 s terminal timeout.

If the whole chain fails, the router makes the **HIGHEST rescue attempt**
(`_highest_rescue`, Flash tier only): one more request on the chain model
that **answered most recently** in this process
(`_highest_rescue_candidate`, using `google_model_last_success_at`),
excluding the model that just failed, day-exhausted models and models in
their timeout cooldown. Models that never answered come last, in chain
order. 503 markers are ignored for this single request. Log events:
`llm_router.highest_rescue_attempt` (with `model`, `after_model`,
`last_success_seconds_ago`), then `..._succeeded`, `..._failed` or
`..._no_candidate`.

The Judge passes `terminal_server_error_retry_delay =
JUDGE_TERMINAL_503_RETRY_DELAY_SECONDS = 10.0`. That gives it a second
rescue attempt after a 10 s pause (`llm_router.terminal_server_error_retry`),
but only if the first rescue failed with a 503 (not a timeout).

Why "the model that answered most recently": during Google's peak-hour
503 periods, which model gets through changes from day to day and during
the day. Sending every extra attempt to the terminal model 3.5 instead
wears that model out: in a measured 15-ticker batch it received 97
requests (44 from extra keys, rescuing 12 calls) and was the first to
run out of its 20 daily requests, while 3.6 and 3.7 still had most of
theirs.

Above the router, the graph has its own slower retries for whole steps
(the Judge waits `JUDGE_DELAYED_RETRY_SECONDS = 120.0` and runs once more,
a failed debate side is re-run after `DEBATE_SIDE_REPAIR_DELAY_SECONDS =
60.0`, Reflection's review after `REFLECTION_REVIEW_DELAYED_RETRY_SECONDS
= 60.0`). Each of these waits first asks
`ai/llm/capacity.py::capacity_aware_delay`: when a model of the step's
chain is free now, the wait is skipped and the step retries at once;
otherwise the full wait runs. "Free" (`free_chain_model`, the same test as
`chain_capacity`, which the Reflection retry gate uses) means: the model's
pooled keys are not out of their daily quota, and the model has no 429
pool cooldown, no 503 cooldown and no request-timeout cooldown (the
router skips a model with any of these). A success clears a model's 503
cooldown, so a model that has just answered another call counts as free.
For a provider other than Google there is no capacity data and the full
wait always runs. Those steps are explained in chapter 16.

### 3.11 Thinking budgets and MAX_TOKENS

Gemini Flash models "think" by default, and the hidden thinking tokens
come from the same output budget. With default thinking,
gemini-3.8-flash can return an empty answer with
`finish_reason=MAX_TOKENS` because thinking used the whole budget. AlphaMind asks for short,
structured answers and has no use for hidden thinking, so
`_thinking_config_for(model)` sets:

| Model | Thinking config sent |
|---|---|
| `gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.5-flash` | `thinking_budget=0` (disabled) |
| `gemini-3.6-flash` | `thinking_level=MINIMAL` (it rejects `thinking_budget` with 400; with no config it thinks without limit) |
| `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` | `thinking_level=MINIMAL` (both reject `thinking_budget=0`; a positive budget is sent only by the runaway retry below) |
| Any other Lite model | nothing (the field is omitted) |

Output budgets (`max_tokens`; thinking tokens come out of the same
budget):

| Caller | `max_tokens` | Where |
|---|---|---|
| Router default (callers that pass nothing: Bull and Bear (`debate_common._write`), Sentiment's label classifier, query expansion, position sizing, decision reviews, the macro note) | 1024 | `LLMRouter.complete` |
| ReAct turn of a data agent | 4096 | `ai/agents/loop.py::_REACT_LOOP_MAX_TOKENS` |
| Judge | 3072 | `ai/agents/judge.py::_JUDGE_MAX_TOKENS` |
| Reflection's review | 2048 | `ai/agents/reflection.py::_REVIEW_MAX_TOKENS` |
| Knowledge-graph extraction | 2048 | `ai/knowledge/extraction.py::_EXTRACTION_MAX_TOKENS` |
| News relevance classifier | 4096, one retry at 8192 | `ai/providers/news_relevance.py::_VERIFY_MAX_TOKENS`, `_RETRY_MAX_TOKENS` |
| Web-search relevance classifier | 4096, one retry at 8192 | same helpers, `verify_with_cheap_tier` and `parsed_with_retry` |
| Filing summary | 12288, one retry at 16384 | `ai/knowledge/filing_summary.py::_SUMMARY_MAX_TOKENS`, `_RETRY_MAX_TOKENS` |
| Filing records-only call | 10240, one retry at 16384 | `filing_summary.py::_RECORDS_MAX_TOKENS`, `_RETRY_MAX_TOKENS` |
| RAG assistant | `RAG_ASK_MAX_OUTPUT_TOKENS` (1024) | `domains/rag/qa.py` |
| User-content sentiment | 128 | `ai/user_sentiment.py` |

Why some callers have more than 1024:

- The Judge, Reflection and knowledge-graph extraction return larger JSON
  objects. At 1024 tokens the JSON could be cut off.
- The two relevance classifiers share `verify_with_cheap_tier`, which
  calls `parsed_with_retry`. When the
  first answer is not valid JSON and reading it entry by entry still
  leaves some item without a verdict, the batch is asked once more with
  8192 tokens (chapter 17).
- A filing summary or a records block can be long. A filing call that
  stops at its limit (`finish_reason` MAX_TOKENS) is retried once with
  16384 tokens (`filing_summary.output_limit_retry`). A summary that is
  still cut off logs `filing_summary.output_truncated`. A review found 5
  of 80 stored summaries ending mid-sentence before this check existed.
- The ReAct cap was once raised to 8192. A Lite runaway thought then ran
  to about 7,860 tokens and the call took 22-44 s instead of 10-14 s plus
  one quick retry, so the cap went back to 4096 and the runaway retry
  below handles those calls.

`thinking_tokens` is logged on every success, so a `MAX_TOKENS` case can
be diagnosed from the log.

When a ReAct turn still ends in `MAX_TOKENS` and the JSON cannot be
parsed, the loop adds a correction turn that says the answer was cut off
and asks for a one-sentence `thought` (chapter 15).

**Lite runaway retry.** A few Lite calls per batch ignore
`thinking_level=MINIMAL` and think until the output cap (seen most often
on Sentiment's first turn). `GoogleAdapter.complete` checks every answer
with `_is_runaway_thinking`. A call is a runaway when all of these are
true:

- the model is a Lite model and not in `_THINKING_BUDGET_REJECTED_MODELS`;
- `0 < LLM_GOOGLE_LITE_RUNAWAY_RETRY_THINKING_BUDGET < max_tokens`;
- the finish reason contains `MAX_TOKENS`;
- `thinking_tokens` is at least `_RUNAWAY_THINKING_SHARE` (0.5) of
  `max_tokens`.

Such a call is sent once more with
`thinking_budget=LLM_GOOGLE_LITE_RUNAWAY_RETRY_THINKING_BUDGET` (1024)
instead of `thinking_level=MINIMAL`. A thinking budget is the only field
that caps thinking, and the two fields cannot be sent together. Setting
the budget to 0 turns the retry off. Logs:

- `google.thinking_runaway_retrying_with_budget` (warning) before the
  retry;
- `google.thinking_runaway_retry_done`: the retry's answer is returned,
  even if it is cut off again (the ReAct loop's correction turn handles
  that);
- `google.thinking_runaway_retry_failed`: any other LLM error; the first
  answer is returned;
- `google.thinking_budget_rejected_disabling_runaway_retry`: a 400/404,
  so the model does not accept the field. The first answer is returned
  and that model never gets the retry again in this process. The Lite
  models reject `thinking_budget=0`; a positive budget is a different
  request, so it is tried until Google rejects it.

The retry runs inside the adapter, so the router does not see it: it is
not a router retry and does not move to a fallback model. In a live
check, 3 of 3 retries answered in 1.6-2.9 s.

### 3.12 JSON answers and code fences

`ai/llm/json_fence.py` has the shared helpers. `strip_json_code_fence(raw)`
returns the content of the first ```` ```json ```` (or bare ```` ``` ````)
fence, case-insensitive, or the trimmed text. Every LLM-JSON parser calls
it first, because Gemini sometimes wraps even a JSON-mode answer such as
`{"issues": []}` in a fence.
`parse_json_list` accepts either a bare list or an object with a
`relevant`, `results` or `items` list.

**Schemas.** A caller can pass `response_schema` together with
`response_format="json_object"`; the adapter sends it as
`response_json_schema`. Today only the ReAct loop does this. It builds
one schema per agent with `_build_react_step_schema`
(`ai/agents/loop.py`):

- `action_input` must match one of the agent's tool parameter schemas
  (an `anyOf` of every tool's `parameters`, plus a `null` arm for the
  finish turn);
- `reported_metrics` has the agent's own shape.

Without it Gemini decoded an open `{"type": "object"}` as `{}`, and the
tool got no arguments. A tool without a `parameters` schema falls back
to an open object, so one missing schema does not break the whole agent.
The router passes the schema to the tier's first model only (the
primary attempts and the Lite cooldown retry). Fallback chain models and
the HIGHEST rescue attempt get JSON mode (`response_format`) without the
schema,
because a schema this specific was checked only with the first model.

### 3.13 Non-English output

Every successful completion passes `_reject_non_english` in the router:
each request (primary attempt, fallback step, HIGHEST rescue, Lite
cooldown retry) goes through one inner helper, `_call`, which calls the
adapter, sets `rate_limited` when the call is a recovery, and then runs
the check.
It runs `non_english_reason_in_output` (`ai/agents/evidence_grounding.py`),
which decodes JSON string values first and rejects text where at least 5
letters (and at least 2% of letters) are non-Latin script in runs of 2 or
more. The router raises `LLMNonEnglishOutputError`
(`llm_router.non_english_output_rejected`), which is deliberately not a
request error: it is not retried inside the router, and each caller
decides (the ReAct loop spends a correction turn asking for English).

### 3.14 The embeddings pool

Embeddings (`ai/rag/embedding.py`, `EMBEDDING_PROVIDER = "google"`) reuse
the same Google keys through `google_pool_labels()` and
`google_client_for(label)`, but not the completion pacer:

- Model `gemini-embedding-2`, 1536 dimensions, every vector L2-normalised.
- Batches of at most `_GOOGLE_BATCH_MAX_INPUTS = 20` texts and about
  `_GOOGLE_BATCH_TOKEN_BUDGET = 15_000` estimated tokens (1.7 tokens per
  word). The reason is Google's free-tier embedding limit: 100 RPM, 30K
  TPM and 1K RPD per project as shown in AI Studio on 2026-09-25, and
  Google counts every input text, not every request.
  These are Google's numbers, not settings. The embedding code has no
  pacer of its own: the batch caps keep one request well under the
  per-minute token limit, and 429s are handled by rotating keys.
- Keys rotate from a moving start (`_google_key_cursor`). A 429 moves to
  the next key; when every key answered 429 in a round, it waits 5, 10,
  20, then 40 s (`_GOOGLE_RETRY_WAITS_SECONDS`) before trying the keys
  again. A 5xx waits the same schedule and moves to the next key.
- Each request has a limit of `EMBEDDING_REQUEST_TIMEOUT_SECONDS` (30 s).
  A request with no answer in time is retried like a server error
  (`embedding.google_timeout_retrying`). When the waits are used up it
  raises `TimeoutError`, which ingestion treats as transient, so the
  durable event is delivered again later.
- Log events: `embedding.google_succeeded`,
  `embedding.google_rate_limited_rotating`,
  `embedding.google_pool_rate_limited_waiting`,
  `embedding.google_server_error_retrying`,
  `embedding.google_timeout_retrying`.

### 3.15 One call, step by step

The diagram follows one `LLMRouter.complete()` call. Bull's argument is a
good example: tier `HIGHEST_ACCURACY`, `retries=4`.

```
complete(tier, messages, retries=4)
 |
 |-- resolve: model = 3.8-flash, chain = [3.7, 3.6, 3.5]
 |-- Lite tier? -> wait for one of 2 Lite slots (not for Flash);
 |                 the slot is given back during every sleep below (router
 |                 and adapter) and taken again before the next request
 |
 |-- skip the first model?  (only when a chain exists)
 |     pool day-exhausted?            -> skip (as a 429)
 |     503 marker set? (Judge/Reflection: only if streak >= 2) -> skip (as a 503)
 |     timeout marker set?            -> skip (as a 503)
 |
 |-- else: attempt loop on 3.8 (up to retries+1)
 |     GoogleAdapter.complete(3.8)
 |       for each key, at most once:
 |         reserve earliest (3.8, key) slot, 13.5 s apart   [Lite: + pool gate 3 s]
 |         wait > one interval?          -> fail fast, stop
 |         sleep, recheck key not benched meanwhile
 |         send, timeout 30 s (3.7: 60 s, terminal: 90 s)
 |           OK        -> reset streak, stamp success, clear marker -> RETURN
 |           429       -> bench key 65 s / until Pacific midnight; 3rd 429 -> raise RateLimit
 |           503       -> streak+1, marker 20/60/180/300 s -> raise Overloaded
 |           timeout   -> model timeout marker 300 s -> raise RequestTimeout
 |           400/404   -> raise InvalidRequest (no retry anywhere)
 |       all keys used / fast fail -> raise RateLimit (or Overloaded)
 |     answer non-English? -> raise NonEnglish (caller decides)
 |     error:
 |       retry limit 0 (400/404; Lite 429/503) -> stop
 |       Google + chain and (sent nothing | exhausted | marker | timed out) -> stop
 |       else sleep 1/2/4/8 s (+-20%) and retry 3.8
 |
 |-- last error is 429/503/timeout family and a chain exists?
 |     for 3.7, 3.6, 3.5:
 |        skip if day-exhausted, or (not last) marker / timed out
 |        call once (3.5 is terminal: 90 s)        -> success -> RETURN (rate_limited=True)
 |     HIGHEST rescue: 1 request on the chain model that answered most recently
 |        (Judge: a 2nd one after 10 s if the 1st met a 503)  -> success -> RETURN
 |     Lite tier and the first error was a 429?
 |        wait for the earliest key cooldown (<= 70 s, not a day quota), try once -> RETURN
 |
 '-- raise the last error  -> the caller's own policy (agent fails, Judge delayed retry, ...)
```

## 4. Configuration

All in `services/ai-service/config.py` unless stated otherwise.

| Setting | Default | Meaning |
|---|---|---|
| `LLM_DEFAULT_PROVIDER` | `google` | Provider for calls that name only a tier. |
| `GOOGLE_API_KEY`, `GOOGLE_API_KEY_2` .. `_10` | empty | The key pool, one Google Cloud project each. |
| `LLM_GOOGLE_MODEL_CHEAP` | `gemini-3.5-flash-lite` | Lite model. |
| `LLM_GOOGLE_MODEL_CHEAP_FALLBACK` | `gemini-3.1-flash-lite` | CHEAP fallback. |
| `LLM_GOOGLE_MODEL_HIGHEST_ACCURACY` | `gemini-3.8-flash` | First Flash model. |
| `LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK` / `_2` / `_3` | `gemini-3.7-flash` / `gemini-3.6-flash` / `gemini-3.5-flash` | Flash chain. |
| `LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS` | 5.0 | Spacing per (Lite model, key). |
| `LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS` | 13.5 | Spacing per (Flash model, key). |
| `LLM_GOOGLE_LITE_MAX_CONCURRENT_REQUESTS` | 2 | Lite requests in flight at once (a slot is not held while the call waits between attempts). |
| `LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS` | 3.0 | Spacing of all Lite dispatches, pool-wide. |
| `LLM_GOOGLE_LITE_POOL_MAX_WAIT_SECONDS` | 30.0 | Fail fast above this pool wait when the Lite cooldown retry is off (and always for `stream()`). With the retry on (default) the effective limit is 60 s (section 3.4). |
| `LLM_GOOGLE_LITE_RUNAWAY_RETRY_THINKING_BUDGET` | 1024 | Thinking budget of the Lite runaway retry; 0 turns the retry off (section 3.11). |
| `LLM_GOOGLE_SERVER_ERROR_COOLDOWN_SECONDS` | 20.0 | First 503 marker duration (then x3). |
| `LLM_GOOGLE_SERVER_ERROR_COOLDOWN_MAX_SECONDS` | 300.0 | Cap of the escalating marker. |
| `LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS` | 30.0 | Flash default timeout. |
| `LLM_GOOGLE_MODEL_REQUEST_TIMEOUT_SECONDS` | `{"gemini-3.7-flash": 60.0}` | Per-model override (non-terminal). |
| `LLM_GOOGLE_HIGHEST_TERMINAL_REQUEST_TIMEOUT_SECONDS` | 90.0 | Terminal model and rescue timeout. |
| `LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS` | 300.0 | Skip a Flash model this long after a timeout (0 = off). |
| `LLM_GOOGLE_DEBATE_PRIMARY_EXTRA_503_KEYS` | 0 | Extra keys for Judge/Bull/Bear on the first model after a 503. |
| `LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS` | 70.0 | Longest wait for the Lite cooldown retry (0 = off). |
| `JUDGE_TERMINAL_503_RETRY_DELAY_SECONDS` | 10.0 | Pause before the Judge's second rescue attempt. |
| `LLM_MISTRAL_*`, `MISTRAL_API_KEY*` | see `config.py` | Mistral models, fallbacks, pacing (15.0 / 22.0 s), 7-key pool. |
| `LLM_OPENAI_*`, `LLM_ANTHROPIC_*`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | see `config.py` | Dormant providers. |
| `EMBEDDING_PROVIDER`, `GOOGLE_EMBEDDING_MODEL`, `GOOGLE_EMBEDDING_DIMENSIONS` | `google`, `gemini-embedding-2`, 1536 | Embeddings. |
| `EMBEDDING_REQUEST_TIMEOUT_SECONDS` | 30.0 | Limit of one embedding request (section 3.14). |
| `RAG_ASK_LLM_TIER`, `RAG_ASK_MAX_OUTPUT_TOKENS` | `cheap`, 1024 | Tier and output budget of the RAG assistant. |
| `FILING_RECORDS_BACKFILL_LLM_TIER` | `cheap` | Tier of the filing-records backfill script (chapter 18). |

Constants in code (not settings):

| Constant | Value | File |
|---|---|---|
| `REQUEST_TIMEOUT_SECONDS` | 60.0 | `ai/llm/google_provider.py` (Lite timeout) |
| `_MINUTE_COOLDOWN_SECONDS` | 65.0 | `google_provider.py` |
| `_DAY_COOLDOWN_MARGIN_SECONDS` | 60.0 | `google_provider.py` |
| `_MAX_QUOTA_EXCEEDED_RETRIES` | 2 | `google_provider.py` |
| `_MAX_SERVER_ERROR_RETRIES` / `_LITE` | 0 / 2 | `google_provider.py` |
| `_MAX_MALFORMED_RETRIES` | 2 | `google_provider.py` |
| `_POOL_EXHAUSTION_FRACTION_THRESHOLD` | 0.7 | `google_provider.py` |
| `RATE_LIMIT_BACKOFF_SCHEDULE` | `[1.0, 2.0, 4.0, 8.0, 60.0]`, +/-20% jitter | `ai/llm/base.py` |
| `AGENT_LLM_CALL_RETRIES` | 4 | `ai/llm/base.py` |
| `_RETRY_BACKOFF_SECONDS` | 0.5 | `ai/llm/router.py` |
| `_IGNORE_COOLDOWN_HONOUR_MIN_STREAK` | 2 | `router.py` |
| `_GOOGLE_LITE_TIMEOUT_RETRIES` | 1 | `router.py` |
| `GOOGLE_LITE_COOLDOWN_DEFAULT_WAIT_SECONDS` | 60.0 | `google_provider.py` (Lite cooldown retry wait when no cooldown is recorded) |
| `_RUNAWAY_THINKING_SHARE` | 0.5 | `google_provider.py` |
| `_REACT_LOOP_MAX_TOKENS` | 4096 | `ai/agents/loop.py` |
| `_JUDGE_MAX_TOKENS` | 3072 | `ai/agents/judge.py` |
| `_REVIEW_MAX_TOKENS` | 2048 | `ai/agents/reflection.py` |
| `_EXTRACTION_MAX_TOKENS` | 2048 | `ai/knowledge/extraction.py` |
| `_VERIFY_MAX_TOKENS` / `_RETRY_MAX_TOKENS` | 4096 / 8192 | `ai/providers/news_relevance.py` |
| `_SUMMARY_MAX_TOKENS` / `_RECORDS_MAX_TOKENS` / `_RETRY_MAX_TOKENS` | 12288 / 10240 / 16384 | `ai/knowledge/filing_summary.py` |
| `_FILING_SUMMARY_TIMEOUT_SECONDS` | 300.0 | `ai/knowledge/filing_summary.py` |

## 5. Design decisions and why

**Key selection by reservation, not round-robin.**
Alternative: round-robin over keys that "look free". Reason: several
agents read the same "free" snapshot and pile onto one key, which
produces sustained 429s on every project of the pool for minutes, and
more keys make it worse. The pacer picks and reserves a slot in one
locked step, so two callers never get the same (key, slot).

**One clock for pacing and cooldowns.**
Alternative: a separate cooldown dictionary. Reason: two clocks
disagree and double-book keys, which shows up as hundreds of real 429s
in a few minutes. Cooldowns live in the pacer table, keyed by
`(google, model, key)`, which also matches Google's per-model counters.

**Per-project intervals, not divided by pool size.**
Alternative: one shared timeline with the interval divided by the number
of keys. Reason: dividing lets more concurrent starts through with no
guarantee which project they land on; adding keys makes storms worse.

**A pool-wide Lite gate and a concurrency cap of 2.**
Reason: 429s fire while each key's own per-minute count is far below
the limit; the aggregate burst across the pool (up to 21 dispatches in
10 s were measured) is the trigger. Also, a 429
cooldown is recorded only after the HTTP round trip, so concurrent
callers could still book the same key in that window; fewer in-flight
calls means fewer such collisions. The cap counts requests in flight,
not calls that are only waiting: a call that sleeps between attempts
gives its slot back, so a long cooldown wait does not starve the other
agents.

**Flash 503: give up at once and mark the model.**
Alternative: try other keys, like a 429. Reason: a 503 is about the
model, not the project, and each one still counts against the 20 daily
requests (Google staff confirmed this on Google's developer forum).
During a model outage nearly every request answers 503 (67 of 79 and
42 of 42 were measured on 3.8-flash in two runs); without a marker each
step (Bull, Bear, Judge, retries, Reflection) rediscovers the outage
from zero, which costs 8 to 11 minutes per ticker. The escalating marker
lets the next calls skip the model for a while.

**Lite models keep their 503 retries.**
Reason: with zero retries a single Lite 503 fails the whole agent. Lite quota (500 per day per project) can afford the
extra keys.

**No short same-model retries for Lite 429s; one retry when the
cooldown ends.**
Reason: the log measurement in section 3.9 shows that short waits
almost never succeed while a minute cooldown is still running.

**Rescue on the model that answered most recently, not on the terminal
model.**
Reason: the terminal model absorbed every failure and ran out of daily
quota first (section 3.10).

**Thinking disabled or minimal.**
Reason: hidden thinking silently used up the output budget (empty
answers with `MAX_TOKENS`); the agents need short structured output.

**A real outage is not a code bug.**
In September 2026 some batches had close to 100% 503s. The project's
lesson was to check Google's developer community for concurrent reports
before changing the retry code: a provider-side "high demand" period
cannot be fixed by client-side retries, only survived.

## 6. When things go wrong

| Symptom | Likely cause | What to check |
|---|---|---|
| Many `google.quota_exceeded_failing_over` with `quota_window: "minute"` | Burst above per-minute limits. | Pacing settings; `pool_size` in the event; whether two batches ran together. |
| `quota_window: "day"`, then `llm_router.skipping_known_exhausted_model` | A model's daily quota is used up on most keys. | Count today's requests per model; quota returns at midnight Pacific (03:00 New York, usually 08:00 London). |
| Many `google.server_error_failing_over` / `server_error_giving_up_early` with rising `overload_streak` | Google-side overload for that model. | Is it all models or one? Check Google's status/community before changing code. |
| `google.request_timed_out` then `google.model_timeout_cooldown_recorded` | A Flash model is slow; it is skipped for 300 s. | `latency_ms` distribution of recent successes (demo step 3); `make probe`. |
| `llm_router.highest_rescue_no_candidate` | Every chain model failed, is day-exhausted or timed out. | The analysis may end with `judge_unavailable` (chapter 16). |
| `google.non_stop_finish_reason` with `MAX_TOKENS` | Output budget too small, or thinking used it. | `thinking_tokens` in the event; the model's thinking config. |
| `llm_router.non_english_output_rejected` | The model answered in another script. | The caller's correction or fallback; repeated cases go to the agent's failure. |
| `LLMConfigError` "GOOGLE_API_KEY is not configured" | No key in the environment. | `.env.docker` values (do not print them); container restart after changes. |
| `google.pool_pacing_wait_exceeds_threshold_failing_fast` | The Lite pool queue is longer than the gate limit (60 s by default, 30 s for streams). | How many Lite calls run at once; whether two batches overlap. |
| `google.thinking_runaway_retrying_with_budget` | A Lite call thought until its output cap. | The following `_retry_done` / `_retry_failed`; a `thinking_budget_rejected_disabling_runaway_retry` means the model refused the budget and no longer gets the retry until restart. |
| `filing_summary.output_limit_retry` / `filing_summary.output_truncated` | A filing call reached its output budget; the retry at 16384 also stopped. | Chapter 18 (filing summaries). |
| Day-quota 429s right after a restart | The cooldown table is in memory, so each day-exhausted key is tried once more. | Expected; each key is benched again after one 429. |
| Metric `alphamind_llm_errors_total{kind="server_overload"}` rising | Same as 503 storms. | Grafana panel (chapter 27). |

Useful log searches (today's file, New York date):

```bash
bash scripts/logs.sh ai-service grep google.server_error_giving_up_early
bash scripts/logs.sh ai-service grep llm_router.skipping_known_exhausted_model
bash scripts/logs.sh ai-service grep '"quota_window": "day"'
bash scripts/logs.sh ai-service grep llm_router.highest_rescue_attempt
```

## 7. Demo walkthrough

Goal: show how fast each Gemini model answers right now, and show the
resilience events in a real log. Steps 1-3 are read-only. Step 4 sends
real requests and spends daily quota.

1. **Show the configured chain (read-only).**
   ```bash
   docker compose --env-file .env.docker exec -T ai-service python -c "from config import settings as s; print(s.LLM_GOOGLE_MODEL_CHEAP, s.LLM_GOOGLE_MODEL_CHEAP_FALLBACK); print(s.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY, s.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK, s.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK_2, s.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK_3)"
   ```
   Say: "CHEAP is the Lite pair; HIGHEST is a four-step Flash chain in
   quality order."

2. **Count successful calls per model today (read-only).**
   ```bash
   bash scripts/logs.sh ai-service grep google.completion_succeeded | grep -o '"model": "[^"]*"' | sort | uniq -c
   ```
   Say: "Each line is one successful Gemini request. Lite models carry
   most of the volume."

3. **Latency per model from the log (read-only).**
   ```bash
   bash scripts/logs.sh ai-service grep google.completion_succeeded | python3 -c '
   import sys, json, statistics, collections
   lat = collections.defaultdict(list)
   for line in sys.stdin:
       d = json.loads(line[line.index("{"):])
       lat[d["model"]].append(d["latency_ms"] / 1000)
   for m, v in sorted(lat.items()):
       v.sort()
       print(f"{m:24} n={len(v):4} p50={statistics.median(v):5.1f}s max={v[-1]:5.1f}s")
   '
   ```
   Each line of the daily log file is `<New York timestamp> stdout
   <JSON>`, so the script cuts at the first `{`. Say: "This is why the
   Flash timeout is 30 s but 3.7 gets 60 s."

4. **Run the latency probe (spends quota).**
   ```bash
   make probe                                   # gemini-3.7-flash, 2 requests per key
   make probe MODEL=gemini-3.8-flash ARGS="--per-key 1 --cap 90"
   ```
   The probe (`services/ai-service/scripts/probe_model_latency.py`) sends
   Judge-sized requests, one key after another, 5 s apart. Each request
   may run up to `--cap` seconds (default 120 s), so even slow answers
   are measured. It prints one
   line per request (key, seconds, result), then "Answered N/M", the
   median, and for each limit of 30, 60 and 90 s how many answers that
   timeout would have kept. Each request uses one of that model's 20
   daily requests on that key, so keep `--per-key` small.

5. **Find the resilience events (read-only).** After a batch (or in a
   run folder's `<T>_ai-service.log`):
   ```bash
   bash scripts/logs.sh ai-service grep google.server_error_failing_over
   bash scripts/logs.sh ai-service grep llm_router.falling_back_to_alternate_model
   bash scripts/logs.sh ai-service grep llm_router.highest_rescue_attempt
   bash scripts/logs.sh ai-service grep llm_router.lite_cooldown_retry
   ```
   An older day: add the date, e.g.
   `bash scripts/logs.sh ai-service grep google.request_timed_out 2026-10-01`.
   Walk the audience through one sequence: a 503 on 3.8
   (`server_error_failing_over`, `server_error_giving_up_early` with
   `overload_streak` and `cooldown_seconds`), then
   `falling_back_to_alternate_model` to 3.7, then
   `completion_succeeded` on 3.7 with its `latency_ms`.

6. **Watch live during an analysis (read-only).**
   ```bash
   bash scripts/logs.sh ai-service | grep -E 'google\.|llm_router\.'
   ```

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Why does each Google key need its own Cloud project?**
   Gemini's free limits count per project, not per key, so two keys of
   one project share the same quota.

2. **What happens on the first 503 from gemini-3.8-flash?**
   The adapter gives up for this call, raises the overload streak and
   sets a 20 s marker (then 60, 180, 300 s on further give-ups); the
   router moves to 3.7.

3. **Why does the Judge ignore the 503 marker, and when does it not?**
   So each chain model gets one real try even if Bull/Bear just set
   markers; it honours the marker once the streak is 2 or more.

4. **How long is a key benched after a per-day 429?**
   Until the next midnight in America/Los_Angeles plus 60 s.

5. **What is the HIGHEST rescue attempt?**
   After the whole Flash chain failed: one request on the chain model
   that answered most recently (two for the Judge, the second after 10 s
   if the first met a 503).

6. **Why is `thinking_budget=0` not sent to gemini-3.6-flash?**
   That model rejects it with 400; it gets `thinking_level=MINIMAL`.

7. **Which timeouts apply to a call on gemini-3.5-flash as the last chain
   model?** 90 s (terminal), unless the caller passed its own timeout.

8. **Why are Lite 429s not retried after 1-2-4-8 s?**
   The logs showed those retries almost always met the same cooldown;
   the router instead waits for the earliest key's cooldown to end and
   tries once.

9. **What does `requests_sent=0` on a rate-limit error mean?**
   The adapter sent nothing because every key was still cooling down;
   the router then stops retrying that model. `requests_sent=None` marks
   an error the router built itself when it skipped a model.

10. **Why may a Lite call wait 60 s at the pool gate when the setting
    says 30 s?**
    While the Lite cooldown retry is on, failing fast would only lead to
    a sleep of about 60 s in that retry, and the fallback model shares
    the same gate. Waiting in the queue is faster.

11. **What happens when a Lite answer stops at MAX_TOKENS with most of
    the budget spent on thinking?**
    The adapter sends it once more with `thinking_budget=1024`. The
    router never sees this retry.

12. **Which model gets the ReAct JSON schema?**
    Only the tier's first model. Fallback models get JSON mode without
    the schema.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/ai/llm/base.py` | `LLMTier`, `LLMMessage`, `LLMResponse`, the exception family, `RATE_LIMIT_BACKOFF_SCHEDULE`, `AGENT_LLM_CALL_RETRIES`, `RequestPacer` and `request_pacer`; `ConcurrencyGate` (holds a Lite slot only while a request is in flight; `released()`), the context variable with the running call's gate, `released_while_waiting()` for waits inside the adapter. |
| `services/ai-service/ai/llm/router.py` | `LLMRouter.complete`/`stream`, tier and chain maps, `router_retry_limit`, skip logic, fallback walk, `_highest_rescue`, Lite cooldown retry, the per-request helper `_call` with the non-English rejection, `_concurrency_gate` (which gate a call gets). |
| `services/ai-service/ai/llm/google_provider.py` | `GoogleAdapter`, `_GoogleKeyPool`, `_reserve_or_fail_fast` (slot reservation and fail-fast log for `complete` and `stream`), thinking config, timeouts, 429/503/timeout handling, pool and marker queries (`google_model_pool_exhausted`, `google_model_confirmed_overloaded`, `google_pool_cooldown`, `google_model_recently_timed_out`, `google_model_last_success_at`). |
| `services/ai-service/ai/llm/mistral_provider.py` | `MistralAdapter` and its key pool. |
| `services/ai-service/ai/llm/openai_provider.py`, `anthropic_provider.py` | Dormant adapters. |
| `services/ai-service/ai/llm/json_fence.py` | `strip_json_code_fence`, `parse_json_list`, `parse_json_object` (the Judge's reader: first JSON object, surrounding text and control characters in strings tolerated and reported). |
| `services/ai-service/ai/llm/capacity.py` | `chain_model_wait`, `chain_capacity` (Reflection retry gate), `free_chain_model` and `capacity_aware_delay` (side repair, Judge delayed retry, citation-review retry, late side repair): whether a model of a tier's chain is free now (no daily-quota exhaustion, no 429 pool, 503 or timeout cooldown). |
| `services/ai-service/ai/agents/loop.py` | `_build_react_step_schema` (the ReAct JSON schema), `_REACT_LOOP_MAX_TOKENS`. |
| `services/ai-service/ai/providers/news_relevance.py` | `verify_with_cheap_tier` (the relevance classifiers' CHEAP call, fail-open and echo check) and `parsed_with_retry` (their JSON retry at 8192 tokens). |
| `services/ai-service/ai/rag/embedding.py` | Gemini and Mistral embeddings, batching, key rotation. |
| `services/ai-service/ai/agents/evidence_grounding.py` | `non_english_reason`, `non_english_reason_in_output`. |
| `services/ai-service/config.py` | Every LLM setting with its default. The reasons and measurements behind the values are in sections 3.2-3.11 and 5. |
| `services/ai-service/observability.py` | `alphamind_llm_errors_total`. |
| `services/ai-service/scripts/probe_model_latency.py`, `scripts/probe.sh` | Latency probe. |
| `services/ai-service/scripts/probe_model_latency.py --health` | Per-key and per-model health check of the Google keys. |

## 10. Related chapters

- [13-ai-service.md](13-ai-service.md): the service around this layer.
- [15-ai-agents.md](15-ai-agents.md): how agents use the router and
  react to its errors.
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md): Judge delayed
  retry, debate side repair, no-verdict endings.
- [17-ai-rag.md](17-ai-rag.md): embeddings and the RAG assistant's
  streaming call.
- [27-observability.md](27-observability.md): LLM error metrics and
  alerts.
