# 16. The analysis pipeline (graph, debate, Judge, recommendation, tiers)

> One analysis turns a ticker (for example `AMD`) into a recommendation:
> BUY, HOLD or SELL, with a confidence number, a plain-English explanation
> of every adjustment, and (for BUY/SELL on a portfolio) concrete price
> "tiers" that can later become real trades. The work is a LangGraph graph
> in `services/ai-service/ai/graph/`: research agents collect evidence in
> parallel, a Bull and a Bear argue about it, a Judge decides, Reflection
> checks the result, and a deterministic recommendation engine applies
> penalties and caps. Position sizing then turns the verdict into tiers,
> and the user decides what to do with them. This chapter follows one
> analysis from start to finish and explains every rule on the way.

---

## 1. The big picture

An analysis is started by an API call (`POST /api/v1/analyses`), by the
market-open job, or by `scripts/run_analysis.sh`. It waits in a strictly
serial queue (`domains/analysis/queue.py`) and then runs
`ai/workflows/analysis_workflow.py::run_analysis_workflow`, which builds
the graph (`ai/graph/builder.py::build_analysis_graph`) and saves what the
graph returns.

The full flow, including what happens after the graph:

```
 POST /api/v1/analyses  (or /internal/analyses, or market-open)
        |
        v
 serial queue (one analysis at a time, 2700 s limit)  status: pending -> running
        |
        v
 _run_and_persist(): holdings, prior open recommendation, position weight
        |
        v
 +--------------------------- LangGraph graph ---------------------------+
 |                                                                       |
 | START -> episodic_memory_node      (L2 track record of this ticker)   |
 |       -> supervisor_node           (pick agents; KG + filing context) |
 |             | fan_out_to_selected_agents (Send[])                     |
 |             v                                                         |
 |   market  news  filing  technical  fundamental  risk  macro  portfolio|
 |              |                                                        |
 |              +-- route_after_news --> sentiment                       |
 |             (all of them)                                             |
 |             v                                                         |
 |        agent_join_node --should_continue_to_debate-->                 |
 |             |   |   |   \                                             |
 |             |   |   |    +-- END (wave incomplete, wait)              |
 |             |   |   +------- recommendation_node (retry brought       |
 |             |   |            nothing, keep the verdict)               |
 |             |   +----------- reflection_node (nothing to debate:      |
 |             |                all failed / below the minimum)          |
 |             v                                                         |
 |        bull_node   bear_node   (in parallel, Bear 0.5 s later)        |
 |             \        /                                                |
 |          debate_join_node     (one-time side repair after 60 s)       |
 |                 |                                                     |
 |             judge_node        (retries up to 4 times, 240 s)          |
 |                 | should_continue_debate_round                        |
 |                 +-- not converged and < 3 rounds --> bull + bear      |
 |                 v                                                     |
 |          reflection_node      (mechanical checks + citation review)   |
 |                 | route_after_reflection                              |
 |                 +-- retry agents (max 2 times) --> back to agents     |
 |                 v                                                     |
 |        recommendation_node    (no_verdict_reason or engine)           |
 |                 |                                                     |
 |                END                                                    |
 +-----------------------------------------------------------------------+
        |
        v
 save agent runs, debate, reflections            (always)
        |
   final verdict?  -- no --> analysis failed with failure_reason
        | yes                (judge_unavailable / no_evidence /
        v                     debate_unavailable / insufficient_evidence)
 position sizing (BUY/SELL with a portfolio) -> tier_generation_note
 tier replacement rules (close older unfilled tiers)
 save recommendation + tiers + episodic memory, status completed
        |
        v
 events: analysis.completed, recommendation.ready, tier.cancelled
 learning writes: procedural memory, RAG archival, KG extraction
        |
        v
 user decision: followed / ignored / partial / declined
 daily tier monitoring (17:00 New York) -> real trades   (chapter 19)
```

Three ideas carry the whole design:

1. **Every step is a real graph node.** Each agent, each debate side, the
   Judge and Reflection are separate nodes. This makes it possible to
   re-run just two agents after Reflection finds a problem, and to log
   each step separately.
2. **An LLM opinion is never the last word on a number.** The Judge
   proposes a confidence. Python code then lowers or caps it for every
   known weakness and writes down why.
3. **No fake verdicts.** If the Judge could not really judge, there is no
   recommendation at all. The analysis is marked `failed` with a reason.

---

## 2. Concepts you need

**LangGraph.** A Python library for building workflows as a graph. Each
*node* is an async function that reads a shared *state* object and returns
the fields it wants to change. *Edges* connect nodes. A *conditional edge*
is a function that looks at the state and decides where to go next.

**State and reducers.** The state here is `AnalysisState`
(`ai/graph/state.py`), a Pydantic model. When several nodes run at the
same time and write the same field, LangGraph needs a *reducer*: a
function that merges the two writes instead of letting the last one win.
`agent_outputs` uses `merge_agent_outputs` (merge by agent role), `debate`
uses `merge_debate_state` (append arguments, adopt round numbers only from
the Judge), and `reflection_history` uses `append_list`.

**Send (fan-out).** A conditional edge can return a list of `Send(node,
state)` objects. LangGraph then runs all those nodes in parallel in the
same *superstep* (one tick of the graph). This is how the Supervisor
starts only the agents it selected, and how Reflection re-runs only the
agents it wants to retry.

**Superstep and "wave".** A join node fires once for every group of
nodes that finish together. If eight agents finish together and Sentiment
finishes one step later, the join fires twice. The routing code must
handle this (see 3.4).

**Tier (LLM tier).** Not to be confused with recommendation tiers. The LLM
layer (chapter 14) has a cheap "Lite" tier (`LLMTier.CHEAP`) and a strong
tier (`LLMTier.HIGHEST_ACCURACY`). Data agents use Lite; Bull, Bear, Judge,
Reflection's citation review and position sizing use HIGHEST_ACCURACY.

**Availability failure.** The provider did not answer: HTTP 429 (rate
limit or quota) or HTTP 503 (server overloaded), or a timeout. In the code
this is `LLMRateLimitError` with its subclasses `LLMServerOverloadedError`
and `LLMRequestTimeoutError`, plus `LLMProviderTimeoutError` (any other
provider timeout, for example Anthropic after its request timeout)
(`ai/llm/base.py`). The pipeline treats these very differently from a
*content* failure (the model answered, but badly): an availability failure
is usually not retried at once, because retrying into the same overload
only makes it worse.

**Usable evidence.** `ai/graph/state.py::is_usable_evidence`: an agent
output counts only if it is `is_well_supported` and it has at least one
successful tool result of its own (`lacks_successful_tool_evidence`).
A knowledge-graph lookup does not count as a tool result here, because
its facts were written by earlier agents. Sentiment is the one exception:
it may build on News's search instead of its own. Every rule that counts
agents uses this one function.

**Recommendation tier.** One row in `ai.recommendation_tiers`: a side
(buy or sell), a target price and a quantity. A recommendation can have
several tiers ("buy 2 shares at 150, 3 more at 145"). All tiers of one
recommendation share one expiry time, `recommendations.window_expires_at`.

**Notional.** Price times quantity: the money value of a tier.

---

## 3. How it works in AlphaMind

### 3.1 Before the graph: context the workflow prepares

`ai/workflows/analysis_workflow.py::_run_and_persist` collects, best
effort (a failure means "no context", never a failed analysis):

| Context | Function | Used by |
|---|---|---|
| number of holdings, held quantity of this ticker | `_resolve_holdings_context` | Supervisor (Portfolio agent), Judge holding line |
| prior still-open recommendation (action, confidence, date, usable-agent count, and the Judge's own stored leaning and confidence when the row has them) | `_resolve_prior_recommendation` | Judge context line, Judge earlier-verdict line, reaffirmation cap |
| ticker's weight in the portfolio, in percent | `_resolve_position_weight` | Judge limit line, single-position limit |

The "prior open recommendation" is the newest recommendation for the same
portfolio and ticker whose window has not passed and that still has an
unfilled tier (3.14; `domains/analysis/repository.py::get_open_recommendation_for`).
The Judge sees only its action, confidence and date, never its reasoning
(`PriorRecommendation.judge_context_line`). Shown the full earlier
reasoning, the Judge restates it as if newly confirmed, run after run;
hiding the reasoning stops that chain.

The graph is invoked with `recursion_limit` 100
(`_GRAPH_RECURSION_LIMIT`), so a routing loop can never run forever.

### 3.2 episodic_memory_node: the track record

`ai/graph/nodes.py::episodic_memory_node` runs first. It calls
`ai/memory/agent_memory.py::retrieve_episodic_context(ticker, user_id=...)`,
which returns the newest `EPISODIC_CONTEXT_LIMIT` (default 3) earlier
completed calls on this ticker: the user's own plus system ones (a system
analysis sees system calls only). Each hit carries date, action,
confidence and, once the review job has scored it, the outcome and the
ticker's return over `AI_ACCURACY_EVALUATION_DAYS` (30). A database error
gives an empty list, never a failure. Setting `EPISODIC_CONTEXT_LIMIT` to
0 turns it off. Chapter 18 explains this memory in depth.

**Experiment isolation.** `ANALYSIS_EXPERIMENT_ISOLATION=true` (default
false) is a switch for cost and quality experiments that rerun the same
tickers: no analysis then reads what an earlier one wrote, or writes
what a later one reads. With it on, the episodic track record above is
not read (the node returns an empty list, whatever
`EPISODIC_CONTEXT_LIMIT` says); the prior open recommendation is not
loaded (no pending-recommendation context, no previous verdict for the
Judge); no procedural hints are read; knowledge-graph facts (all of
them extracted from archived agent reports) are not read by the ticker
and portfolio context, the `query_knowledge_graph` tool or the search
query expansion; the research-archive search tools drop prior
AI-written notes (`agent:*` passages) and keep every other passage
(seed, system and uploaded documents); and after the verdict no
procedural memory, archived note, knowledge-graph fact or episodic
memory is written. The archive search drops prior agent notes after the
retriever has already cut its results to RAG_TOP_K_RESULTS, so under
isolation a search can return fewer passages than usual, or none, even
when matching seed documents rank just below the cut. Every skipped
path goes through one helper,
`ai/experiment_isolation.py::isolation_skips`, which logs
`experiment_isolation.skipped` with its `path` (for example
`episodic_memory_read`). Market data, filings, filing records and
summaries, system documents and the news caches stay shared. Open
tiers of other analyses are ignored: position sizing and the portfolio
tool count no cash committed by them, and the analysis supersedes or
keeps no earlier tier. The analysis still writes its own
recommendation and tiers; decline them before the next group
(`make tiers-decline A="<ids>"`). While the switch is on, the
knowledge-graph reads of the RAG assistant return nothing too.

### 3.3 supervisor_node: agent selection and shared context

`supervisor_node` calls `ai/graph/nodes.py::select_agents`,
which has no LLM at all:

- `requested_agents`, when given, replaces the default set.
- Otherwise `DEFAULT_AGENTS_BY_TYPE[analysis_type]`; `full` means all eight
  data agents (Market, News, Sentiment, Filing, Technical, Fundamental,
  Risk, Macro).
- Portfolio is added when `holdings_count >= 1`, unless an explicit
  selection left it out.
- Every role not run gets an `AgentSkip` with a reason; it later appears
  in the confidence note as "Not run: ...".

The Supervisor node also computes context once and shares it, so agents
stay pure functions of the state. Every item below is an optional
enrichment: a failure is logged (`supervisor_node.procedural_hints_failed`,
`fresh_filing_check.sec_check_failed`,
`fresh_filing_check.timed_out`,
`supervisor_node.kg_context_failed`,
`supervisor_node.portfolio_kg_context_failed`) and gives "no context" for
that item, never a failed analysis.

- **L4 procedural hints** for each selected role
  (`ai/memory/agent_memory.py::retrieve_procedural_hints`). A hint lists only
  tool calls the model chose itself in an earlier high-scoring run; the
  calls code makes before the first turn (prefetched calls) are never
  stored, and a role with no such stored calls gets no hint. Only rows
  written by analyses of the same owner are read (chapter 18).
- **Fresh filing context** (`ai/graph/nodes.py::load_fresh_filing_context`)
  when Filing, Fundamental or Risk is selected: checks SEC EDGAR for
  10-K/10-Q filings that are not yet stored and ingests them
  (`ingest_new_filings`, chapter 18, 3.10). This runs inside
  `supervisor_node`, before any agent starts, so a ticker with new filings
  waits for their calls: per new filing one HIGHEST summary call and one
  HIGHEST records call (minutes for a large 10-K; a first-time ticker gets
  up to one 10-K and four 10-Qs, 3 s apart). Only the newest new filing's
  summary is pasted into the Filing, Fundamental and Risk prompts; pasting
  all of them (about 40k tokens for a new ticker) crowded the context and
  older text was read as current. The wait is capped at
  `FRESH_FILING_BUDGET_SECONDS` (600 s): one filing call at its worst
  case (2 attempts x 300 s) and the slowest normal first ingest (5
  filings, about 465 s) fit, and 2100 s of the 2700 s
  `ANALYSIS_TIMEOUT_SECONDS` stay for agents, debate and Judge. When the
  cap is reached, `fresh_filing_check.timed_out` is logged and this run
  gets no fresh-filing context; the shielded ingest keeps running in the
  background, and the next analysis of the same ticker joins it. Any
  failure gives no context, never a failed analysis.
- **Knowledge-graph context** (`ai/knowledge/kg_context.py::get_kg_context_for_ticker`)
  for Fundamental, Risk and Filing, limited to relations from system
  analyses and this user's own analyses. The log event
  `supervisor_node.kg_context_injected` says whether something was found
  (`found`, `char_count`).
- **Portfolio KG context** for the Portfolio agent: KG facts for the top
  20 holdings by current market value (`get_kg_context_for_portfolio`).

### 3.4 The data agents and the join

`fan_out_to_selected_agents` (`ai/graph/builder.py`) sends the state to each
selected agent node. One exception: when both News and Sentiment are
selected, Sentiment is held back. News's node has a conditional edge,
`route_after_news`, that dispatches Sentiment every time News finishes, so
Sentiment always builds on News's real summary. The same rule holds for
Reflection retries (3.10): when News is retried, Sentiment is never sent
directly, whether or not Sentiment is retried too; it runs after the new
News output exists (`ai/graph/builder.py::_sentiment_waits_for_news`).

Each agent node goes through `_run_agent_safely` (`ai/graph/nodes.py`):

- It sleeps a small, fixed stagger first (`_FAN_OUT_STAGGER_SECONDS` 0.65 s
  times the role's position in `_FAN_OUT_STAGGER_ORDER`; Market 0 s,
  Portfolio about 5.2 s), so nine first LLM calls do not hit the rate
  limiter in the same instant. This is the only start stagger.
- An exception becomes a `FAILED` `AgentOutput` with a `failure_kind`:
  `credit_exhausted` (Anthropic or OpenAI credit used up),
  `server_overload` (503 or timeout), `rate_limit` (429/quota),
  `non_english`, or `other` (`_failure_kind_of`).
- A successful but non-English summary is rejected and replaced by a
  `FAILED` output (`failure_kind="non_english"`).
- It stamps real `started_at`/`completed_at`, later saved as the agent
  run's duration.

All agent nodes lead to `agent_join_node`, which changes nothing. The
decision is made by `should_continue_to_debate`, in this order:

| Check | Route | Why |
|---|---|---|
| no agent output at all | `reflection_node` | nothing to debate |
| not every selected agent has reported | `END` for this wave | the join fires once per wave; only the last wave may start the debate |
| News was retried, Sentiment is selected, and Sentiment's output is older than News's | `END` (wait) | prevents a debate on stale Sentiment and a second, duplicate debate lineage; holds whether or not Sentiment itself was retried |
| every agent failed | `reflection_node` | skip Bull/Bear; their strong-model calls would be wasted |
| this join follows a Reflection retry, a real verdict exists, and every retried agent failed again for availability | `recommendation_node` | keep the verdict instead of losing it in another round (`_retry_wave_brought_nothing`) |
| fewer usable agents than `MIN_USABLE_AGENTS_FOR_DEBATE` | `reflection_node` | not enough evidence to debate (3.5) |
| otherwise | `Send` to `bull_node` and `bear_node` | start (or restart) the debate |

The "wave" check fixes a real bug: before it, a separate Sentiment wave
fired the join a second time and started a second, independent round 1.

### 3.5 The minimum usable agents rule

`MIN_USABLE_AGENTS_FOR_DEBATE` (default 4, `config.py`) is the smallest
number of agents with usable evidence that is worth a debate.
`AnalysisState.below_debate_evidence_minimum` caps the minimum at the
number of selected agents (a `technical` analysis selects 2, so it needs
2), and 0 turns the rule off.

Below the minimum, Bull, Bear and the Judge never run. Reflection still
runs, because it may retry agents that failed for a content reason; they
rejoin at `agent_join_node` and the check runs again. If the run still
ends below the minimum, `no_verdict_reason` returns
`insufficient_evidence` and the analysis fails with that reason.

Why: during a provider outage an analysis can end with 2 or 3 of 9
usable agents. A debate on that evidence spends the whole strong-model
chain and fails anyway, while on a normal day every completed analysis
has 9 of 9 usable agents (measured over three 15-ticker batches).

### 3.6 Bull and Bear

`bull_node` and `bear_node` run in parallel every round. Bear sleeps
`_DEBATE_SIDE_STAGGER_SECONDS` (0.5 s) first. Both call
`DebateSideAgent(side).make_argument` (`ai/agents/debate_common.py`, one
class; the sides differ only in their prompt), built on
`ai/agents/debate_common.py::generate_debate_argument`.

What a side receives:

- `format_evidence(state)`: one line per agent summary, then the
  code-built lines: the latest quarter from SEC XBRL (with the items below
  operating income, the year to date and the cash flow), the unusual items,
  the key records and the year-to-date / year-ago unusual-item lists from
  the filing records (chapter 15, section 3.17). If some agents carry a
  verification warning, a first line "UNVERIFIED CLAIMS FLAGGED this run
  by: ..." points at them. The debate never starts when every agent
  failed (`should_continue_to_debate` sends that state to Reflection, and
  the run ends with `no_evidence`), so at least one agent in the evidence
  did not fail.
- The other side's latest completed argument from an earlier round
  (`DebateState.previous_round_argument`: the newest non-superseded one
  with a round number up to the current one, so a side still sees its
  opponent after a one-side correction pass).
- The previous Reflection round's issues about this side's own claims
  (`format_prior_reflection_issues`): selected by the structured `role`
  and code, with a hard rule not to repeat them, and `not_engaged` issues
  listed separately as points to answer.

The prompt (`debate_common.py`, one per side) asks for 3-6 sentences (the
statements the rules require count towards them), an inline source tag
after each claim, like `[fundamental]`, and the shared rules on periods,
unusual items, overlapping items, free cash flow, legal amounts and source
wording (chapter 15, section 3.16). `extract_citations` turns the tags into
the argument's citations.

**Fabrication defense.** After the first draft one free mechanical check
runs: `find_ungrounded_figures` (a number in the draft has no matching
number in the evidence). The reference text for this check
(`format_grounding_reference`) leaves out agents that carry a verification
warning, so a fabricated number from a flagged agent cannot "ground" Bull's
repetition of it. With ungrounded figures the side writes once more with
the list of figures (`DEBATE_MAX_REGENERATIONS` = 1); a regenerated draft
that is still flagged is accepted and logged
(`<side>.final_draft_still_flagged`). If the regeneration call fails, the
first draft is kept (`<side>.regeneration_failed_kept_draft`). Reflection's strong-model review
still runs afterwards.

Why there is no LLM self-check: a Lite self-check of every draft (about
36 calls per batch) found more false than true problems, and across 77
analyses the checks never changed a verdict. The mechanical check plus
Reflection's review cover the same ground.

**A side that fails.** If the LLM call raises, the node stores a
`DebateArgument` with `available=False` and `unavailable_kind` set (for
example `server_overload`). Availability is structural, not hidden in the
text, so later code can count it.

### 3.7 debate_join_node and the side repair

`debate_join_node` waits for both sides. In the first round only (no
verdict yet, and only once per analysis, `debate_side_repair_used`), it
checks whether a side's argument is unavailable *for availability*
(`server_overload` or `rate_limit`). If so, it logs
`debate_join_node.side_repair_retry`, waits
`DEBATE_SIDE_REPAIR_DELAY_SECONDS` (60 s) and re-runs that side. The wait
is skipped when a model of the strong chain is free now
(`ai/llm/router.py::capacity_aware_delay`, chapter 14, 3.10; the log
line then has `delay_seconds=0` and the `free_model`), for example when
the other side has just been answered by a fallback model. If both sides
failed, both are re-run in parallel (Bear 0.5 s later). The repaired
argument is appended to the state.

**Placeholders in the stored debate.** The API returns each stored
argument as round, side, content and citations (API_SPEC, no
availability field), so a failed side's placeholder text "[<side>
argument unavailable (<kind>): ...]" would read as an argument. When a
later available argument of the same side and round replaced it
(`DebateState.transcript_arguments`), the placeholder is not stored
(`analysis_workflow.replaced_side_placeholders_dropped`, field `count`)
and is not shown in Reflection's transcript. A placeholder that was never
replaced stays: it is the record that the side was missing, matching the
0.30 confidence cap and its note. In the 2026-10-05 batch this removes
the placeholders of GOOGL, JPM and NVDA; MSFT's stays.

Why: a side's chain can answer 503 within seconds during a short
overload. Without a repair the Judge sees only one side, cannot converge
and spends a second round, and when both sides fail the analysis ends
with no verdict. The delay is 60 s because after 30 s only 2 of 6
measured repairs succeeded; it is skipped when a chain model is free,
because then the router has a model to send the retry to at once.

**A failed correction keeps the earlier argument.** The side repair runs
only in the first round. In a *single-side correction pass* (3.9:
Reflection asked only Bull, or only Bear, to fix its argument), the
retried side can also fail for availability (`server_overload` or
`rate_limit`). If that side already has an earlier available argument,
`bull_node` / `bear_node` keeps it: the node adds nothing to the debate
and logs `<side>_node.correction_failed_kept_previous` (fields
`failure_kind` and `kept_round_number`). The Judge then reads the earlier
argument as the side's latest one. Why: an unavailable placeholder would
make the Judge treat the side as missing, which caps the confidence at
0.30 (3.12). Example: on 2026-10-10 the Bull correction pass of an AMZN
analysis failed on both models; with a placeholder, a debate that had two
real sides would get the one-side cap of 0.30. A side with no earlier
available argument still gets the placeholder, as with any other
failure.

### 3.8 judge_node: the verdict

`judge_node` calls `ai/agents/judge.py::JudgeAgent.assess_round` on the
strong tier. The Judge reads the evidence, each side's *latest* argument
(`DebateState.latest_argument`), and context lines:

- the prior open recommendation (action, confidence, date only);
- the held quantity ("a SELL view is allowed whether or not the ticker is
  held");
- the position weight and the single-position limit
  (`ai/recommendation/engine.py::position_limit_line`);
- a valuation note when net margin is above operating margin
  (`trailing_pe_line`, from Fundamental's `key_facts`): then the trailing
  P/E is flattered by non-operating or one-off gains and must not be cited
  as "cheap". Only when the forward P/E is at least 1.1 x the trailing P/E
  (`_ONE_OFF_MATERIAL_PE_RATIO`; a material one-off; AMD at 160/41 gets no
  note, logged `judge.trailing_pe_note_skipped`), when both P/Es are in the
  `get_fundamentals` output; without them the margin test alone decides.
  The margins (`key_facts`) and the P/Es both come from the last
  successful `get_fundamentals` result for the analysed ticker; a result
  for another ticker (a peer comparison, chapter 15, 3.2) is never used;
- the profit-trend block (`profit_trend_line`), built only from structured
  data: (a) each latest-quarter metric (SEC XBRL, Fundamental's
  `raw_output["latest_quarter"]`) that fell more than
  `JUDGE_STATE_QUARTER_DECLINE_PCT` (15%) year over year, or swung to a
  loss, or whose loss widened that much, which the rationale must state
  with its quarter and the unusual-item driver the evidence gives; when
  XBRL lags the newest 10-Q/10-K, the title says so and tells the Judge to
  take the newest quarter from that filing's passages; (b) when the
  latest-quarter payload has year-to-date figures: the rationale must also
  state the year-to-date change of any metric whose quarter change it
  states, with drivers only as the filing states them; (c) when the Filing
  agent listed year-to-date or year-ago unusual items: a year-to-date or
  year-over-year change must say it includes them and give their amounts,
  with no self-computed adjusted figure unless both numbers are in the
  evidence. The block names the evidence lines by their titles instead of
  repeating their values. It is absent when none of (a)-(c) applies;
- the most recent earlier verdict (`previous_verdict_line`, below);
- the track record from episodic memory (`_track_record_block`), with a
  system-prompt clause: use it to calibrate, never as evidence.

The system prompt also gets a low-participation clause when fewer than
half of the agents produced usable evidence.

The Judge must answer with one JSON object:

```
{"converged": true|false, "leaning": "buy"|"hold"|"sell",
 "confidence": 0.0-1.0,
 "rationale": "2-8 sentences: required statements first, then the main reasons",
 "deciding_factor": "a few words",
 "change_since_previous": "..." or "",
 "change_basis": "new_evidence" | "reweighting" | "",
 "opposing_points": [{"point": "...", "response": "..."}]}   (at least two)
```

Only the first four fields are stored (`JudgeVerdict`). The others are
checked and logged (`judge.deciding_factor`, `judge.opposing_points`,
`judge.change_since_previous_missing`, `judge.change_basis_missing`,
`judge.opposing_points_missing`), never stored and never a reason for
another call.

**Unreadable JSON.** The answer is parsed with general rules only (code
fence removed; the first JSON object is read, text before or after it is
ignored, line breaks inside strings are accepted; each logged as
`judge.json_parsed_leniently`). An answer that still cannot be read is
logged with the parse error, its position, the text around it and the
full length (`judge.unparseable_response`), and the Judge is asked once
more in the same conversation for one valid JSON object, with the parse
error and its position (`judge.json_reask`, then
`judge.json_reask_recovered` or `judge.json_reask_failed`). Only when
that also fails does the verdict force another debate round (below). In
the stored batches 6 Judge answers (all gemini-3.6-flash, finish STOP)
were unreadable; each forced a whole Bull/Bear/Judge round (3 strong-model
calls, 12-39 s and new debate text) where the re-ask costs one Judge
call.

**Earlier verdict.** `previous_verdict` takes the most recent of the open
prior recommendation and the newest track-record call. The recommendation
row stores the Judge's own final-round leaning and confidence next to the
final, code-adjusted values (`ai.recommendations.judge_leaning`,
`judge_confidence`, migration `f1240a000001`). When the earlier analysis
has them, the line shows "your own final-round leaning ... with confidence
... (before code adjustments)", and a changed leaning OR a confidence move
of 0.05 or more needs `change_since_previous` and `change_basis`. For an
older row only the final values exist: the line says so, only a changed
leaning needs an explanation, and the Judge is told not to anchor to the
final confidence. Comparing the raw verdict with a penalised value (BUY
0.85 stored as 0.55) asked for explanations of moves that never happened.

**Figure check.** After a successful parse, the rationale's figures are
checked with `find_ungrounded_figures` against the grounding reference
plus the code-built lines of the user message (never the Bull and Bear
texts). With an ungrounded figure the Judge is asked ONCE, in the same
conversation, to restate each listed figure exactly as the evidence gives
it or remove it; a usable second answer (an English rationale, a
leaning and a confidence) is accepted whatever it says, and a remaining
problem is only logged. A failed or unusable rewrite keeps the
first answer (chapter 15, section 3.18).

Parsing rules: confidence is clamped to 0..1 (a non-number, `NaN` or
`Infinity` becomes 0.0, never 1.0), an unknown leaning becomes
HOLD (logged `judge.unknown_leaning`), and `converged` counts only a real `true` or the string `"true"`
(`_parse_converged`; a plain truthiness test would count the string
`"false"` as true).

The Judge is skipped (no LLM call) when both latest sides are
unavailable: an explicit failed verdict with `"debate_unavailable"`. It
never runs when every agent failed (that state goes to Reflection and ends
with `no_evidence`). An answer that is
unreadable even after the JSON re-ask gives `judge_failed=True,
retry_round=True`: the one Judge failure where another round may help. A non-English rationale is a failed Judge.
The rationale is stored as written: a regex that removes debate-process
sentences also deletes investment text, so only the prompt rule asks
the Judge to leave debate-process text out.

**Delayed retry.** If the call fails for availability
(`LLMRateLimitError`, which includes 429s, 503s and Google strong-model
timeouts, or `LLMProviderTimeoutError`), `judge_node` asks `_judge_retry_wait` how long to wait. It
reads the cooldowns of the strong chain
(`ai/llm/router.py::chain_capacity` over
`tier_model_chain(HIGHEST_ACCURACY)`, chapter 14, 3.10):

- a model has no 429, 503 or timeout cooldown: wait 0 s;
- every model is in a cooldown: wait until the earliest cooldown ends;
- every model is out of its daily quota: no retry.

The cooldowns exist only for Google models. With another provider
`chain_capacity` reports a free model, so the Judge retries without a
wait, and the side repair and the review retry always wait in full
(chapter 14, 3.10).

`judge_node` logs `judge_node.delayed_retry` (fields `error`,
`failure_kind`, `attempt` and `delay_seconds`), waits, runs the late side
repair below and calls the Judge again. It repeats this after each
availability failure, within a budget per analysis:
`JUDGE_RETRY_MAX_ATTEMPTS` (4) retries and `JUDGE_RETRY_BUDGET_SECONDS`
(240 s) of waiting in total. Each wait is capped by the rest of the
budget. The budget counts only waits: for a provider timeout each retry
costs the full call time, and only `JUDGE_RETRY_MAX_ATTEMPTS` bounds it.
The state keeps the count and the seconds used
(`judge_retry_attempts`, `judge_retry_seconds`). When the budget is used
up or no retry is possible, the error is raised. Why: in
run_20261011_130633 one fixed 120 s wait was too short for NVDA
(gemini-3.6-flash answered again about 4 minutes after the first
failure) and too long for UNH and BA (their cooldowns ended 40-100 s
before the wait did).

**Late side repair.** Before each Judge call (the first one and each one
after a retry wait), when a latest side is missing for capacity
(`server_overload` or `rate_limit`) and a strong-chain model is free now
(a model that has answered since counts, as its 503 cooldown is cleared),
`judge_node` regenerates the missing side(s) once per analysis
(`debate_late_side_repair_used`; `judge_node.late_side_repair` with
`sides` and `free_model`, then `judge_node.late_side_repaired` or
`judge_node.late_side_repair_failed`). Not in a single-side correction
pass. A repaired argument is added to the debate (the Judge reads it as
the side's latest argument; the placeholder is then dropped from the
stored debate, 3.7); a failed attempt adds nothing and the side stays
missing. Why: on 2026-10-05 MSFT's Bull failed on every model, its 60 s
repair failed too, and the Judge's retry then succeeded on
gemini-3.6-flash; the Bull stayed missing and the verdict was capped at
0.30. Replayed on that log: no chain model was free at the first Judge
call (08:36:02, every chain model in 503 cooldown), and gemini-3.6-flash
was free at the Judge's retry (08:38:15), so the Bull would have been
regenerated once before the Judge's second call. Inside the router the
Judge's call is an ordinary HIGHEST call: the primary model, then the
fallback model with up to 3 keys on a 503 (chapter 14, 3.10). If it
still fails, the verdict is
`judge_failed=True, failure_reason="judge_unavailable"`.

### 3.9 Rounds and convergence

`should_continue_debate_round` loops back to Bull and Bear only when the
verdict exists, is not converged, is not a hard failure (an unparseable
answer is allowed to loop), and fewer than `MAX_DEBATE_ROUNDS` (3)
*mutual* rounds are done. Otherwise it goes to Reflection.

There are two counters in `DebateState`:

- `round_number`: bumped by every Judge pass; used as an audit id.
- `mutual_rounds_completed`: bumped only for a real two-sided round. A
  *single-side correction pass* (Reflection asked only Bull, or only Bear,
  to fix its argument) does not count against the cap
  (`_is_single_side_correction_pass`). If the corrected side fails for
  availability, its earlier argument is kept (3.7).

A debate that ends without convergence still produces a verdict; it is
just penalized (3.12).

### 3.10 reflection_node: checking the result

`ai/agents/reflection.py::ReflectionAgent.check` merges two checks.

**Mechanical (no LLM).** For each agent output:

- failed for availability or non-English: *not* retried, recorded as
  "failed, not retried (rate_limit)" and so on;
- honest "no evidence" (`raw_output["no_evidence"]`): not retried, a retry
  would search the same empty source;
- failed for another reason, hit the iteration cap, had zero successful
  tool results, or is not well supported: retry candidate.

There are no cross-agent figure checks here. The own-evidence figure
check runs inside each data agent's loop (a hit costs one Lite
correction turn instead of a data-agent rerun plus a new debate round);
key-fact, margin-sanity and agent-derived-figure checks are not used
because they never fired, or a retry re-read the same cached data.

**Citation review (strong LLM, best effort).** The model sees the evidence
and the current (non-superseded) debate transcript and returns
`checked_claims` and structured `issues` (`{role, kind,
figure, claimed_period, evidence_quote, description}`, `kind` one of
`unsupported`, `wrong_period`, `stale`, `not_engaged`, `inconsistent`).
Each issue is routed to its `role` (a data agent, Bull or Bear). Code
checks: the quote must be in the agent evidence (for `not_engaged`, in
the other side's arguments from rounds earlier than the criticised
side's latest round; chapter 15), otherwise the issue is dropped and logged
(`reflection.issue_quote_not_grounded`); an `unsupported` Bull/Bear issue
whose figure *is* in the trusted evidence is dropped (a known false
positive, `reflection.mechanical_false_positive_discarded`). This
filter also drops some real reasoning errors built on a real figure (KO,
BA, GOOGL on 2026-10-19). It stays: every dropped issue in three replayed
batches had kind `unsupported`, so no structural field tells "figure not
found" from "wrong reasoning", and keeping all of them would have added
4 debate rounds and up to 5 penalties in one batch, with most of the
re-admitted issues in another batch invalid (FIX_IMPL4). Routing
reads only the structured `role`, never words in the text. A wrongly
shaped answer is treated as "review did not run"; a single malformed
issue is dropped on its own. Every issue (mechanical ones too) is kept as a
`ReflectionIssue` with the `ISSUE_*` code of its check, and
`_classify_reflection_causes` names the confidence-note causes from the
codes and kinds, not from the wording.

The review is skipped, with a stated reason, when there are no outputs,
when there is no verdict because the run is below the debate minimum,
when the Judge failed for good
(nothing can change), or when no agent has usable evidence. If the call
fails for availability (the Judge's error set above, timeouts included),
it waits `REFLECTION_REVIEW_DELAYED_RETRY_SECONDS` (60 s; skipped when a
strong-chain model is free now) and tries once more (0 turns this off).
A skipped review after a timeout gives the reason "the LLM provider timed
out".

**Capacity-aware waits, replayed.** Of the 8 fixed waits in the stored
batches (side repair 4, citation-review retry 4), a strong-chain model
was free when 5 started (2026-10-05: AMZN review, GOOGL, JPM, MSFT and
NVDA side repairs): 300 s of sleep that the capacity check skips. The
other 3 (JPM and TSLA review on 2026-10-05; UNH review on 2026-10-03) had
every chain model in a 503, timeout or daily-quota cooldown and keep
their wait. The cooldowns were rebuilt from each batch's merged log
(`google.server_error_giving_up_early` cooldowns, cleared by
`google.completion_succeeded`, `google.model_timeout_cooldown_recorded`,
HIGHEST `google.quota_exceeded_*`).

**The verdict.** `verified` is true only when there are no retry
candidates, at least one usable agent, and the LLM review really ran. Each
round is saved with an extra line `REVIEW_META: llm_review_ran=...;
usable_agents=N/M` (`ReflectionVerdict.review_meta_line`).

**Which retries really happen** (`nodes.py::reflection_node`):

1. At most `MAX_REFLECTION_RETRIES` (2) retry rounds per analysis.
2. Data-agent candidates win: if any data agent is retried, Bull/Bear are
   dropped from the list, because the retry starts a fresh debate anyway.
3. `_drop_no_progress_retries`: an agent whose current output already came
   from a retry with the same feedback is not retried again (a KO run once
   retried News three rounds for the same issue).
4. A single-side Bull/Bear correction is allowed once per mutual round
   (`correction_used_for_round`).
   This cap is applied before the capacity gate, so a side retry that the
   cap drops never waits for capacity or gets a capacity note.
5. `_rate_limit_gate_for_retry` (Google only) looks at the whole model
   chain the retry would use (`router.tier_model_chain`: the CHEAP chain
   for data agents, the HIGHEST chain for Bull/Bear), not only the
   primary model:
   - If some chain model has quota and no cooldown, the retry goes ahead
     at once; the router skips a busy primary model itself and goes to
     the fallback model.
   - If every chain model is out of its daily quota
     (`google_model_pool_exhausted`, the same check the router uses), the
     retry is dropped (`reflection_node.rate_limit_skip`), with the note
     "retry skipped this round (provider capacity)".
   - Otherwise every usable chain model is near its per-minute limit or
     has a 503 or request-timeout marker (`ai/llm/router.py`); the node
     waits for the shortest of those waits,
     at most 20 s (`_RATE_LIMIT_RETRY_BACKOFF_SECONDS`,
     `reflection_node.rate_limit_backoff`), and adds the note "retry
     delayed before dispatch (provider capacity)".
6. A data-agent retry marks every earlier debate round as superseded
   (`superseded_through_round`); the next Bull/Bear round starts fresh.

`route_after_reflection` then sends the retried roles back to their nodes
(they rejoin at `agent_join_node`), or goes to `recommendation_node`.
When News is among the retried roles, Sentiment is not sent from here;
`route_after_news` sends it after the retried News finishes (3.4). If
the Judge failed for good, all retries are dropped: re-running agents
would only lead to another Judge call against the same unavailable model.

### 3.11 recommendation_node and no-verdict reasons

`recommendation_node` first calls
`ai/recommendation/engine.py::no_verdict_reason`:

| Reason | When |
|---|---|
| `llm_credit_exhausted` | any of the reasons below, when an agent, a debate side or the Judge failed because the Anthropic or OpenAI API credit is used up (chapter 14, section 3.16) |
| `no_evidence` | no verdict and no agent output, or every agent failed |
| `insufficient_evidence` | no verdict and fewer usable agents than the minimum |
| `debate_unavailable` | the Judge was skipped because both sides were unavailable |
| `judge_unavailable` | the Judge call failed (provider error, unparseable at the cap, non-English), or no verdict for another reason |

The `debate_unavailable` and `judge_unavailable` rows apply only when no
valid verdict exists in this analysis. If a later Judge pass fails (a
correction pass, a re-debate after a retry wave, round 2 or 3) after an
earlier pass gave a valid verdict, the node keeps the newest valid verdict
(`DebateState.latest_valid_verdict`), in the same spirit as "a retry wave
that brought nothing keeps the verdict" (3.4). The confidence note then
says "The last Judge pass failed (...), so this recommendation keeps the
Judge's earlier valid verdict from round N."

With a reason, the node sets `final_recommendation=None`. The workflow
keeps the agent runs, debate and reflections for inspection, writes no
recommendation, no tiers, no episodic memory, no procedural memory and no
archive notes, and calls `mark_analysis_failed` with the reason. These
five values are listed in `shared/enums.py::AnalysisFailureReason`
together with the queue's own reasons (`timeout`, `cancelled`,
`service_restart`, `worker_error`, `workflow_error`).

Why no fake HOLD: a failed Judge saved as HOLD with confidence 0.0
looks like a real low-confidence hold. It would feed the review scores,
the episodic memory and the portfolio's "latest recommendation".

### 3.12 The recommendation engine: penalties and caps

`build_final_recommendation` starts from the leaning and confidence of
the verdict the node keeps (the newest valid one, 3.11) and applies, in
this order:

| Step | Rule | Constant |
|---|---|---|
| 1 | the kept verdict did not converge: subtract 0.15 (a later failed Judge pass does not change this) | `_UNCONVERGED_DEBATE_PENALTY` |
| 2 | last Reflection round not verified: no deduction; the note lists the open issues (below) | none |
| 3a | usable-agent ratio below 0.5: cap at 0.3 | `_LOW_PARTICIPATION_CONFIDENCE_CAP` |
| 3b | ratio from 0.5 to below 0.8: subtract 0.15 | `_PARTIAL_PARTICIPATION_PENALTY` |
| 4 | BUY while the ticker is at or above `MAX_POSITION_WEIGHT_PCT` (50) of net value: action becomes HOLD | `engine.py` |
| 5 | exactly one side's final argument unavailable, when the kept verdict is the last Judge pass: cap at 0.3, and `debate_converged` is reported as False (skipped when an earlier verdict is kept) | `_ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP` |
| 6 | same action as the prior open recommendation, with fewer usable agents than the prior had: confidence may not exceed the prior's (final values on both sides: this cap is about what was issued) | reaffirmation cap |

**Why an unverified Reflection costs no confidence.** Reflection's last
pass runs after the Judge, so the Judge never saw the issues it raises,
and the issue often sits on a side that the correction cap or the retry
cap no longer lets answer. A flat 0.15 for that case was tried. Across five
Claude batches it was applied 32 times. Only once did it rest on a real
figure error that the Judge relied on, and even then it lowered a BUY for
an error in the Bear's case. Whether it fired depended on the kind label
Reflection chose (0 penalties in one batch, 5 in the next, on the same
code). It also dropped BUYs below the tier minimum. The open issues stay
visible in the note and in the stored reflections. A data agent that
fails or is not well supported still counts through steps 3a/3b.

The result is rounded to 2 decimals (without it 0.70 - 0.15 was served as
0.5499999999999999). The Judge's own leaning and confidence are kept next
to the final values (`judge_leaning`, `judge_confidence`) and saved on the
recommendation row for the next run's comparison. Subtractions never go
below 0. Caps are applied as `min(...)`, so no order
of earlier steps can lift a capped value again. All magnitudes are
fixed constants, not yet calibrated against outcomes (an open item,
[00](00-index.md) section 8).

What each cap prevents (each seen in real runs without it): a Judge
confidence of 0.85 with 7 of 9 agents lost to a 429 storm (step 3a); a
bear-only debate after Bull failed with a 503 still coming out BUY 0.85
(step 5); an earlier BUY of 0.60 reaffirmed at 0.70 on fewer agents
(step 6); and a HOLD ("48.8% of the portfolio already") followed hours
later by a BUY on the same data, with tiers that would push the position
above 60% (step 4).

**`confidence_adjustment_note`.** Every adjustment explains itself. The
note starts with `AnalysisState.confidence_adjustment_note()`:

- "Not run: role (reason)."
- "Limited evidence despite retry attempts: ...", "Failed, not retried:
  market (server_overload).", "Limited evidence, not retried: ...";
- the prior recommendation provenance line;
- "Confidence capped: only N of M selected agents produced usable
  evidence this run ...";
- "Unresolved verification warning(s) from: ...";
- "Bull/Bear debate did not converge within the round cap; confidence
  reflects that.";
- "Reflection could not fully verify the debate content ..." with causes
  from `_classify_reflection_causes`, ending "confidence is not reduced
  for this.";
  or an informational note when an earlier round found issues that a
  later round verified.

Then the engine appends the sentences of steps 4-6 (for example "BUY
changed to HOLD: MSFT is already 52.3% of the portfolio's net value ...").

Every step that lowers the confidence also adds a fixed sentence with the
amount, after the state's notes, so the amounts add up to the difference
between the Judge's confidence and the stored one:
"Confidence lowered by 0.15 because the Bull/Bear debate did not
converge.", "Confidence lowered by 0.15
because only 7 of 9 selected agents produced usable evidence." (also for
the 0.30 cap, with the actual amount); the one-side cap and the
reaffirmation cap add "(lowered by D)" to their sentences. COST lost 0.30
in two steps while the note named only one; now both appear. A step that
does not lower the value adds nothing. Each sentence states its amount
only, never an intermediate value.

The note then ends with code-built sentences about the stored value
(`_stored_confidence_sentence`): "Stored confidence is 0.27, against the
Judge's 0.57, because of the code adjustments listed in this note." when
the two differ, and, when the Judge was shown its own earlier confidence
(before code adjustments) and the recommendation published then had a
different value: "The Judge compared its confidence with its own earlier
0.78 from <date>, before code adjustments; the recommendation published
then had 0.63, and this one is stored at 0.74." The Judge's rationale is
not changed. Why: NVDA's rationale said confidence was "a little lower
than the 0.78" while the published value rose from 0.63 to 0.74; the
comparison with the Judge's own figure is by design
(`previous_verdict_line`), so the note now states both numbers.

Last comes the backup-model sentence (`_backup_model_sentence`), only
when at least one Bull, Bear, Judge or Reflection call of the analysis
was answered by the provider's fallback model (`fallback_calls` on each
`DebateArgument`, `JudgeVerdict` and `ReflectionVerdict`, counted by the
router, chapter 14 section 3.10): "Backup model: 4 Bull, Bear, Judge or
reflection call(s) in this analysis were answered by the provider's
fallback model, not its primary model. The final Judge verdict was
written by gemini-3.6-flash." (`JudgeVerdict.model` of the verdict the
recommendation uses: the model that wrote its stored rationale, which is
the rewrite's model when the figure rewrite was accepted). It changes no
value. Why: in one Gemini
batch 15 of 19 Judges ran on the fallback model and nothing told the
reader.

**`debate_converged`** on the saved recommendation is true only when the
kept verdict (3.11) converged and, when that verdict is from the last
Judge pass, no side was missing in that round.

### 3.13 Position sizing: from verdict to tiers

After a verdict, `_save_recommendation_and_tiers` (analysis workflow) runs
position sizing for BUY or SELL when the analysis has a portfolio. HOLD,
or no portfolio, means no tiers. The core is
`ai/recommendation/position_sizing.py::generate_position_sizing`. It
always returns `(result, note)`, and the note is saved in
`recommendations.tier_generation_note`. When tiers are generated the note
always includes a code-built "Tier sizing (computed in code)" sentence: total notional, its % of the portfolio's net value, each
tier's notional and %, the share of the BUY budget used, and the position
weight if every tier fills (SELL: shares sold of those held).

**Order of checks for a BUY:**

1. `confidence < MIN_BUY_TIER_CONFIDENCE` (0.5): no tiers, no lookups, no
   LLM call; a plain note. (A Bull-only debate can give BUY 0.30, which
   would otherwise reserve real cash.) The ticker's earlier unfilled BUY
   tiers are then closed as superseded (below).
2. Live price (from the market-data provider) and cash balance (from
   core-service). If either lookup fails, there are no tiers, and the
   note says which lookup failed.
3. **Budget** = cash minus `committed_buy_cash`: the notional of this
   portfolio's *other* still-open BUY tiers (pending acceptance or accepted
   and waiting, window not passed), excluding this ticker, because a new
   BUY replaces this ticker's earlier BUY tiers
   (`RecommendationTierRepository.committed_buy_cash`). A pending tier of
   a system analysis (`user_id` null) older than
   `SYSTEM_TIER_ACCEPTANCE_HOURS` does not count. Without this
   rule, two open recommendations could each plan to spend about 98% of
   the cash. When the note is a `BudgetBlockedNote`, the workflow adds
   "The committed cash is held by open BUY tiers on: <TICKER> (issued
   <YYYY-MM-DD>), ..." (from `committed_buy_holders`; the date is the New
   York market date of the holder's recommendation). It names tickers and
   dates only, never analysis ids or amounts, because a system analysis
   on an organization-shared portfolio is readable by every member. The
   admin CLI `analysis_cli committed` prints the ids and amounts.
4. **Minimum tier size** = `max(TIER_MIN_NOTIONAL_USD,
   TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION x net value)`, so `max(100, 1% of
   net value)` by default (`min_tier_notional`). A budget above 0 but below
   it, or a budget of 0 or less, gives a `BudgetBlockedNote`. (Without a
   minimum, a tier of 0.0039 shares worth about 1.34 USD is possible.)
5. **Single-position room** = 50% of net value minus the current position
   value. If the room is below the minimum tier size: no tiers. If the
   room is smaller than the budget, the budget is cut to the room.
   There is no per-recommendation BUY cap (an owner decision): a BUY may use
   the whole remaining BUY budget; the single-position limit, the minimum
   tier size and `MIN_BUY_TIER_CONFIDENCE` still apply. The room needs the
   net value. When a holding has no live price but the cash is known, the
   room is computed from the priced holdings plus cash, with the analysed
   ticker always counted at the price this sizing run looked up
   (`room_net_value`). Other unpriced holdings are left out, so this is a
   lower bound of the net value, and the limit can only be stricter; a
   "no tiers" note then says which ticker
   had no live price. The engine's BUY-to-HOLD switch and the Judge's
   limit line need the true weight, so they stay off in that case, and
   the tier note says that the share of the portfolio's net value is
   unavailable. Only when the cash or holdings lookup failed is the room
   not computed and the limit not applied in that run. Consequence to
   explain to a reviewer: in a batch, the first BUYs can reserve almost
   all cash (two BUYs can reserve about 99%), so later BUYs get a
   `BudgetBlockedNote` and no tiers; tier size depends on run order. This
   is accepted: batch tiers are test data and are declined. A real user
   sees the split in the Portfolio agent's `buy_budget` and in the tier
   note.
6. One strong-model call with the price, cash, committed cash, budget,
   minimum size, position and weight. The model returns `{"window_days":
   int, "tiers": [{"target_price", "quantity"}, ...]}`. The prompt asks
   for a window of at most `TIER_WINDOW_DAYS_MAX` (30) days.

**For a SELL:** the holdings are required. Not held: no tiers, note
"bearish view; no position held in X (SELL tiers are only generated for a
held position; short selling is never proposed)". The SELL view itself
stays valid.

**Validation of the answer** (`_validated_sizing`), the same for the main
and the fallback model:

- prices and quantities are parsed as `Decimal` through `str()`, never
  float, so `164.23` stays exactly `164.23`;
- tiers with a non-positive or non-finite (`NaN`, `Infinity`) price or
  quantity are dropped; the other tiers of the answer are kept;
- BUY tiers below the minimum size are dropped;
- BUY total above the budget: if the excess is at most 0.5% of the budget
  (`_BUDGET_OVERSHOOT_TOLERANCE`), the last tier's quantity is trimmed
  (rounded down to 0.0001) and a note explains it. The trimmed tier must
  still meet the minimum tier size. Otherwise there are no tiers
  (a `BudgetBlockedNote`, unless the budget was cut by the position limit);
- SELL total quantity above the held quantity: no tiers;
- `window_days` must be a whole number (`14` or `14.0`; a boolean, a
  string or `14.5` means the answer is missing a required field, so no
  tiers); it is kept between 1 and `TIER_WINDOW_DAYS_MAX` (30); a
  larger answer is capped (log `position_sizing.window_days_clamped` with
  `proposed` and `used`), so one answer cannot keep cash reserved for
  months.

**Lite fallback.** If the strong call fails with an `LLMRequestError`
(timeout, overload, rate limit) or its answer is rejected as non-English
(`LLMNonEnglishOutputError`), the same prompt goes once to the Lite
tier. Its tiers pass the same checks, and the note says "Entry/exit tiers
were computed by the fallback model (...)". (Without it, one strong-model
timeout would cost every tier.)

**Keeping or closing earlier tiers.** `BudgetBlockedNote` is a `str`
subclass. Only when a BUY gets no
tiers purely for a budget reason (no budget left, budget below the minimum
tier, or a proposal over budget) does the workflow *keep* the ticker's
earlier unfilled BUY tiers, adding "Kept N earlier unfilled BUY tier(s)
...". Every other BUY without tiers, including one whose confidence is
below `MIN_BUY_TIER_CONFIDENCE`, goes through the normal invalidation
path: the earlier unfilled BUY tiers are closed as `superseded` ("Closed
N earlier unfilled BUY tier(s) of recommendation ... as superseded:
replaced by a newer BUY recommendation."), with `tier.cancelled` events.
Before this, KO's BUY at 0.45 kept three tiers sized at the earlier 0.62
(17.9% of net value). A HOLD or SELL already closed them.
Open BUY tiers of other tickers still reduce a new BUY's budget across
analyses and batches (`committed_buy_cash`); that is intended: cash
reserved by an open tier is not free. N comes from
`RecommendationTierRepository.unfilled_buy_tier_count`, which counts only
tiers whose recommendation's `window_expires_at` is still in the future
and, for a pending tier of a system analysis, whose recommendation is
younger than `SYSTEM_TIER_ACCEPTANCE_HOURS`.
A tier whose window or acceptance deadline has passed can never fill, so
it is not counted as kept, even if the daily job has not marked it
`expired` yet. (Without
this rule, a proposal slightly over budget would get no tiers and still
close good earlier tiers, leaving a BUY with no entry plan.) Any other no-tier note (price lookup failed, model failed,
position limit) does not keep them.

When tiers are saved (`save_recommendation_tiers`), the recommendation's
`window_expires_at` is set once to now plus `window_days`.

### 3.14 Tier replacement and invalidation

Unless the keep rule applies, every saved recommendation that has a
portfolio (HOLD included) runs `RecommendationTierRepository.invalidate_unfilled_tiers_for_new_recommendation`
for its portfolio and ticker. "Unfilled" means `pending_acceptance` or
`accepted_awaiting_trigger`, and only tiers whose recommendation's
`window_expires_at` is still in the future; a pending tier of a system
analysis must also be younger than `SYSTEM_TIER_ACCEPTANCE_HOURS`. This
rule lives in one place,
`domains/analysis/repository.py::unfilled_tier_condition()`, and is
shared by the budget, the keep rule, this function and
`AnalysisRepository.get_open_recommendation_for`. A tier past its window
or its acceptance deadline that the daily tier-monitoring job has not
swept yet is left alone here; that job marks it `expired`, not
`superseded` (chapter 19, section 3.3). A
"set" is one older recommendation's tiers of one side. Two sets *cross* when any BUY price is at or above any SELL price
(`bands_cross`).

| New recommendation | Older BUY sets | Older SELL sets |
|---|---|---|
| HOLD | closed | closed |
| BUY (with or without tiers) | closed (replaced) | closed only if they cross the new BUY prices |
| SELL with SELL tiers | closed only if they cross the new SELL prices | closed (replaced) |
| SELL without tiers | closed (bearish view, nothing to compare) | closed (replaced) |

Closed tiers get the status `superseded`. The UPDATE re-checks the status,
so a tier that triggered in the meantime is never overwritten. Each closed
set adds a sentence to the new recommendation's `tier_generation_note`
("Closed 2 earlier unfilled BUY tier(s) of recommendation ... as
superseded: replaced by a newer BUY recommendation."), and after commit a
`tier.cancelled` event with reason `superseded` is published per tier.

The same function runs when a user *accepts* a recommendation (3.16), with
`source="an accepted"` and the accepted recommendation excluded.

All of this, the recommendation row, the tiers, the episodic memory row
and the `completed` status, is committed in one transaction. If the queue
already failed the analysis (timeout, restart), the completion is rolled
back and nothing is saved (`analysis_workflow.completion_discarded_not_active`).

### 3.15 Tier statuses

`shared/enums.py::TierStatus`:

```
              (created)
                  |
         pending_acceptance --------------------+------------+
           |      |      \                      |            |
 "followed"|  "declined"  window passed, or   newer rec.   portfolio
           |      |       system analysis    (rules 3.14)  deleted
           |      |       not accepted in
           |      |       SYSTEM_TIER_ACCEPTANCE_HOURS
           v      |       (daily job)
 accepted_awaiting_trigger    |                 |            |
   |    |    |     \          v                 v            v
   |    |    |      +----> declined          superseded    expired
   |    |    +-- window passed ----------------------------> expired
   |    +---- core refused the order (4xx) --> execution_failed
   +---- price crossed (daily job, order recorded) --> triggered
```

| Status | Meaning | Terminal? |
|---|---|---|
| `pending_acceptance` | created, waiting for the user | no |
| `accepted_awaiting_trigger` | user followed it; the daily job may execute it | no |
| `triggered` | the daily bar crossed the target; the trade was recorded in core-service | yes |
| `expired` | window passed, system tier not accepted in time, or portfolio deleted | yes |
| `superseded` | a newer or accepted recommendation closed it | yes |
| `declined` | the user declined (or changed a decision to ignored) | yes |
| `execution_failed` | core-service refused the trade for a business reason | yes |

Only the two non-terminal statuses reserve cash and are checked by the
daily tier-monitoring job (chapter 19). A pending or accepted tier whose
window (`window_expires_at`) has passed, and a pending tier of a system
analysis past its acceptance deadline, reserve no cash, and API reads
already show them as `expired` before the daily job sweeps them
(`domains/analysis/service.py::_tier_reads`).

### 3.16 Decisions: followed, ignored, partial, declined

`POST /api/v1/analyses/{analysis_id}/decisions` (route in
`domains/analysis/api.py`, logic in `domains/decision/service.py::DecisionService.record_decision`)
records what the user did. Body: `action_taken` (one of `followed`,
`ignored`, `partial`, `declined`), optional `portfolio_id`, optional
`notes` (max 2000 characters).

**Who may decide.** The caller needs the `analysis:create` permission (no
guests), must be able to see the analysis (`ensure_analysis_access`), and
must be the **owner** of the analysis's portfolio
(`ensure_portfolio_owner`): a decision can lead to trades, so an
organization member who can only read the portfolio gets 403.

| Action | Effect on tiers |
|---|---|
| `followed` | all `pending_acceptance` tiers become `accepted_awaiting_trigger` in one `UPDATE ... RETURNING`; `accepted_at` is set; other recommendations' unfilled tiers are closed by the rules of 3.14. Refused (422, "acceptance window has already expired") if the window already passed, or if it is a system analysis whose tiers passed `SYSTEM_TIER_ACCEPTANCE_HOURS`. If no tier is still `pending_acceptance` but at least one is `accepted_awaiting_trigger` or `triggered` (for example followed, then partial, then followed again), nothing changes on the tiers and the decision is stored as `followed`; this check runs before the window check. Otherwise refused (422, "no tier still awaiting acceptance") if the `UPDATE` changes no row; nothing is stored then (race with the expiry job or another supersede). |
| `ignored` (first time) | nothing; tiers stay pending and expire with the window |
| `partial` | nothing |
| `declined` | every pending or accepted-but-not-triggered tier becomes `declined` |
| `ignored` replacing an earlier decision | the same as `declined` |

One decision per user and analysis: posting again updates it, deletes its
old outcome rows (they are scored again) and its old LLM reviews (they
described the old action), logs `decision.updated`
(with `outcomes_removed` and `reviews_removed`), and publishes
`decision.recorded`.
Tier changes use explicit SQL `UPDATE` statements
(`RecommendationTierRepository.accept_pending` and `.decline_unfilled`,
ORM `update()`), not the ORM relationship: through the relationship, its
`delete-orphan` cascade can delete every tier of a recommendation during
a decline. The decision service holds no SQL.

Decisions are later scored by the review job (chapter 19), and
`GET /api/v1/decisions`, `/{id}/outcome` and `/{id}/review` serve only the
caller's own decisions.

### 3.17 What the workflow writes, and when

| Data | Table(s) | Written when |
|---|---|---|
| agent runs and outputs | `ai.agent_runs`, `ai.agent_results` | always |
| debate transcript | `ai.debate_sessions`, `ai.debate_arguments` | if any argument |
| reflection rounds | `ai.reflections` | always |
| recommendation (final action and confidence, plus the Judge's own `judge_leaning` and `judge_confidence`), tiers | `ai.recommendations`, `ai.recommendation_tiers` | only with a verdict |
| episodic memory | `ai.episodic_memories` | only with a verdict |
| procedural memory, archived notes, KG | `ai.agent_memories`, `ai.agent_reports`, `ai.kg_*` | only after `completed`, after the events |

The learning writes run last because they take several seconds (embedding
and KG extraction calls); if they ran first, clients that stop
listening when the status reads `completed` would miss the
`analysis.completed` event.

---

## 4. Configuration

All settings are in `services/ai-service/config.py` (class
`AIServiceSettings`). Constants that are not settings are marked "code".

| Setting | Default | Meaning |
|---|---|---|
| `ANALYSIS_TIMEOUT_SECONDS` | 2700.0 | wall-clock limit of one analysis in the serial queue |
| `ANALYSIS_MAX_ACTIVE_PER_USER` | 3 | pending/running analyses per user (429 above) |
| `ANALYSIS_QUEUE_MAX_PENDING` | 50 | pending analyses in total (503 above) |
| `EPISODIC_CONTEXT_LIMIT` | 3 | earlier calls shown to the Judge; 0 = off |
| `ANALYSIS_EXPERIMENT_ISOLATION` | false | experiment switch: no analysis reads or writes prior-analysis context, memory, archived notes or knowledge-graph facts (section 3.2) |
| `AI_ACCURACY_EVALUATION_DAYS` | 30 | review window named in the track record |
| `MIN_USABLE_AGENTS_FOR_DEBATE` | 4 | usable agents needed for a debate; 0 = off |
| `DEBATE_SIDE_REPAIR_DELAY_SECONDS` | 60.0 | pause before re-running an unavailable first-round side; skipped when a strong-chain model is free |
| `JUDGE_RETRY_BUDGET_SECONDS` | 240.0 | total wait of the Judge's availability retries in one analysis; each wait lasts until the earliest strong-chain cooldown ends (0 s when a model is free) |
| `JUDGE_RETRY_MAX_ATTEMPTS` | 4 | most availability retries of the Judge in one analysis |
| `REFLECTION_REVIEW_DELAYED_RETRY_SECONDS` | 60.0 | pause before the citation review's retry; skipped when a strong-chain model is free; 0 = off |
| `MAX_POSITION_WEIGHT_PCT` | 50.0 | single-position limit, percent of net value |
| `JUDGE_STATE_QUARTER_DECLINE_PCT` | 15.0 | a latest-quarter decline beyond this (or a swing to a loss) is named in the Judge's user message ("state this change") |
| `MIN_BUY_TIER_CONFIDENCE` | 0.5 | a BUY below this gets no tiers |
| `TIER_MIN_NOTIONAL_USD` | 100.0 | smallest BUY tier in USD |
| `TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION` | 0.01 | smallest BUY tier as a share of net value |
| `TIER_WINDOW_DAYS_MAX` | 30 | longest tier validity window; a larger `window_days` answer is capped |
| `SYSTEM_TIER_ACCEPTANCE_HOURS` | 72 | how long a `pending_acceptance` tier of a system analysis (`user_id` null) may wait for acceptance; after that it reserves no cash, is shown as `expired`, cannot be accepted, and the daily job marks it `expired`. User-owned analyses keep their own window. Counted in calendar hours (an owner decision); 72 h lets a system analysis issued on a Friday evening still be accepted at Monday's open |
| code: `MAX_DEBATE_ROUNDS` (`ai/graph/state.py`) | 3 | mutual debate rounds |
| code: `MAX_REFLECTION_RETRIES` (`ai/graph/state.py`) | 2 | Reflection retry rounds |
| code: `DEBATE_MAX_REGENERATIONS` (`debate_common.py`) | 1 | rewrites of one argument (only for ungrounded figures) |
| code: `_JUDGE_MAX_TOKENS` (`judge.py`) | 3072 | Judge output budget |
| code: `CONFIDENCE_CHANGE_TO_EXPLAIN` (`judge.py`) | 0.05 | confidence move against the Judge's own earlier verdict that needs a stated change |
| code: `_ONE_OFF_MATERIAL_PE_RATIO` (`judge.py`) | 1.10 | forward / trailing P/E needed for the valuation note |
| code: `_DEBATE_SIDE_STAGGER_SECONDS` (`nodes.py`) | 0.5 | Bear starts this much later than Bull |
| code: `FRESH_FILING_BUDGET_SECONDS` (`nodes.py`) | 600.0 | longest wait for the inline fresh-filing ingest in `supervisor_node` (3.3) |
| code: `_GRAPH_RECURSION_LIMIT` (`analysis_workflow.py`) | 100 | LangGraph recursion limit |
| code: penalties/caps (`engine.py`) | 0.15 / 0.3 / 0.15 / 0.3 | see 3.12 |
| code: participation thresholds (`state.py`) | 0.5 / 0.8 | low / partial participation |
| code: `_BUDGET_OVERSHOOT_TOLERANCE` (`position_sizing.py`) | 0.005 | BUY overshoot that is trimmed |
| code: `_RATE_LIMIT_RETRY_BACKOFF_SECONDS` (`nodes.py`) | 20.0 | max wait before a gated retry |

---

## 5. Design decisions and why

**Every agent is a node, not one node with `asyncio.gather`.**
Alternative: one "research" node running nine agents internally. Reason:
Reflection's targeted retry needs a real node to send back to, and logs
and metrics need agent-level grain.

**Supervisor without an LLM.** Alternative: let a model choose agents.
Reason: agent selection is control flow; a deterministic table is
predictable, free and testable, and every skip gets a stated reason.

**Sentiment waits for News.** Alternative: run both at once and let
Sentiment "peek". Reason: the peek is a race; a guaranteed order
costs only News's run time.

**Availability failures are not retried at once.** Alternative: retry
everything that failed. Reason: blind retries turn one 429 into a storm
(28 Gemini attempts in 54 seconds were measured in one run).
Instead there are a few bounded, delayed retries at the points that
matter most: the side repair (60 s, once), the Judge (until the earliest
model cooldown ends, at most 4 times and 240 s in all) and the citation
review (60 s, once).

**No verdict means no recommendation.** Alternative: save a HOLD 0.0.
Reason: a fake hold is indistinguishable from a real one and pollutes
accuracy scores and memory (3.11).

**Numbers are capped in code, not by prompt.** Alternative: tell the
Judge to lower its confidence when evidence is thin. Reason: the model
does not reliably do that. The prompt clause stays, but
the cap in `engine.py` is the guarantee, and the note says why.

**The Judge never sees an earlier rationale.** Alternative: show the
prior recommendation in full. Reason: the model restates it as new
evidence and the chain continues run after run.

**The Judge's own verdict is stored next to the final one.** Alternative:
compare with the final, penalised values. Reason: a like-for-like
comparison is the only way the "explain a confidence move" rule measures
the Judge and not the engine's penalties.

**Audit fields are logged, not stored or enforced.** Alternative: retry
the Judge when a field is missing. Reason: the only retry is a whole new
debate round of strong-model calls; the fields are for review, so they go
to the log. The figure check is the one exception (one rewrite), because
the rationale is shown to the user.

**Budget is cash minus other open BUY tiers.** Alternative: size each
recommendation against all cash. Reason: two open tier sets could commit
twice the available cash.

**Over-budget proposals are rejected, except a tiny overshoot.**
Alternative: scale every proposal down. Reason: scaling changes the
model's plan silently; a 0.5% trim of the last tier is small enough to be
safe and is explained in the note.

**Tier changes by explicit SQL.** Alternative: mutate the ORM collection.
Reason: the `delete-orphan` cascade can delete real tiers (3.16).

---

## 6. When things go wrong

| Symptom | What the system does | How to diagnose |
|---|---|---|
| analysis `failed`, `failure_reason=judge_unavailable` | no recommendation saved (no Judge pass of the analysis gave a valid verdict; otherwise the earlier verdict is kept, 3.11) | `bash scripts/logs.sh ai-service grep judge_node` (look for `judge_node.delayed_retry`, `judge_node.failed`) |
| `failure_reason=insufficient_evidence` | Bull/Bear/Judge skipped | log `agent_join_node.insufficient_evidence_skipping_debate` (fields `usable_agents`, `minimum`) |
| `failure_reason=debate_unavailable` | Judge skipped, both sides missing | `bull_node.failed`, `bear_node.failed`, `debate_join_node.side_repair_failed`, `judge_node.late_side_repair_failed` |
| an extra debate round with "Judge response could not be parsed" | the Judge's JSON was unreadable twice | `judge.unparseable_response` (`parse_error`, `error_pos`, `error_context`, `raw_chars`), `judge.json_reask_failed` |
| `failure_reason=no_evidence` | every agent failed | `agent_node.failed` lines with `failure_kind` |
| `failure_reason=timeout` | queue cancelled the run after 2700 s | log `analysis_queue_job_timed_out`; check provider latency (`make probe`) |
| low confidence, odd note | caps applied | read `confidence_adjustment_note` in the result |
| BUY without tiers | sizing refused | read `tier_generation_note`; logs `position_sizing.*` (e.g. `position_sizing.buy_over_budget`, `position_sizing.buy_confidence_below_minimum`) |
| tiers disappeared | superseded or declined | `bash scripts/tiers.sh all`; log `analysis_workflow.earlier_tiers_superseded` |
| a system analysis's tier shows `expired` while its window is still open | not accepted within `SYSTEM_TIER_ACCEPTANCE_HOURS`; it reserves no cash and cannot be accepted | compare the recommendation's `created_at` with the setting (3.15) |
| want the Judge's deciding factor, change basis or opposing points | not stored, only logged | `bash scripts/logs.sh ai-service grep judge.deciding_factor` and `judge.opposing_points`, filtered by the analysis id |
| Judge rationale changed after a figure check | a rationale figure had no match in the evidence; one rewrite | `judge.ungrounded_figures`, `judge.ungrounded_figures_after_rewrite`, `judge.ungrounded_rewrite_failed` |
| need every log line of one analysis | each line of the run (agents, LLM calls, tools, archival) carries `analysis_id` and, once known, `ticker` (structlog context variables bound in `run_analysis_workflow`) | `bash scripts/logs.sh ai-service grep '"analysis_id": "<analysis-uuid>"'` |
| many failures in an hour | alert `AnalysisFailureRateHigh` fires (more than half failed, at least 3 finished) | metric `alphamind_analyses_total{status, failure_reason}` (`observability.py`) |

Useful queries (read-only):

```
bash scripts/psql.sh -c "SELECT status, failure_reason, count(*) FROM ai.analyses GROUP BY 1, 2 ORDER BY 3 DESC;"
```

```
bash scripts/psql.sh -c "SELECT a.ticker, r.recommendation, round(r.confidence::numeric, 2) AS conf, r.debate_converged, left(r.tier_generation_note, 120) AS tiers_note FROM ai.recommendations r JOIN ai.analyses a ON a.id = r.analysis_id ORDER BY r.created_at DESC LIMIT 10;"
```

```
bash scripts/psql.sh -c "SELECT round_number, verified, jsonb_array_length(issues) AS issues, retry_agents FROM ai.reflections WHERE analysis_id = '<analysis-uuid>' ORDER BY round_number;"
```

(`issues` and `retry_agents` are JSONB columns. JSONB is PostgreSQL's
binary JSON type, so SQL functions such as `jsonb_array_length` can read
inside it. The issue count includes the `REVIEW_META:` line.)

The debug log lines `debate_debug.*` (log level debug) trace every routing
decision: which node fired, with which round counters and retry lists.

---

## 7. Demo walkthrough

One analysis usually takes about 1.5 to 4 minutes (up to 10 minutes more
when a new SEC filing must be summarised first) and makes real LLM calls. For a
live demo, run one beforehand and show its run folder; optionally start a
new one at the beginning of the session.

1. **Run an analysis (writes data: a system analysis and possibly pending
   tiers).**

   ```
   make analyze T="AMD"
   ```

   Say: "This creates a system analysis bound to the test portfolio
   through the internal API, polls every 5 seconds, and saves everything
   in a `runs/run_<timestamp>/` folder." The terminal shows
   `elapsed=...s status=running` lines, then `final status=completed`.

2. **Show the recommendation fields from the saved result.**

   ```
   python3 - <<'PY'
   import json, glob
   path = sorted(glob.glob("runs/run_*/AMD_result.json"))[-1]
   data = json.load(open(path))["data"]
   rec = data["recommendation"]
   print("file:", path, "status:", data["status"], "failure:", data.get("failure_reason"))
   if rec:
       for key in ("recommendation", "confidence", "debate_converged",
                   "window_expires_at", "confidence_adjustment_note",
                   "tier_generation_note"):
           print(f"{key}: {rec[key]}")
       for t in rec["tiers"]:
           print("tier:", t["side"], t["target_price"], t["quantity"], t["status"])
   print("debate arguments:", len((data.get("debate") or {}).get("arguments", [])))
   for r in data["reflections"]:
       print("reflection", r["round_number"], "verified:", r["verified"])
   PY
   ```

   Point at: the Judge's action and confidence; each sentence of
   `confidence_adjustment_note` and which rule produced it (3.12); the
   `tier_generation_note` (budget, trimming, superseded tiers); the
   `REVIEW_META:` line at the end of each reflection's issues.

3. **Show the BUY cash held by the open tiers of the test portfolio.**

   ```
   make tiers
   ```

   (or `bash scripts/tiers.sh`). It runs `python -m scripts.analysis_cli
   committed <PORTFOLIO_ID>` in the ai-service container and prints one
   line per analysis holding BUY cash ("TICKER amount (analysis <id>,
   status)") and then "total committed BUY cash: X", exactly as the
   position-sizing budget counts it. Say: "Only `pending_acceptance` and
   `accepted_awaiting_trigger` tiers with a live window are open; a
   pending tier of a system analysis also stops counting after
   `SYSTEM_TIER_ACCEPTANCE_HOURS`. Their BUY notional is the cash the next
   sizing step treats as committed." SELL tiers are not listed:
   `bash scripts/tiers.sh all` shows every tier, including SELL,
   `superseded` and `declined` tiers.

4. **Show the replacement rule in the database.**

   ```
   bash scripts/psql.sh -c "SELECT t.status, count(*) FROM ai.recommendation_tiers t GROUP BY 1 ORDER BY 2 DESC;"
   ```

5. **Decline the recommendation (writes data: its tiers become
   `declined`; not reversible).** Use an analysis id from `make tiers`
   (or the `analysis_id` column of `bash scripts/tiers.sh all`).

   ```
   make tiers-decline A="<analysis-uuid>"
   ```

   (or `bash scripts/tiers.sh decline <analysis-uuid>`). The script mints
   a token for the portfolio owner, because only the owner may decide.
   The response is the saved decision inside a `data` object, with
   `"action_taken": "declined"`.
   Run `make tiers` again: the analysis is gone from the list.

6. **Optional: show the delayed retries in the log of the day.**

   ```
   bash scripts/logs.sh ai-service grep judge_node.delayed_retry
   bash scripts/logs.sh ai-service grep debate_join_node.side_repair_retry
   ```

What to say at the end: "The model proposes, the code disposes. Every cap
is explained in the note, a failed Judge never becomes a fake HOLD, and a
tier can only become a trade after the owner accepts it."

---

## 8. Check your understanding

1. Why does `agent_join_node` sometimes route to `END`?
2. A `technical` analysis without a portfolio selects Market and
   Technical. Market fails with a 503. Does the debate run, and what is
   the failure reason?
3. What check runs on a Bull/Bear draft, and how many extra calls can it
   cost?
4. The Judge answers 503 twice. What happens to the analysis?
5. The Judge says BUY 0.85, the debate did not converge, and only 4 of 9
   agents were usable. What is the final confidence?
6. A BUY has confidence 0.42. What happens to its tiers, and to the
   ticker's earlier unfilled BUY tiers?
7. A new SELL for a ticker you do not hold arrives. What happens to your
   open BUY tiers for that ticker?
8. Who may decline a recommendation of a system analysis on a shared
   organization portfolio?

<details>
<summary>Answers</summary>

1. The join fires once per wave of finished agents. Until every selected
   agent has an output (and, after a News retry, until the re-sent
   Sentiment is newer than News), the wave is a no-op; only the last wave
   starts the debate.
2. Only Technical is usable; the minimum is `min(4, 2) = 2`, so the run
   is below the minimum. Reflection runs; Market failed for availability
   and is not retried; the analysis fails with `insufficient_evidence`.
3. One free mechanical check: every figure must match a number in the
   grounding reference (`find_ungrounded_figures`). With an ungrounded
   figure the side writes once more (one extra strong-model call); a
   regenerated draft that is still flagged is accepted and logged.
4. After each availability failure `judge_node` waits until the
   earliest strong-chain cooldown ends (0 s when a model is free) and
   tries again, so a second 503 leads to another retry. It stops after 4
   retries, after 240 s of waiting, or at once when every chain model is
   out of its daily quota. The last failure gives `judge_failed`,
   `judge_unavailable`; the debate stops, Reflection skips its review,
   and retries are dropped. If this was the first Judge pass, the
   analysis fails with no recommendation; if an earlier pass gave a valid
   verdict, that verdict is kept and the note says so (3.11).
5. 0.85 - 0.15 (not converged) = 0.70; ratio 4/9 = 0.44 < 0.5, so it is
   capped at 0.30 (an unverified Reflection adds a note, not a
   deduction).
6. No tiers (confidence below 0.5); no cash is reserved; the earlier
   unfilled BUY tiers are closed as superseded and the note says so.
7. A SELL without SELL tiers closes every unfilled BUY set of that ticker
   as `superseded` ("bearish view").
8. Only the portfolio's owner (`ensure_portfolio_owner`); organization
   members may read the analysis but get 403 when deciding.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/ai/graph/builder.py` | `build_analysis_graph`: nodes and edges; routing functions |
| `services/ai-service/ai/graph/nodes.py` | every node; `_run_agent_safely`, the Supervisor's guarded enrichments, side repair, Judge retry (`_judge_retry_wait`, using `ai/llm/router.py::chain_capacity`) and late side repair (`_late_side_repair`), Reflection retry gating (`_rate_limit_gate_for_retry`, using `ai/llm/router.py::chain_capacity`); `AGENT_REGISTRY`, the agent selection table, `make_agent_node`, `load_fresh_filing_context` |
| `services/ai-service/ai/graph/state.py` | `AnalysisState`, reducers, `DebateState`, `JudgeVerdict`, `ReflectionVerdict`, `confidence_adjustment_note`, `node_name` (node names per role), `MAX_DEBATE_ROUNDS`, `MAX_REFLECTION_RETRIES` |
| `services/ai-service/ai/agents/debate_common.py` | Bull/Bear shared generation, evidence formatting and code-built lines, figure check, shared prompt rules, `DebateSideAgent` and both side prompts |
| `services/ai-service/ai/agents/judge.py` | Judge prompt, context lines, earlier-verdict rules, figure check and rewrite, parsing, audit logs |
| `services/ai-service/ai/agents/reflection.py` | mechanical checks, citation review |
| `services/ai-service/ai/recommendation/engine.py` | single-position limit helpers, `no_verdict_reason`, `build_final_recommendation` |
| `services/ai-service/ai/recommendation/position_sizing.py` | budget, minimum size, sizing call, validation, note types |
| `services/ai-service/ai/workflows/analysis_workflow.py` | context, graph run, persistence, tiers, events, learning writes |
| `services/ai-service/ai/experiment_isolation.py` | `isolation_skips`: the `ANALYSIS_EXPERIMENT_ISOLATION` check and its `experiment_isolation.skipped` log (section 3.2) |
| `services/ai-service/domains/analysis/repository.py` | tier queries, `UNFILLED_TIER_STATUSES`, `unfilled_tier_condition`, `acceptance_lapsed`, `committed_buy_cash`, `committed_buy_holders`, `accept_pending`, `decline_unfilled`, `invalidate_unfilled_tiers_for_new_recommendation` |
| `services/ai-service/domains/analysis/queue.py` | serial queue and timeout |
| `services/ai-service/domains/decision/service.py` | record decision, accept/decline tiers, decision review |
| `shared/enums.py` | `RecommendationAction`, `TierSide`, `TierStatus`, `AnalysisFailureReason` |
| `scripts/run_analysis.sh`, `scripts/tiers.sh` | demo scripts |
| `services/ai-service/scripts/analysis_cli.py` | `committed <portfolio-uuid>`: BUY cash held by open tiers, per analysis (used by `tiers.sh`) |

---

## 10. Related chapters

- [13. ai-service overview](13-ai-service.md)
- [14. The LLM layer](14-ai-llm-layer.md) (tiers, router, retries, 429/503 handling)
- [15. The agents](15-ai-agents.md) (what each data agent does)
- [17. Retrieval-augmented generation](17-ai-rag.md)
- [18. Knowledge graph and memory](18-ai-knowledge-memory.md)
- [19. Workflows, tier monitoring and backtests](19-ai-workflows-backtest.md)
- [24. Events between services](24-events.md)
- [../API_SPEC.md](../API_SPEC.md), [../ERD.md](../ERD.md)
