# AlphaMind v4.1
# Agents.md — Multi-Agent System Design

Version: 4.1 

---

# 2. Architecture

```
                         Supervisor
                    (dynamic agent selection —
                     see §3)
                              │
        ┌──────┬──────┬──────┼──────┬──────┬──────┬───────────┐
        ▼      ▼      ▼      ▼      ▼      ▼      ▼           ▼
      Market  News  Sentiment Filing Technical Fundamental Risk  Macro  [Portfolio]*
     (each an autonomous ReAct loop — see §4)                          *only if 2+ holdings
        └──────┴──────┴──────┴──────┴──────┴──────┴───────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
                  Bull  ◄───────────►  Bear      (one round)
                    └─────────┬─────────┘
                              ▼
                            Judge  ──── converged? ────┐
                              │  no (and under round cap)│
                              └──────────loop───────────┘
                              │ yes, or cap reached
                              ▼
                         Reflection
                        /           \
              retry specific        accept
              agents (bounded)         │
                    │                  ▼
                    ▼            Final Recommendation
              [back to data       (action, confidence,
               agents, then        reasoning, citations,
               debate again]       participation + debate
                                   transparency — see §8)
```

Concrete file locations (see `PROJECT_STRUCTURE.md v4.1`):

```text
BaseAgent, ReActAgentLoop  -> ai/agents/base.py, ai/agents/loop.py
Tool interface              -> ai/tools/base.py
AnalysisState                 -> ai/graph/state.py
Graph wiring (with cycles)     -> ai/graph/builder.py, nodes.py, edges.py
Hard caps                       -> ai/graph/edges.py
  (MAX_DEBATE_ROUNDS, MAX_REFLECTION_RETRIES)
Final recommendation             -> ai/recommendation/engine.py
What other services may reference -> shared.schemas.refs
  (AnalysisRef, RecommendationRef)
```

---

# 3. Dynamic Agent Selection (Supervisor)

`ai/agents/supervisor.py::SupervisorAgent.select_agents` decides which
agents actually run, using three inputs, in priority order:

1. **Explicit user choice** (`AnalysisCreate.requested_agents`) — when
   provided, this REPLACES the task-type default entirely. An explicit
   choice should not be silently expanded by the system.
2. **Task type** (`AnalysisCreate.analysis_type`,
   `shared.enums.analysis.AnalysisType`) — otherwise, a default agent
   subset per type:

   | Task type | Default agents |
   |---|---|
   | `FULL` | all 8 data/analysis agents |
   | `TECHNICAL` | Market, Technical |
   | `FUNDAMENTAL` | Market, Fundamental, Filing |
   | `SENTIMENT` | News, Sentiment |
   | `RISK` | Market, Risk |
   | `MACRO` | Macro, Market |
   | `NEWS` | News, Sentiment |
   | `FILING` | Filing, Fundamental |

3. **Portfolio context** — the **Portfolio Agent** (§4.9) is added
   automatically whenever the analysis has 2+ holdings in context,
   independent of task type/user choice, unless the user's explicit
   selection positively excluded it. A single-ticker analysis (no
   portfolio, or a portfolio with only 1 holding) never includes it —
   there's no cross-holding scenario to reason about.

**Every agent NOT selected is recorded, with a reason, not just
silently skipped.** This is surfaced directly in the final output
(`AnalysisRead.participation`, `ai/schemas/analysis.py`) — the user
sees exactly which agents contributed to a given recommendation, which
didn't, and why. `AnalysisState.confidence_adjustment_note()`
(`ai/graph/state.py`) turns a non-empty skip list into a plain-English
note attached to the final recommendation's confidence score — skipped
agents are never a silent factor in a number the user can't interpret.

---

# 4. The Autonomous Agent Loop (ReAct)

## 4.1 Why ReAct, and why explicit rather than a framework helper

`ai/agents/loop.py::ReActAgentLoop` implements the standard ReAct
pattern (Yao et al., 2022) by hand — deliberately not hidden behind
`langgraph.prebuilt`'s ready-made agent helper. AlphaMind's whole pitch
is explainability; a black-box "agent executor" import would work, but
every step of *how* an agent decided to stop gathering evidence would
then live inside a library the team didn't write and can't fully walk
an evaluator through. The explicit version is a few dozen lines longer
and completely transparent.

## 4.2 The Loop, Concretely

Every data/analysis agent wraps a `ReActAgentLoop` configured with its
own **tools**, **system prompt**, and **iteration cap**:

```
for iteration in range(max_iterations):
    Thought, Action, ActionInput, FinishAnswer = ask the LLM, given the
        system prompt + task + everything gathered so far (the
        "scratchpad": every prior Thought/Action/Observation triple)

    if FinishAnswer is not None:
        return  # the agent itself judged its evidence sufficient

    Observation = execute the named tool with ActionInput
    append (Thought, Action, ActionInput, Observation) to the scratchpad
    # loop again with this new information

# hit max_iterations without a Finish — return a best-effort summary,
# flagged as `hit_iteration_cap=True`
```

`AgentLoopResult.is_well_supported` is `False` whenever the cap was hit
rather than the agent volunteering `Finish` — this feeds directly into
Reflection's decision about which agents to send back for more
evidence (§6).

## 4.3 Market Agent

- **Task**: establish price trend and volume pattern.
- **Tools**: `get_current_price`, `get_recent_price_history` (varying
  interval/window).
- **Loop behavior**: keeps pulling price history at different
  intervals until confident in a trend conclusion (or confident the
  signal is genuinely mixed) — per your example, "collect and validate
  market data until evidence is sufficient."
- **Cap**: 5 iterations.

## 4.4 News Agent

- **Task**: search, deduplicate, and weigh news by source credibility.
- **Tools**: `search_news`, `assess_source_credibility`.
- **Loop behavior**: keeps searching/re-assessing until its view of
  recent sentiment is stable (an additional search unlikely to change
  the conclusion) — matches "retrieval, dedup, credibility assessment,
  until a stable conclusion."
- **Cap**: 5 iterations.

## 4.5 Sentiment Agent

- **Task**: score sentiment (-1 to 1), building on News Agent's output
  already in `AnalysisState`.
- **Tools**: `search_news`, `assess_source_credibility` (same tools as
  News — used to dig deeper when the initial signal is mixed).
- **Cap**: 3 iterations (lighter — usually converges fast once News has
  already done the heavy lifting).

## 4.6 Filing Agent

- **Task**: extract risk factors, recent results, guidance from
  uploaded filings.
- **Tools**: `search_filings` (RAG-backed — see §7).
- **Cap**: 5 iterations.

## 4.7 Fundamental Agent

- **Task**: valuation (P/E, P/B, revenue growth, margins).
- **Tools**: `search_filings`, `get_current_price`.
- **Loop behavior**: "repeatedly queries filings/financial metrics as
  needed" — per your example, this agent gets a slightly higher cap
  than most since valuation typically needs several distinct filing
  lookups (revenue, margins, growth are rarely all in one passage).
- **Cap**: 6 iterations.

## 4.8 Technical Agent

- **Task**: RSI, MACD, moving averages, Bollinger Bands, ATR.
- **Tools**: `get_technical_indicators`, `get_recent_price_history`.
- **Loop behavior**: checks more than one indicator before concluding —
  a single indicator alone isn't sufficient evidence.
- **Cap**: 4 iterations.

## 4.9 Risk Agent

- **Task**: single-ticker volatility/drawdown/sector risk. Distinct
  from Portfolio Agent (§4.10) — Risk never looks at the user's other
  holdings.
- **Tools**: `get_recent_price_history`.
- **Cap**: 4 iterations.

## 4.10 Macro Agent

- **Task**: macroeconomic context (rates, sector rotation, market
  regime).
- **Tools**: `get_macro_indicators`.
- **Cap**: 2 iterations (macro context changes slowly; rarely needs
  more than one or two lookups).

## 4.11 Portfolio Agent — NEW

- **Only runs when the analysis has 2+ holdings in context** (§3) —
  clarified during design review: this is not "risk, but
  portfolio-flavored"; it specifically evaluates **what happens to the
  whole portfolio** under a hypothetical action (e.g. buying more of
  the analyzed ticker), which only makes sense when there's more than
  one position to reason about concentration/correlation across. A
  single-ticker analysis has nothing for this agent to do, so it's
  skipped, not run with degraded input.
- **Tools**: `get_portfolio_composition`, `recompute_scenario` (try
  more than one position-size variant before concluding whether the
  action meaningfully changes the portfolio's risk profile).
- **Cap**: 4 iterations.

---

# 5. Multi-Round Debate (Bull / Bear / Judge)

Previously a single Bull argument + single Bear argument + one Judge
call. Now:

1. **Bull** builds a case FOR, citing only facts already in
   `AnalysisState.agent_outputs` (never inventing evidence).
2. **Bear** builds a case AGAINST, same constraint. From round 2
   onward, each side's argument explicitly responds to the other
   side's argument from the previous round (not just repeating itself).
3. **Judge** (`ai/agents/judge.py::assess_round`) evaluates the round
   and returns **`converged: bool`** — "would another round change my
   mind" — alongside its running verdict and confidence.
4. If `converged` is `False` and the round count is still under
   `MAX_DEBATE_ROUNDS` (**3**, `ai/graph/edges.py`), the graph loops
   back to another Bull/Bear round. Otherwise it proceeds to
   Reflection.

`AnalysisState.debate.converged` — whether the debate stopped because
the Judge said so, or because the round cap was hit — is preserved and
surfaced to the user (`DebateRead.converged`,
`ai/schemas/analysis.py`), and directly lowers the final confidence
score when `False` (`ai/recommendation/engine.py`'s
`_UNCONVERGED_DEBATE_PENALTY`).

---

# 6. Reflection — Now Able to Send Agents Back

`ai/agents/reflection.py::ReflectionAgent.check` still verifies
confidence calibration (`ai/evaluation/confidence.py`) and citation
integrity (`ai/evaluation/hallucination.py`), but now ALSO inspects
each agent's own result: any agent whose `AgentLoopResult` hit its
iteration cap without volunteering `Finish` (i.e.
`is_well_supported == False`, surfaced as a low `AgentResult.score`) is
a candidate to be sent back for another pass.

Reflection expresses this as `retry_agents: list[AgentRole]` on its
verdict. The graph's `should_retry_after_reflection`
(`ai/graph/edges.py`) allows the retry only if
`reflection_retry_count < MAX_REFLECTION_RETRIES` (**2**) — Reflection
decides *which* agents need another pass (dynamic), the cap decides
*how many total rounds of this* the whole analysis is allowed to spend
(hard limit), matching the team's explicit choice for both debate and
reflection loops. After a retry, the graph re-runs one more debate
round with the updated evidence before Reflection checks again.

---

# 7. Retrieval — One Knowledge Base, Many Business-Facing Retrievers

`ai/rag/retriever.py` holds **one** `BaseRetriever` over the **one**
Qdrant collection (`settings.QDRANT_COLLECTION_RESEARCH`) — there is no
per-agent Qdrant collection. What differs per agent is the **retriever
subclass**, which tailors the query/filter/top_k for its specific need:

- **`FilingRetriever`** — filters to `document_type: filing`, higher
  `top_k` (financial facts are often spread across several passages).
  Used by Filing Agent and Fundamental Agent (via `SearchFilingsTool`,
  `ai/tools/filing_tool.py`).
- **`GeneralResearchRetriever`** — no document-type filter, used when
  an agent wants broader context (e.g. Sentiment reading a user's own
  research notes, if/when those get indexed too).

Adding a new agent-specific retriever means subclassing
`BaseRetriever` and overriding its filter/top_k — never creating a new
Qdrant collection. This keeps ingestion (`ai/rag/{loader,parser,
chunking,embedding,indexing}.py`) simple: one pipeline, one index,
indexed once regardless of how many different agents will eventually
query it differently.

---

# 8. Final Recommendation Output

`ai/recommendation/engine.py::build_final_recommendation` combines the
Judge's verdict with two explicit confidence adjustments — surfaced as
text, not folded silently into a number:

```text
action, confidence, reasoning        (as before)
+ confidence_adjustment_note          (from skipped/thin-evidence agents
                                        — AnalysisState.confidence_adjustment_note())
+ debate_converged: bool               (False = round cap was hit, not
                                          a Judge-reached stable verdict)
```

Both are part of `AnalysisRead` (`ai/schemas/analysis.py`) — a user
looking at any recommendation can always see which agents ran, which
didn't and why, and whether the debate actually converged or just ran
out of rounds.

---

# 9. Hard Caps — Full List

| Cap | Value | File | Rationale |
|---|---|---|---|
| Per-agent ReAct loop iterations | 2–6 (varies per agent, §4) | each agent's own `max_iterations` | Bounds cost/latency per agent; higher for agents whose task genuinely needs more lookups (Fundamental) |
| Debate rounds | 3 | `ai/graph/edges.py::MAX_DEBATE_ROUNDS` | Judge decides convergence dynamically; this is the safety net if it never does |
| Reflection-triggered retries | 2 | `ai/graph/edges.py::MAX_REFLECTION_RETRIES` | Reflection decides which agents need retrying dynamically; this bounds total retry rounds for the whole analysis |

All three are deliberately small — a larger number doesn't meaningfully
improve answer quality once the Judge/Reflection would have converged
anyway; it mainly adds cost in the pathological case where convergence
never naturally triggers.

---

# 10. Testing

See `tests/README.md` and `tests/ai_service/`:

- `test_agent_loop.py` — the generic `ReActAgentLoop` mechanics (stops
  on Finish, respects the cap), independent of any specific agent.
- `test_supervisor_selection.py` — task-type defaults, explicit user
  override, Portfolio Agent's conditional inclusion/exclusion.
- `test_debate_convergence.py` — both hard caps in `ai/graph/edges.py`.

---


# END OF Agents.md
# Version 4.1
