# 18. Knowledge graph and memory

> AlphaMind remembers in several ways. Within one analysis, everything
> lives in the graph state. Across analyses, it keeps three kinds of
> memory: a per-ticker track record of earlier calls and how they turned
> out (episodic memory), examples of tool-use patterns that worked for
> each agent role (procedural memory), and the agents' own finished
> reports, archived into the RAG index and mined for a small knowledge
> graph of facts (`ai/knowledge/`). Every write is gated by quality checks,
> because a memory system that stores bad output feeds that output back
> into every later run. The chapter also covers the filing knowledge the
> agents read: LLM summaries of SEC filings, structured filing records with
> checked quotes, and the latest-quarter line computed in code from SEC
> XBRL data. It explains each memory level, what is stored, when, and how
> it is read back.

---

## 1. The big picture

AlphaMind's memory has five levels, called L0 to L4 in this handbook:

```
 level  what                         lives in                       lifetime
 -----  ---------------------------  -----------------------------  ------------------
 L0     one agent's scratchpad       ai/agents/loop.py (memory)     one agent run
        (thought, tool call, result)  -> saved for audit only in
                                         ai.agent_results.output
 L1     the analysis state           AnalysisState (ai/graph/)      one analysis
 L2     episodic: earlier calls on   ai.episodic_memories            permanent
        this ticker + outcomes
 L3     RAG: documents and archived  Qdrant + BM25 + ai.agent_       permanent
        agent notes; knowledge graph reports, ai.kg_entities,
                                     ai.kg_relations
 L4     procedural: tool-use         ai.agent_memories               last 3 per
        patterns per agent role                                      role+ticker
        and owner                                                    +owner
```

How they connect to one analysis:

```
                 read (start of the analysis)          write (end of the analysis)
                 ----------------------------          ---------------------------
 L2 episodic --> episodic_memory_node -> Judge         <- analysis_workflow: one row
                 (track record block)                     per verdict; review job
                                                          fills the outcome later
 L4 procedural-> supervisor_node -> each agent's       <- maybe_remember_trajectory
                 extra_context (tool patterns)            (only clean outputs)
 L3 KG -------> supervisor_node -> Fundamental/Risk/   <- archive_agent_report ->
                 Filing (kg_context), Portfolio           _extract_and_store_kg
                 (portfolio_kg_context); also the
                 query_knowledge_graph tool (Filing,
                 Fundamental, Risk)
 L3 notes ----> search_prior_* tools (RAG)             <- archive_agent_report
                                                          (Macro, News, Fundamental,
                                                           Risk notes)
```

---

## 2. Concepts you need

**Episodic memory.** Memory of events: "on 2026-09-16 we said BUY AMD at
0.60, and over the next 30 days AMD rose 4.2%, so the call was correct".

**Procedural memory.** Memory of how to do something: "for the Market
agent, calling `get_current_price` then `get_recent_price_history` worked
well".

**Knowledge graph (KG).** Facts stored as *triples*: (source entity,
relation, target entity), for example (AMD, `depends_on`, TSMC). Entities
are nodes, relations are edges. Here the triples are extracted by an LLM
from archived agent reports.

**Archival.** Saving an agent's finished report (its final summary) into
the RAG index, so later runs can search it like any other document.

**Near-duplicate.** Two texts that say the same thing in different words.
Detected here with embeddings (chapter 17): if the cosine similarity of
two reports' vectors is at or above a threshold, they count as duplicates.

**Superseding.** Replacing an older item by a newer one. For archived
reports, the older near-duplicate is deleted after the newer one is safely
stored. In the KG display, an older numeric reading of the same metric is
tagged as superseded.

**Recycled content.** Text an agent copied from earlier agent notes
instead of deriving it from this run's own tool results.

**Fail open / fail closed.** Fail open: on an error, continue without the
feature (for example no KG context). Fail closed: on an error, refuse (for
example a note whose owner is unknown is made visible to nobody).

---

## 3. How it works in AlphaMind

### 3.1 L0 and L1: inside one run

**L0** is the scratchpad of `ai/agents/loop.py` (the ReAct loop, chapter
15): each step's thought, action, action input and observation. It is not
read back as memory. After the run it is stored inside the agent's result
(`ai.agent_results.output`, key `scratchpad`) for auditing, and it is
what the procedural memory condenses (3.4) and the recycled-content check
reads (3.6).

**L1** is `AnalysisState` (`ai/graph/state.py`): agent outputs, debate,
verdicts, reflection history, and the context loaded from the other
levels (`episodic_context`, `procedural_hints`, `kg_context`,
`portfolio_kg_context`, `fresh_filing_context`). It disappears when the
graph ends; only what `analysis_workflow.py` persists remains.

### 3.2 L2 episodic memory: writing

`ai/workflows/analysis_workflow.py` writes one `ai.episodic_memories` row
per analysis that ends with a real verdict, in the same transaction as the
recommendation (`EpisodicMemoryRepository.create`):

| Column | Value |
|---|---|
| `user_id` | the analysis user, NULL for a system analysis |
| `analysis_id` | the analysis |
| `ticker` | upper case |
| `recommendation` | `buy` / `hold` / `sell` (the final action, after caps) |
| `confidence` | the final confidence (after penalties and caps) |
| `summary` | the Judge's rationale |
| `outcome`, `outcome_return` | NULL at first |
| `created_at` | now |

An analysis without a verdict writes no row (chapter 16, 3.11). The
Judge's own final-round leaning and confidence, before code adjustments,
are not in this table: they are stored on the recommendation row
(`ai.recommendations.judge_leaning`, `judge_confidence`, chapter 16), and
`retrieve_episodic_context` reads them from the newest recommendation row
of each hit's analysis (one extra query per analysis start, only when there
are hits). `stored_judge_verdict` uses the pair only when both values are
present and the leaning parses; rows written before these columns existed
give `(None, None)`.

### 3.3 L2 episodic memory: outcomes and reading

**Outcomes.** The daily review job (02:00 New York, chapter 19) scores
every recommendation that is at least `AI_ACCURACY_EVALUATION_DAYS` (30)
days old and not yet scored, as long as its window ended at most
`REVIEW_GIVE_UP_AFTER_DAYS` (30) days ago; after that an unscorable
recommendation is left unscored
(`ai/workflows/review_workflow.py::_score_one_recommendation`):

- the ticker's return from the analysis date to 30 days later
  (`_realized_return`, first to last close);
- BUY is correct if the return is above 0, SELL if below 0, HOLD if the
  absolute return is at most 2% (`_HOLD_FLAT_BAND` 0.02);
- the score (1.0 or 0.0) goes to `ai.recommendation_metrics`, and the
  episodic row gets `outcome` = `correct` or `incorrect` and
  `outcome_return` = the ticker's return.

**Reading.** `ai/graph/nodes.py::episodic_memory_node` runs first in every
analysis and calls `ai/memory/agent_memory.py::retrieve_episodic_context`,
which uses `EpisodicMemoryRepository.list_recent_for_ticker`:

- newest first, at most `EPISODIC_CONTEXT_LIMIT` (3) rows;
- a user's analysis sees that user's rows plus system rows (NULL user);
- a system analysis sees system rows only, so one user's calls never reach
  another user's analysis.

The Judge receives them as a "Track record" block, one line per call
(`EpisodicMemoryHit.track_record_line`):

```
- 2026-09-16: BUY (confidence 0.60) -> 30-day return +4.2%, call was correct
- 2026-09-27: HOLD (confidence 0.55) -> outcome not known yet (under 30 days)
```

The lines show the FINAL action and confidence (what was issued). The
Judge's own stored values are used only for the "earlier verdict"
comparison (chapter 15, section 3.18). The rationale is deliberately not
shown. The system prompt clause
(`judge.py::_TRACK_RECORD_CLAUSE`) says to use the record only to
calibrate confidence, never to repeat a call because it was made before.
A database error gives an empty list (log
`episodic_memory_node.retrieval_failed`); the analysis continues.

### 3.4 L4 procedural memory

**Writing** (`ai/memory/agent_memory.py::maybe_remember_trajectory`), called
for every agent after the analysis is `completed`. An output becomes an
example only if `procedural_memory_skip_reason` returns nothing:

| Check | Skip reason |
|---|---|
| score below 0.7 (`_MIN_SCORE_TO_REMEMBER`) | `score_below_threshold` |
| not well supported | `not_well_supported` |
| hit the iteration cap | `hit_iteration_cap` |
| has verification warnings | `verification_warnings` |
| honest "no evidence" output | `no_evidence` |
| not English | `non_english` |

Also, any role that Reflection named for a retry in *any* round writes
nothing (`analysis_workflow.flagged_role_sets`, first set). Agent scores
come from `ai/agents/loop.py::derive_agent_quality` (chapter 15): graded
from evidence for a clean first pass (0.40-0.96), at most 0.65 for a clean
retry, at most 0.6 for thin evidence, 0.35 for an unresolved issue. With
the 0.7 bar, only clean first-pass outputs without heavy penalties
qualify. A skipped write logs `procedural_memory.write_skipped` with the
reason.

What is stored (`build_trajectory_content`): only the tool calls the model
chose itself. Calls that code makes before the first turn (`prefetched`
entries) and blocked entries are left out, because they are the same in
every run and teach nothing. Of the remaining calls the first 4
(`_MAX_STEPS_PER_TRAJECTORY`) are kept, each only `action` and
`action_input` (observations dropped), plus the final answer and the
marker `model_steps_only: true`. A run with no model-chosen call is not
stored at all (`procedural_memory.write_skipped`, reason
`no_model_chosen_calls`). The row goes to `ai.agent_memories` (`agent_id`,
`ticker`, `user_id` = the owner of the analysis, null for an analysis
without an owner, `memory_type` = `trajectory`, `score`, `content` JSONB).
After each write, `prune_beyond_retention` keeps only the newest 3 rows
for that agent, ticker and owner (the read path uses 2; one row of
buffer). Without the pruning the table would grow without limit. When an
account is deleted, its procedural rows and its episodic rows are removed
by the ai user cleanup (chapter 13, section 3.8); system rows stay.

**Reading** (`retrieve_procedural_hints(role, ticker, user_id=...)`), once
per selected role in `supervisor_node`, with the analysis owner:
`recent_qualifying_for_agent` scans up to 10 rows (`_ROW_SCAN_LIMIT`) of
that role **and that owner only**, the same ticker first, then the
owner's other tickers. An analysis without an owner reads only rows
without an owner. A row of another user is never read: the stored steps
hold that user's own search and filing queries. Rows are ranked by
recency, not by score: the scores of qualifying rows are too similar to
rank by. Only rows with the `model_steps_only` marker and at least one
step are used (older rows, which held the prefetched calls, are skipped
and logged as `procedural_memory.rows_without_model_calls_skipped`); the
2 newest usable rows become the hint. When no usable row is left there is
no hint at all. The read is read-only (a role without a catalog row has
no memories). Only the tool steps are injected:

```
Past high-scoring tool-use patterns for this role: calls the model chose
itself, beyond the calls code makes before the first turn (for reference,
not a script to follow verbatim; they carry no facts about today):
- [score=0.90] tools used: search_news({...}); web_search({...})
```

Because most agents get their data from prefetched calls, few runs make
a call of their own, so this hint is rare in practice.

Why so restrictive: with "score >= 0.7" as the only gate, off-topic
answers (foreign-language text, unrelated advice) and answers without a
single tool call get stored, and later runs are shown them as "past
high-scoring approaches"; a fabricated price in a stored final answer
then reappears in a later run. So the final answer is never injected,
only the tool steps, and every gate above applies.

The hints and any Reflection retry feedback are merged into one
`extra_context` string for the agent (`ai/agents/loop.py::combine_context`).

### 3.5 L3: archiving agent reports

After a completed analysis, `analysis_workflow.py` calls
`ai/knowledge/archival.py::archive_agent_report` for four roles, each
mapped to a RAG category (`_CATEGORY_BY_ROLE`):

| Role | Category |
|---|---|
| Macro | `macro_research` |
| News | `news` |
| Fundamental | `analyst_report` |
| Risk | `risk_analysis` |

Only `COMPLETED` outputs are candidates, and a role named for a retry in
the *last* Reflection round is skipped (log
`analysis_workflow.archival_skipped_flagged`). A role flagged earlier but
clean in the last round is archived.

**Gates inside `archive_agent_report`**, in order, each with a log event:

| Gate | Log event |
|---|---|
| output not well supported | `archival.skipped_not_well_supported` |
| recycled share at or above `ARCHIVAL_RECYCLED_SHARE_MAX` (0.5) | `archival.skipped_recycled_prior_notes` |
| has verification warnings | `archival.skipped_verification_warning` |
| hit the iteration cap | `archival.skipped_hit_iteration_cap` |
| not English | `archival.skipped_non_english` |
| fewer than 40 words (`_MIN_WORDS_TO_ARCHIVE`) | `archival.skipped_too_short` |

Each gate stops a real contamination loop. Example: a News summary with a
"[VERIFICATION WARNING: ...]" banner was archived, a later run's
`search_prior_news_notes` found it and read it as a thorough recent
roundup, skipped its own search, got flagged too, and archived the same
warning again.

Archival runs in its own sessions (`SessionLocal()`), not the workflow's
session, so a failure there cannot roll back the analysis and the other
way round. Inside `archive_agent_report` real failures raise; the workflow
catches them (below).

**Visibility.** The note's owner is the analysis user
(`_archive_owner`, `_archive_visibility`): a user's analysis produces a
note only that user (and analyses run by that user) can retrieve; a system
analysis produces a system note. If the owner cannot be read, the note is
visible to nobody (fail closed). Reason: an agent's note may paraphrase
the user's private uploads that its searches found.

**Near-duplicate superseding.** For all four archived categories,
before archiving:

1. Load the 3 most recent reports of the same category and ticker
   (`_NEAR_DUPLICATE_DEDUP_LOOKBACK`, `list_recent_by_category`), then keep
   only those of the same owner.
2. Embed the new report and those reports in one batch (one embedding
   request for up to 4 texts) and compute the cosine similarity. The
   reports are embedded whole: the vectors in Qdrant belong to single
   chunks, so they cannot stand in for a whole report.
3. A similarity at or above `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` is a
   match. The default 0.85 is calibrated for `gemini-embedding-2` (the
   default embedding); use 0.90 with the older `mistral-embed`. Cosine
   values are not comparable across embedding models. Embedding meaning, not characters: two LLM rewordings of the
   same facts scored as low as 0.11 on character similarity. Without a
   known owner nothing is superseded.

Then the new report is archived normally: chunked (category sizes from
chapter 17), embedded, indexed into Qdrant and BM25 with `source =
agent:<role>` and `content_date` = today, and recorded in `ai.agent_reports`
(the report id doubles as the RAG document id), `ai.embedding_jobs` and
`ai.document_chunks` in one commit (`_persist_report_rows`; the embedding
job is created as `embedding` and set to `ready` in the same session). A
report that yields no chunks is skipped (`archival.skipped_zero_chunks`).
If writing the rows fails, the chunks just indexed are deleted again
(`delete_document_chunks`; log `archival.orphan_chunk_cleanup_failed` if
that also fails) and the error is re-raised, so no orphan report stays
searchable without its rows. Success logs `archival.report_archived`. Only
*after* that, the matched old report is superseded (`_supersede_report`,
one session and one commit, through `delete_archived_report`): its chunks
are removed from Qdrant, BM25, `ai.document_chunks` and
`ai.embedding_jobs` (`ai/rag/indexing.py::delete_document_completely`),
its KG relations are deleted (`delete_relations_for_report`), and its
`ai.agent_reports` row is deleted. The same helper removes the archived
notes of a deleted user's analyses (chapter 13, section 3.8). Log: `archival.superseded_near_duplicate` (chunks and relations
removed). A failed supersede is logged (`archival.supersede_failed`) and
leaves one extra near-duplicate note, never a lost one.

Why supersede instead of skipping the new report: skipping freezes a
ticker's facts at the first extraction (the KG would keep one old
volatility reading, because every later Risk report is skipped as a
duplicate and never extracted). Why only after the new report is
stored: deleting the old note first and then failing to embed the new
one loses both.

Archival failures never fail the analysis: the workflow wraps the call
and logs `analysis_workflow.archival_failed`.

### 3.6 The recycled-content share

`ai/knowledge/recycled_content.py::recycled_share(answer, scratchpad)`
measures how much of an answer was copied from earlier agent notes, with
no LLM call:

1. Split the answer into sentences, ignoring code-added banners and
   appendix lines ("Live FRED this run", "Data caveat", ...).
2. From the scratchpad, take every successful tool result (entries with
   `tool_error` set are skipped) and split it into passages at each
   result line (`"\n- "`; `note_and_source_passages`). A result line of
   the form `(<ticker>) [agent:<role>] ...` is an earlier agent note (the
   line shape `search_prior_*` writes for an archived note); every other
   result line is a source of this run, and so is the whole observation
   of a tool without notes. The tool's own header and guidance text
   before the first result line (the query echo and the "passages tagged
   [agent:...]" guidance) counts as neither when the result holds notes.
   The numbers in the sources are collected too.
3. A sentence counts as recycled when at least half of its content words
   appear in one agent-note passage (`_MIN_PRIOR_OVERLAP` 0.5), clearly
   more than in any source passage (margin at least 0.2,
   `_MIN_MARGIN_OVER_SOURCES`), and it carries no number from this run's
   sources.
4. The share is recycled content words divided by all content words.

When the agent's own reuse check fired (`raw_output["reused_prior_note"]`:
Macro's `macro_reuse` decision, or Risk or News still at least
`PRIOR_NOTE_MAX_SIMILARITY` (0.9) similar to a prior note, chapter 15),
the share is 1.0 and the note is not archived
(`analysis_workflow._archival_recycled_share`).

The same split is shared by the prior-note provenance check in the same
file (`split_agent_note_passages`, `prior_note_only_claims_in_run`: amounts an
answer takes only from prior notes, chapter 15), so the three measures
always agree on what a prior note is.

The same module has a second measure, `prior_note_similarity`: the best
word-sequence ratio (`difflib`) between the answer and one prior agent-note
passage, with the passage header and Risk's code-built "Named risk events"
line removed from both. `recycled_share` deliberately ignores sentences that
carry this run's numbers, so a word-for-word copy of a note whose figures
are still current scores low; this ratio catches it (calibration on 30 Risk
answers: copies 0.92-1.0, every other answer 0.89 or lower). Risk and News
use it (chapter 15).

Why: an agent that restates an earlier note (Macro restating a macro
note written for another ticker, News restating an older note) gets it
archived again, so the same unsourced wording keeps returning as "prior
notes". A calibration over 51 archived answers set the 0.5 bar.

### 3.7 Knowledge-graph extraction

After the report is stored, `_extract_and_store_kg` runs (its own
try/except; a failure logs `archival.kg_extraction_failed` and leaves the
note in place):

1. `resolve_ticker_name` (`ai/knowledge/kg_context.py`) gives the
   official company name, cached in `ai.ticker_names` and fetched once
   from SEC's `company_tickers.json` (needs `SEC_EDGAR_USER_AGENT`;
   falls back to the bare ticker). The cache row is written and committed
   in its own short session, so a lookup from a read path that never
   commits its session (`kg_context`, query expansion, the
   `query_knowledge_graph` tool) is kept too; a concurrent insert of the
   same ticker is logged (`ticker_names.cache_write_failed`) and the
   name is still returned.
2. `extract_entities_relations` (`ai/knowledge/archival.py`) calls the
   tier in `KG_EXTRACTION_LLM_TIER` (`cheap`) at temperature 0 with the
   report, prefixed by "This report is specifically about ticker AMD,
   official name ... Use "AMD" as the entity name ...". The model returns a
   JSON array of at most 15 triples (`_MAX_TRIPLES`):
   `{"source", "source_type", "relation", "target", "target_type",
   "confidence", "kind", "as_of"}` (`max_tokens` 2,048: a cut-off array
   does not parse and every triple is lost). `kind` is "stable" (a
   lasting relationship: competitor, supplier, product, role) or "event"
   (a fact whose status or value can change: a lawsuit, a deal,
   guidance, a metric reading); `as_of` is the date the report states
   the fact as of ("YYYY-MM-DD" or null).
3. Validation (`_parse_response`): entity names 1-200 characters,
   relation 1-100; confidence clamped to 0..1 (missing = 0.5); a missing
   type becomes `concept`; an unknown type drops the triple. Allowed
   types: company, person, institution, economic_indicator, event,
   concept, product, position, country (a closed list so the type stays
   usable as a filter; a type is added only when extraction keeps needing
   it). A missing type key becomes `concept`
   (`kg_extraction.recovered_missing_type_field`); a present but unknown
   type drops the triple (`kg_extraction.unknown_entity_type`); a
   malformed triple is skipped alone (`kg_extraction.malformed_triple`); an
   unparseable reply gives no triples. A `kind` other than stable/event and
   an `as_of` that is not an ISO date become NULL (the triple is kept). An
   LLM failure gives no triples (`kg_extraction.unavailable`).
4. Triples with non-English text are dropped as a whole set
   (`archival.kg_skipped_non_english`).
5. Any endpoint whose core name (legal suffix removed,
   `core_company_name`) equals the company's core name is rewritten to
   the ticker. Without it most relations of a company end up under its
   long name ("JPMorgan Chase") and are unreachable by agents that query
   the ticker ("JPM").
6. Write: `get_or_create_entity` (unique by upper-cased name and type in
   `ai.kg_entities`), then one `ai.kg_relations` row per triple with
   `relation_type`, `confidence`, `source_report_id` (the provenance:
   which archived report said this), `kind` and `as_of` (migration
   `f1230a000001`; NULL on older rows). The log line
   `archival.kg_extracted` gives `triple_count`, `event_count` and
   `event_as_of_after_today` (events dated in the future; they expire by
   their extraction date, see 3.8).

Relations are never merged on write; every extraction is its own row.
They are aggregated on read.

### 3.8 Reading the KG: kg_context

`ai/knowledge/kg_context.py` has one shared implementation used by both
the proactive injection and the `query_knowledge_graph` tool (Filing,
Fundamental and Risk agents).

**Fetching** (`fetch_kg_relations`): `find_relations_for_entity_name`
looks the name up (upper case, every entity type), collects relations
where it is the source (`->`) or the target (`<-`), and keeps only
relations whose source report belongs to a system analysis or to the
viewer's own analyses. With a ticker filter it also tries the raw ticker
and the core company name, merges, and keeps the 20 most recent facts.

**Event expiry.** `events_expire_before` leaves out every relation with
`kind = 'event'` whose date is before the cutoff. The date is
`least(coalesce(as_of, extraction date), extraction date)`: an `as_of`
after the extraction date (a scheduled trial, a guidance period end the
model wrote as the date) counts as the extraction date, else the event
would stay current until then. Stable and unlabelled (older) relations are
unaffected. The cutoff is the filing date of the ticker's
newest filing that has structured records (`newest_filing_date`, from
`ai.filing_records`; none -> no expiry): the proactive injection uses the
analysed ticker (log `kg_context.injected` with `events_expire_before`),
`query_knowledge_graph` uses its `ticker_filter`, else the entity name
when it is a ticker with records. A matter's status comes from the
filing records, not from an earlier agent report.

**Aggregating** (`domains/knowledge/repository.py::_aggregate_relation_rows`):
rows with the same other entity, relation type and direction become one
fact with a count of extractions, an average confidence, the source
categories, and first/last dates.

**Formatting** (`format_kg_relations`), one line per fact:

```
Knowledge graph relations for 'AMD' (restricted to ticker='AMD') (3 distinct facts found):
- AMD --[depends_on]--> TSMC (stated by 2 different agent roles, 4 extractions, 2026-09-02 to 2026-09-28, avg confidence=0.88) [source: analyst_report, risk_analysis] [agent-derived, not independently sourced]
- AMD --[has_annualized_volatility_of]--> 52.1% (dated 2026-09-28, avg confidence=0.90) [source: risk_analysis] [agent-derived, not independently sourced] [MOST RECENT reading]
- AMD --[has_annualized_volatility_of]--> 48.7% (dated 2026-09-10, avg confidence=0.90) [source: risk_analysis] [agent-derived, not independently sourced] [SUPERSEDED by the MOST RECENT reading]
```

(Illustrative values; the shape is the code's.)

Rules behind the line:

- A fact extracted several times from the *same* role's reports says
  "re-extracted Nx from the same agent role's reports, not corroboration".
  Only different roles count as corroboration: a Risk claim archived,
  extracted and injected into the next Risk prompt would otherwise
  "confirm itself" every run.
- Every line says "[agent-derived, not independently sourced]" (every
  relation comes from an archived agent report; there is no other writer).
  A second header line (`KG_PRIOR_NOTE_GUIDANCE`) labels all relations as
  prior AI-written notes: unverified leads to confirm with a fresh tool
  result; the fresh data wins; a matter's status is the one the newest
  filing gives. (Otherwise a settled lawsuit can still appear open in the
  injected context.)
- There is no status-word filter on relations: matching relation names
  and status words was a word list over model text; event expiry replaces
  it.
- **MOST RECENT / SUPERSEDED tags** (`_tag_superseded_readings`): when two
  or more facts share a relation type and direction and their other
  entity is a bare number, percentage or dollar value (for example
  "82.6%"), the newest one is tagged `[MOST RECENT reading]` and the rest
  `[SUPERSEDED by the MOST RECENT reading]`, so an agent does not cite a
  stale 82.6% reading next to a fresher 83.3% from the same run's Risk
  agent. The Risk, Fundamental and Portfolio prompts also say to prefer
  the latest-dated reading.
- Non-English rows (older data) are dropped before injection
  (`drop_non_english_relations`).

Each of the two direction queries reads every raw row for the entity (no
SQL limit), because the corroboration count needs all of them; aggregation
then sorts by the newest extraction and keeps 20. When a ticker filter adds
name variants, the merged list is re-sorted and cut to 20 again. Grouping
is by exact strings, so "depends_on" and "relies_on" stay two facts
(under-merging is the safe direction).

**Injection.** `supervisor_node` calls `get_kg_context_for_ticker` once
when Fundamental, Risk or Filing is selected and puts the text into
`state.kg_context` (log `supervisor_node.kg_context_injected` with
`found` and `char_count`). For the Portfolio agent,
`get_kg_context_for_portfolio` builds one section per holding, for the
top 20 holdings by market value. An empty result is `None` and is simply
left out. The KG also seeds query expansion (chapter 17, 3.10).

### 3.9 Archived notes as search results

Archived notes are ordinary RAG chunks with `source = agent:<role>`.
Agents read them back with `search_prior_analysis`,
`search_prior_risk_notes`, `search_prior_macro_notes` and
`search_prior_news_notes` (`ai/tools/research_archive_tool.py`). When a
result contains agent notes, the tool adds a fixed guidance line: notes
tagged `[agent:...]` show what was already covered and must never be the
source of a current fact. Stale notes are flagged by age (news 3 days,
macro 45, analyst report 100, risk 180).

### 3.10 Filing summaries

SEC filings are long (a 10-K is about 30,000 words, a 10-Q 12,000-20,000)
and a fixed cut of the first words kept mostly cover-page and XBRL tag
noise. So every new 10-K and 10-Q is summarised by the strong model, and
the summary (not the raw text) becomes the filing document in RAG.

**Listing new filings.** `ai/ingestion/sec_edgar_filings.py::list_new_filings`
uses the `edgartools` library. The library is synchronous, so the whole
listing and text fetch (`_fetch_real_filings_sync`) runs in one worker
thread (`asyncio.to_thread`). Time limits:

- Each HTTP request that `edgartools` sends has the library's own httpx
  timeout, `EDGAR_HTTP_TIMEOUT` (an environment variable read by
  `edgartools`, default 30 s per request). One library call can send
  several requests.
- The listing calls (the "recent" block, each older history page, the
  amendment lookup) are additionally wrapped in `_call_with_timeout`
  (15 s, `_EDGAR_API_TIMEOUT_SECONDS`): the caller stops waiting after
  15 s and treats the call as failed, so one slow SEC page cannot hold
  an analysis for minutes. The inner thread is not killed; it ends when
  the library's own request timeout fires.
- `Company(ticker)` and the text fetches (`filing_text`) are not
  wrapped; they are bounded only by the library's per-request timeout.

**Filing text** (`filing_text`, used for the filing, its 10-K/A and
`fetch_filing_text_sync`). It reads the primary document from the full
submission file (`filing.sgml().text()`, the `{accession}.txt` download)
and falls back to `filing.text()` only when that fails or is empty. Every
modern 10-K and 10-Q is an inline-XBRL document that starts with
`<?xml`, and for such a document `filing.text()` in edgartools 5.48 looks
the primary document up through the legacy index page
`/Archives/edgar/data/{cik}/{accession}-index.html`. SEC answers that
URL with 403 (seen from October 2026), the library parses the error page
as a page with no documents, and the call fails with `AttributeError:
'NoneType' object has no attribute 'download'`. The submission file is
not affected (ORCL's 10-K filed 2026-06-22: 62,218 words through the
SGML path, while `filing.text()` fails). A failed SGML read is logged as
`sec_edgar_filings.sgml_text_failed` (`error=repr(...)`) at info level;
a failed fetch as `sec_edgar_filings.filing_text_fetch_failed` and a
failed amendment lookup as `sec_edgar_filings.amendment_check_failed`,
both with `error=repr(...)`.

`edgartools` also has its own request limiter, `EDGAR_RATE_LIMIT_PER_SEC`
(library default 9 per second), read by the library from the environment.
It is separate from the throttle of `ai/providers/sec_edgar.py` (8 per
second, 3.12): when a filing listing and an XBRL or submissions lookup run
at the same time, the two add up. `.env.example` therefore sets
`EDGAR_RATE_LIMIT_PER_SEC=2`, so the sum stays within SEC's 10 requests
per second; an `.env.docker` without the line uses the library default.

What the listing does:

- Only `10-K` and `10-Q` are ingested (`_WANTED_FORMS`). 8-K current
  reports are not; such events reach an analysis only through news.
- Per form it fixes a window of the newest filings first (`_MAX_PER_FORM`:
  one 10-K, four 10-Qs, i.e. the current annual report and a full
  trailing year of quarters) and only then skips the ones already stored as
  FILING documents in core-service. The window never slides back to older
  filings when the newest is already stored.
- The form must match exactly: `get_filings(form="10-K")` also returns
  `10-K/A` amendments, which would replace the real annual report.
- Listing reads SEC's "recent" block first (no extra request) and older
  history pages one at a time only while the window is still short
  (`list_form_filings`). A full history load takes 10-15 s for a filer
  like JPM (70 pages of mostly structured-note prospectuses) and would
  time out on every run.
- A 10-K/A with the same `period_of_report` as a 10-K is merged into that
  10-K's input (`_merged_amendment_text`); the prompt then says that the
  amendment wins on the topics it covers and the original stays valid for
  everything else (an amendment often re-files one item only, e.g. a
  corrected MD&A). The amendment lookup fails open.
- A text under 100 words is skipped (`sec_edgar_filings.filing_text_too_short`).

**Per filing, oldest first, 3 s apart** (the pause lowers HIGHEST-tier
contention between back-to-back calls):

1. The raw text (and the merged 10-K/A, if any) is archived for audit in
   object storage under `raw_filings/<ticker>/<filename>` (and
   `<filename>.amendment.txt`). It is never indexed and has no database
   row; it exists so a summary or a record can be checked against its
   source later, and the backfill reads it. Failure is logged
   (`sec_edgar_filings.raw_archive_failed`) and does not stop the filing.
2. The period of report comes from `edgartools`, else from SEC's
   submissions JSON (`filing_period`, section 3.12).
3. `ai/knowledge/filing_summary.py::summarize_filing` makes one call at
   the tier `FILING_LLM_TIER` (`highest_accuracy` by default; temperature 0, timeout 300 s
   `_FILING_SUMMARY_TIMEOUT_SECONDS`, `max_tokens` 12,288
   `_SUMMARY_MAX_TOKENS`). The 10-K and 10-Q prompts differ: a 10-Q's risk
   factors section lists only changes since the last report, so the 10-Q
   prompt says to present it as "new or updated risk factors this
   quarter", never as the full risk profile. Both prompts ask for every
   segment row, every distinct risk category and legal matter with its
   status, guidance with its period, a "Capital allocation and
   commitments" part (capex and outlook, buybacks and dividends or none,
   cash and debt, new debt, commitments, the gap between operating and net
   income), and a hard rule that every figure must be traceable to the
   text. An unknown form gets the 10-K prompt (logged
   `filing_summary.unknown_form_type`).
4. If the summary is real (not the fallback below), a second call at the
   same `FILING_LLM_TIER` extracts the structured records (section 3.11). An exception there is
   logged (`sec_edgar_filings.records_failed`) and the summary is still
   stored.

**Cost of the two calls.** Both calls send the whole filing. On Anthropic
(Sonnet 5.5 for `highest_accuracy`) a new ticker's five filings cost about
$1-1.5 and 2-3 minutes inside its first analysis (one RUN2 batch: 30 calls,
1.69M input tokens, $3.94 for three new tickers). `FILING_LLM_TIER=cheap`
moves both to the cheap model (Haiku 5.5 on Anthropic); its 100K-token
long-prompt price then applies to the largest 10-Ks (ORCL 10-K about
140K tokens). The two calls do not share a prompt cache: Anthropic caches
a prefix in the order tools, system, messages, and the two calls have
different system prompts (the summary prompt and `RECORDS_ONLY_PROMPT`),
so the identical filing text, which comes after them, is never part of a
shared prefix. Putting the filing first would mean moving it into the
system prompt and changing the shared provider interface, which was not
done.

**Input size.** `_TRUNCATION_STAIRCASE` cuts the combined text (original
plus amendment) before the summary call: up to 1.2 million characters keep
all; up to 1.6M keep 80%; 1.8M 70%; 2.1M 60%; 2.5M 50%; above 2.5M the
filing is skipped (`FilingTooLargeError`, log
`sec_edgar_filings.filing_too_large_skipped`; losing one document is
better than stalling the batch). A cut is logged
(`filing_summary.staircase_truncated`) and is a blind character cut (it can
land mid-table; an amendment appended at the end is the first text lost).
The tiers were sized for the model's 262k-token context from a single
measurement (about 5.14 characters per token); the 1.6M / 80% tier has the
thinnest margin. Known limit: the free tier also limits input to 250,000
tokens per minute per key, and the summary call does not use the smaller
records cut, so a JPM-size new filing (about 1M characters) can be refused
on input size. Whether the summary call should get the records cap is an
open owner decision.

**Output limit.** A call that stops at its token limit (Google finish
reason `MAX_TOKENS`, Mistral/OpenAI `length`; `hit_output_limit`) is retried
once with 16,384 tokens (`_RETRY_MAX_TOKENS`, log
`filing_summary.output_limit_retry`). If the retry fails, the first answer
is kept; if it is still cut, it is stored anyway and logged
(`filing_summary.output_truncated`, `FilingSummary.truncated`). Without
the retry, about 5 of 80 summaries end mid-sentence.

**Failure.** If the call fails (`filing_summary.llm_call_failed`), a
clearly labelled raw excerpt is returned (`FALLBACK_SUMMARY_PREFIX` plus the
first 1,200 words of the original; log
`filing_summary.using_fallback_excerpt`). It may serve as this run's
context, but it is never stored under the filing's filename (the dedup
key, `sec_filings.fallback_not_persisted`), so the next run tries again,
and no records call is made for it.

**Storing.** `ingest_new_filings` (shared by the daily ingestion and the
fresh-filing check) stores each summary through core-service
(`create_system_document`, category `filing`, `content_date` = filing
date), which starts normal RAG ingestion (chapter 17). Then it drops the
ticker's SEC caches (`forget_filing_caches`, section 3.12) and stores the
records row (`_store_records`; a missing period of report is looked up
again). A failed store is logged (`sec_filings.document_create_failed`) and
listed in `failed_filenames`; the other filings continue. The raw filing
text is never indexed (owner decision: retrieval depth is what the
summary keeps).

**Fresh-filing check.** `ai/graph/nodes.py::load_fresh_filing_context`
runs this same ingestion inside `supervisor_node` when Filing, Fundamental
or Risk is selected (chapter 16), so an analysis waits for the summary and
records calls of any new filing. Only the newest new filing's summary is
pasted into the prompts (`fresh_filing_context`, with the rule that this
newer filing wins over older `search_filings` passages on the same topic);
older new filings are only indexed
(`fresh_filing_check.older_new_filings_not_pasted`): pasting all of them
crowded the context and older text was read as current. The wait is
capped at `FRESH_FILING_BUDGET_SECONDS` (600 s); on expiry the check logs
`fresh_filing_check.timed_out` (`ticker`, `budget_seconds`) and returns
`None`. The shielded ingest keeps running in the background and stays in
`_ingests_in_flight`, so the next analysis of the same ticker joins it;
source-failure reports of that background ingest are not attached to the
timed-out analysis. Any failure returns `None`
(`fresh_filing_check.sec_check_failed`).

**Hallucination guard.** The prompt rule is the only guard on the summary
text. There is no mechanical figure check after generation: tried on one
filing's 35 flagged figures it found no real fabrication (every flag was a correctly
derived number or a number lost in a flattened table), it missed the real
errors a human review found, and it was most of a new ticker's analysis
time. Known quality limit (review of about 5,580 figures): about 3% of
summary figures are wrong (a year to date labelled as the quarter, a change
since year end called year over year, a misread segment table). This is why
the agents also get SEC XBRL figures (3.12) and quote-checked records
(3.11), and why `scripts/regenerate_filing_summaries.py` exists (3.13).

### 3.11 Filing records (`ai.filing_records`)

A filing record is one structured fact taken from one filing, with a quote
from that filing that code has found in the raw text. Records let code
(not an LLM summary) put unusual items, guidance, deals and legal status in
front of the agents, the debate and the Judge.

**Design rules** (owner position): each filing is extracted once, at
ingestion, on its own (no input from earlier filings); re-extraction is
needed only when an extraction rule changes. The most material numbers do
not depend on the records: non-operating totals, equity-investment gains,
impairments, restructuring, tax and cash flow come from SEC XBRL in code
(3.12). The records name and explain items; missing a non-material item is
acceptable. Reason: the same filing gave different record lists on each
extraction (AMZN's $50.5 billion equity-investment gain and INTC's $3.9
billion impairment appeared once and were lost on a re-run, although every
check passed), so a material number must not depend on model recall.

**The call.** `filing_summary.extract_filing_records` is the one path that
produces records, at ingestion (tier `FILING_LLM_TIER`) and in the
backfill script (default tier `FILING_RECORDS_BACKFILL_LLM_TIER`, `cheap`;
`--tier` overrides it). Prompt: `filing_records.RECORDS_ONLY_PROMPT` ("Read
this SEC filing. Do not summarize it.") plus the schema. Temperature 0,
timeout 300 s, `max_tokens` 10,240 (`_RECORDS_MAX_TOKENS`), one retry at
16,384 after a length stop (log `filing_summary.output_limit_retry` with
`records_only=True`). The reply is a line `=== FILING RECORDS JSON ===`
followed by one JSON object (`split_summary_and_records` takes the text
from the first `{` to the last `}` after the marker). The serving
"provider/model" is logged (`filing_summary.records_served_by`) and
returned (`ExtractedRecords.served_by`), because fallback models omit more
items. Returns `None` (nothing stored) when the call fails
(`filing_summary.records_call_failed`), the block is missing or does not
parse (`filing_summary.records_block_missing`), or a category key is
missing or not a list (`missing_categories`,
`filing_summary.records_incomplete`): the prompt requires every key ("an
empty list when the filing has none"), so a missing key is an incomplete
reply, not "none", and on `--redo` it must not overwrite a stored
category with an empty list.

**Input cut.** The records call keeps the first `RECORDS_MAX_CHARS`
(900,000) characters of the original plus amendment
(`records_keep_fraction` = min(1, 900,000 / size); logs
`filing_summary.records_input_truncated`, and always
`filing_summary.records_call_input` with `original_chars` and
`sent_chars`). Reason: the free tier limits input to 250,000 tokens per
minute per key, and Google's quota check behaves like about 4 characters
per token (count_tokens gave JPM's 10-Qs only 191-195k tokens, yet requests
of 980,400-1,009,111 characters were refused while 869,446 passed). Owner
decision: cut by percentage, never split a filing. Known loss: JPM's
litigation note starts beyond the cut.

**Categories and fields** (`CATEGORIES`, `_FIELDS`, `_ENUMS`; in prompt
and display order; the letter is the id letter):

| Category (letter, label) | Required | Optional | Enums |
|---|---|---|---|
| `one_off_items` (O, "unusual item") | description, amount_text, quote, period_end, period_months | - | period_months: 3, 6, 9 or 12 |
| `guidance` (G) | metric, value_text, quote | period_covered | - |
| `transactions` (T) | name, type, status, quote | date, detail | type: acquisition, divestiture, investment, financing, guarantee, other; status: announced, pending, completed, terminated |
| `legal_matters` (L) | name, status, quote | status_date, detail, exposure_text | status: pending, appeal, judgment, settled, dismissed, other |

What the prompt asks for (`_RECORDS_SCHEMA`):

- *Unusual item*: an amount recorded in the period's income statement (a
  charge, gain or loss) that is material to results (impairments,
  restructuring, litigation charges, gains or losses on divestitures or
  investments, remeasurements, unusual tax items); it may recur. Never a
  balance at a date (fair value, reserve, accrual) and never a routine
  recurring charge (amortization of acquired intangibles, depreciation,
  routine stock compensation). A material gain or loss on equity
  investments, derivatives or remeasurements IS an unusual item even when
  such a line appears every period (without this rule GOOGL's $77.4B,
  AMZN's $50.5B, INTC's $12.5B, NVDA's $13.4B and MSFT's $6.5B were
  dropped as "recurring"); largest first; a loss keeps a minus sign.
  `period_months` is the length the quote itself states (a first-half
  amount is 6, not 3); only the filing's own period is listed.
- *Guidance*: a financial target or a dated operating target (production
  or delivery rate, a certification or launch date), with its period.
- *Transaction*: an acquisition, divestiture, investment in another
  company, material financing (debt or equity issued, a new facility) or a
  guarantee given; only when announced, changed status or still open in
  this filing's period. A completed or terminated deal needs its date.
- *Legal matter*: a material lawsuit, investigation, regulatory proceeding
  or dispute with a tax authority, most material first. `status_date` is
  the date of the latest event that already happened, never the period end
  when the filing gives no date and never a scheduled hearing.
  `exposure_text` is the amount claimed, sought, assessed or awarded and in
  force now; never an amount accrued, paid, escrowed or reserved, never a
  valuation of an asset.
- Shared rules: read the whole filing, starting with the notes on other
  income (expense), investments, impairments, restructuring and income
  taxes; every quote is copied word for word (at least eight words, "..."
  only to skip words inside it); every amount carries its "$" and its scale
  word as the filing writes it (a table value "in millions" becomes
  "$1,423 million", a per-share amount keeps "per share"); no computed
  amounts or percentages.

**Checks in code** (`validate_records`). Code checks structure and
presence only, never meaning. A failing record is dropped, never edited
(only whitespace is trimmed and enum values lower-cased), and each drop is
logged (`filing_records.dropped` with ticker, filename, category, title,
reason and `served_by`) so a drop can be told apart from an omission. The
summary event `filing_records.validated` gives `kept` per category,
`dropped` per reason and `adjusted` (field-level changes) per reason. In
order, per record:

1. Shape: an object (`not_an_object`); required fields present and
   non-empty (`missing_<field>`); optional fields text or null
   (`bad_<field>`); enum values allowed (`bad_<field>`). Only the listed
   fields are copied, so keys of older schemas are never stored again.
2. Dates: `status_date` and a deal `date` must be ISO (`YYYY`, `YYYY-MM`,
   `YYYY-MM-DD`; `bad_<field>`). A date after the filing date is a
   scheduled event, not the status date: it is cleared
   (`future_date_cleared`). A legal `status_date` equal to the period of
   report is cleared (`status_date_is_period_end_cleared`) unless the quote
   itself writes that date (`status_date_is_period_end_kept`): the period
   end written as a status date made old matters look freshly updated
   (five META matters), while BA had a real court decision on the period
   end.
3. Quote (`quote_in_filing`, reason `quote_not_in_filing`): the quote and
   the filing are compared as lower-case letters and digits only
   (`squash`), so spacing, line breaks, punctuation and table padding do
   not matter. The quote is split at "..."; every part with at least 12
   such characters must be found, and all parts together must have at
   least 25 (`QUOTE_MIN_CHARS`). Replayed on 1,600 real sentences: every
   long enough sentence matched; no one-word paraphrase did. This is the
   strongest check.
4. Amount readable: `one_off_items.amount_text` and
   `legal_matters.exposure_text` must state at least one readable amount
   (`stated_amounts`: a number with "$", a scale word or "%"; an accounting
   negative "$(1,180) million" is read as 1,180). A required field without
   one drops the record (`amount_unreadable`: a bare table cell "56,311"
   would otherwise pass every amount check); an optional one is cleared
   (`amount_unreadable_cleared`, e.g. an exposure of "billions of
   dollars").
5. Scale word: an amount of $10 or more in `amount_text`, `exposure_text`
   or a guided `value_text` without a scale word or "per share" is dropped
   (`amount_without_unit`: "$280" copied from a table in millions is not
   $280). The first end of a range takes the second end's unit ("$450-$900
   million").
6. Deals: a completed or terminated transaction without a date is dropped
   (`missing_date`: without it the period check cannot tell an old deal
   from a current one; GOOGL's Q2 10-Q re-listed two Q1 acquisitions), and
   one whose date surely falls before the filing's own period (a partial
   date counts as its last possible day) is dropped (`not_own_period`).
7. Own period of an unusual item: `period_months` must be 3, 6, 9 or 12
   (`bad_period_months`), `period_end` an ISO date (`bad_period_end`), the
   length 3 for a 10-Q or 12 for a 10-K, and `period_end` within 7 days of
   the period of report (`not_own_period`; fiscal calendars end on a
   weekday near month end). Known limit: a charge the filing states only
   for the first half cannot be stored.
8. Amount in quote: an unusual item's amount must appear in its own quote
   (`amount_not_in_quote`). A legal exposure missing from its quote is
   only counted (`exposure_not_in_quote`).
9. Amount in filing: every amount stated in any field except the quote
   must appear in the raw filing as the same value at the stated precision
   (`RawNumbers.contains`, `amount_not_in_filing`). A percentage must match
   a percentage; any amount may match a number written with its unit; an
   amount with three or more significant digits may also match a table cell
   in dollars, thousands or millions. Limit: a wrong amount that happens to
   appear elsewhere in a large filing still passes (in JPM's 10-Q a random
   "$xxx million" exists about 4 times in 5, in INTC's about 1 in 4).

**Table.** `ai.filing_records` (migration `f1220a000001`,
`domains/filings/model.py`): one row per filing document (`filename`
unique, the same dedup key core-service uses,
`<TICKER>_<FORM>_<filing date>.txt`), `ticker`, `form`, `filing_date`,
`report_date` (EDGAR period of report, NULL when unknown), `records` JSONB
(the four lists, validated records only), index on (ticker, filing_date).
`FilingRecordRepository.upsert` replaces an existing row's records.
`store_filing_records` and `load_filing_records` each use their own session
and fail open (`filing_records.stored` with counts,
`filing_records.store_failed`, `filing_records.load_failed`). Agents load
at most `FILING_RECORDS_MAX_FILINGS` (5) newest rows per ticker. Rows
written by older code keep extra keys (`commitments`, `results`, `line`,
`part_of`, `id`, `next_event`, `next_event_date`, `prior_id`); every reader
goes by `CATEGORIES` and its fields, so these keys are never rendered or
resolved (`F1.C1` and `F1.R1` give `None`).

**How records are shown.** `render_filing_records` (in the first
`search_filings` result, chapter 17) prints a header saying the records are
an LLM's reading whose quotes and amounts were checked but whose status and
selection can be wrong or incomplete, that an unusual item may recur, and
that a legal amount is what is claimed or awarded, not a recorded loss.
Then per filing, newest first: `F1 = 10-Q filed <date>, period of report
<date> [NEWEST]`, `F2 = ... [OLDER]`, and per record its id
`F<filing>.<letter><n>` (`F1.L2` = newest filing, second legal matter), its
fields and the quote (first 200 characters, `Filing text: "..."`); "(no
records)" for an empty filing. Amounts are shown without a space after "$"
(`display_amounts`). `record_by_id` resolves an id against the same order
and adds the filing's metadata. Ids are for agents only; the prose checks
of chapter 15 send back an answer that writes one.

**Who uses the records.**

- The Filing agent builds four code lists from them (chapter 15, section
  3.10): `one_off_items` (newest filing's unusual items, with id and quote),
  `key_records` (legal matters with an amount, financing and guarantee
  deals and any open deal, every guidance item), `year_to_date_unusual_items`
  and `prior_year_unusual_items`. They reach the debate and the Judge as
  code-built evidence lines (chapter 15, section 3.17).
- Risk checks every cited `source_id` against these rows (chapter 15,
  section 3.14).
- The newest filing date with records is the cutoff for knowledge-graph
  event relations (3.8).

### 3.12 The SEC XBRL latest-quarter line

`ai/providers/sec_edgar.py` reads two free SEC endpoints (header
`SEC_EDGAR_USER_AGENT` required; every request goes through
`_sec_get_json`, waits until `1 / SEC_EDGAR_MAX_REQUESTS_PER_SECOND`
seconds (default 8 per second; SEC allows 10) have passed since the
previous `_sec_get_json` request in the process, and retries network
errors and 5xx but not 4xx). The `edgartools` requests of the filing
listing (3.10) have their own limiter and are not counted here:

- **Submissions JSON** (`data.sec.gov/submissions/CIK...json`): every
  filing's form, filing date, report date and accession number, cached per
  ticker per UTC day (`_submissions_cache`); older history pages are loaded
  only when a date falls into them. Two concurrent lookups that need the
  same older page merge it once: a page already merged by the other call
  is skipped, so the cached rows hold no duplicates. `filing_period(ticker, form,
  filing_date)` returns the filer's own `reportDate` (the period end);
  failures log `sec_edgar.filing_period_unavailable` and give `None`
  (XOM's filings before its CIK change have none). Every filing passage and
  record row is labelled with this period; no code guesses a period from
  summary text.
- **Companyfacts JSON** (`data.sec.gov/api/xbrl/companyfacts/...`): every
  us-gaap fact the filer tagged, in USD. Not cached as JSON (tens of MB for
  a large filer); the derived quarter result is cached per ticker per UTC
  day (`_quarter_cache`). `get_fundamentals` fills that cache from its own
  download.

`latest_quarter_results(ticker)` builds a `QuarterResults`;
`quarter_results_payload` renders it; `latest_quarter_payload` combines
both. A cached result is re-checked for a newer filing on every read.
`forget_filing_caches(ticker)` drops both caches after ingestion stores a
new filing, so the "newer filing" title below is correct the same day.

**Quarter values** (`_quarter_values`). For each concept list (revenue:
`Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`,
`RevenuesNetOfInterestExpense` (banks); operating income:
`OperatingIncomeLoss`; net income: `NetIncomeLoss`, then `ProfitLoss`), a
10-Q/10-K fact of 80-100 days is a quarter; for the same period the
latest-filed fact wins. A filer that tags only year-to-date values in its
10-Qs, and every Q4 (no 10-Q reports it), gets the quarter as the
difference of two facts with the same start one quarter apart (6M - 3M =
Q2, 9M - 6M = Q3, FY - 9M = Q4), marked "quarter derived from year-to-date
figures". The latest quarter is the newest quarter end over the three
metrics; the year-ago quarter ends 350-380 days earlier, closest to 364
days. Each metric takes the first concept that has the current quarter (the
year-ago value from the same concept when it has one).

**Costs** (`_quarter_costs`, each part fails open and is logged): gross
margin in percent of revenue from `GrossProfit`, else revenue minus cost of
revenue (`CostOfGoodsAndServicesSold`, `CostOfRevenue`, `CostOfGoodsSold`)
for exactly that quarter end, skipped for a quarter that also tags
`PolicyholderBenefitsAndClaimsIncurredNet` (an insurer; UNH showed an 88%
"gross margin", `sec_edgar.gross_margin_skipped_policy_benefits`); R&D
(`ResearchAndDevelopmentExpense`) and SG&A
(`SellingGeneralAndAdministrativeExpense`). A year-ago cost value is shown
only next to a current one.

**Year to date** (`_year_to_date`, only when the quarter comes from a
10-Q): per metric, the longest fact ending on the quarter end that is longer
than a quarter and shorter than a year and starts a fiscal year. The start
counts as a fiscal-year start when it lies 1-8 days after the end of an
annual (350-380 day) fact of the same concepts, or, for a filer whose
companyfacts lack the annual facts (XOM after its CIK change), when SEC's
fiscal-period field `fp` of that fact is Q2 or Q3. The prior year to date
ends 350-380 days earlier with a length within 10 days. When no metric has
both, the line is left out and `sec_edgar.year_to_date_unavailable` logs
the reason per metric ("no tag", "no year-to-date fact", "no prior year to
date"). A first fiscal quarter and a 10-K have no year-to-date line.

**Items below operating income and cash flow** (one concept table,
`_BELOW_OPERATING_GROUPS` and `_CASH_FLOW_GROUPS`). Each group lists
us-gaap concepts in priority order with a display label:

| Group (key) | Concepts, in priority order |
|---|---|
| pretax income (`pretax_income`) | IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest, ...MinorityInterestAndIncomeLossFromEquityMethodInvestments |
| non-operating total (`nonoperating_total`) | NonoperatingIncomeExpense (see the computed rule below) |
| other non-operating (`other_nonoperating`) | OtherNonoperatingIncomeExpense |
| equity securities gains (`equity_securities_gains`) | EquitySecuritiesFvNiGainLoss, EquitySecuritiesFvNiUnrealizedGainLoss, GainLossOnInvestments |
| upward adjustments (`equity_upward_adjustments`) | EquitySecuritiesWithoutReadilyDeterminableFairValueUpwardPriceAdjustmentAnnualAmount |
| downward adjustments (`equity_downward_adjustments`) | EquitySecuritiesWithoutReadilyDeterminableFairValueDownwardPriceAdjustmentAnnualAmount |
| impairment (`impairment`) | GoodwillAndIntangibleAssetImpairment, GoodwillImpairmentLoss, AssetImpairmentCharges, ImpairmentOfIntangibleAssetsExcludingGoodwill |
| restructuring (`restructuring`) | RestructuringCharges, RestructuringSettlementAndImpairmentProvisions |
| income tax (`income_tax`) | IncomeTaxExpenseBenefit |
| operating cash flow (`operating_cash_flow`) | NetCashProvidedByUsedInOperatingActivities |
| capital expenditures (`capital_expenditures`) | PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets |
| share repurchases | PaymentsForRepurchaseOfCommonStock |
| dividends paid | PaymentsOfDividendsCommonStock, PaymentsOfDividends, PaymentsOfOrdinaryDividends |
| long-term debt issued (`long_term_debt_issued`) | ProceedsFromIssuanceOfLongTermDebt ("long-term debt issued"), else ProceedsFromIssuanceOfDebt ("debt issued"), else ProceedsFromDebtNetOfIssuanceCosts ("debt issued (proceeds after issuance costs, before repayments)") |
| long-term debt repaid (`long_term_debt_repaid`) | RepaymentsOfLongTermDebt ("long-term debt repaid"), else RepaymentsOfDebt ("debt repaid") |
| net change in cash (`net_change_in_cash`, signed) | CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect ("net change in cash, cash equivalents and restricted cash"), else CashAndCashEquivalentsPeriodIncreaseDecrease ("net change in cash and cash equivalents") |

Rules (`_group_item`):

- For a period, the first concept of the group with a NON-ZERO fact for
  exactly that period is used, and the comparison value comes from the
  same concept (when only the comparison period has a non-zero fact, the
  first such concept is used). A zero is no item. Values are never added
  across concepts or groups. A group without such a fact is not shown.
- Facts tagged only with a company-specific concept or only with a
  dimension are not in companyfacts, so such items are not listed.
- Two values are computed, each labelled "computed": the non-operating
  total is pretax income minus operating income whenever both are tagged
  for the same period, so it always reconciles the two lines (a filer's
  own `NonoperatingIncomeExpense` used a narrower definition for QCOM and
  INTC, INTC's year-ago value even had the opposite sign); the filer's own
  tag is shown only when operating income is not tagged. Free cash flow is
  operating cash flow minus capital expenditures, inserted right after
  capex, only when both groups exist; its current value needs both facts
  for the current period and its prior value both for the prior period.
  Free cash flow after buybacks and dividends (key
  `free_cash_flow_after_returns`) is free cash flow minus share repurchases
  minus dividends paid. Its label names only the return lines present:
  "free cash flow after buybacks and dividends (computed: free cash flow
  minus share repurchases minus dividends paid)" when both exist, else for
  example "free cash flow after buybacks (computed: free cash flow minus
  share repurchases, no dividends paid line in the data)". A fixed label
  once called QCOM's +$21M (buybacks only) the result after dividends too,
  while about $2.9B of dividends had been paid. It is inserted right after the
  last of those two lines, only when free cash flow and at least one of
  them exist; for each period it needs free cash flow and a value of every
  return line shown (a return line not tagged for that period leaves that
  period "not tagged"). Nothing else is computed (no effective tax rate:
  no computed guesses).
- Periods: below-operating items use the quarter values (tagged, else
  derived from two year-to-date facts, marked with "*") for the latest and
  the year-ago quarter, and the exact year-to-date periods for the
  year-to-date line. Cash flows of a 10-Q are year-to-date facts, so the
  cash-flow line uses the year to date of a 10-Q after Q1, the quarter of
  a first fiscal quarter, and the fiscal year of a 10-K (tagged facts only,
  never derived).
- Overlap: an upward or downward adjustment is shown as "of which ...
  (included in the <equity gains label> above)" only when the equity gains
  item is in the same list and, in every period where both are tagged,
  the adjustment is not larger than the gains (`_within`). Some filers tag
  the adjustment as a separate component of other income (AMZN Q2: an
  upward adjustment of $50.49B next to equity securities gains of $1.30B),
  and calling it "included" would then be a new factual error.
- Debt issued, debt repaid and the net change in cash are tagged lines,
  never computed: they let a reader see how a negative free cash flow was
  funded (the debate's `FREE_CASH_FLOW_RULE` names a funding source only
  with such a figure). The general debt concepts are the fallback for
  filers that do not use the long-term ones (BA tags RepaymentsOfDebt and
  ProceedsFromIssuanceOfDebt); the label says which concept was used.
  Two more standard concepts are fallbacks because companies switched to
  them: PaymentsOfOrdinaryDividends (QCOM's and GOOGL's recent dividends;
  without it QCOM had no dividend line and GOOGL's current dividends were
  "not tagged") and ProceedsFromDebtNetOfIssuanceCosts (NVDA's $24.90B
  six-month issuance, GOOGL's $56.23B). Its label says "proceeds after
  issuance costs, before repayments" because the first wording, "net of
  issuance costs", was read as net debt issued by every GOOGL writer. Replayed on the 16 tickers' October
  2026 companyfacts: only AMD, BA, GOOGL, NVDA and QCOM change; QCOM's
  after-returns value goes from +$21M to -$2.85B and GOOGL's from "not
  tagged" to -$970M.
  Measured on the live companyfacts of the 16 batch tickers (October
  2026): the net change in cash is tagged for all 16, the general debt
  concepts add BA (debt repaid $8.38B, debt issued $35M, six months), KO
  and TSLA (debt issued); the lines that existed before are unchanged.
- Groups for gains on disposals of a business and on debt extinguishment
  were dropped: no fact existed in any shown period of 16 tickers.
- Each part fails open: `()` and a log (`sec_edgar.below_operating_failed`,
  `sec_edgar.cash_flow_failed`).

**Lag.** SEC's XBRL API can lag a new filing by weeks (KO's 10-Q of
2026-07-29 was not in companyfacts days later). When the submissions JSON
lists a 10-Q or 10-K filed after the one the quarter comes from,
`newer_filing` is set (e.g. "10-Q filed 2026-07-29") and the title changes
(see below). A trailing caveat under the plain title "Latest quarter"
is read as "the latest quarter" by the debate, so the title itself says it.
The prompts then tell agents to take the newest quarter from that filing's
passages.

**Rendering** (`quarter_results_payload`, the `text` key), line by line:

```
Latest quarter (SEC XBRL, 10-Q filed <date>, quarter <start> to <end>, compared with the quarter ended <date>[; quarter derived from year-to-date figures]): revenue $X vs $Y (+n%); operating income ...; net income .... Costs: gross margin a% vs b%; R&D ...; SG&A ....
Net income minus operating income: +$Z
Below operating income (SEC XBRL, quarter ended <end>, compared with the quarter ended <date>): pretax income ... vs ...; non-operating total (computed: pretax income minus operating income) ...; ...
Year to date (6 months <start> to <end>, compared with <start> to <end>): revenue ...; operating income ...; net income ...
Below operating income, year to date (6 months ended <end>, compared with the period ended <date>): ...
Cash flow (SEC XBRL, year to date, 6 months <start> to <end>, compared with <start> to <end>): operating cash flow ...; capital expenditures ...; free cash flow (computed: operating cash flow minus capital expenditures) ...; share repurchases ...; dividends paid ...; free cash flow after buybacks and dividends (computed: free cash flow minus share repurchases minus dividends paid) ...; long-term debt issued ...; long-term debt repaid ...; net change in cash, cash equivalents and restricted cash ...
Only items the filer tags with these standard XBRL concepts are listed (...). Items can overlap (...): never add them; cite a single item or the total. [* = derived as the difference of two year-to-date facts.]
```

With `newer_filing` the first words are "Latest quarter in SEC XBRL -- the
newer 10-Q filed <date> is not in XBRL yet". Formatting: amounts as
`$1.23B`, `$456M` or `$1,234`; a change of sign says "swung to a loss" or
"swung to a profit" (no percentage); two losses say "loss narrowed/widened
n%"; a missing current value says "not reported by the filer" (banks tag
no operating income); a missing comparison says "(comparison period not
tagged)". The "Net income minus operating income" line lists its
non-operating and tax components only when there is no below-operating
line. The note line appears whenever any item is shown.

**Payload keys** (a contract read by the Fundamental agent, the Judge and
the debate): `text`, `form`, `filed`, `period_start`, `period_end`,
`revenue`, `revenue_prior`, `revenue_yoy_pct`, `operating_income`,
`operating_income_prior`, `operating_income_yoy_pct`, `net_income`,
`net_income_prior`, `net_income_yoy_pct`, `net_minus_operating`,
`newer_filing`, `year_to_date` (dict with periods, `months`, the three
metrics with prior and `*_yoy_pct`, optional `below_operating`; or null);
optional cost keys `gross_margin_pct(_prior)`, `rd_expense(_prior,
_yoy_pct)`, `sga_expense(_prior, _yoy_pct)` (absent when unknown);
optional `below_operating` (list of items) and `cash_flow` (`period`,
`items`), each item `{key, label, current, prior, derived, prior_derived,
computed}`. A `*_yoy_pct` is null when a value is missing, the prior is
zero or the sign changed.

**Where it appears**: appended to the `get_fundamentals` tool output and to
the Fundamental summary, stored as `raw_output["latest_quarter"]`, shown at
the top of the first `search_filings` result, and in the debate and Judge
evidence, once (chapter 15, section 3.17). It fails open
(`sec_edgar.latest_quarter_unavailable`, `sec_edgar.quarter_results_failed`);
IFRS filers (20-F) have no us-gaap quarters.

Why XBRL: without the quarter line the Judge can call a "27.9% annual
operating margin" strong while the newest quarter's operating income has
fallen 41%. Parsing profit changes out of the LLM summaries and labelling
them "computed in code" injects wrong facts. XBRL facts
are the filer's own tagged numbers. What XBRL cannot cover: company-specific
tags, items reported only with a dimension, what an item is, legal matters,
guidance, and the days when XBRL lags a new filing; the records (3.11) and
the filing passages cover those.

### 3.13 Filing maintenance scripts

**`scripts/backfill_filing_records.py`** (records only):

```
docker compose --env-file .env.docker exec -T ai-service \
    python -m scripts.backfill_filing_records [--ticker T ...] [--redo] \
    [--latest N] [--filename NAME ...] [--tier highest_accuracy] [--apply]
```

- Which filings: the FILING documents core-service lists per ticker
  (`get_existing_document_filenames`, named
  `<TICKER>_<FORM>_<date>.txt`), oldest first. Without `--redo` a filing
  that already has a row is skipped (safe to re-run). `--latest N` keeps
  each ticker's N newest filings first, then drops those with a row
  (without `--redo`), so the window never slides back to older filings.
  `--filename NAME` (repeatable) keeps only those documents; without
  `--ticker` the tickers come from the names; a name that matched nothing
  prints "NOT FOUND" (without `--redo` a filing that already has records is
  skipped). Without `--ticker` or `--filename`, every ticker with FILING
  documents (direct read of `core.documents`).
- Per filing (only with `--apply`): the raw text from the
  `raw_filings/` archive (plus `.amendment.txt`), else from EDGAR as
  ingestion reads it (`fetch_filing_text_sync`: recent block, older pages
  until that date, the same 10-K/A merge); the period of report from
  edgartools, else `filing_period`; one records-only call
  (`extract_filing_records` with `--tier` or the default tier); the row is
  upserted. A filing larger than 500,000 characters (`LARGE_FILING_CHARS`)
  is sent only after a 65 s pause (`IDLE_WAIT_SECONDS`), so earlier calls no
  longer count in the keys' per-minute input window. The "stored" line
  names the period, the counts per category and the serving model.
- A failed filing prints `FAILED <file>: <reason>`, stores nothing (the
  old row is kept) and the exit code is 1; the rest continue. Default is a
  dry run: no LLM, SEC or database writes.
- Tier: new filings are always extracted on HIGHEST at ingestion. For a
  re-extraction the owner rule is `--tier highest_accuracy` for each
  ticker's two newest filings (`--latest 2`); a tier comparison on four
  filings found 22 material misses on Lite and 13 on HIGHEST. The script
  does not choose the tier by age. Since the simplification no
  re-extraction is needed; a redo is only for restoring item names.

**`scripts/regenerate_filing_summaries.py`** (summary only), for a stored
summary that is bad (for example cut off):

1. `--filename F` (repeatable): reads the archived raw text, makes one
   HIGHEST `summarize_filing` call and saves
   `/app/_regenerated/<F>.summary.txt`; prints the current core document
   id; writes nothing to the database. A fallback or still-truncated
   summary counts as not ok.
2. The owner deletes the old document with core-service's
   `scripts.delete_documents --id <id> --confirm` (which also removes its
   RAG chunks).
3. `--filename F --apply`: stores the saved summary as the filing document
   (normal RAG ingestion follows). A filing whose old document still exists
   is skipped. The records row is not touched; redo it with the backfill
   (`--filename F --redo`).

Each filename runs in its own try/except; the exit code is 1 when any is
not ok.

---

## 4. Configuration

| Setting | Default | File | Meaning |
|---|---|---|---|
| `EPISODIC_CONTEXT_LIMIT` | 3 | `services/ai-service/config.py` | earlier calls shown to the Judge; 0 = off |
| `AI_ACCURACY_EVALUATION_DAYS` | 30 | same | review window for outcomes |
| `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` | 0.85 | same | cosine bar for superseding (calibrated for `gemini-embedding-2`; use 0.90 with `mistral-embed`) |
| `ARCHIVAL_RECYCLED_SHARE_MAX` | 0.5 | same | recycled share that blocks archival |
| `KG_EXTRACTION_LLM_TIER` | `cheap` | same | tier of the triple extraction call |
| `SEC_EDGAR_USER_AGENT` | empty | same | "Company Name contact@domain.com"; required by SEC for every request (filings, XBRL, official company names) |
| `SEC_EDGAR_MAX_REQUESTS_PER_SECOND` | 8.0 | same | spacing of the SEC requests sent by `ai/providers/sec_edgar.py` in one process (SEC allows 10/s); 0 = no spacing; `edgartools` requests have their own limiter (`EDGAR_RATE_LIMIT_PER_SEC`, library default 9, `.env.example` sets 2) |
| `FILING_RECORDS_MAX_FILINGS` | 5 | same | newest filings whose records agents load and `search_filings` shows |
| `FILING_RECORDS_BACKFILL_LLM_TIER` | `cheap` | same | default tier of the backfill's records-only call (`--tier` overrides; ingestion uses `FILING_LLM_TIER`) |
| `FILING_LLM_TIER` | `highest_accuracy` | same | tier of the filing summary and records calls at ingestion (`highest_accuracy` or `cheap`) |
| `PRIOR_NOTE_MAX_SIMILARITY` | 0.9 | same | `prior_note_similarity` at which a Risk or News answer counts as a copy of a prior note (archive skip, chapter 15) |
| code: `_MIN_SCORE_TO_REMEMBER` | 0.7 | `ai/memory/agent_memory.py` | procedural memory bar |
| code: `_DEFAULT_LIMIT` | 2 | same | hints per role |
| code: `_MAX_STEPS_PER_TRAJECTORY` | 4 | same | steps kept per example |
| code: `prune_beyond_retention(keep=3)` | 3 | `domains/memory/repository.py` | rows kept per role, ticker and owner |
| code: `_MIN_WORDS_TO_ARCHIVE` | 40 | `ai/knowledge/archival.py` | shortest archived note |
| code: `_NEAR_DUPLICATE_DEDUP_LOOKBACK` | 3 | same | recent reports compared |
| code: `_MAX_TRIPLES` | 15 | `ai/knowledge/archival.py` | triples per report |
| code: `_HOLD_FLAT_BAND` | 0.02 | `ai/workflows/review_workflow.py` | HOLD counts as correct within +-2% |
| code: `_EXTRACTION_MAX_TOKENS` | 2,048 | `ai/knowledge/archival.py` | output budget of the triple extraction |
| code: `_MAX_PER_FORM` | 10-K 1, 10-Q 4 | `ai/ingestion/sec_edgar_filings.py` | filings in the per-form ingestion window |
| code: `_EDGAR_API_TIMEOUT_SECONDS` | 15 | same | how long the caller waits for each `edgartools` listing call (each HTTP request also has the library's `EDGAR_HTTP_TIMEOUT`, default 30 s) |
| code: `_TRUNCATION_STAIRCASE` | 1.2M: 100%, 1.6M: 80%, 1.8M: 70%, 2.1M: 60%, 2.5M: 50%, above: skip | `ai/knowledge/filing_summary.py` | summary-call input cut |
| code: `RECORDS_MAX_CHARS` | 900,000 | same | records-call input cut |
| code: `_SUMMARY_MAX_TOKENS` / `_RECORDS_MAX_TOKENS` / `_RETRY_MAX_TOKENS` | 12,288 / 10,240 / 16,384 | same | output limits; one retry after a length stop |
| code: `_FILING_SUMMARY_TIMEOUT_SECONDS` | 300 | same | request timeout of both filing calls (at most 2 attempts each) |
| code: `QUOTE_MIN_CHARS` / `_QUOTE_PART_MIN_CHARS` | 25 / 12 | `ai/knowledge/filing_records.py` | quote check (letters and digits) |
| code: `_UNIT_REQUIRED_MIN` | 10 | same | currency amount that needs a scale word |
| code: `_PERIOD_END_TOLERANCE_DAYS` | 7 | same | own-period tolerance of records |
| code: `LARGE_FILING_CHARS` / `IDLE_WAIT_SECONDS` | 500,000 / 65 | `scripts/backfill_filing_records.py` | pause before a large backfill call |
| code: `_QUARTER_MIN_DAYS`..`_QUARTER_MAX_DAYS` | 80..100 | `ai/providers/sec_edgar.py` | a quarter fact |
| code: `_YEAR_AGO_MIN_DAYS`..`_YEAR_AGO_MAX_DAYS` | 350..380 | same | year-ago quarter and annual facts |
| code: `_FISCAL_START_TOLERANCE_DAYS` / `_YTD_LENGTH_TOLERANCE_DAYS` | 7 / 10 | same | fiscal-year start; prior year-to-date length |

---

## 5. Design decisions and why

**Memory is gated, not just stored.** Alternative: store every output and
let retrieval sort it out. Reason: each stored mistake comes back as
"prior knowledge" in later prompts (off-topic procedural examples,
self-repeating warned notes, recycled macro notes all come from storing
too much).

**The Judge sees outcomes, not rationales.** Alternative: show earlier
reasoning. Reason: models restate earlier reasoning as new evidence.
Action, confidence and outcome are enough to calibrate.

**Episodic scope per user.** Reason: one user's calls (which may draw on
private documents) must not shape another user's analysis.

**Procedural memory stores tool steps only.** Reason: a past final answer
contains past facts (prices, headlines) that look like guidance, and a
fabricated price in it reappears in later runs.

**Procedural memory per owner.** Reason: the stored steps contain the
search and filing queries of one user's analysis; another user's analysis
must not see them. Rows are therefore read only for the same owner (or
only ownerless rows for an analysis without an owner).

**Supersede after storing.** Reason: replacing must never lose both the
old and the new note (section 3.5).

**KG relations are not deduplicated on write.** Alternative: merge on
insert. Reason: keeping every extraction as its own row keeps full
provenance and a simple write path; aggregation on read gives counts and
date ranges, and can be changed without touching stored data.

**Corroboration counts roles, not extractions.** Reason: the same agent
re-extracting its own earlier claim is repetition, not confirmation.

**Entity resolution stays simple.** Names are matched by exact upper-case
string within one entity type, with the ticker rewrite as the one targeted
fix ("AMD" and "Advanced Micro Devices" are two nodes otherwise). Real
entity resolution (embeddings, an alias table, an LLM merge pass) is future
work, once data shows which approach is worth it.

**Filing facts come from structure, not from prose.** Alternative: let
code read figures or statuses out of the LLM summaries. Reason: a parser
of summary prose injects wrong facts as "computed in code"; word
lists over model text cannot work for every company. So the material
numbers come from SEC XBRL (the filer's own tags), and the records carry
quotes that code finds in the filing. Code checks presence and structure
(quote found, amount found, enum, date, period), never meaning; a record
that fails is dropped, never repaired.

**Each filing is extracted once.** Alternative: re-extract to improve
recall, or give each extraction the previous filing's records as a
checklist. Reason: re-extractions lost material items that the first run
had found, and the checklist added complexity without fixing recall. The
material numbers are covered by XBRL instead; a missed non-material record
is accepted.

**The raw filing text is archived but never indexed.** Reason: the summary
is the retrievable artifact; indexing the raw text too would duplicate it
in retrieval. The archive exists for audits and the backfill.

---

## 6. When things go wrong

| Symptom | Cause | Diagnose |
|---|---|---|
| Judge prompt has no track record | first analysis of the ticker, limit 0, or a DB error | log `episodic_memory_node.retrieved` (`count`, `scored`) or `episodic_memory_node.retrieval_failed` |
| outcomes stay NULL | review job not run, or no price data (retried on each run for up to 30 days after the window ends, then left unscored) | `bash scripts/logs.sh scheduler grep review-run`; log `review.no_price_data` |
| an agent note was not archived | one of the gates | `bash scripts/logs.sh ai-service grep archival.skipped` |
| a note is found in search but has no `ai.agent_reports` row | writing the rows failed and the cleanup of its indexed chunks failed too | `bash scripts/logs.sh ai-service grep archival.orphan_chunk_cleanup_failed` (`report_id`) |
| old note still present next to a new one | supersede failed (logged), or similarity below the threshold | `archival.supersede_failed`, `archival.found_near_duplicate` (`similarity`) |
| KG empty for a ticker | extraction failed, no archived notes, or only other users' notes | `archival.kg_extracted` (`triple_count`), `kg_extraction.unavailable` |
| KG facts under a long company name | entity not rewritten to the ticker | check `ai.ticker_names` for the ticker; `kg_extraction.ticker_name_resolution_failed` |
| agents keep repeating old wording | recycled content below the bar or not tagged | `archival.skipped_recycled_prior_notes` (`recycled_share`) |
| a filing has no records row | no parseable block, a category missing, every record dropped, or the store failed | `filing_summary.records_block_missing`, `filing_summary.records_incomplete`, `filing_records.validated` (`dropped` by reason), `filing_records.store_failed`; then `python -m scripts.backfill_filing_records --filename <file> --apply` |
| a known material item is not in the records | omitted by the model (often a fallback model) or dropped by a check | `filing_records.dropped` (title and reason) for that file; `filing_summary.records_served_by`; check the SEC XBRL line, which carries the material below-operating numbers |
| records call refused on input size | free-tier limit of 250,000 input tokens per minute per key | `filing_summary.records_input_truncated`, `filing_summary.records_call_input` (`sent_chars`); large filings need the 65 s pause |
| a stored summary ends mid-sentence | output limit even after the retry | `filing_summary.output_truncated`; `scripts.regenerate_filing_summaries` (3.13) |
| a new filing was never stored | larger than 2.5M characters, or the summary failed | `sec_edgar_filings.filing_too_large_skipped`; `sec_filings.fallback_not_persisted` (retried on the next run) |
| filing passages say "period: not available from SEC EDGAR" | EDGAR has no `reportDate` for that filing (XOM's filings before its CIK change) | `sec_edgar.filing_period_unavailable` |
| no "Latest quarter" line in Fundamental or the Judge | no usable us-gaap quarter (IFRS filer) or the lookup failed | `sec_edgar.latest_quarter_unavailable`, `sec_edgar.quarter_results_failed` |
| no year-to-date line | first fiscal quarter, a 10-K, or no fiscal-year start found | `sec_edgar.year_to_date_unavailable` (`metrics` gives the reason per metric) |
| the line says the newer 10-Q "is not in XBRL yet" | SEC's XBRL API lags the filing | expected; the newest filing's passages and records cover the gap |

---

## 7. Demo walkthrough

All queries below are read-only.

1. **Archived reports of the latest analyses.**

   ```
   bash scripts/psql.sh -c "SELECT r.agent_role, r.ticker, r.category, r.created_at, left(r.content, 80) AS start FROM ai.agent_reports r ORDER BY r.created_at DESC LIMIT 10;"
   ```

   Say: "Only Macro, News, Fundamental and Risk archive notes, and only
   when they pass every gate. A near-duplicate replaces the older note."

2. **Which reports were skipped, and why, today.**

   ```
   bash scripts/logs.sh ai-service grep archival.skipped
   bash scripts/logs.sh ai-service grep archival.superseded_near_duplicate
   ```

3. **The knowledge graph for one ticker.**

   ```
   bash scripts/psql.sh -c "SELECT s.name AS source, k.relation_type, t.name AS target, round(k.confidence::numeric, 2) AS conf, r.agent_role, k.created_at::date FROM ai.kg_relations k JOIN ai.kg_entities s ON s.id = k.source_entity_id JOIN ai.kg_entities t ON t.id = k.target_entity_id JOIN ai.agent_reports r ON r.id = k.source_report_id WHERE s.normalized_name = 'AMD' OR t.normalized_name = 'AMD' ORDER BY k.created_at DESC LIMIT 20;"
   ```

   Point at `agent_role`: this is what decides whether a repeated fact
   counts as corroboration.

4. **Entity and relation counts.**

   ```
   bash scripts/psql.sh -c "SELECT entity_type, count(*) FROM ai.kg_entities GROUP BY 1 ORDER BY 2 DESC;"
   bash scripts/psql.sh -c "SELECT count(*) AS relations, count(DISTINCT source_report_id) AS reports FROM ai.kg_relations;"
   ```

5. **Episodic memory: earlier calls and outcomes for a ticker.**

   ```
   bash scripts/psql.sh -c "SELECT created_at::date, recommendation, round(confidence::numeric, 2) AS conf, outcome, round(outcome_return::numeric, 4) AS ret, user_id IS NULL AS system FROM ai.episodic_memories WHERE ticker = 'AMD' ORDER BY created_at DESC LIMIT 10;"
   ```

   Say: "Rows younger than 30 days have no outcome yet; the review job
   fills it in. The Judge sees the newest three, never the old rationale."

6. **Procedural memory per role.**

   ```
   bash scripts/psql.sh -c "SELECT a.name AS role, m.ticker, m.user_id, m.score, m.created_at::date, m.content->'steps' AS steps FROM ai.agent_memories m JOIN ai.agents a ON a.id = m.agent_id ORDER BY m.created_at DESC LIMIT 10;"
   ```

7. **Show the injected KG context in the log of a run.**

   ```
   bash scripts/logs.sh ai-service grep supervisor_node.kg_context_injected
   ```

8. **Filing records per filing.**

   ```
   bash scripts/psql.sh -c "SELECT ticker, form, filing_date, report_date, (SELECT string_agg(k || '=' || jsonb_array_length(records->k), ', ') FROM jsonb_object_keys(records) k WHERE jsonb_typeof(records->k) = 'array') AS counts FROM ai.filing_records ORDER BY ticker, filing_date DESC LIMIT 12;"
   ```

   Say: "One row per filing; each record has a quote that code found in the
   filing text. The material below-operating numbers do not depend on these
   records: they come from SEC XBRL."

9. **What a records extraction dropped, and why.**

   ```
   bash scripts/logs.sh ai-service grep filing_records.dropped
   ```

---

## 8. Check your understanding

1. Which memory level is the Judge's track record, and who fills in its
   outcome?
2. Why does a system analysis never see a user's episodic rows?
3. An agent output has score 0.65. Can it become procedural memory?
4. Which four roles archive their reports, and into which categories?
5. A new Risk note is 0.88 similar to yesterday's Risk note for the same
   ticker and owner, with `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD=0.85`. What
   happens, and in which order?
6. Why is a KG fact extracted five times from Risk reports shown as
   "re-extracted 5x ... not corroboration"?
7. What does `[SUPERSEDED by the MOST RECENT reading]` mean?
8. What happens to an agent note when the analysis's owner cannot be read?
9. A 10-Q record says a lawsuit is pending, but its quote is not in the
   filing. What happens?
10. Why does the Judge see "Latest quarter in SEC XBRL -- the newer 10-Q
    filed ... is not in XBRL yet"?
11. AMZN tags an upward adjustment of $50.49B and equity securities gains
    of $1.30B in the same quarter. Is the adjustment shown as "of which"?

<details>
<summary>Answers</summary>

1. L2 episodic memory (`ai.episodic_memories`); the daily review job sets
   `outcome` and `outcome_return` once the analysis is 30 days old.
2. `list_recent_for_ticker` with no user returns only rows with a NULL
   user, so private calls never cross over.
3. No: the bar is 0.7 (and 0.65 is the score of a retried output; a role
   flagged for retry writes no procedural memory anyway).
4. Macro -> macro_research, News -> news, Fundamental -> analyst_report,
   Risk -> risk_analysis.
5. It is a near-duplicate (0.88 >= 0.85). The new note is chunked,
   embedded, indexed and recorded first; then the old note's chunks, KG
   relations and row are deleted; then KG triples are extracted from the
   new note.
6. All five extractions came from one role, and that role reads its own
   earlier facts back; repetition is not independent confirmation.
7. Several numeric readings of the same relation exist; a newer reading
   exists, so this one should not be treated as current.
8. It is archived with an empty visibility (not system, no owner), so no
   one can retrieve it (fail closed), and no superseding happens.
9. The record is dropped (`quote_not_in_filing`), counted in
   `filing_records.validated` and logged in `filing_records.dropped`; it is
   never repaired.
10. SEC's XBRL API lags new filings; the submissions JSON already lists a
    newer 10-Q. The newest filing's passages and records win for the newest
    quarter.
11. No. "of which" is used only when the adjustment is not larger than the
    gains in every period where both are tagged; a larger adjustment cannot
    be a component of the gains line, so it is shown as its own item.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/ai/memory/agent_memory.py` | L2 read path; L4 gates, write, read (`combine_context` is in `ai/agents/loop.py`) |
| `services/ai-service/domains/memory/model.py` | `EpisodicMemory`, `AgentMemory` |
| `services/ai-service/domains/memory/repository.py` | scoped queries, retention pruning |
| `services/ai-service/ai/knowledge/archival.py` | archival gates, visibility, near-duplicate supersede, KG extraction call; triple extraction prompt and validation (`extract_entities_relations`) |
| `services/ai-service/ai/knowledge/recycled_content.py` | recycled share, amounts found only in prior agent notes (chapter 15) |
| `services/ai-service/ai/knowledge/kg_context.py` | fetch, format, tag, inject KG facts; official names, core-name matching |
| `services/ai-service/ai/ingestion/sec_edgar_filings.py` | listing new filings, raw archive, `ingest_new_filings` |
| `services/ai-service/ai/knowledge/filing_summary.py` | summary call (staircase, retry, fallback) and the records-only call |
| `services/ai-service/ai/knowledge/filing_records.py` | records schema and prompt, checks, amount parser, rendering, `record_by_id`, storage |
| `services/ai-service/ai/providers/sec_edgar.py` | SEC throttle, ticker map, `filing_period`, XBRL quarter line |
| `services/ai-service/domains/filings/` | `filing_records` model and repository |
| `services/ai-service/scripts/backfill_filing_records.py` | records extraction for stored filings |
| `services/ai-service/scripts/regenerate_filing_summaries.py` | regenerate chosen stored filing summaries (two steps) |
| `services/ai-service/ai/graph/nodes.py` (`load_fresh_filing_context`) | new-filing check during an analysis |
| `services/ai-service/domains/knowledge/model.py` | `AgentReport`, `KgEntity`, `KgRelation`, `TickerName` |
| `services/ai-service/domains/knowledge/repository.py` | report and KG queries, aggregation, viewer filter |
| `services/ai-service/ai/tools/knowledge_graph_tool.py` | `query_knowledge_graph` tool |
| `services/ai-service/ai/tools/research_archive_tool.py` | `search_prior_*` tools |
| `services/ai-service/ai/workflows/analysis_workflow.py` | when each memory is written |
| `services/ai-service/ai/workflows/review_workflow.py` | episodic outcomes |

---

## 10. Related chapters

- [15. The agents](15-ai-agents.md) (the ReAct loop and the scratchpad)
- [16. The analysis pipeline](16-ai-analysis-pipeline.md) (where context is injected)
- [17. Retrieval-augmented generation](17-ai-rag.md) (the index the notes live in)
- [19. Workflows, tier monitoring and backtests](19-ai-workflows-backtest.md) (the review job)
- [20. PostgreSQL](20-postgresql.md), [21. Qdrant](21-qdrant.md)
- [../ERD.md](../ERD.md)
