# 15. The agents

> An AlphaMind analysis is done by a team of LLM "agents". Nine data
> agents (Market, Technical, Fundamental, Filing, News, Sentiment, Macro,
> Risk, Portfolio) each collect evidence with tools and write a short
> report. Then Bull and Bear argue for and against, the Judge weighs the
> debate, and Reflection checks the work. Each data agent is a ReAct
> loop: think, call a tool, read the result, repeat, then answer. The
> loop and the per-agent validators are the heart of AlphaMind's
> quality story: an answer is accepted only when its numbers and names
> can be found in the agent's own tool results from the same run. This
> chapter explains the loop, the shared checks, each agent, the debate,
> the code-built evidence the debate reads, the Judge and Reflection.

## 1. The big picture

```
                         Supervisor (pure code: which agents run)
                                         |
     +---------+---------+---------+-----+-----+---------+---------+---------+
     v         v         v         v           v         v         v         v
  Market  Technical  Fundamental Filing      News      Macro     Risk    Portfolio
     |         |         |         |           |                           (if the
     |         |         |         |           v                          portfolio
     |         |         |         |       Sentiment (after News)          has holdings)
     |   each one: ReActAgentLoop (ai/agents/loop.py), Lite model (CHEAP tier)
     |     prefetched tool calls -> thought / action / observation ... -> finish_answer
     |     finish gates + the agent's own validator -> accept, or a correction turn
     v
  AgentOutput {summary, score, is_well_supported, raw_output{scratchpad, ...}}
     |
     v
  Bull  <->  Bear   (HIGHEST_ACCURACY argument, figure check, at most 1 rewrite)
     |
     v
  Judge  (HIGHEST_ACCURACY: converged?, leaning, confidence; figure check, at most 1 rewrite)
     |
     v
  Reflection (mechanical checks + LLM citation review) -> retry some agents, or finish
     |
     v
  archival of good reports into RAG (Macro, News, Fundamental, Risk)
```

This chapter covers the agents themselves. How the graph schedules them,
the debate rounds, the Judge's retries and the final recommendation are
in [chapter 16](16-ai-analysis-pipeline.md).

## 2. Concepts you need

| Term | Meaning |
|---|---|
| Agent | Here: one role with its own system prompt, tools and checks. The nine data agents subclass `BaseAgent` (`ai/agents/loop.py`) and return an `AgentOutput`. Bull, Bear, Judge and Reflection are plain classes: no ReAct loop and no tools. The Judge and Reflection's review make one main LLM call; Bull and Bear may rewrite their argument (section 3.15). |
| ReAct | "Reason + Act": the model alternates between a thought, an action (a tool call) and reading the observation (the tool's result), until it gives a final answer. |
| Scratchpad | The list of steps so far in one agent run (`ScratchpadEntry`: `thought`, `action`, `action_input`, `observation`, `is_tool_result`, `tool_error`, `prefetched`, `peer_ticker`). It is sent back to the model every turn and saved in the result. |
| Tool | A Python class with a `name`, a `description`, a JSON-schema `parameters` and an async `run(**kwargs) -> str` (`ai/tools/base.py`). Provider failures come back as text, not exceptions. |
| Prefetch | A fixed first tool call that the code runs before the first LLM turn, to save LLM calls (`AGENT_PREFETCH_ENABLED`). |
| `finish_answer` | The agent's final answer, a few sentences of prose. |
| `reported_metrics` | An optional structured object the model fills when it finishes (for example `{"latest_close": 623.41}`). Validators compare these numbers with the tool output instead of parsing prose. |
| Finish gate | A generic check in the loop that can reject a `finish_answer` (no tool evidence, no anchor). |
| Validator | The function `(final_answer, reported_metrics, scratchpad) -> issue or None` passed as `finish_answer_validator`; for every agent an `InternalTokenValidator` with the agent's own base check and its reject-once checks (section 3.5). |
| Reject-once check | A finish check that sends an answer back at most once per run and only while the loop has a correction turn left; after that it only logs. |
| Code-built evidence | Evidence lines that code builds from structured data (SEC XBRL, filing records, tool values) and adds next to the agents' summaries for the debate, the Judge and Reflection (section 3.17). |
| Prior note | An earlier AlphaMind agent report returned by a `search_prior_*` tool (tag `[agent:<role>]` in the passage header); a lead to confirm, never evidence. |
| Correction turn | A synthetic scratchpad entry that tells the model why its answer was rejected; it does not count as research. |
| Grounding | Whether a figure or phrase in a text can be found in the evidence it claims to come from. |
| Verification warning | A banner `[VERIFICATION WARNING: ...]` added in code to the front of a summary when a problem survived the corrections. |
| Archival | Saving a finished agent report into the RAG store so later runs can search it as "prior notes". |

## 3. How it works in AlphaMind

### 3.1 The roster

`shared/enums.py::AgentRole` has the nine data roles plus `bull`
and `bear` (the Supervisor, Judge and Reflection are graph nodes, not
roles in that enum). The graph's generic
node factory (`ai/agents/supervisor.py::AGENT_REGISTRY`) maps eight of them
to classes: Market, News, Sentiment, Filing, Technical, Fundamental, Risk,
Macro. Portfolio is built separately by `portfolio_node` because it needs
a `portfolio_id`. Bull, Bear, Judge and Reflection have hand-written
nodes. The supervisor node picks the others.

`ai/agents/supervisor.py::select_agents` is pure code, no
LLM:

1. If `requested_agents` is given, it replaces the default set.
2. Otherwise the default set for the `analysis_type`
   (`DEFAULT_AGENTS_BY_TYPE`): `full` = all eight data roles;
   `technical` = Market, Technical; `fundamental` = Market, Fundamental,
   Filing; `sentiment` and `news` = News, Sentiment; `risk` = Market,
   Risk; `macro` = Macro, Market; `filing` = Filing, Fundamental.
3. Portfolio is added when the analysis has at least one holding in its
   portfolio (`holdings_count >= 1`), unless an explicit
   `requested_agents` list left it out.

Every role that does not run gets a written skip reason (`AgentSkip`).

### 3.2 The ReAct loop

`ai/agents/loop.py::ReActAgentLoop` is written by hand rather than taken
from a framework, so that every step of how an agent decides to stop can
be explained. Each agent builds one loop with:
`role`, `tools`, `system_prompt`, `max_iterations`, an optional
`finish_answer_validator`, an optional `reported_metrics_schema`,
`require_tool_evidence` (default true), `evidence_yield` and
`anchor_fallback_reference`.

**One turn.** `_build_messages` builds a system message (today's date as
ground truth, the agent's prompt, the JSON contract, a rule that
`thought` stays short, the tool list) and a user message (the task, extra
context, a note about prefetched calls, the scratchpad so far). The router
call is:

```
llm_router.complete(LLMTier.CHEAP, messages,
                    retries=AGENT_LLM_CALL_RETRIES,   # 4
                    max_tokens=4096,                  # _REACT_LOOP_MAX_TOKENS
                    response_format="json_object",
                    response_schema=<built from the tools' parameters>)
```

A loop built with `thinking_budget=N` also passes `thinking_budget=N` on
every call. Only Sentiment does this (3.12; chapter 14, 3.11).

The model must answer with one JSON object:

```json
{"thought": "...", "action": "tool_name" | null, "action_input": {...} | null,
 "finish_answer": "..." | null, "reported_metrics": {...} | null}
```

The response schema (`_build_react_step_schema`) is built per agent:
`action_input` is an `anyOf` of the agent's own tools' parameter schemas
plus `null` for the finish turn, and `reported_metrics` has the agent's
own shape (`reported_metrics_schema`). A single schema with an open
`{"type": "object"}` made Gemini decode `{}` and every tool got no
arguments; a global union of all tools would let a model pick a tool it was
not given. A tool without a `parameters` schema falls back to an open
object. Only the tier's first model gets the schema; fallback models get
JSON mode alone (chapter 14).

**What happens to the answer:**

```
response
  |-- non-English (LLMNonEnglishOutputError)  -> correction turn "respond in English"
  |-- not valid JSON                          -> correction turn
  |      (finish_reason MAX_TOKENS -> "you were cut off, keep thought to one sentence")
  |-- neither action nor finish_answer        -> correction turn
  |-- finish_answer set:
  |      gate 1 no_tool_evidence     (up to 2 rejections)
  |      gate 2 evidence_anchor      (1 rejection)
  |      agent validator             (up to 2 rejections)
  |      -> accept (unresolved issues are recorded, not hidden)
  |-- last turn and action set               -> use the thought as the answer (hit cap)
  |-- action repeats an earlier call         -> "blocked before running" turn
  '-- action                                  -> run the tool, append the observation
```

Limits (constants at the top of `loop.py`): correction turns for bad
responses `_MAX_CORRECTION_RETRIES = 2` (the loop stops after the third),
blocked duplicates `_MAX_DUPLICATE_BLOCK_RETRIES = 2`, zero-evidence
rejections `_MAX_ZERO_EVIDENCE_RETRIES = 2` (its own budget, so an agent
that first finished without evidence still gets its validator's turns),
validator rejections `MAX_FINISH_VALIDATION_RETRIES = 2` (public: the
shared reject-once budget reads it). Only real tool calls count toward
`max_iterations`; correction and blocked turns do not. Each budget is
separate so a format problem never eats research turns, but every one is
small so a model that never complies cannot loop forever.

**Tool errors.** Tools never raise on a provider failure; they return a
fixed text. An observation counts as a tool error (`tool_error=True`, not
evidence) when its first line matches one of the fixed failure formats
(`_TOOL_ERROR_PATTERNS`: "<Thing> ... failed for ...:" / "... failed:",
"Not enough [valid] price history for", "Unknown tool", "Tool ... was
called with invalid arguments", "Tool ... raised an error:"). Only the
first line is read, so a result that merely mentions "failed" is not an
error (`react_loop.tool_reported_error`, `react_loop.tool_raised`).

**Duplicates.** A tool call with the same name and the same arguments
(compared as sorted JSON) that already succeeded, or that already failed
twice, is blocked before it runs (`react_loop.duplicate_action_blocked`).
`get_current_price` is exempt (a live price can change). When a new
observation is at least 85% similar (`difflib`) to one of the last three,
a note is appended that says another similar search is unlikely to help,
and repeating that exact call is blocked next time.

**The last turn.** When one productive iteration is left, the system
prompt says "this is your LAST turn ... you MUST set finish_answer". If
the model still sets an action, the loop uses its thought as the answer
and marks `hit_iteration_cap=True`
(`react_loop.final_turn_action_overridden`). If the loop ends by running
out of corrections, `_best_effort_result` builds a summary from the last
real observation, also with `hit_iteration_cap=True`.

**Start staggers.** Agents do not all call the LLM at the same instant.
`ai/graph/nodes.py::_run_agent_safely` waits `0.65 s x` the role's
position (`_FAN_OUT_STAGGER_SECONDS`; Market 0 s ... Macro 4.55 s,
Portfolio 5.2 s), to spread the first Lite calls of a fan-out (chapter
14). This is the only start stagger; `loop.run` adds none.

**Prefetched tool calls.** `prefetch_tool_calls` runs an agent's fixed
first calls in code, concurrently, before the loop starts. Their results
become the first scratchpad entries with `prefetched=True` and the
thought `(fetched in code before the first turn: <tool>)`. They count as
the agent's own evidence, and repeating them with the same arguments is
blocked. Turning `AGENT_PREFETCH_ENABLED` off returns an empty list
(Macro's two FRED calls are the exception, see 3.13). It saves about 8 to
10 Lite calls per analysis. Every prefetched call goes through
`prefetch_tool_call`, which turns an exception into an ordinary errored
scratchpad entry, so a failed prefetch never fails the agent.

**Results for another ticker.** The data tools that take a `ticker`
(`get_current_price`, `get_recent_price_history`,
`get_technical_indicators`, `get_volatility_and_drawdown`,
`get_fundamentals`) are built with `analysis_ticker=state.ticker`. The
model may still ask for another ticker, for example a competitor or an
index for comparison. Then:

- the tool appends a label to its result: `[PEER TICKER AMD: every figure
  above is for AMD, not for NVDA, the company under analysis. ...]`
  (`ai/tools/base.py::peer_ticker_label`, log event
  `tool.peer_ticker_result_labelled`);
- the loop stores the other ticker in the entry's `peer_ticker` field
  (`Tool.peer_ticker`, compared with `same_ticker`: case- and
  space-insensitive, and `.`, `-` and `/` count as the same share-class
  separator, so `BRK.B` and `BRK-B` are one ticker while an exchange
  suffix such as `SHOP.TO` still differs from `SHOP`);
- the entry is not the company's own evidence: `is_successful_tool_entry`
  and `has_successful_tool_evidence` skip it, so every validator,
  `key_facts`, `reference_price`, the quality score and the
  `no_tool_evidence` gate read only the analysed ticker's results; the
  Judge's valuation note (3.18) skips it too;
- its text still counts as a tool result of this run for the figure
  checks (`evidence_reference_text`), so a clearly labelled comparison
  such as "AMD trades at 40 times earnings" is not rejected as an
  invented figure.

The tools that never take a ticker from the model (filing, archive and
news searches) are bound to the analysed ticker when they are built, and
a ticker the model still sends is logged and ignored.

### 3.3 The finish gates

When the model sets `finish_answer`, the loop runs these checks in order.
A rejection appends a correction entry (`is_tool_result=False`) whose
observation starts with `[Your finish_answer was not accepted: ...` and
logs `react_loop.finish_gate_rejected` with the `gate` name, or
`react_loop.finish_answer_validation_failed` for the agent validator.

1. **`no_tool_evidence`** (when `require_tool_evidence`). The agent must
   have at least one successful tool result
   (`has_successful_tool_evidence`). Failed calls, correction entries and
   `query_knowledge_graph` (`NON_EVIDENTIAL_TOOLS`, because its facts
   come from earlier agent reports) do not count. Up to 2 rejections.
2. **`evidence_anchor`** (always on; skipped only when the agent's
   evidence-yield function reports zero items, see News). `evidence_anchor_issue` accepts the answer if
   it shares at least one number with the agent's own successful tool
   observations, within `NUMBER_MATCH_REL_TOLERANCE = 0.01` (1%), or at
   least one run of three consecutive words of 3+ letters each. Dates
   ("2026-09-22"), small integers below 10, years 1900-2100 and count
   headers ("20 bars", "5 passages") are ignored as numbers. The
   reference text is only the agent's own tool output, never the injected
   context, because that is exactly what a recycled answer would copy.
   For an agent with no tool evidence but a `anchor_fallback_reference`
   (Sentiment reading News's summary), `loosely_anchored` is used: a
   shared number, a shared 3-word run, two shared word pairs, or a word
   share (`word_share`: the share of the answer's distinct words of 5+
   letters, lightly stemmed, that also appear in the reference) of at
   least `FALLBACK_ANCHOR_MIN_WORD_SHARE` (0.3). The word share is a
   general similarity measure; there is no list of "generic" words. 1
   rejection. Why the anchor exists: without it an answer that is a
   foreign-language news blurb, unrelated advice or a paraphrase of our
   own prompt is accepted with a high score, because nothing compares the
   answer with the agent's own tool results. An on-topic answer almost always repeats
   one of its numbers or a short phrase from them.
3. **The agent's validator** (section 3.5). Up to 2 rejections; the
   correction text ends with the agent's `finish_validation_suffix`
   (default: "Recompute using the actual figures already shown above in
   this scratchpad, then set finish_answer again with the corrected
   value."; Filing, News, Sentiment and Risk use "Fix only what the check
   above names ...", because their checks are about wording or sources,
   not arithmetic).

When a check is still failing after its budget, the answer is accepted
anyway, but the issue is stored in
`AgentLoopResult.unresolved_finish_validation_issue` and logged as
`react_loop.finish_answer_validation_unresolved` or
`react_loop.finish_gate_unresolved`. Nothing is silently dropped.

### 3.4 From loop result to agent output

`derive_agent_quality` turns the loop result into `(score,
is_well_supported)`:

| Situation | Score | Well supported |
|---|---|---|
| Hit the iteration cap, an unresolved issue, or "no evidence" | 0.35 (`_SCORE_UNRESOLVED`) | no |
| Clean first pass | graded, 0.40-0.96 | yes |
| Accepted on a Reflection retry (`feedback` present) | at most 0.65 (`_SCORE_FIXED_ON_RETRY`) | yes |
| Thin evidence (1 or 2 live-search items, or News's unverified web fallback) | at most 0.6 (`_SCORE_THIN_EVIDENCE`) | yes |

A well-supported score is graded from the evidence: start at 0.84 for one
tool with usable data (`_SCORE_ONE_EVIDENCE_TOOL`), +0.04 per further
distinct evidence tool (up to 3 more), or 0.76 with no own tool evidence;
then subtract 0.05 per tool error (max 0.15), 0.04 per correction entry
(a rejected finish, a malformed turn or a blocked duplicate; max 0.12),
0.08 per verification warning (max 0.16), 0.10 when there was no accepted
turn (best-effort result), 0.05 when the accepted turn stopped on the
token limit, 0.03 when the accepted turn was not served cleanly by the
primary model (`rate_limited`), and 0.10 for recycled content (the agent's
own reuse flag). Floor 0.40 (`_SCORE_WELL_SUPPORTED_FLOOR`, always above
0.35). The score is rounded to 2 decimals and logged as
`agent_loop.quality_score_derived` with every signal. There is no penalty
for figures or prior-note amounts: replayed on five batches, such
penalties never moved a score across the 0.7 procedural-memory bar; the
figure problem is a reject-once check inside the loop (3.5).

Each agent then returns `AgentOutput` with `summary`, `score`,
`is_well_supported`, `iterations_used`, `hit_iteration_cap`,
`retry_of_feedback` and `raw_output`. `raw_output` always holds the
scratchpad (`vars()` of each entry) and `finish_meta`: the accepted turn's
`thought`, `provider`, `model`, `finish_reason`, token counts,
`thinking_tokens` and `rate_limited` (`None` when no turn was accepted).
Agents add their own fields: `verification_warnings` (a list, the
structured copy of the banners: a banner inside a summary is easily lost by
the next LLM that reads it), `key_facts`, `no_evidence`, `evidence_yield`,
`risk_events`, `live_fred`, `injected_context` (context the agent was given,
never evidence), `final_answer` (the answer before code-added lines),
`disclosures`, `reused_prior_note`, `latest_quarter`, the Filing lists,
and Sentiment's `sentiment_label`.

If the agent raises, `_run_agent_safely` returns a `FAILED` output
(`score=0.0`, not well supported) with a `failure_kind`; a summary that
is not English is also turned into a `FAILED` output
(`agent_node.non_english_output_rejected`). The workflow saves each
output as one `ai.agent_runs` row plus one `ai.agent_results` row whose
`output` column is `raw_output` plus `is_well_supported`.

### 3.5 The shared finish checks (`ai/agents/finish_checks.py`)

Every agent with checks uses ONE validator class,
`InternalTokenValidator(role, base, once_checks)`:

1. **`base`**: the agent's own structured check (Market prices, Technical
   indicators, Fundamental metrics, Macro rates, Risk events, Portfolio
   percentages, News live search). It may reject on every attempt, up to
   the loop's budget `MAX_FINISH_VALIDATION_RETRIES` (2). If it rejects, the
   other checks do not run on that attempt.
2. **Reject-once checks**, run in order when `base` passes: the
   internal-token check (always), then the agent's `once_checks`. Each one
   sends the answer back at most once per run; all that fire on the same
   attempt go back in ONE message (one correction turn); every send-back
   logs `finish_checks.once_check_rejected` (`role`, `check`, `issue`).
3. **The budget** (`RejectOnceBudget`): every rejection of the validator
   (base or once) is counted. A reject-once check fires only while the
   loop still has a correction turn left (`rejections < 2`); after that the
   finding is only logged (`finish_checks.once_check_logged_only`) and the
   answer is accepted. So a reject-once check can never make an answer
   "unresolved" (score 0.35, not well supported). Before this rule, Risk
   failed good answers on a third, cosmetic rejection.

**Internal-token check** (`internal_tokens_in_text`). Our own structured
tokens must not reach the reader: filing-record ids (`F1.L2`,
`RECORD_ID_IN_TEXT_RE` = `F<n>.<capital letter><n>`) and the search tools'
labels (`news_tool.TOOL_LABELS`: `catalog=`, "catalog outlet", "not in
catalog", the kind tags `[consumer product]`, `[opinion/listicle]`,
`[unverified]`, and "link unavailable"). These are formats we print, not
prose meaning. The message (`INTERNAL_TOKEN_MESSAGE`) asks to name a filing
by form and date and a source by its own name; ids inside
`reported_metrics` fields are fine. Log
`finish_checks.internal_tokens_in_answer` (`tokens`, `rejected`). Every
agent with a validator has it: Market, Technical, Fundamental, Filing,
News, Sentiment, Macro, Risk, Portfolio. Nothing downstream reads ids from
an agent's prose (the debate and the Judge read code-built `raw_output`
lists), so the check is safe for all of them.

**Own-evidence figure check** (`own_evidence_figure_check(extra)`, check
name `own_evidence_figures`). Every "%" and "$" figure, and every number
written with thousands separators (such as 496,579), in the answer must
match a number in this run's own primary evidence
(`finish_checks.primary_reference_text`: the successful evidence-tool
results of `loop.evidence_reference_text`, with knowledge-graph lookups,
errored calls and correction turns left out, and with every prior
AI-written `[agent:...]` note passage removed, as split by
`recycled_content.note_and_source_passages`) plus `extra` (context the
agent was given in code). A prior note is a lead, never evidence: a TSLA
News answer (October 2026) invented a year-ago delivery count, 496,579,
and later runs repeated it from the archived note, where the old check
counted it as grounded and never looked at plain counts. Counts
(`ungrounded_counts`) match any number of the reference, written grouped
or as plain digits of four or more, within 1%. Replayed on 1,041 stored
answers of eight batches, the counts and the note exclusion flag exactly
the four TSLA News answers with 496,579 and nothing else (the note
exclusion alone changes no "$"/"%" result). Matching of "$" and "%" figures is `debate_common.find_ungrounded_figures` (3.16): scale
normalized ($17.8B = $17,800 million), 1.5% relative tolerance for scaled
figures and percentages, 1% for plain dollar amounts; a figure with three
or more significant digits must instead match at its own precision (3.16). With no evidence at
all the check is silent (the zero-evidence gate handles it); an internal
error is logged (`finish_checks.own_evidence_check_failed`) and never
rejects. Wired for Market, Technical, Fundamental (extra: fresh filing and
KG context), Filing (same extra), Macro, Risk (its injected context), News
and Portfolio (portfolio KG context); not for Sentiment (it builds on
News's summary). Why in the loop: as a Reflection check, one hit would
cost a data-agent rerun plus a whole new debate round; in the loop it
costs one Lite correction turn. Replayed on 616 stored final answers of
five batches, the check produced no false positive.

`reported_number_issue(name, claimed, allowed, tolerance, template)` is
the shared numeric comparison of the `base` checks: a non-finite value
(NaN, infinity) is "must be a plain number"; otherwise it must be within
the tolerance of one allowed value.

### 3.6 Shared evidence helpers

- **Source catalog field.** Every `search_news` and `web_search` line ends
  with ` | catalog=yes` or ` | catalog=no` (nothing when the catalog is
  unknown), computed in code from NewsAPI's source catalog (chapter 17,
  section 3.11). A data field, not a phrase: when it was the bracket text
  "[catalog outlet]", agents copied it into their prose. Prompts tell
  agents to weigh a `catalog=no` source lower.
- **Prior-note provenance** (`ai/knowledge/recycled_content.py`).
  `prior_note_only_claims` lists the amounts (a number with a currency
  sign, scale unit or percent sign) an answer states that appear in a
  prior `[agent:...]` note returned this run and in no other tool result of
  this run, compared by value at the printed precision
  (`filing_records.RawNumbers`). Only News uses it, as a reject-once check
  (3.11). Dates are not checked; there is no score penalty.
- **Evidence grounding** (`ai/agents/evidence_grounding.py`).
  `extract_reported_percentages(*texts)` collects every `12.3%` token
  from the trusted texts; `final_answer_has_unverifiable_percentages`
  lists every percentage in an answer that is not in that set (exact
  string match, so a misquote like 49.7% -> 99.4% is caught; Portfolio);
  `wrap_verification_warning` builds the banner; `non_english_reason`
  is the English-only detector (chapter 14).
- **Recycled content** (`ai/knowledge/recycled_content.py`, chapter 18,
  3.6). `recycled_share` (sentence level, content words, this run's
  numbers exempt) and `prior_note_similarity` (word-sequence ratio against
  one prior note) are lexical, no LLM call.
- **Archival** (`ai/knowledge/archival.py::archive_agent_report`, chapter
  18, 3.5): Macro, News, Fundamental and Risk notes, only when clean.

### 3.7 Market agent (`ai/agents/market.py`)

- **Purpose:** the price trend and volume pattern of the ticker.
- **Tools:** `get_current_price`, `get_recent_price_history`
  (`ai/tools/market_tool.py`). `MAX_ITERATIONS = 5`. The history tool's
  `interval` is an enum of the intervals the price cache knows
  (`PRICE_INTERVALS`: `1d`, `1wk`, `1mo`); any other value returns a
  failed-lookup result instead of reaching the provider or the cache.
- **Prefetch:** `get_recent_price_history(ticker, interval="1d",
  lookback_days=30)`.
- **Prompt essentials:** pull history at different windows until the
  trend (or a genuinely mixed signal) is clear; never invent prices; when
  finishing, fill `reported_metrics` with exactly `latest_close`,
  `period_high`, `period_low`; describe volume with the up/down/flat
  direction the tool printed for each high-volume session.
- **Validator:** base `_market_claims_disagree_with_prices`. Each reported
  value must equal (within 0.005, float formatting only) a value the price
  tools printed this run: the last close of a history window (or a
  current-price quote when no history was fetched), the highest high, the
  lowest low of one window. The metrics are required whenever the matching
  tool output exists. The prose is never parsed. Plus the shared
  reject-once checks (tokens, own-evidence figures).
- **Extra output:** `key_facts.reference_price` (the current quote if
  fetched, else the latest close of the analysed ticker; results for
  another ticker are never used, see 3.2), kept as structured output.
- **Unresolved check:** when a check is still failing after the
  corrections, the summary gets a `[VERIFICATION WARNING: ...]` banner,
  the reason is stored in `raw_output["verification_warnings"]`
  (`market.unresolved_finish_issue_after_retries`), and the output is
  scored 0.35 and not well supported. The debate evidence then names
  Market under "UNVERIFIED CLAIMS FLAGGED" and leaves its numbers out of
  the grounding reference (3.17).
- **Typical failure and how it is caught:** a summary quoting a price no
  tool returned. The
  validator rejects the mismatching `reported_metrics` and quotes the
  real values back.

### 3.8 Technical agent (`ai/agents/technical.py`)

- **Purpose:** a technical view from RSI, MACD, moving averages,
  Bollinger Bands and ATR.
- **Tools:** `get_technical_indicators` (`ai/tools/technical_tool.py`,
  math in `ai/tools/indicators.py`), `get_recent_price_history`.
  `MAX_ITERATIONS = 4`.
- **Prefetch:** `get_technical_indicators(ticker, lookback_days=90)`.
- **Prompt essentials:** use at least two indicators; report the cited
  values among `sma20, sma50, rsi14, macd_line, macd_signal,
  macd_histogram, bb_upper, bb_mid, bb_lower, atr14`; always set
  `price_vs_sma20` and `price_vs_sma50` to the word the tool printed
  (`above`/`below`/`at`), `macd_trend` to `bullish`, `bearish` or
  `neutral`, and `rsi_band` to the band word printed next to RSI(14)
  (`oversold`, `neutral`, `overbought`).
- **Validator (base, the indicator check):**
  `_technical_claims_disagree_with_indicators`. It reads the tool's own
  fixed-format output with regexes (`_TECHNICAL_FIELD_PATTERNS`; safe,
  because our code writes that format), then:
  1. every reported number must be within 0.05 of the tool's value;
  2. the categorical claims are required whenever the tool printed the
     facts they are checked against;
  3. `price_vs_sma20/50` must equal the printed "Price vs SMA(N)" word
     (or, without one, the position recomputed from the latest close
     with a 0.1% band);
  4. `macd_trend` must match the histogram's sign; near zero
     (`|histogram|` below 5% of ATR(14), at least 0.01) "neutral" is also
     accepted;
  5. `rsi_band` must equal the printed band (standard bands: 30 or below
     oversold, 70 or above overbought; log
     `technical.rsi_band_claim_rejected`). No check reads the prose.
  Plus the shared reject-once checks.
- **After the run:** an unresolved mismatch becomes a verification
  warning (`technical.unresolved_indicator_mismatch_after_retries`);
  `key_facts.reference_price` is the indicator tool's own latest close.
- **Typical failure:** a model reads "SMA(20) above SMA(50)" as "price
  above its 20-day average", or recalls numbers from another ticker. The
  validator compares the structured claim with the printed word and the
  numbers and asks for a recomputation.

### 3.9 Fundamental agent (`ai/agents/fundamental.py`)

- **Purpose:** valuation: P/E, P/B, revenue growth, margins.
- **Tools:** `search_filings` (RAG), `get_fundamentals` (SEC EDGAR merged
  with Yahoo, `ai/tools/fundamentals_tool.py`; it also prints the latest
  quarter from SEC XBRL), `query_knowledge_graph`, `search_prior_analysis`
  (prior Fundamental notes, category `analyst_report`),
  `search_earnings_call_transcripts`, `web_search`. `MAX_ITERATIONS = 8`.
  It has no price tool on purpose: raw price is Market's job, and an
  earlier version stated an unsourced dollar price.
- **Prefetch:** `get_fundamentals(ticker)`, `search_filings("latest
  quarter results revenue operating income net income net loss")` (so the
  newest 10-Q reaches the agent; without it an annual margin can be
  presented as current while the newest 10-Q shows a quarterly loss) and
  `search_earnings_call_transcripts("results drivers guidance outlook
  margins capital spending")` (as an optional tool it is never called:
  agents answer on their first turn from prefetched data).
  A ticker without a transcript gets a short "none found" result.
- **Extra context:** the newest filing summary (`fresh_filing_context`)
  and knowledge-graph facts (`kg_context`).
- **Prompt essentials:** call `search_prior_analysis` once before
  `web_search`; weigh a `catalog=no` web result lower; never state a
  dollar share price; prefer the company's own newest filing over web
  articles for guidance (guidance is revised every quarter); a transcript
  is management's own words, attributed to its call, and the filing or
  `get_fundamentals` wins on a reported figure; compute margins and growth
  rates from this run's raw figures, never copy one from a prior note;
  report the cited values among `trailing_pe, forward_pe, pb,
  revenue_growth_yoy_pct, gross_margin_pct, operating_margin_pct,
  net_margin_pct` (every one cited, and at least one), plus
  `revenue_growth_period` ("annual" or "quarterly") with the growth figure;
  use the code-rendered values and the filing records: report the latest
  quarter when it differs materially from the annual figures; when the
  XBRL line says the newer filing is not in XBRL yet, take the newest
  quarter from that filing's passages; `[agent:...]` passages are prior
  notes to confirm; no record ids or catalog fields in the prose.
- **Validator:** base `fundamental_answer_issue` = first of
  1. `_reported_metrics_disagree_with_fundamentals`: each reported metric
     within 0.05 of this run's last successful `get_fundamentals` value;
     `reported_metrics` is required when `get_fundamentals` succeeded; a
     key outside the 7 names is not checked (and the schema forbids it);
  2. `revenue_growth_period_issue`: when `revenue_growth_yoy_pct` is
     reported, `revenue_growth_period` must equal the period
     `get_fundamentals` labels the figure with (log
     `fundamental.revenue_growth_period_rejected`). The prose is not read.
  Plus the reject-once checks (tokens; own-evidence figures with the
  fresh-filing and KG context as extra reference).
- **After the run:** an unresolved issue becomes a warning
  (`fundamental.unresolved_finish_issue_after_retries`);
  `get_fundamentals`' soft "note: ..." flags are appended as "Data caveat
  (from get_fundamentals): ..." and stored as `raw_output["data_caveats"]`
  (data context, not warnings, so they do not block archival). The
  operating-below-net-margin note names its period ("for the fiscal year
  ended <date>"): without it the AMD Judge (October 2026) tied FY2025
  margins to 2026 investment gains. The SEC
  XBRL latest-quarter text and "Revenue growth YoY (get_fundamentals,
  <label>): <value>%" are appended to the summary; the three margins are
  exported as `key_facts` (the Judge's valuation note reads them).
- **raw_output:** `latest_quarter` (the `quarter_results_payload` dict,
  chapter 18 section 3.12: text, periods, the three metrics with prior
  values and `*_yoy_pct`, `net_minus_operating`, `newer_filing`,
  `year_to_date`, optional cost keys, optional `below_operating` and
  `cash_flow`; absent when SEC XBRL has no quarter), `one_off_items` (the
  same list as Filing's, from the same records), `key_facts`,
  `data_caveats`, `verification_warnings`, `final_answer`,
  `injected_context`, `finish_meta`.
- **Typical failure:** a margin copied from an older note whose own
  arithmetic was wrong (15.6% stated where this run's $2.30B / $11.5B
  gives 20.0%). Caught by the metric check against this run's tool output
  and the recompute rule.

### 3.10 Filing agent (`ai/agents/filing.py`)

- **Purpose:** risk factors, recent results and guidance from the
  company's 10-K/10-Q filings in the RAG store.
- **Tools:** `search_filings` (`ai/tools/filing_tool.py` ->
  `ai/rag/retriever.py::FilingRetriever`), `query_knowledge_graph`.
  `MAX_ITERATIONS = 5`.
- **Prefetch:** `search_filings(query="risk factors OR recent results OR
  guidance OR outlook OR Item 1A")`.
- **Extra context:** `fresh_filing_context`, `kg_context`. The prompt
  says the fresh summary covers only the newest filing(s); `search_filings`
  must still be used.
- **Prompt essentials:** say plainly when there is no filing evidence; use
  the code-rendered values and the filing records: report the latest
  quarter as the SEC XBRL line states it (with period and year-over-year
  change; the line also carries the items below operating income and, for
  a 10-Q, the year to date and its cash flow); when the line's title says
  the newer filing is not in XBRL yet, take the newest quarter from the
  newest filing's passages; state each legal matter's or deal's status as
  the newest filing text supports (records can be wrong); give an older
  filing's guidance with its filing date and period; name an unusual item
  next to the figure it affects (it may recur; never "one-time" unless the
  filing says so); every other figure carries its period in plain words
  from the passage's own labels ("Six Months Ended" is never annual);
  never combine a quarterly and an annual figure; never write a record id.
- **Validator:** no base check (the loop's generic gates apply); the
  reject-once checks (tokens; own-evidence figures with the fresh-filing
  and KG context as extra reference). Correction suffix: "Fix only what
  the check above names" (the default "recompute the figures" does not fit
  a wording check).
- **raw_output (all built in code from `ai.filing_records`, never from
  prose; each list fails open to `[]` with a log event):**
  - `one_off_items` (`one_off_item_lines`): the newest filing's unusual
    items `F1.O1`..`F1.O6`, each "<description>: <amount> (<n> months ended
    <date>) [F1.O2, 10-Q filed <date>] Filing text: "<quote>"" (the quote
    usually carries the year-ago column, which the debate needs to name a
    year-ago item). Fundamental stores the same list.
  - `key_records` (`key_record_lines`, newest filing only, plain words, no
    ids): legal matters that state an amount ("amount stated in the filing
    (claimed, sought, assessed or awarded)"); transactions of type
    financing or guarantee, and any announced or pending transaction; every
    guidance item.
  - `year_to_date_unusual_items` (`year_to_date_unusual_item_lines`): for a
    newest 10-Q, the unusual items of the earlier quarters of the same
    fiscal year to date, from the other rows. Window: after the report date
    of the newest 10-K before it and before the newest report date, each
    end narrowed by 7 days (the newest quarter's own items are
    `one_off_items`); only items with `period_months` 3 or missing;
    de-duplicated by (period end, amount). An older 10-K without an EDGAR
    report date takes its fiscal year end from its own records (the latest
    `period_end` of a 12-month record before its filing date); the records
    of an older filing without a report date count only with
    `period_months` exactly 3 (a clean quarter inside the window). When
    either is used, `filing.ytd_window_from_record_periods` is logged
    (`year_end`, `items`). Empty for a 10-K, a first quarter, or when no
    fiscal year end is known or it is more than about nine months older
    (`filing.ytd_window_unknown`).
  - `prior_year_unusual_items` (`prior_year_unusual_item_lines`): the
    unusual items of the same form filed for the period one year earlier
    (report date 364 +- 10 days before the newest one, the closest),
    of that filing's own period length (3 months for a 10-Q, 12 for a
    10-K; a missing length counts as its own).
  These lists reach the debate, the Judge and Reflection as code-built
  evidence (3.17). The search tool's records are loaded once per agent
  (`FilingRecordsCache`), so the lists and the record ids show the same
  rows the tool rendered.
- **Tool output:** `search_filings` lists passages newest filing first,
  each labelled `[form; period: ...; NEWEST filing in these results |
  OLDER filing -- may be superseded by the <form> filed <date>]`; the
  period is SEC EDGAR's report date for that filing, never parsed from the
  summary text; the first result also lists the latest-quarter XBRL line
  and the filing records with ids such as `F1.L2` (chapter 17, 3.9;
  chapter 18, 3.11).

### 3.11 News agent (`ai/agents/news.py`)

- **Purpose:** recent material developments and news sentiment, with
  duplicates removed and sources weighed.
- **Tools:** `search_prior_news_notes` (prior News notes, category
  `news`), `search_news` (NewsAPI with the relevance classifier),
  `web_search`. `MAX_ITERATIONS = 7`.
- **Prefetch:** `search_prior_news_notes(query="recent material
  developments")` and `search_news(query="latest news", limit=10)`. When
  that `search_news` failed (tool error format), code also runs
  `web_search("<Company> company news", limit=5)` in Tavily's news mode
  for the last `NEWS_WEB_FALLBACK_DAYS` (7) days (`prefetch_web_fallback`,
  log `news.web_search_fallback`). A query like "<TICKER> stock news
  <Month YYYY>" found only price-forecast pages; when NewsAPI's quota ran
  out mid-batch, agents did not switch to web search by themselves.
- **Prompt essentials:** prior notes first, then always `search_news`
  next ("finishing before a live search is rejected"); drop off-topic
  results; weigh `catalog=no`, `[consumer product]` and `[opinion/listicle]`
  items lower; `[unverified]` items are not evidence; never write the
  markers, `catalog=` fields or "link unavailable"; name the scope of two
  figures that could differ in scope; never write "rose X% to $Y" (name the
  current figure first and the prior as the base: "current-quarter revenue
  is $11.5B, up ~50% from $7.69B a year earlier"); an item a prior note
  covered is not a new catalyst; write from this run's results, not from a
  prior note's wording or details; resolve "on Thursday" from the item's
  own date and write every date as YYYY-MM-DD without weekday names.
- **Validator** (`_NewsFinishValidator`, an `InternalTokenValidator`):
  - base `_reject_finish_before_live_search`: no finish until
    `search_news` or `web_search` has run (may reject on every attempt). A
    call counts when it ran without error, or failed in the tool's own
    "... search failed" format (an honest zero-evidence search); a call
    that errored before searching (invalid arguments, an exception) does
    not (`counts_as_live_search`).
  - reject-once: internal tokens; `prior_note_copy` (the summary's
    word-sequence similarity to a prior News note returned this run is at
    least `PRIOR_NOTE_MAX_SIMILARITY`, 0.9); `prior_note_claims`
    (amounts that only a prior AI-written note of this run states, at most
    6 named; one new search is allowed to confirm them); own-evidence
    figures. All in one message, within the budget.
  - correction suffix: fix only what the check names.
- **After the run:** a run that still has no live search gets a
  verification warning (`news.finished_without_live_search`); an issue that
  survived the correction turns is logged `news.finish_issue_unresolved`
  with `kind` "live_search" or "generic". A weekday written next to a date
  that falls on another weekday is logged only
  (`news.weekday_date_mismatch`, `weekday_date_mismatches`: format parsing
  of the date and the weekday name; a date without a year is read in the
  year closest to the run date). A send-back fixed the weekday but not the
  event date, so it became log-only and the prompt asks for no weekday
  names. A summary still at least 0.9 similar to a prior note is scored
  recycled, `reused_prior_note=True`, not archived
  (`news.summary_near_duplicate_of_prior_note`).
- **Evidence yield:** `live_search_yield` counts the evidence-kind items of
  the live searches from the tools' result headers (company_business,
  stock_market, and unclassified items kept on a classifier error;
  consumer, opinion/listicle and `[unverified]` items do not count; chapter
  17, 3.11). Zero items -> `no_evidence=True`, a `[NO EVIDENCE: ...]`
  banner and score 0.35 (`news.live_search_no_evidence`); 1-2 items ->
  thin evidence. When no `search_news` succeeded and the web results
  include no catalog outlet (`fallback_sources_unverified`, read from the
  result header; an unknown catalog never flags), the run is thin evidence
  with a verification warning (`news.fallback_sources_unverified`).
- **raw_output:** `verification_warnings`, `no_evidence`, `evidence_yield`
  (`None` when no live search ran), `fallback_sources_unverified`,
  `reused_prior_note`, `finish_meta`.
- **Typical failure:** finishing from stale prior notes only, or
  reporting an old development as a new catalyst (which can flip
  Sentiment on items that were already covered). The prefetch plus the
  validator force a live search; prior notes are labelled `[agent:...]`
  and checked.

### 3.12 Sentiment agent (`ai/agents/sentiment.py`)

- **Purpose:** current market sentiment for the ticker, in words only.
- **Tools:** `search_news` (lines carry the catalog field).
  `MAX_ITERATIONS = 4`. No prefetch.
- **Ordering:** when News is selected, the graph runs Sentiment only
  after News has finished (`ai/graph/edges.py::route_after_news`). This
  also holds when Reflection retries News, alone or together with
  Sentiment: Sentiment then runs again after the retried News, so it
  always reads the current News summary, freshness signal and yield
  (chapter 16, 3.4).
- **Extra context:** News's summary plus a "freshness signal" built in
  code: News failed; News searched but found 0 items; News ran a live
  search; or News did not search. "Ran a live search" uses the same
  predicate as News's own check (`news_tool.ran_live_search`): a
  `search_news` or `web_search` call that really searched, including one
  that errored after searching with zero results, but not a call that
  failed on its arguments or was blocked. In the first, second and fourth case
  the prompt says Sentiment must call `search_news` itself. When News's
  output carries verification warnings, a caveat line says to treat its
  web-derived claims as unconfirmed.
- **Prompt essentials:** never state a numeric score; end with
  `Sentiment: <label>` where the label is one of `very negative`,
  `negative`, `neutral`, `mixed`, `positive`, `very positive`; write no
  label line when no search returned a relevant article.
- **Loop settings:** when News's live search yielded items,
  `require_tool_evidence` is off and the anchor check uses News's summary
  as the fallback reference (`loosely_anchored`). Without this,
  Sentiment can make no tool call, run its Lite model to the token cap
  and answer about the wrong product. The loop also gets
  `thinking_budget=SENTIMENT_THINKING_BUDGET` (1024): on a Lite model each
  ReAct turn may think for at most 1024 tokens, from the first call
  (chapter 14, 3.11). Without the cap, long thinking cost about 148 s per
  batch; budgeted calls answered in 1.9-10.3 s.
- **Validator:** no base check; reject-once: internal tokens and
  `label_without_evidence` (a "Sentiment:" line although neither News nor
  its own search returned a relevant article). No own-evidence figure
  check.
- **After the run:** the label is read with a regex; if missing, a Lite
  classifier call (`_classify_sentiment_label`, JSON mode) recovers it,
  or it stays visibly missing (`None`, never a default "neutral"). With no
  evidence at all, no classifier call is made, the label is `None`, a
  `[NO EVIDENCE: ...]` banner is added and the score is 0.35; a label line
  the model still wrote is kept as written and logged
  (`sentiment.label_written_without_evidence`). Thin evidence (1-2 items,
  or News relied only on an unverified web fallback) caps the score at 0.6.
  `raw_output["verification_warnings"]` is always `[]` (the key is kept for
  readers); `finish_meta`, `no_evidence` and `evidence_yield` are stored.
- **Typical failure:** restating News's summary when News itself only
  read old notes. Caught by the freshness signal and the forced own
  search.

### 3.13 Macro agent (`ai/agents/macro.py`)

- **Purpose:** the macro context for this ticker: rates, sector rotation,
  market regime.
- **Tools:** `get_macro_indicators` (FRED, `ai/tools/macro_tool.py`),
  `search_prior_macro_notes` (prior Macro notes of **all** tickers,
  category `macro_research`; macro commentary is not ticker-specific),
  `web_search` (lines carry the catalog field). `MAX_ITERATIONS = 7`.
- **Prefetch:** `fed_funds_rate` (DFF) and `10y_treasury_yield` (DGS10)
  always, in code (`_prefetch_live_fred`, one `prefetch_tool_call` per
  series, deliberately not tied to `AGENT_PREFETCH_ENABLED` because the
  validator checks against them); then,
  with prefetch enabled, `cpi` with a lookback long enough for a
  year-over-year rate and `search_prior_macro_notes(query="Fed policy
  stance and rate trajectory")`.
- **Prompt essentials:** do not re-fetch DFF/DGS10; describe the Fed's
  stance from the live DFF move, never from a note; quote CPI as a
  year-over-year rate, not an index level; specific `web_search` queries,
  weigh `catalog=no` results lower; check result dates; always include a
  part that connects the backdrop to **this** ticker's industry (several
  unrelated tickers would otherwise get the same macro prose); state none of the
  company's own financials (no filings or results are available to Macro:
  no revenue, earnings, margins, cash flow, capex or balance sheet, not
  even in words such as "robust cash flow"); report `fed_funds_rate_pct`
  and `ten_year_yield_pct`.
- **Validator:** base `_macro_metrics_disagree_with_live_fred`: each
  reported rate must equal this run's live FRED value (within 0.005), must
  be present when that series was fetched, and must be null when it was
  not fetched live this run. Plus the reject-once checks.
- **After the run:** the code appends `Live FRED this run: DFF x.xx%
  (date); DGS10 ...` ("lookup FAILED this run" for a missing series); a
  failed lookup becomes a verification warning
  (`macro.live_fred_lookup_failed`). A check still failing after the
  corrections (for example a reported rate that does not match the live
  value) puts a `[VERIFICATION WARNING: ...]` banner in front of the
  summary and stores the reason in `verification_warnings`
  (`macro.unresolved_finish_issue_after_retries`); the output is scored
  0.35 and not well supported. One
  reuse decision, `macro_reuse`: the stored summary is reused when its
  `recycled_share` is at least `ARCHIVAL_RECYCLED_SHARE_MAX` (0.5). A
  reused answer gets `reused_prior_note=True`, the recycled score penalty,
  a disclosure in `raw_output["disclosures"]` (Bull/Bear/Judge see it as a
  system note) and is not archived; `raw_output["reuse_measures"]` keeps
  `reused`, `recycled_share` and the threshold (log
  `macro.summary_near_duplicate_of_retrieved_note`). One measure, the
  archive's own: a second character-level measure flagged 5 of 77 answers
  alone, each a reused backdrop whose sentences carry this run's live FRED
  figures.
- **Typical failure:** near-identical macro prose across unrelated
  tickers and stale rates from old notes. Caught by the live-rate
  validator, the industry rule and the recycled share.

### 3.14 Risk agent (`ai/agents/risk.py`)

- **Purpose:** the ticker's own volatility, drawdown and sector or event
  risk (not portfolio interaction, which is Portfolio's job).
- **Tools:** `get_volatility_and_drawdown` (`ai/tools/risk_tool.py`),
  `get_recent_price_history`, `query_knowledge_graph`, `search_filings`,
  `search_prior_risk_notes` (category `risk_analysis`),
  `search_earnings_call_transcripts`, `web_search`. `MAX_ITERATIONS = 7`.
- **Prefetch:** volatility and drawdown;
  `search_prior_risk_notes("regulatory competition litigation")`;
  `search_filings("risk factors legal proceedings litigation investigation
  regulatory")` (Risk runs in parallel with Filing, so it cannot read
  Filing's answer; without the prefetch an answer can call regulatory
  risk "contained" while the filings list open DOJ and IRS matters);
  `search_earnings_call_transcripts("risks legal and regulatory matters
  headwinds")` ("none found" when the ticker has no transcript).
- **Prompt essentials:** use the tool's volatility numbers, never estimate
  them; name the open matters the filing lists and never call legal or
  regulatory risk contained while matters are open; search for specific
  named events, not a list of categories; weigh `catalog=no` results lower;
  the records' statuses can be wrong: state the status the newest filing
  text supports, cite the newest filing, never present a dismissed,
  settled, completed or terminated matter as an open risk, give a stated
  exposure amount with its matter; transcripts are management's words and
  the filing wins on legal status; do not copy a prior note's wording;
  record ids only in `source_id`, never in the prose; answer in plain
  prose.
- **`reported_metrics.risk_events`:** one object per named risk event
  (`entity`, `evidence_tool`, `status`, `source_id`). `status` is one of
  pending, dismissed, settled, judgment, appeal, other, announced,
  completed, terminated, not_applicable (the records' legal and
  transaction statuses). `source_id` is a record id (`F1.L2`), a filing
  label (`F1`), a filing file name shown by `search_filings`, or empty for a
  non-filing source.
- **Validator** (`_CapturingRiskEventsValidator`, an
  `InternalTokenValidator` that also remembers the last `reported_metrics`
  it saw, which belong to the accepted answer). Base (may reject on every
  attempt), first issue wins:
  1. shape: a list of objects with non-empty entity and tool and a status
     from the enum;
  2. sourcing (`_risk_event_issue`): `evidence_tool` must not be
     `query_knowledge_graph` (its facts come from earlier agent reports);
     that tool must have a successful result this run (background context
     does not count); the entity must appear (case-insensitive) in that
     output, and outside earlier agent notes (`_independent_text` removes
     every `[agent:...]` passage, so a matter that exists only in an
     earlier agent note cannot pass);
  3. source lookup (`_cited_filing`): a `search_filings` event with a legal
     or deal status needs a `source_id`; a filing label must name a filing
     listed this run (`risk.filing_label_resolved`); a record id must
     resolve with `record_by_id` against the records the tool rendered this
     run; a file name must appear in a `search_filings` result this run.
  Reject-once checks: internal tokens; `record_name` (one message for every
  event that cites a record of ANOTHER matter); own-evidence figures (with
  its injected context as extra reference); `empty_risk_events`: the list
  is empty although the filing records shown to Risk this run
  (`FilingRecordsCache.shown`) hold at least one legal matter
  (`filing_records.legal_record_count`, a structural count of the
  rendered records; the prose is not read). The message gives the count
  and asks for one object per named event the answer mentions; a second
  empty list is accepted. Logged `risk.empty_events_with_legal_records`
  (field `legal_records`) and `finish_checks.once_check_rejected`
  (`check=empty_risk_events`). Replayed on 125 stored Risk runs (8
  batches): it would have fired twice (INTC 2026-10-05 with 54 shown legal
  records, BA 2026-10-03 with 12), both runs whose answer named legal
  matters but listed none.
- **Record match** (`_record_is_of_entity`; case, spacing and inner
  punctuation ignored, whole words): a cited legal-matter or transaction
  record is the event's record when the entity appears in its name, quote
  or detail; or the first party of a caption "A v. B" does; or another
  record of the same category with the same name names the entity (the
  newer record of the same matter under a shorter name). Otherwise
  `risk.record_name_mismatch` is logged and the answer goes back once with
  a hint listing the records of the newest filing that names the matter,
  saying when that filing is older than F1. After that the citation is
  accepted and the events line shows only the filing.
- **Logged, never rejected:** a stated status that differs from the cited
  record (`risk.record_status_mismatch`): the extracted statuses were found
  wrong or stale too often to enforce, and enforcing a wrong record forced
  a wrong status into the answer. The record's status is not shown next to
  the agent's.
- **After the run:** the code appends `Named risk events (sourced): ...`
  built only from events that pass the sourcing and lookup checks (a
  malformed event drops only itself), each "<entity> (status <s>; source:
  10-Q filed 2026-07-23, legal matter: <name>)", in plain words (ids stay
  in `raw_output["risk_events"]`; no tool names). An unresolved
  risk-events issue becomes a warning (`risk.evidence_validator_exhausted`);
  a run without any successful tool gets a warning
  (`risk.finished_without_any_evidence`). An answer whose
  `prior_note_similarity` is at least 0.9 is logged
  (`risk.final_answer_repeats_prior_note`), scored recycled, disclosed to
  the debate (`RISK_REUSED_DISCLOSURE`) and not archived; it is not sent
  back (in four batches the rewrites stayed 0.85-0.95 similar and the
  copied facts were current).
- **raw_output:** `risk_events`, `verification_warnings`, `final_answer`,
  `injected_context`, `finish_meta`, `reused_prior_note`,
  `prior_note_similarity`, `disclosures`.
- **Typical failure:** repeating a lawsuit or regulator action that only
  ever existed in an earlier run's note, so it kept coming back as
  "prior notes". Caught by the `[agent:...]` filter.

### 3.15 Portfolio agent (`ai/agents/portfolio.py`)

- **Purpose:** what acting on this ticker does to the whole portfolio:
  concentration, correlation, cash.
- **Tools:** `get_portfolio_composition` and `recompute_scenario`
  (`ai/tools/portfolio_tool.py`; data from core-service through
  `clients/core_client.py`, live prices from Yahoo).
  `MAX_ITERATIONS = 7`.
- **Prefetch:** `get_portfolio_composition()`, then two
  `recompute_scenario` calls for the ticker, run one after the other
  (the second reuses the first's cached holdings and cash):
  `RecomputeScenarioTool.suggested_share_counts` picks whole shares worth
  about 5% and 10% of the total net value (`SCENARIO_NET_VALUE_FRACTIONS`;
  at least 1 and 2 distinct counts). When the price, holdings or cash are
  unavailable, or an unexpected error occurs, the scenarios are not
  prefetched (`portfolio.scenario_prefetch_skipped`) and the agent calls
  the tool itself. Added after a batch where 7 answers stated the second
  scenario's percentage before computing it.
- **Extra context:** `portfolio_kg_context` (knowledge-graph facts for
  the held tickers) and, on a retry, a note listing the figures its
  previous attempt was flagged for.
- **What `recompute_scenario` returns:** `Position concentration: X% ->
  Y% of portfolio`, then a code-computed line `cost=..., available_cash=...,
  shortfall=...`, and for a buy scenario a second line
  `committed_to_other_open_buy_tiers=..., buy_budget=...,
  budget_shortfall=...`. **`buy_budget`** is the cash minus what other
  tickers' open BUY tiers already reserve. Without it the agent can call
  cash "fully available" that other open tiers have almost entirely
  reserved.
- **Prompt essentials:** base the answer on the two prefetched
  scenarios; call `recompute_scenario` only when they are missing or
  failed, or for a different `additional_shares` value; copy percentages
  verbatim; every scenario mentioned needs its own `recompute_scenario`
  call; describe funding with `buy_budget`, not `available_cash`; never
  estimate weights when a live price is missing.
- **Validator:** base `_portfolio_finish_validator(ticker, kg_context)`:
  1. a successful `get_portfolio_composition` is required;
  2. at least one successful `recompute_scenario` **for this ticker**;
  3. every percentage in the answer must appear in the trusted set: its
     own successful tool results plus `portfolio_kg_context`
     (`_trusted_percentages`). Correction entries are excluded, so a
     rejected figure quoted back to the model cannot "prove" itself.
  The rejection lists the scenarios actually computed, and the suffix
  says: call `recompute_scenario` for that share count or describe it in
  words. Plus the reject-once checks (tokens; own-evidence figures with the
  portfolio KG context).
- **After the run:** any remaining unverified percentage -> warning
  (`portfolio.unverified_percentage_in_final_answer`), score capped at
  0.35, not well supported. Portfolio's report is never archived.
- **Typical failure:** a percentage for a second, never-computed scenario
  (concentration is not linear in share count, so the guess was wrong and
  changed on every retry: 68.2% -> 68.2% -> 66.8% in one case). Caught by
  the percentage check with a precise correction.

### 3.16 Bull and Bear (`ai/agents/debate_side.py`, `debate_common.py`)

- **Purpose:** the strongest honest case for (Bull) and against (Bear),
  3-6 sentences, from the agents' evidence only.
- **Tier:** `HIGHEST_ACCURACY`, `retries=4` (the primary model, then
  the fallback model, chapter 14). No tools.
  One class, `DebateSideAgent(side)`; the sides differ only in their system
  prompt. They run concurrently each round, so each sees the other side's
  latest COMPLETED argument from an earlier round
  (`DebateState.previous_round_argument`, which also works after a
  one-side correction pass), never the current round's.
- **Input (user message):** the ticker; `format_evidence(state)` (3.17);
  the other side's previous-round argument; and, after a Reflection round,
  this side's own flagged issues (`format_prior_reflection_issues`): only
  issues whose structured `role` is this side and whose code is an LLM
  review issue; "do not repeat these claims, not even reworded; omit a
  claim you cannot trace" for problems, and a separate "answer these points
  of the other side" list for `not_engaged` issues. (A soft "do not
  repeat" lets the same fabricated figure survive several rounds.)
- **Prompt rules** (shared constants in `debate_common`, also used by the
  Judge): every number, date, deal or rating must be traceable to an
  evidence sentence; tag each claim with its source role in brackets
  (`[fundamental]`; an XBRL line is `[fundamental]`, a filing-record line
  `[filing]`, never "system" or a record id: `_TAG_RULE`); argue from
  operating income when net income includes unusual or non-operating
  gains; `UNUSUAL_ITEMS_RULE` (name the reported line an unusual item or an
  XBRL below-operating item sits in, as its quote or the XBRL line states
  it; do not cite a line's growth as underlying growth without naming the
  unusual items inside it; name the year-ago items behind a swing);
  `OVERLAP_RULE` (unusual items and XBRL items can overlap, e.g. an
  impairment also tagged as restructuring, an upward adjustment inside the
  equity gains, the same event in an XBRL line and a record: count each
  event once, never add overlapping items; a year-to-date amount already
  contains its quarters, so cite that amount or its quarterly parts, never
  both -- the INTC, UNH and DIS Judges listed both in October 2026);
  `FREE_CASH_FLOW_RULE` (when
  arguing from cash flow and the line gives free cash flow, cite it with
  its period; if negative, say so, never call capex covered by operating
  cash flow, and say how the gap was funded; if free cash flow after
  buybacks and dividends is negative, say so, never say cash flow funds or
  supports those returns, and say how the gap was funded; operating cash
  flow, not free cash flow, is what covers capex: with a positive free cash
  flow "operating cash flow covered capital spending" is correct and must
  not be flagged, and only "free cash flow covered (or funded) capital
  spending" is wrong (the first wording, "never say free cash flow covered
  capital spending", made Reflection flag correct operating-cash-flow
  sentences for AMD, TSLA and UNH in October 2026 and cost AMD and UNH 0.15
  confidence); with a positive free cash flow, debt issued is not presented
  as funding capex, operations or a gap; when the
  after-returns value (or free cash flow, with no return line) is positive
  there is no funding gap, and a fall in cash reflects other uses the line
  does not show, so no funding is described as "not shown" (the AMD Judge
  wrote "free cash flow fully covered capital expenditures" and called a
  cash fall with positive after-returns flow "funding not shown"); the
  after-returns line covers only the
  returns its label names; a funding source is named only with a figure
  the evidence gives for it (debt issued, a fall in cash; debt repaid is a
  use of cash, not a source) and covers only its own amount -- a smaller
  source leaves the rest "not shown" -- and without such a figure the text
  says the funding is not shown. The TSLA Judge (October 2026) read a
  positive six-month free cash flow as "requiring" the debt raised, and
  the KO Judge called a $1.00B gap "funded by existing cash" when cash
  fell $15M; the two middle clauses answer those. The earlier
  wording "name how the gap was funded (debt issued, cash)" made the BA
  Judge write "funded by cash reserves and debt" while the filing showed
  debt repaid; the XBRL cash-flow line now also carries debt issued, debt
  repaid and the net change in cash when tagged, chapter 18);
  `LEGAL_AMOUNT_RULE` (an amount stated is claimed, sought, assessed or
  awarded, not a recorded loss; an accrued, paid or escrowed amount is
  already in the results); `PERIOD_LABEL_RULE` (every figure carries its
  period; never place an annual or TTM figure beside a quarterly one;
  never present an older growth rate as current momentum); `PROFIT_TREND_RULE`
  with `YEAR_TO_DATE_AND_DRIVERS_RULE` (the newest quarter's change as the
  XBRL line states it, the year-to-date change next to it, drivers only as
  the filing states them; an `[agent-derived ...]` KG figure is not a
  fact); `SOURCE_WORDING_RULE` (plain source names, no ids, line labels or
  tool names). Reflection's flagged list is the top priority of the round.
- **Checks (`generate_debate_argument`):** the draft gets one free
  mechanical check, `find_ungrounded_figures` against
  `format_grounding_reference(state)` (the evidence without flagged
  agents' lines, plus the code-built lines). With ungrounded figures
  (`{side}.ungrounded_figures`) the side writes once more with the list of
  figures (`DEBATE_MAX_REGENERATIONS = 1`, `{side}.combined_regeneration_done`);
  a regenerated draft that is still ungrounded is accepted and logged
  (`{side}.final_draft_still_flagged`). There is no other LLM call and
  no other check: a Lite self-check of every draft would cost about 36
  calls per batch and gave more false than true positives when measured.
  Citations are the bracketed roles that
  really ran (`extract_citations`).
- **`find_ungrounded_figures(text, evidence)`:** reads figures with "%",
  "$" amounts with a scale word (trillion, billion, million, bn, mn, mln,
  tn, trn, B, M, K, T), plain "$" amounts, "$ 68 million" with a space, and
  accounting negatives "$(1,180) million". A range ("$165-169 billion",
  "$165 to $169 billion", "5-7%", "5%-7%") is read as both ends, with the
  "$" and the scale word (or "%") carried to both; a range with a higher
  first end, or whose "$" second end is followed by "%" (a signed change
  such as "$10,365 -4%"), is not a range. An ungrounded range in the text
  is reported as the whole range. Scaled figures and percentages match by
  value after scale normalization or within 1.5% relative; plain "$" amounts match any number in the evidence within 1%
  (`NUMBER_MATCH_REL_TOLERANCE`; "close=151.23" grounds "$151.23"), and a
  plain "$7.46" never grounds "7.46%". These relative tolerances apply only
  to rounded figures (one or two significant digits, such as "$600 million"
  or "$2.3B"). A precise figure (three or more significant digits) must
  agree with an evidence number to within one unit in the last digit of
  the coarser of the two (`_figure_matches`): "$27.5B" matches 27.46 and
  "$15.84B" matches "$15,848 million", but "$593 million" does not match
  598 (a made-up sum that once passed the 1.5% rule). Replayed on 289
  stored analyses (2026-10-02 to 10-11): 6 new firings, 4 correct and 2
  caused only by an older evidence layout. Known blind spot: a correct sum,
  difference or midpoint of two evidence figures is flagged.
- **Typical failure:** invented figures. Caught by the figure check and,
  later, Reflection.

### 3.17 The code-built evidence block

Bull, Bear, the Judge and Reflection all read `format_evidence(state)`:
one line per data agent (`- <role>: <summary>` plus any disclosure as
"(System note, not part of the agent's text: ...)"), then lines built in
code from structured data. When any agent carries verification warnings, a
first line "UNVERIFIED CLAIMS FLAGGED this run by: <roles>" names them
(a banner inside a summary would otherwise be repeated as fact). When every agent
failed, a fixed placeholder replaces all lines. Each code-built line starts
with the role a claim from it is tagged with and says how it was checked;
the shared rule `CODE_BUILT_LINES_ARE_EVIDENCE` tells every reader that
such lines are evidence as reliable as the filing, not metadata (an LLM
check can otherwise drop verified amounts as "unverified metadata"). In order:

1. `fundamental (computed in code from the company's SEC XBRL financial
   data): <text>`: Fundamental's `raw_output["latest_quarter"]["text"]`,
   whitespace-collapsed (quarter vs year-ago quarter, costs,
   net-minus-operating, below-operating items, year to date, cash flow,
   the overlap note). Absent when Fundamental failed or SEC has no quarter.
2. `filing (unusual items from the filing records, each checked in code
   against a quote from the filing; recorded in results and they may
   recur): ...`: Filing's and Fundamental's `one_off_items`, de-duplicated,
   at most 6 (`debate.one_off_items_dropped` when more), each at most 500
   characters (`debate.one_off_item_cut`).
3. `filing (key records from the newest filing, checked against quotes):
   ...`: Filing's `key_records`.
4. Up to two `filing (from the filing records, checked in code against
   quotes) -- <title>: ...` lines: "Unusual items in earlier quarters of
   this year-to-date window (they are inside the year-to-date figures)" and
   "Unusual items in the year-ago period (inside the year-ago figures the
   latest quarter is compared with)", at most 6 items of 500 characters
   each (`filing_raw_list`).

`format_grounding_reference(state)` is the same text without the lines of
flagged agents (their own possibly fabricated figures must not ground a
side's repetition of them). It is the reference of the debate figure
check, the Judge figure check and Reflection's false-positive filter.

The **Judge's user message** adds, when they apply: the prior open
recommendation context; the current holding; the single-position limit
line; the valuation note (`trailing_pe_line`); the profit-trend block
(`profit_trend_line`); the most recent earlier verdict
(`previous_verdict_line`); and the track record (chapter 18). Details in
3.18 and chapter 16.

Why code-built lines: a debate that restates figures from LLM-written
summaries gets the period or meaning wrong (a Filing summary can give
$1.626B operating income without its $2.762B year-ago base, so nobody
sees -41%). Parsing profit changes out of summary prose and presenting
them as "computed in code" injects wrong facts. Rule: code-built lines
come only from structured data (XBRL facts, filing records with checked
quotes, tool values), never from re-parsed model prose.

### 3.18 Judge (`ai/agents/judge.py`)

- **Purpose:** weigh Bull and Bear against the evidence and decide
  `converged`, `leaning` (buy/hold/sell), `confidence` (0-1) and a
  `rationale`, as JSON.
- **Tier:** `HIGHEST_ACCURACY`, `response_format="json_object"`,
  `max_tokens` 3072 (`_JUDGE_MAX_TOKENS`: a truncated answer does not
  parse and costs a whole extra debate round), `retries=4`, through the
  same primary and fallback model as every HIGHEST call (chapter 14).
  One call path, `JudgeAgent._complete`, used by both calls
  below.
- **Short-cuts without an LLM call:** every agent failed -> `judge_failed`
  with `no_evidence`; both latest debate sides unavailable ->
  `judge_failed` with `debate_unavailable`. The Judge reads each side's
  LATEST argument, whatever its round (a one-side correction pass re-argues
  one side only); an unavailable side is shown as "(UNAVAILABLE -- do not
  infer what it would have argued)".
- **System prompt** (`_BASE_SYSTEM_PROMPT` plus optional clauses): weigh
  evidence, not eloquence; a one-off GAIN makes the trailing P/E lower,
  never higher, a tax reserve is not a gain, and this argument may be used
  only when the valuation note is in the user message; cash and BUY-budget
  figures only limit how much can be bought, never the leaning or
  confidence (only the single-position limit may change the action); name
  the period of every period figure and do not call a result ongoing
  unless the latest period shows it; the shared rules of 3.16
  (`PERIOD_LABEL_RULE`, `PROFIT_TREND_RULE`, `UNUSUAL_ITEMS_RULE`,
  `OVERLAP_RULE`, `LEGAL_AMOUNT_RULE`, `FREE_CASH_FLOW_RULE`,
  `SOURCE_WORDING_RULE`); state a listed latest-quarter change with its
  driver and the year-to-date change of the same metric; a year-to-date or
  year-over-year change must say it includes the listed unusual items, with
  amounts, and no self-computed adjusted figure unless both numbers are in
  the evidence; compare guidance with the latest actual quarter before
  calling it strong or weak; name the single deciding factor; the change
  rules below; list the two strongest opposing points; investment reasons
  only, never the debate process. Optional clauses: a prior open
  recommendation (action, confidence and date only, never its reasoning;
  say whether the verdict supersedes it); low participation (fewer than
  half the selected agents well supported: acknowledge the evidence gap,
  do not treat a prior call as still supported); the track record
  (calibration only, never repeat a call because it was made before).
- **Answer (JSON):** `converged`, `leaning`, `confidence`, `rationale` (2-8
  sentences: first every statement the rules and the user message require,
  then the main reasons), and four audit fields: `deciding_factor` (a few
  words); `change_since_previous` and `change_basis` (`new_evidence` or
  `reweighting`, empty when there is no change to explain);
  `opposing_points` (at least the two strongest points of the side the
  verdict goes against, each `{point, response}`).
- **Earlier verdict and the change rules.** `previous_verdict(state)` takes
  the most recent of the open prior recommendation and the newest
  track-record call. When that analysis stored the Judge's own final-round
  verdict (`ai.recommendations.judge_leaning`, `judge_confidence`, written
  before penalties, caps and the position-limit HOLD), the comparison is
  like for like: the user message says "your own final-round leaning BUY
  with confidence 0.85 (before code adjustments)", and a leaning change OR
  a confidence move of `CONFIDENCE_CHANGE_TO_EXPLAIN` (0.05) or more
  (compared at 4 decimals) needs `change_since_previous` and
  `change_basis`. For an older row without those columns, only the final
  action and confidence exist; the line says so, only a leaning change
  needs the fields, and the Judge is told not to anchor to or explain the
  final confidence. Reason: comparing the raw verdict with the previous
  run's penalised value asked for explanations of moves that never
  happened (BUY 0.85 stored as 0.55) and ratcheted confidence down.
- **Figure check, one rewrite** (`_rewrite_ungrounded_figures`). Right
  after a successful parse, `find_ungrounded_figures(rationale, reference)`
  runs with reference = `format_grounding_reference(state)` plus the
  code-built lines of the user message (prior recommendation, holding,
  position limit, valuation note, profit-trend block, earlier verdict,
  track record); the Bull and Bear texts are NOT reference (a side's
  ungrounded figure must not ground the Judge). Nothing ungrounded: no
  extra call. Otherwise `judge.ungrounded_figures` is logged and ONE more
  call is made on the same path: the same messages, the first answer as
  the assistant turn, and a request to restate each listed figure exactly
  as the evidence gives it (value, unit and period) or remove it, not to
  add items together, and to return the whole JSON again. The second
  answer is accepted whatever it says (leaning and confidence included);
  its rationale is checked once more and a remaining problem is only logged
  (`judge.ungrounded_figures_after_rewrite`). Fails open: when the call
  raises, the reply does not parse, or it has no English rationale, the
  first answer is kept (`judge.ungrounded_rewrite_failed`). Only the
  rationale is checked (`opposing_points` are not shown to users; checking
  them would add false positives on correct sums and midpoints). Replay on
  32 stored final rationales: 1 trigger (GOOGL "$36.915 million" for
  $36,915 million), 0 false positives; cost about one extra HIGHEST call
  per 16-32 analyses.
- **What is stored:** only `converged`, `leaning`, `confidence` and
  `rationale` (`ai/graph/state.py::JudgeVerdict`). The audit fields are
  checked and written to the log, never stored and never a reason for
  another call (the only Judge retry would be a whole new debate round of
  HIGHEST calls). Log events: `judge.deciding_factor` (always; with
  `previous_leaning`, `previous_confidence`, `flipped`, `confidence_moved`,
  `previous_is_final`, `change_since_previous`, `change_basis`),
  `judge.change_since_previous_missing`, `judge.change_basis_missing`,
  `judge.opposing_points` (up to 4) and `judge.opposing_points_missing`
  (fewer than 2 well-formed items). To review a verdict's reasons, grep the
  analysis id with `judge.deciding_factor` and `judge.opposing_points`.
- **Failure handling:** the answer is read with
  `json_fence.parse_json_object`: code fence removed; if strict JSON fails,
  a comma directly before a closing brace or bracket (outside strings) is
  dropped first (`json.trailing_comma_removed`; every 3.8 Judge re-ask in
  the batches of 2026-10-11 was this slip), then the first JSON object is decoded from the first "{", with line breaks
  and other control characters allowed inside strings; text before or
  after that object (including a second object) is ignored. Any of these
  general slips is logged (`judge.json_parsed_leniently`: ignored leading
  and trailing character counts, the trailing text, whether control
  characters were in strings). An answer that still does not parse is
  logged (`judge.unparseable_response`: first 500 characters, the last
  300, `raw_chars`, `parse_error`, `error_pos`/`error_line`/`error_col`,
  `error_context` = 300 characters before to 200 after the error, `model`,
  `finish_reason`) and the Judge is asked ONCE again in the same
  conversation (same chain, same evidence, its reply and the parse error
  with its position; `judge.json_reask`). A readable second answer is used
  as the first one would have been (`judge.json_reask_recovered`; the
  figure check then runs on it); a failed call or a second unreadable
  answer (`judge.json_reask_failed`) gives a `judge_failed` verdict with
  `retry_round=True` (forces another round; never a silent default). All
  six unreadable Judge answers in the stored batches (5 on 2026-10-05, 1
  on 2026-10-04) came from gemini-3.6-flash with finish STOP and 468-617
  completion tokens; the 500-character excerpts were valid JSON so far, so
  the cause is not known yet; each forced a full Bull/Bear/Judge round
  (12-39 s each);
  a non-English rationale is `judge_failed`
  (`judge.non_english_rationale`). The confidence is clamped to 0-1, an
  unknown leaning becomes HOLD, and `converged` counts only a real boolean
  or the string "true" (so the string `"false"` cannot end a debate). The graph's
  availability retries and no-verdict handling are in chapter 16.
- **The rationale is stored as written:** a regex that removes "debate
  process" sentences also removes investment text, so only the prompt
  rule asks the Judge to leave them out.

### 3.19 Reflection (`ai/agents/reflection.py`)

- **Purpose:** decide whether the round is trustworthy and which roles to
  retry.
- **Mechanical part (no LLM, always runs):** per agent output: a failure
  of a provider-capacity or language kind (`rate_limit`,
  `server_overload`, `non_english`) and an honest `no_evidence` are listed
  but never retried (a retry would hit the same condition, and during a
  503 outage such a loop can run three debate rounds for nothing); other candidates are
  a FAILED agent, an iteration cap, a COMPLETED answer with no successful
  tool result of its own (`lacks_successful_tool_evidence`), and an output
  that is not well supported. Each finding is a structured
  `ReflectionIssue` with an `ISSUE_*` code. Reflection has no key-fact,
  margin-sanity or figure checks: such checks never fired or re-read the
  same cached data on a retry, and the own-evidence check runs in each
  agent's loop (3.5).
- **LLM part:** a `HIGHEST_ACCURACY` citation review (`max_tokens` 2048,
  JSON mode, one delayed retry after
  `REFLECTION_REVIEW_DELAYED_RETRY_SECONDS` (60) on a 429/503 after the
  primary and the fallback model both failed; the wait is skipped when
  one of the two models is free now, chapter 14, 3.10, logged `free_model` on
  `reflection.citation_review_delayed_retry`). It checks four things:
  citation integrity, argument quality (did the winning side engage the
  other side's strongest point; does its certainty match its evidence),
  internal consistency, and period/label consistency; it gets
  `CODE_BUILT_LINES_ARE_EVIDENCE`, `COMPUTED_VALUES_ARE_EXACT` (values the
  XBRL lines label "computed: ...", values marked * and the percentages
  and margins of those lines were computed from unrounded filing values;
  a difference from redoing the arithmetic with the rounded figures is
  never an issue: on 2026-10-05 the review flagged TSLA's "free cash flow
  (computed ...) +$352M" against "$8.63B - $8.28B = $350M" and sent the
  Bull back; that was the only stored issue, of 16, quoting a computed
  line) and the debate transcript with each side's latest round marked
  "(LATEST)" (superseded rounds left out, and a failed-side placeholder
  that a later argument of the same side and round replaced is left out;
  a claim no longer in the latest round must not be flagged). It must list `checked_claims` (logged, never a gate)
  and `issues`, each `{role, kind, figure, claimed_period, evidence_quote,
  description}` with `kind` one of `unsupported`, `wrong_period`, `stale`,
  `not_engaged`, `inconsistent`. Code keeps an issue only when it is an
  object with a description and its quote is found (whitespace and case
  normalized) in the agent evidence, or in the debate transcript for
  `not_engaged` (`reflection.issue_quote_not_grounded`); an `unsupported`
  Bull/Bear issue whose figure IS in the grounding reference is discarded
  (`reflection.mechanical_false_positive_discarded`); an unknown kind is
  kept as `None` (`reflection.issue_unknown_kind`); a role that is unknown,
  or that did not take part in this run (no agent output, no debate
  argument), is kept unrouted (`reflection.issue_role_not_routable`), so
  the review can never send a retry to a role that never ran. The issue text is built
  in code: "Bull (wrong period): <description> [figure 28.7%; given for Q2
  2026; evidence: "..."]" (quote at most 200 characters).
- **Skipped review:** no outputs; no debate because too few agents had
  usable evidence; the Judge produced no verdict and no retry round will
  follow; or no usable evidence. A failed call or an unparseable or
  wrongly shaped answer also skips it. A skipped review is recorded with
  its reason (`llm_review_skip_reason`, a provider error named by its kind),
  and the round is then never "verified".
- **Routing:** mechanical candidates first; LLM issues add roles but never
  remove a mechanical finding; not-retriable roles are removed; a debate
  side that is unavailable for capacity is not regenerated by Reflection
  (`debate side unavailable, not regenerated`; the graph's side repair
  and the Judge node's late side repair, chapter 16, are the only
  regenerations); when any data agent is
  retried, Bull/Bear are dropped (the data-agent retry already causes a
  new debate round). `verified` = no retry candidates AND at least one
  usable agent AND the LLM review ran.
- **Storage:** every issue is kept in the graph state as a
  `ReflectionIssue` (`code`, `role`, `kind`, `figure`, `claimed_period`,
  `evidence_quote`, `text`); the database stores only each issue's `text`
  (plus a `REVIEW_META:` line added at persistence). The confidence note is
  built from the structured form.
- Chapter 16 explains how its verdict drives retries.

### 3.20 Which checks reject, log or score

| Check | Where | Effect |
|---|---|---|
| Malformed / empty / non-English turn | loop | correction turn, up to 2 (`_MAX_CORRECTION_RETRIES`), then best-effort result |
| `no_tool_evidence` gate | loop | rejects up to 2 times, then unresolved (0.35) |
| `evidence_anchor` gate | loop | rejects once, then unresolved |
| Agent base validator (prices, indicators, metrics, rates, risk events, portfolio %, News live search) | loop | rejects up to 2 times (`MAX_FINISH_VALIDATION_RETRIES`), then unresolved: score 0.35; Market, Technical, Fundamental, Macro and Risk add a `[VERIFICATION WARNING: ...]` banner and a `verification_warnings` entry for it (News warns when no live search ran, Portfolio when a percentage is unverified; their other unresolved issues are logged and scored only) |
| Internal tokens, own-evidence figures, News prior-note copy and prior-note amounts, Risk record name, Sentiment label without evidence | `InternalTokenValidator` | each rejects once, all in one message, only within the budget; then logged |
| Risk record status differs from the record | Risk | log only |
| News weekday next to a wrong date | News | log only |
| Risk / News copy of a prior note (>= 0.9) after the run | Risk, News | recycled score, disclosure (Risk), not archived |
| Macro reuse (recycled share >= 0.5) | Macro | recycled score, disclosure, not archived |
| Bull/Bear ungrounded figures | debate | one regeneration, then log |
| Judge ungrounded figures | Judge | one rewrite, then log |
| Judge audit fields (deciding factor, change fields, opposing points) | Judge | log only |
| Reflection mechanical candidates and LLM issues | Reflection | retry routing (chapter 16) |

## 4. Configuration

| Setting / constant | Default | Where | Meaning |
|---|---|---|---|
| `AGENT_PREFETCH_ENABLED` | True | `config.py` | Run each agent's fixed first calls in code. |
| `ARCHIVAL_RECYCLED_SHARE_MAX` | 0.5 | `config.py` | Skip archiving a note at least this recycled. |
| `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` | None (0.90) | `config.py` | Cosine similarity for superseding an older note. |
| `MIN_USABLE_AGENTS_FOR_DEBATE` | 4 | `config.py` | Fewer usable agents: no debate (chapter 16). |
| `_REACT_LOOP_MAX_TOKENS` | 4096 | `ai/agents/loop.py` | Output budget of one ReAct turn. |
| `_MAX_CORRECTION_RETRIES` | 2 | `loop.py` | Bad-response corrections before giving up. |
| `_MAX_ZERO_EVIDENCE_RETRIES` | 2 | `loop.py` | `no_tool_evidence` rejections. |
| `MAX_FINISH_VALIDATION_RETRIES` | 2 | `loop.py` | Validator rejections; also the budget of every reject-once check (`finish_checks.RejectOnceBudget`). |
| `_MAX_DUPLICATE_BLOCK_RETRIES` | 2 | `loop.py` | Blocked repeated calls. |
| `_NEAR_DUPLICATE_OBSERVATION_THRESHOLD` | 0.85 | `loop.py` | Near-duplicate observation nudge. |
| `NUMBER_MATCH_REL_TOLERANCE` | 0.01 | `loop.py` | Relative tolerance for number grounding (also used by debate checks). |
| `FALLBACK_ANCHOR_MIN_WORD_SHARE` | 0.3 | `config.py` | Word share for the fallback evidence anchor. |
| `PRIOR_NOTE_MAX_SIMILARITY` | 0.9 | `config.py` | Word-sequence similarity at which a Risk or News answer is a copy of a prior note (News: rejected once; both: recycled, not archived). |
| `NEWS_WEB_FALLBACK_DAYS` | 7 | `config.py` | Look-back of News's Tavily news-mode fallback. |
| `JUDGE_STATE_QUARTER_DECLINE_PCT` | 15.0 | `config.py` | Latest-quarter decline (or a swing to a loss) the Judge must state. |
| `REFLECTION_REVIEW_DELAYED_RETRY_SECONDS` | 60.0 | `config.py` | Reflection review's one delayed retry after 429/503; 0 = off. |
| `SENTIMENT_THINKING_BUDGET` | 1024 | `config.py` | Thinking budget of Sentiment's ReAct turns on Lite models; 0 = off. |
| `_SCORE_*`, `_PENALTY_*` | see 3.4 | `loop.py` | The graded score. |
| `CONFIDENCE_CHANGE_TO_EXPLAIN` | 0.05 | `ai/agents/judge.py` | Confidence move against the Judge's own earlier verdict that needs `change_since_previous`. |
| `_JUDGE_MAX_TOKENS` / `_REVIEW_MAX_TOKENS` | 3072 / 2048 | `judge.py` / `reflection.py` | Output budgets. |
| `_ONE_OFF_MATERIAL_PE_RATIO` | 1.10 | `judge.py` | Forward P/E at least this times the trailing P/E for the valuation note. |
| `_FILING_LIST_MAX_ITEMS` / `_FILING_LIST_ITEM_CHARS` | 6 / 500 | `debate_common.py` | Caps of the code-built filing lists. |
| `THIN_EVIDENCE_MAX_ITEMS` | 2 | `loop.py` | 1-2 items = thin evidence. |
| `MAX_ITERATIONS` | Market 5, Technical 4, Fundamental 8, Filing 5, News 7, Sentiment 4, Macro 7, Risk 7, Portfolio 7 | each agent module | Productive tool calls per run. |
| `DEBATE_MAX_REGENERATIONS` | 1 | `ai/agents/debate_common.py` | Bull/Bear rewrites per round (a second one was never needed in five batches). |
| `_MIN_WORDS_TO_ARCHIVE` | 40 | `ai/knowledge/archival.py` | Shortest archivable report. |

## 5. Design decisions and why

**A hand-written loop instead of a framework agent executor.**
Alternative: LangGraph's prebuilt agent. Reason: explainability; every rule for when an agent may stop is code the team
owns and can show an evaluator.

**Structured JSON turns instead of "Thought: ... Action: ..." text.**
Reason: far less fragile to parse; the tool argument schemas also let
Gemini's structured decoding fill real arguments.

**Validators check structured `reported_metrics`, not prose.**
Alternative: regexes over the model's sentences. Reason: guessing
meaning from prose produces false alarms and misses; asking the model to restate the exact numbers as
labelled values makes the check a plain numeric comparison against the
tool's own fixed-format output.

**Accept with a warning after the correction budget.**
Alternative: fail the agent. Reason: one stubborn figure should not
remove an otherwise useful report; the warning travels with the summary
(Bull, Bear, Judge and Reflection see it), lowers the score and blocks
archival and procedural memory.

**Prompts plus mechanical backstops.**
Reason: in real runs a "mandatory" prompt instruction (live search,
prior-notes step) is sometimes simply skipped. Wherever skipping
matters, a code check follows.

**General checks only.** Every check must work for any US stock on any
day. So: no word or phrase lists over model prose, no regular expressions
over model prose (only over our own fixed tool formats), no hand-picked
sites or company names, no thresholds tuned to the tickers of one batch.
Checks use structured data instead: enums the model must choose from (news
`subject`/`kind`, record and event statuses, issue `kind`s), ids (`F1.L2`),
values compared with tool output (`reported_metrics`), and quotes that must
be found in the source text. A check never changes content silently: it
rejects once (the agent rewrites) or it logs. Every new check is replayed
on real batch data before it is delivered. Why: word lists, site lists
and title patterns fitted to the cases seen in a few tickers produce
false positives, cannot generalise, and can inject wrong figures as
"computed in code".

**Few checks, each with demonstrated value.** Measured across 77
analyses, a larger check layer (an LLM self-check of every debate draft,
a repeat check, Reflection key-fact and margin checks, agent-derived-
figure checks, a Judge `cited_figures` field, figure-based score
penalties) never changed a verdict and changed one confidence, through a
false positive. So there is one token check, one budget, the
own-evidence check inside the loop and one regeneration per side. A
check that fires rarely and is right stays as a reject-once or a log;
a check without demonstrated value is not added.

**Source credibility is a structured field.** The catalog flag comes from
NewsAPI's own catalog, computed in code, not from an LLM tool call: the
agents have no credibility tool to call and no prompt step to skip.

**Earlier agent notes are context, not evidence.**
Reason: an agent that restates earlier notes gets them archived again,
and they come back as "prior notes" in later runs, so unsourced wording
circulates. Hence the `[agent:...]` tag, the anchor
check against the agent's own tool output only, the Risk independence
rule and the recycled-share archival gate.

**Prefetch the fixed first calls.**
Reason: every agent's first move was predictable; doing it in code saves
Lite calls (and quota) and removes a whole class of "forgot to search
first" failures.

**Only four roles are archived.**
The scope (Macro, News, Fundamental, Risk) is a fixed decision recorded
in `ai/knowledge/archival.py::_CATEGORY_BY_ROLE`. Three of them reuse an
existing RAG category that fits what they write; Risk got its own
category, `risk_analysis`. The other roles are not archived. Portfolio
in particular never is, because archived notes must not contain
portfolio details.

## 6. When things go wrong

| Symptom | Where to look |
|---|---|
| An agent `failed` in the result | Its `summary` holds the exception text ("... agent failed: ..."); log `agent_node.failed` with `failure_kind` (`rate_limit`, `server_overload`, `non_english`, ...). |
| `react_loop.unparseable_response` repeated | The Lite model returned broken JSON; check `google.non_stop_finish_reason` with `MAX_TOKENS` and `thinking_tokens` for the same moment. |
| `react_loop.finish_answer_validation_failed` | A validator rejected an answer; `issue` and `attempt` say why. Usually fixed on the next attempt. |
| `react_loop.finish_answer_validation_unresolved` / `finish_gate_unresolved` | Not fixed within the budget; the summary should carry a `[VERIFICATION WARNING: ...]`. |
| `react_loop.duplicate_action_blocked` many times | The agent keeps repeating calls; the run may end with "Repeated duplicate/blocked tool calls". |
| `react_loop.final_turn_action_overridden` | Iteration cap reached; `hit_iteration_cap=True`, score 0.35. |
| `news.live_search_no_evidence`, `sentiment.no_evidence` | NewsAPI returned nothing relevant; check `NEWSAPI_API_KEY` and the relevance filter logs. Look for `newsapi.request_failed` (status, NewsAPI code) and `newsapi.rate_limited_cooldown_started`. |
| `macro.live_fred_lookup_failed` | FRED failed; check `FRED_API_KEY` and FRED availability. |
| `risk.evidence_validator_exhausted` | A named risk event could not be sourced; see the warning in the summary. |
| `risk.record_name_mismatch`, `risk.record_status_mismatch` | A cited record names another matter (rejected once, then accepted), or the stated status differs from the record (log only). |
| `finish_checks.once_check_rejected` | A reject-once check sent the answer back (`check`: internal_tokens, own_evidence_figures, prior_note_copy, prior_note_claims, record_name, label_without_evidence). |
| `finish_checks.once_check_logged_only` | The same finding after the correction budget was spent: the answer was accepted. |
| `finish_checks.internal_tokens_in_answer` | Record ids or tool labels in an answer (`rejected` says whether it went back). |
| `bull.ungrounded_figures` / `bear.ungrounded_figures`, `*.final_draft_still_flagged` | A side's draft had figures with no match in the evidence; still flagged after the one regeneration. |
| `judge.ungrounded_figures`, `judge.ungrounded_figures_after_rewrite`, `judge.ungrounded_rewrite_failed` | The Judge figure check and its one rewrite. |
| want the Judge's deciding factor, change basis or opposing points | Not stored, only logged: `bash scripts/logs.sh ai-service grep judge.deciding_factor` and `judge.opposing_points`, then filter by the analysis id. |
| `portfolio.unverified_percentage_in_final_answer` | A percentage without a tool source survived. |
| `archival.skipped_*` | Why a report was not stored (`not_well_supported`, `recycled_prior_notes`, `verification_warning`, `hit_iteration_cap`, `non_english`, `too_short`, `zero_chunks`). A role flagged in the last Reflection round logs `analysis_workflow.archival_skipped_flagged` instead. |

Every log line written during an analysis carries `analysis_id` and
`ticker` (bound with `structlog.contextvars` in `run_analysis_workflow`),
so `bash scripts/logs.sh ai-service grep <analysis id>` finds one run's
lines, agents included. `scripts/run_analysis.sh` also saves the run's
time window as `runs/run_<timestamp>/<T>_ai-service.log`.

## 7. Demo walkthrough

Goal: open a finished analysis and show, step by step, how one agent
gathered evidence, how an answer was rejected and then fixed, and what
the stored output contains. You need a run folder from
`scripts/run_analysis.sh` (under `runs/`; chapter 13, section 7;
creating one writes data). Everything below is read-only.

1. **Pick a run folder and list the agents.**
   ```bash
   ls -d runs/run_*/ | tail -3
   RUN=$(ls -d runs/run_*/ | tail -1); T=AAPL
   python3 - "$RUN" "$T" <<'EOF'
   import json, sys
   d = json.load(open(f"{sys.argv[1]}/{sys.argv[2]}_result.json"))["data"]
   for r in d["agent_runs"]:
       res = r["result"] or {}
       out = res.get("output") or {}
       meta = out.get("finish_meta") or {}
       print(f'{r["agent_name"]:12} {r["status"]:9} score={res.get("score")} '
             f'steps={len(out.get("scratchpad", []))} model={meta.get("model")} '
             f'warnings={len(out.get("verification_warnings") or [])}')
   EOF
   ```
   Say: "Nine data agents. Each has a score, the number of scratchpad
   steps, the model that wrote the accepted answer, and any verification
   warnings." (Portfolio appears only when the portfolio has
   holdings.)

2. **Walk through one agent's scratchpad (Market).**
   ```bash
   python3 - "$RUN" "$T" market <<'EOF'
   import json, sys
   d = json.load(open(f"{sys.argv[1]}/{sys.argv[2]}_result.json"))["data"]
   run = next(r for r in d["agent_runs"] if r["agent_name"] == sys.argv[3])
   for i, e in enumerate(run["result"]["output"]["scratchpad"]):
       kind = "PREFETCH" if e.get("prefetched") else ("TOOL" if e["is_tool_result"] else "CORRECTION")
       print(f'#{i} [{kind}] action={e["action"]} input={e["action_input"]}')
       print("   thought:", (e["thought"] or "")[:120])
       print("   observation:", (e["observation"] or "")[:160].replace("\n", " | "))
   print("\nSUMMARY:", run["result"]["summary"][:400])
   print("KEY FACTS:", run["result"]["output"].get("key_facts"))
   EOF
   ```
   Point out: entry 0 is the prefetched `get_recent_price_history` call
   (`thought` starts with "(fetched in code before the first turn");
   then the model's own steps; `is_tool_result=false` entries are
   correction turns, not evidence; `key_facts.reference_price` is the
   price the agent established, kept as structured output.

3. **Find answers that were rejected and then fixed.**
   ```bash
   python3 - "$RUN" "$T" <<'EOF'
   import json, sys
   d = json.load(open(f"{sys.argv[1]}/{sys.argv[2]}_result.json"))["data"]
   for r in d["agent_runs"]:
       pad = (r["result"]["output"] or {}).get("scratchpad", [])
       for i, e in enumerate(pad):
           if (e["observation"] or "").startswith("[Your finish_answer was not accepted"):
               print(f'{r["agent_name"]} step #{i}: {e["observation"][:300]}')
   EOF
   ```
   Then show the same rejection in the log saved with the run:
   ```bash
   grep react_loop.finish_answer_validation_failed "$RUN/${T}_ai-service.log"
   grep react_loop.finish_gate_rejected "$RUN/${T}_ai-service.log"
   ```
   A `finish_answer_validation_failed` line has `role`, `issue` (the
   reason, up to 300 characters) and `attempt`. A `finish_gate_rejected`
   line has `role`, `gate` (`no_tool_evidence` or `evidence_anchor`) and
   `attempt`; its reason is in the scratchpad entry printed above. A
   reject-once send-back also logs `finish_checks.once_check_rejected`
   with the check name. Say: "The validator compared the model's reported numbers
   with the tool's own output, found a mismatch, and sent the model a
   correction turn. The next finish was accepted: there is no
   `finish_answer_validation_unresolved` line for this role." Check that
   claim live:
   ```bash
   grep -E 'finish_answer_validation_unresolved|finish_gate_unresolved' "$RUN/${T}_ai-service.log" || echo "none unresolved"
   ```
   For today's live log instead of a run folder:
   `bash scripts/logs.sh ai-service grep react_loop.finish_answer_validation_failed`.

4. **Show the evidence trail of one validator.** For a Technical
   rejection, print its `get_technical_indicators` observation and the
   accepted `summary` side by side (adapt step 2 with `technical`), and
   show that `price_vs_sma20` in the corrected answer matches the
   printed "Price vs SMA(20)" line.

5. **Show what was archived.**
   ```bash
   cat "$RUN/${T}_agent_reports.txt"
   grep -E 'archival\.|archival_skipped' "$RUN/${T}_ai-service.log"
   ```
   Say: "Only Macro, News, Fundamental and Risk can be archived, and only
   clean reports: no warnings, not recycled, not flagged by Reflection."

6. **Show the debate citations (optional).**
   ```bash
   python3 -c "import json,sys; d=json.load(open(sys.argv[1]))['data']; [print(a['round_number'], a['side'], a['citations'], a['content'][:200]) for a in (d['debate'] or {}).get('arguments', [])]" "$RUN/${T}_result.json"
   ```

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Which nine agents gather data, and which of them can be absent from
   a full analysis?** Market, Technical, Fundamental, Filing, News,
   Sentiment, Macro, Risk, Portfolio. Portfolio runs only when the
   analysis has a portfolio with at least one holding.

2. **What counts as "tool evidence" for the `no_tool_evidence` gate?**
   A successful real tool call: not a failed one, not a correction
   entry, and not `query_knowledge_graph`.

3. **When does the `evidence_anchor` gate accept an answer?** When it
   shares one number (within 1%) or one three-word run (3+ letters each)
   with the agent's own successful tool observations.

4. **Why do validators read `reported_metrics` instead of the prose?**
   Numbers stated as labelled values can be compared exactly with the
   tool's fixed-format output; guessing from prose was unreliable.

5. **What happens if a validator still fails after two corrections?**
   The answer is accepted, the issue is recorded as unresolved, the score
   drops to 0.35 and the output is not well supported. Market,
   Technical, Fundamental, Macro and Risk also put a verification warning
   in front of the summary, so the debate names the agent under
   "UNVERIFIED CLAIMS FLAGGED" and keeps its numbers out of the grounding
   reference. News and Portfolio add a warning only for their own
   specific cases (no live search; an unverified percentage).

6. **Why does Risk ignore passages tagged `[agent:...]`?** They are
   earlier agents' notes, not independent sources; using them let
   unsourced events circulate between runs.

7. **What must a Portfolio answer have before it is accepted?** A
   successful composition call, a successful `recompute_scenario` for
   this ticker, and only percentages found in its tool results or the
   portfolio KG context.

8. **What is `buy_budget`?** Cash minus the cash already reserved by
   other tickers' open BUY tiers; the money really free for a new buy.

9. **Which agent reports are archived into RAG?** Macro, News,
   Fundamental and Risk, when clean.

10. **A Market answer contains a record id and a "$612" that no tool
    printed. How many correction turns does it cost?** One: the token check
    and the own-evidence figure check fire on the same attempt and go back
    in one message. Each fires at most once per run.

11. **The Judge writes "$36.915 million" where the evidence says
    "$36,915 million". What happens?** The figure check finds no match,
    logs `judge.ungrounded_figures` and asks once for a rewrite; the second
    answer is accepted whatever it says.

12. **Which of the Judge's answer fields are stored?** Only `converged`,
    `leaning`, `confidence` and `rationale`. The deciding factor, the change
    fields and the opposing points are only logged.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/ai/agents/loop.py` | `BaseAgent`, `ReActAgentLoop`, `ScratchpadEntry`, prefetch, finish gates, peer-ticker tagging, `is_successful_tool_entry`, `tool_reported_error`, `derive_agent_quality`, `SCORE_UNRESOLVED`, `evidence_reference_text`. |
| `services/ai-service/ai/agents/finish_checks.py` | `InternalTokenValidator`, `RejectOnceBudget`, the internal-token and own-evidence figure checks (`primary_reference_text`, `ungrounded_counts`), `reported_number_issue`. |
| `services/ai-service/ai/agents/supervisor.py` | `AGENT_REGISTRY` (eight generic roles), `select_agents`, `DEFAULT_AGENTS_BY_TYPE`. |
| `services/ai-service/ai/agents/market.py` ... `portfolio.py` | The nine data agents with prompts and validators. |
| `services/ai-service/ai/agents/debate_side.py`, `debate_common.py` | Debate sides (`DebateSideAgent`), the shared prompt rules, `format_evidence` and the code-built lines, `find_ungrounded_figures`. |
| `services/ai-service/ai/agents/judge.py` | The Judge. |
| `services/ai-service/ai/agents/reflection.py` | Reflection's mechanical and LLM checks. |
| `services/ai-service/ai/agents/evidence_grounding.py` | Percentage grounding, warning banner, English detector. |
| `services/ai-service/ai/knowledge/recycled_content.py` | `AGENT_NOTE_TAG`, `recycled_share`, `prior_note_similarity`, amounts found only in prior notes (`prior_note_only_claims`, News's reject-once check). |
| `services/ai-service/ai/knowledge/archival.py` | `archive_agent_report` and its gates. |
| `services/ai-service/ai/tools/base.py` | `Tool` (with `analysis_ticker`, `peer_ticker`, `label_result`), `peer_ticker_label`, `NON_EVIDENTIAL_TOOLS`, `is_evidence_record` (the one evidence rule shared by the loop and the graph state), `clamp_int`. |
| `services/ai-service/ai/tools/*.py` | The tools (market, technical, fundamentals, filing, news, macro, risk, portfolio, research archive, knowledge graph, web search). |
| `services/ai-service/ai/graph/nodes.py` | `_run_agent_safely`, fan-out stagger, failure kinds. |
| `services/ai-service/ai/workflows/analysis_workflow.py` | Saving agent runs; archival and procedural memory after the verdict. |

## 10. Related chapters

- [13-ai-service.md](13-ai-service.md): running an analysis and the
  result JSON.
- [14-ai-llm-layer.md](14-ai-llm-layer.md): what happens inside each
  LLM call.
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md): fan-out,
  debate rounds, Judge retries, Reflection retries, recommendation.
- [17-ai-rag.md](17-ai-rag.md): `search_filings` and the prior-notes
  tools.
- [18-ai-knowledge-memory.md](18-ai-knowledge-memory.md): archival,
  knowledge graph, procedural and episodic memory.
- [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md): how agent
  and recommendation quality is measured (section 3.11).
