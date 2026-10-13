# 17. Retrieval-augmented generation (RAG)

> RAG ("retrieval-augmented generation") means: before an LLM answers,
> search a document library for the passages that matter and give them to
> the model as its only sources. AlphaMind's library holds SEC filings,
> earnings-call transcripts, analyst reports, news digests, macro research
> and the agents' own archived notes. The code in
> `services/ai-service/ai/rag/` turns uploaded files into searchable
> chunks (parse, chunk, embed, index) and finds the best chunks for a
> query with a hybrid search: keyword search (BM25) and meaning search
> (vectors in Qdrant), fused with Reciprocal Rank Fusion. The same search
> serves the research agents and the user-facing "ask" endpoint
> (`POST /api/v1/rag/ask`).

---

## 1. The big picture

There are two flows: writing into the index (ingestion) and reading from
it (retrieval).

```
 INGESTION (asynchronous, event-driven)

 user upload                          auto-ingestion (03:00 New York)
 POST /api/v1/research/documents      SEC EDGAR filings, NewsAPI digests,
        |                             monthly FRED macro report
        v                                        |
 core-service: file -> object storage            | create_system_document
 row in core.documents (status uploaded)         |
        |   <------------------------------------+
        | event document.uploaded (Redis stream, durable)
        v
 ai-service event_listener -> run_rag_ingestion_workflow
   status parsing   : load bytes (object storage) -> parse (pypdf / utf-8)
   status chunking  : chunk_document (by category)
   status embedding : embed_texts (Gemini, 1536 dims)
                      delete old points -> index_chunks
                         |-> Qdrant  (vector + payload)
                         |-> BM25    (in-memory, rebuilt)
                      ai.document_chunks, ai.embedding_jobs
   status ready     (or failed)
        |
        v  event document.status_changed -> core.documents.status, WebSocket

 agent reports (after a completed analysis) -> archive_agent_report
        -> chunk -> embed -> index (same Qdrant + BM25)     (chapter 18)


 RETRIEVAL (synchronous, per query)

 query ("AMD data center revenue growth")
   |
   +-- query expansion (Lite LLM, + KG-related names)  [agent tools only]
   |      -> [query, variant1, variant2, variant3]
   v
 for every variant:
   BM25 search  (category / ticker / visibility mask)   -> ranking
   vector search (Qdrant filter: category, ticker, visibility) -> ranking
   |
   v
 Reciprocal Rank Fusion (weighted per category) -> one fused ranking
   |
   v
 resolve payloads, final visibility check, top_k
   |
   +-- FilingRetriever: reserve 2 slots for the newest filing
   v
 chunks with text, date, source -> agent tool output / RAG answer
```

---

## 2. Concepts you need

**Chunk.** A piece of a document small enough to search and to fit into a
prompt, here a few hundred words. Each chunk is stored with its text and
metadata (ticker, category, date, owner).

**Embedding.** A list of numbers (a vector) that represents the meaning of
a text. Texts with similar meaning have vectors that point in similar
directions. AlphaMind uses Google's `gemini-embedding-2` model with 1536
numbers per vector (`config.py`: `EMBEDDING_PROVIDER=google`,
`GOOGLE_EMBEDDING_MODEL`, `GOOGLE_EMBEDDING_DIMENSIONS`).

**Cosine similarity.** How close two vectors point: 1.0 is the same
direction, 0 is unrelated. Qdrant's collection uses the cosine distance
(`qdrant.py::ensure_collection`).

**Qdrant.** A vector database. It stores one *point* per chunk: an id, the
vector and a JSON *payload*. It finds the nearest vectors to a query
vector, optionally with a filter on payload fields. One collection,
`research_documents_gemini` (`QDRANT_COLLECTION_RESEARCH`). Chapter 21
covers Qdrant itself.

**BM25.** A classic keyword ranking formula: a chunk scores high when it
contains the query's words, especially rare words, and is not too long.
It is very good at exact terms ("10-Q", "EBITDA", a product name) where
vectors can be fuzzy. AlphaMind uses the `bm25s` library.

**Hybrid search.** Running both BM25 and vector search and combining the
results. Each method catches what the other misses.

**RRF (Reciprocal Rank Fusion).** A way to combine rankings without
comparing their raw scores (a BM25 score and a cosine similarity are on
different scales). Each result gets `1 / (k + rank)` from every ranking
it appears in, summed; `k` is 60. A chunk ranked 1st by BM25 and 3rd by
vectors scores `1/61 + 1/63`. Rank, not score, is what counts.

**Query expansion (RAG-Fusion).** Asking an LLM for a few rephrasings of
the query, searching with all of them, and fusing everything. A relevant
chunk phrased differently from the user's words then still surfaces.

**Recall@K and MRR.** Evaluation measures. Recall@5: in what share of test
questions is the right chunk among the top 5 results? MRR (mean
reciprocal rank): the average of `1/rank` of the right chunk, so finding
it at rank 1 is better than at rank 5.

**Golden set.** A fixed list of test questions, each linked to the one
chunk that holds its answer (`tests/fixtures/rag_golden_set.json`).

**Server-Sent Events (SSE).** A simple HTTP streaming format: the server
keeps the response open and sends lines `event: <name>` and `data:
<json>`. Used to stream an answer token by token.

---

## 3. How it works in AlphaMind

### 3.1 Document categories

`shared/enums.py::DocumentCategory` drives chunk sizes, retrieval
filters and weights. The category describes what the text is, not its
file format.

| Category | Typical source |
|---|---|
| `filing` | SEC 10-K/10-Q summaries (auto-ingestion, seeds, uploads) |
| `earnings_call` | earnings-call transcripts (seed documents, uploads) |
| `analyst_report` | analyst PDFs; also Fundamental agent notes |
| `news` | daily NewsAPI digests; also News agent notes |
| `macro_research` | monthly FRED report, seeds; also Macro agent notes |
| `risk_analysis` | Risk agent notes |

core-service accepts uploads only in these categories
(`ALLOWED_DOCUMENT_CATEGORIES`) and only `.pdf`, `.txt`, `.md`
(`ALLOWED_DOCUMENT_EXTENSIONS`), up to `MAX_UPLOAD_SIZE_BYTES` (20 MB).

### 3.2 Parsing

`shared/storage.py::download_bytes` downloads the file from object
storage (chapter 22). `ai/rag/chunking.py::parse_document`
then picks a parser by document type:

- `pdf`: `pypdf` (`PdfReader`), page texts joined with blank lines. It is
  pure Python with a permissive licence. It has no OCR (optical character
  recognition), so a scanned PDF gives almost no text.
- `txt`, `md`: UTF-8 decode with replacement characters.

An empty result is treated as an error in the workflow ("likely a
scanned/image-only PDF ... no OCR fallback").

### 3.3 Chunking

`ai/rag/chunking.py::chunk_document(text, category, metadata=...)`:

1. Split into paragraphs on blank lines.
2. Drop low-value paragraphs:
   - legal disclaimers: a *run* of consecutive paragraphs that each
     contain at least one phrase from `_BOILERPLATE_MARKERS` ("accepts no
     responsibility", "registration number", ...), when the run's
     distinct markers together reach 2 (`_boilerplate_run_indices`). A
     single paragraph with 2 markers is a run of one and is dropped; a
     run of one-line paragraphs is dropped as a whole, because some
     analyst PDFs split one disclaimer over several lines;
   - "tag soup": more than 25% of words look like `prefix:Name` XBRL tags
     (`_is_tag_soup_paragraph`). XBRL is the machine-readable tagging
     format of SEC filings; its tags carry no meaning for a reader.
   If filtering would empty a short document (2 paragraphs or fewer), the
   filter is skipped.
3. Break any "word" longer than 200 characters (`_MAX_WORD_CHARS`); a
   leftover tag run without spaces can otherwise produce a chunk over the
   embedding model's token limit.
4. Pack paragraphs into chunks up to the category's target size, with an
   overlap of the previous chunk's last words; a paragraph longer than the
   target is split into overlapping windows.

Sizes are counted in words, not tokens (about 1.3 tokens per word for
English financial text; `WORDS_TO_TOKENS` in `indexing.py`):

| Category | Target words | Overlap words |
|---|---|---|
| `filing` | 600 | 100 |
| `analyst_report` | 400 | 80 |
| `earnings_call` | 350 | 70 |
| `news` | 250 | 40 |
| `macro_research` | 400 | 80 |
| `risk_analysis` | 400 | 80 |

Filings get the largest chunks because one financial fact is often spread
over several sentences; news paragraphs stand on their own.

### 3.4 Embeddings

`ai/rag/embedding.py::embed_texts(texts, purpose="document")` and
`embed_query(text)` (purpose `"query"`):

- With `EMBEDDING_PROVIDER=google` the texts are grouped into batches of
  at most 20 inputs and about 15,000 estimated tokens (`google_batches`).
  Each batch goes through the pooled Google keys: a 429 rotates to the
  next key; when every key is rate-limited, or on a 5xx, it waits 5, 10,
  20, 40 seconds (`_GOOGLE_RETRY_WAITS_SECONDS`; the all-keys-429 wait
  logs `embedding.google_pool_rate_limited_waiting`) and then gives up. If
  every pooled project still answers 429 after the last wait, it raises
  `EmbeddingRateLimitedError` (a subclass of `EmbeddingError`), which ingestion treats as transient (the event is delivered again
  instead of the document failing for good).
- Each embedding request has a time limit,
  `EMBEDDING_REQUEST_TIMEOUT_SECONDS` (30.0, `config.py`). A request that
  has not answered by then is treated like a 5xx: it is retried with the
  same waits (log `embedding.google_timeout_retrying`). The last timeout
  raises `TimeoutError`, which ingestion treats as transient (the event
  is delivered again). Without the limit, a request that never answered
  could hold up ingestion, which handles one document at a time.
- For `gemini-embedding-2` the input text is prefixed by purpose:
  `"task: search result | query: ..."` for queries and `"title: none |
  text: ..."` for documents. The model's retrieval embeddings are
  asymmetric, so queries and documents are formatted differently.
- Every vector is L2-normalised (scaled to length 1).
- `check_embedding_configuration` runs at startup and refuses to start if
  `QDRANT_VECTOR_SIZE` does not match the model's dimensions.

A Mistral path (`mistral-embed`, 1024 dims) also exists
(`EMBEDDING_PROVIDER=mistral`). It is not used because Mistral's
embedding endpoint answered "backend out of capacity" to about three
quarters of requests when measured (September 2026). It is also less
guarded than the Gemini path: its requests use the chat time limit
(`REQUEST_TIMEOUT_SECONDS` of the Mistral provider), not
`EMBEDDING_REQUEST_TIMEOUT_SECONDS`, and its fallback for a 400 answer
(`_embed_individually_with_truncation_fallback`, which embeds one text
at a time and halves an oversized input) has no time limit and no
429/503 handling. Review both before switching back to it. Vectors from
different models cannot be compared, so switching the embedding model
needs a new collection: `scripts/reembed_collection.py` copies every
point with new vectors and keeps the old collection as a rollback copy.

### 3.5 Indexing: Qdrant and BM25

`ai/rag/indexing.py::index_chunks(...)` writes each chunk twice.

**Qdrant.** Point id = `chunk_point_id(document_id, chunk_index)`, a UUID5
derived from the document id and chunk number. Re-indexing a document
therefore overwrites its points instead of duplicating them. Payload
fields: `document_id`, `chunk_id`, `chunk_index`, `ticker`, `category`,
`text`, `source` (file stem or `agent:<role>`), `created_at` (indexing
time), `content_date` (the date the content is about, e.g. the filing
date), and the visibility fields `owner_id`, `organization_id`,
`is_system` (3.8).

**BM25.** `BM25Index` keeps every chunk's text in memory, together with its
category, ticker and visibility, and rebuilds the `bm25s` index from
scratch on every write (the library has no incremental update). The cost
of a rebuild grows linearly with the corpus: tokenizing and indexing
5,000 chunks of 400 words takes about 1.3-1.7 s. So the rebuild never
runs on the event loop:

- `index_chunks`, `delete_document_chunks` and `rebuild_bm25_from_qdrant`
  call `BM25Index.add_many` / `remove_many` through
  `asyncio.to_thread`. The ai-service event loop keeps serving SSE
  streams, `/health`, the analysis queue and timers during a rebuild
  (measured: the longest loop pause during a 5,000-chunk rebuild fell
  from 1.24 s to about 0.02 s).
- A `threading.Lock` serialises the writers, so two documents indexed at
  the same time rebuild one after the other, each from the full corpus.
- Each rebuild produces a new immutable snapshot (`_BM25Snapshot`: the
  `bm25s` model, its chunk-id order, and the category/ticker and
  visibility maps of exactly that corpus) and swaps it in with one
  assignment. `search` reads the snapshot once, so a search that runs
  during a rebuild uses the previous complete index and never a mix of
  old model and new metadata. The awaiting writer returns only after the
  swap, so a search started after `index_chunks` returns sees the new
  chunks.

The in-memory index is lost on restart, so `main.py` calls
`rebuild_bm25_from_qdrant()` at startup: it scrolls every Qdrant payload
and rebuilds BM25 from it in one write (log event
`rag.bm25_rebuilt_from_qdrant` with `chunk_count`).

**Bookkeeping in PostgreSQL.** `record_document_chunks` writes one
`ai.document_chunks` row per chunk (`document_id`, `chunk_index`,
`token_count` estimate, `qdrant_point_id`); `ai.embedding_jobs` records
the model name and the status of the embedding job.

**Deletion.** `delete_document_completely(document_id, session)` removes a
document from all four stores: Qdrant and BM25 (via
`delete_document_chunks`, which first scrolls Qdrant for the chunk ids,
because BM25 keeps no document id), then `ai.document_chunks` and
`ai.embedding_jobs`. It logs the counts and returns a `DeletionResult` with the Qdrant/BM25
chunk count.

### 3.6 The ingestion workflow

`ai/workflows/rag_ingestion_workflow.py::run_rag_ingestion_workflow` is
started by `event_listener.py::_handle_document_uploaded` when the durable
event `document.uploaded` arrives. The event payload carries
`document_id`, `storage_bucket`, `storage_key`, `document_type`,
`category`, `ticker`, `content_date`, `filename`, `owner_id`,
`organization_id` and `is_system`. ai-service does not read `is_system`:
`ChunkVisibility.from_upload_event` derives the system flag from
`owner_id` (the system owner id, 3.8), so the two services cannot
disagree about who owns a chunk.

Steps and status events (`document.status_changed`, published twice: as
a durable event, which is stored in a Redis stream until it is
processed, and as a cross-service pub/sub message, which is delivered
only to listeners connected at that moment). `_publish_status` retries
the durable publish after 1, 5 and 15 seconds
(`_STATUS_PUBLISH_RETRY_WAITS_SECONDS`, log
`document_status_publish_retrying`) and raises if the last try fails. A
failed pub/sub publish is only logged
(`document_status_live_publish_failed`):

| Status | Work |
|---|---|
| `parsing` | download bytes, parse; empty text -> error |
| `chunking` | `chunk_document` with metadata `ticker`, `content_date`; zero chunks -> error |
| `embedding` | new `ai.embedding_jobs` row (old ones for this document deleted); `embed_texts`; then delete the document's old points and `index_chunks`; replace `ai.document_chunks` rows |
| `ready` | job status `ready` |
| `failed` | any exception: job set to `failed`; if this run already touched the index (old points deleted), the document's Qdrant/BM25 points are deleted again (`delete_document_chunks`; log `rag_ingestion_failure_chunk_cleanup_failed` if that fails) and its chunk rows removed, so a `failed` document is not searchable |

Embedding happens *before* the old points are deleted, so a re-ingestion
that fails at the embedding step keeps the previous index entries.

**Transient versus permanent failures.** `is_transient_ingestion_error`
treats timeouts, connection errors, LLM rate-limit/timeout errors,
`EmbeddingRateLimitedError` (every pooled Gemini project kept answering
429), and HTTP 408/429/500/502/503/504 as transient. For those the workflow raises
`TransientIngestionError` after publishing `failed`; the durable
subscriber then leaves the event unacknowledged and it is delivered again,
at most `_MAX_DELIVERY_ATTEMPTS` (5) times (`shared/events.py`). Anything
else (a bad file, empty text) is permanent and swallowed.

core-service listens for `document.status_changed`, stores the status on
`core.documents` and broadcasts it on the document's WebSocket channel
(chapter 25). Uploads are handled one at a time: the handler awaits the
whole workflow.

**Duplicates.** core-service looks for the same owner, organization,
ticker, category and filename before storing anything. A duplicate returns
the existing document without a new event, except: a document in status
`failed` is re-processed in place, and one whose non-final status has
not changed for too long has its event re-published: `uploaded` for 10
minutes (`STUCK_DOCUMENT_AFTER`), `parsing`, `chunking` or `embedding` for
60 minutes (`STUCK_PROCESSING_AFTER`), both measured from the document's
last status change (`status_changed_at`; constants in
`services/core-service/domains/research/repository.py`). A document still
being processed is therefore not ingested a second time. (core-service writes `document.uploaded` to its outbox
table in the upload's transaction, so a Redis outage only delays the
event; see [24-events.md](24-events.md), section 3.5.)

### 3.7 Deletion chain

`DELETE /api/v1/research/documents/{id}` in core-service removes the row
and the stored object and publishes `document.deleted`.
`event_listener.py::_handle_document_deleted` calls
`delete_document_completely` and commits. Without this chain, a
document deleted through the API would stay searchable with all its
chunks in Qdrant.

### 3.8 Visibility: system, own, organization

Every chunk carries (`ai/rag/visibility.py::ChunkVisibility`):

- `is_system=True`: a system document (auto-ingested, seeded as system,
  or an archived note of an analysis without a user). Everyone may read
  it. The system ingestion owner id is `SYSTEM_DOCUMENT_OWNER_ID`
  (`00000000-0000-0000-0000-0000000005ec`, defined once in
  `shared/schemas.py` and used by core-service too); an upload event with that
  owner becomes a system chunk.
- `owner_id`: the uploading user, or the analysis user for an archived
  agent note.
- `organization_id`: the organization the document was shared with.

A chunk with neither `owner_id` nor `is_system` (written before these
fields existed) is a *legacy* chunk and is treated as system by every
layer below, including the final payload check. `index_chunks` requires
a `visibility` argument (no default), so no new legacy chunk can be
written. An upload event without an owner fails closed: only an
organization member could match it.

A reader's `VisibilityScope` allows: system, legacy, own (`owner_id ==
user`), or `organization_id` in the user's organizations. An analysis
without a user (a system analysis) sees system chunks only. The user's
organizations come from auth-service (`resolve_visibility_scope` through
`clients/auth_client.list_user_organization_ids`), cached in Redis by
`shared/user_state.py` (60 s) and deleted by auth-service when a
membership changes. There is no second cache inside the process: a
membership change could not clear it. If auth-service fails, the scope falls back to system plus the user's
own documents.

The rule is enforced three times: as a Qdrant filter
(`VisibilityScope.qdrant_condition`), as the BM25 mask (`BM25Index.search`
with `scope=`), and as a final payload check on every result. Agents get a
`LazyVisibilityScope` (`visibility_for_state`), resolved on first search,
so agents that never search never call auth-service.

Why so strict: agent notes are written from the user's searches, which
may include the user's private uploads. A note marked "system" would leak
a private document through its summary.

### 3.9 Retrieval: BaseRetriever

`ai/rag/retriever.py::BaseRetriever(category_filter, top_k,
use_query_expansion, visibility)`. `retrieve(query, ticker=...)`:

1. Resolve the visibility scope.
2. Pick sources: by default both BM25 and vector, but a category whose
   configured weight for a source is 0 skips that source entirely
   (`_resolve_default_sources`).
3. Query expansion if enabled (3.10): `[query, v1, v2, v3]`.
4. When the vector source is used, embed every query variant in one
   request (`embed_texts(queries, purpose="query")`; a single variant
   goes through `embed_query`). The Gemini free tier is limited per
   project in requests per minute and per day, so four variants cost one
   request, not four. A caller that disabled expansion may pass its own
   `query_vector` instead. If the embedding fails and the caller did not
   pin the sources, the vector side is skipped for every variant with
   one warning (`retriever.vector_search_skipped_embedding_failed`):
   search degrades to BM25 instead of failing. With pinned sources the
   error is raised.
5. For each query variant: BM25 search (`overfetch = top_k x 3`) within
   the category/ticker/visibility mask, keeping only scores above 0; and
   vector search in Qdrant with that variant's vector and a `must` filter
   on category, ticker and visibility.
6. Fuse all rankings with `reciprocal_rank_fusion(rankings, weights=...)`.
7. Fetch payloads for ids that only BM25 found, apply the final category,
   ticker and visibility checks, and return the top `top_k` as
   `RetrievedChunk` (text, score, category, ticker, document_id, source,
   content_date).

`top_k` defaults to `RAG_TOP_K_RESULTS` (5).

**Category weights.** `ai/rag/category_weights.json` sets how much each
source counts in the fusion, per category. It is loaded when the module is
imported (a restart picks up a change). Current values:

| Category | BM25 weight | Vector weight |
|---|---|---|
| `news` | 0.8 | 0.2 |
| `risk_analysis` | 0.7 | 0.3 |
| `macro_research` | 0.7 | 0.3 |
| `filing` | 0.5 | 0.5 |
| `earnings_call` | 0.5 | 0.5 |
| `analyst_report` | 0.5 | 0.5 |

The file is written by `scripts/measure_rag_recall.py --sweep --apply`
(without `--apply` it is a dry run), which tries a grid of ratios against the
golden set with query-purpose embeddings and keeps the best one; each
entry has an `_evidence` field with the measured hits. Weights only apply
when the retriever filters one category.

**FilingRetriever.** A subclass for `category=filing` with `top_k =
RAG_TOP_K_RESULTS + 3`. Ranking has no time signal, and a long 10-K
summary has more chunks than a 10-Q, so the newest quarter could be pushed
out entirely (agents then answer from an older quarter although the
newest 10-Q is indexed). So, when a ticker is given and the
sources are not pinned, up to `NEWEST_FILING_SLOTS` (2) places are
reserved for the best-matching chunks of the ticker's newest filing (by
`content_date`), chosen by a small local BM25 ranking without any LLM call
(`reserve_newest_filing_slots`). When two visible filing documents share
the newest date (for example a user's upload and the system copy of the
same filing), the choice is deterministic (`_newest_document_rank`): a
system (or legacy) document first, then the later `created_at`, then the
document id. It is used by the `search_filings` tool
(Filing, Fundamental and Risk agents).

**`search_filings`** (`ai/tools/filing_tool.py::SearchFilingsTool`) is
bound to the analysis ticker. A `ticker` argument the model still sends is
ignored (`filing_tool.ticker_argument_ignored`), so a call can never
search another company.

- **Header.** The tool's first result starts with the SEC XBRL
  latest-quarter line (`latest_quarter_payload`) and the structured
  records of the ticker's newest `FILING_RECORDS_MAX_FILINGS` (5) filings
  (`ai/knowledge/filing_records.py::render_filing_records`; chapter 18,
  sections 3.11 and 3.12). Each part fails open. Later calls only say
  that these figures were "listed with this tool's first result". When
  neither part could be shown, the next call tries again, so the tool
  never points back to figures it did not list.
- **Order.** Passages are listed newest filing first (by `content_date`),
  after a fixed rule text (`NEWEST_FILING_GUIDANCE`): the newest filing
  wins on the same topic, forward guidance in an older filing is
  history, and a legal matter's status is the one in the newest filing
  that mentions it.
- **Labels.** Each passage carries `[<form>; period: <period>; <recency>]`.
  The period is "quarter ended <date>" for a 10-Q and "fiscal year ended
  <date>" for a 10-K, from SEC EDGAR's `reportDate` for that filing
  (`ai/providers/sec_edgar.py::filing_period`). The filing is found by the
  form and filing date in the document's filename; one submissions
  download per ticker per day. The period is never parsed from the
  summary text. When SEC has no record (for example XOM's filings before
  its 2026 CIK change) it says "not available from SEC EDGAR". The
  recency is "NEWEST filing in these results" or "OLDER filing -- may be
  superseded by the <form> filed <date>". A filing older than 450 days
  also gets `[STALE FILING ...]`.
- Non-English passages are dropped
  (`filing_tool.non_english_passages_dropped`).

Other agent tools (`ai/tools/research_archive_tool.py::SearchResearchArchiveTool`)
use a `BaseRetriever` for one category: `search_prior_analysis`
(analyst_report), `search_prior_risk_notes`, `search_prior_macro_notes`,
`search_prior_news_notes` and `search_earnings_call_transcripts`. They
drop non-English passages and flag stale ones. The thresholds differ per
category (`_STALE_THRESHOLD_DAYS`), because each kind of text goes out of
date at its own speed:

| Category | Stale after | Reason |
|---|---|---|
| `filing` (`search_filings`) | 450 days | a 10-K or 10-Q covers a whole fiscal period and stays current for about a year |
| `analyst_report` | 100 days | the ratios behind a valuation note change with every quarterly filing |
| `risk_analysis` | 180 days | a company's risk profile is durable, but a named risk event can resolve within two quarters |
| `macro_research` | 45 days | the policy regime can shift between two FOMC meetings (about 6 weeks) |
| `news` | 3 days | news is the fastest-moving category |
| `earnings_call` | never | a transcript is a dated record of what management said, not a claim about current conditions; its date is always shown |

### 3.10 Query expansion

`ai/rag/retriever.py::expand_query(query, ticker=..., viewer_user_id=...)`
asks the Lite tier (`LLMTier.CHEAP`) for 3 alternative phrasings, one per
line, and returns `[query, *variants[:3]]`. When a ticker is given, it
first loads up to 10 names of entities the knowledge graph already links
to that ticker (competitors, suppliers; only relations the viewer may see)
and offers them to the model as possible variants. If the LLM call fails,
the result is just `[query]`: search still works, only without the boost.

Expansion is on by default in `BaseRetriever` (agent tools) and off in the
ask endpoint and in evaluation scripts (one LLM call per question would
cost too much there).

### 3.11 Relevance filtering of incoming news and web results

Some sources are filtered *before* their text reaches the corpus or an
agent, because keyword search engines return off-topic items. NewsAPI's
`/v2/everything` is a keyword search with no idea of which company a word
means: a search for a company returns items that only mention it, items
about another company, and items where the ticker letters mean something
else.

There are no site lists, title patterns or word lists. Every item is
classified by the LLM into fixed enums, and code decides only from those
enums, so the rules hold for any US stock. The only checks without the
LLM are structural (`ai/providers/news_relevance.py`):

- a placeholder title or a news headline of fewer than three words (it
  cannot be classified or matched by its echo); web results need one
  word;
- a title that is only a lowercase identifier and a version number (a
  package release page);
- a consent-page link: the item keeps its headline, and its link is
  replaced by the target URL found in the consent link, or marked
  "link unavailable" (`relink_consent_items`; never dropped).

Every structural check fails open: an exception keeps the item.

**The news classifier** (`ai/providers/news_relevance.py::classify_articles`).

1. Items go to the CHEAP tier in batches of `NEWS_RELEVANCE_BATCH_SIZE`
   (8); the batches run concurrently and the original order is kept.
   Without a company name from SEC's ticker list
   (`lookup_company_name`), nothing is classified.
2. For each item the model returns `subject` (`this_company` | `party` |
   `other_company` | `none`), `kind` (`company_business` |
   `stock_market` | `consumer_product` | `opinion_listicle` |
   `price_page` | `not_news`), the first words of the item's title, and a
   `reasoning`. The kinds are defined once (`KIND_DEFINITIONS`), with no
   site names or company examples. `party` means the company is one of
   several parties to the event: a counterparty, customer or supplier, a
   co-defendant, one of the companies a comparison is about, or the maker
   of the product at the center of the event. Without it such items were
   dropped as `other_company` (for example "Microsoft vs. Alphabet" for
   GOOGL).
3. Code decides from the enums. Subject `this_company` or `party` with
   kind company_business, stock_market or opinion_listicle is kept.
   Such consumer_product items are kept up to `NEWS_CONSUMER_MAX_ITEMS`
   (2) per agent run (`cap_consumer_items`) and listed last. Everything
   else (other_company, none, price_page, not_news) is dropped and logged
   (`news_relevance.classified`). The reasoning is logged only; it never
   changes the decision.
4. A verdict counts only when its echoed title matches its item
   (`align_verdicts`): at least 60% of the echo's first six words must be
   in the indexed title, and no other title in the batch may match
   better. Reason: in one GOOGL call of 23 articles, every verdict from
   item 4 on belonged to the next item, so relevant news was dropped and
   an unrelated piece kept. Titles are echoed without quotation marks,
   because a curly quote that the model turned into a straight one broke
   the JSON.

Which kept items count as evidence for the News and Sentiment evidence
yield (`EVIDENCE_KINDS`): company_business, stock_market and
unclassified. An opinion/listicle or consumer item is shown to the agent
but is not evidence, so a run of only such items is "thin" (chapter 15).

Items without a trusted verdict:

| Case | Result |
|---|---|
| The answer is valid JSON, but an item's verdict is missing, untrusted or invalid | the item is kept as `unclassified` (fail open, counts as evidence); logged `news_relevance.verdicts_misaligned` / `invalid_verdicts` |
| The answer is not valid JSON | every complete verdict object is read on its own (`salvage_json_objects`, `news_relevance.salvaged_without_retry`). Only when some item is still missing is the batch asked once more with 8192 tokens instead of 4096 (`news_relevance.unparseable_retry`); the better reading wins. Items still without a verdict are kept as `unverified`: shown with "[unverified]", never evidence (`news_relevance.unparseable_after_retry`, with their titles) |
| The classifier call fails, or there is no company name | the whole batch is kept as `unclassified` with `checked=False` (`news_relevance.batch_failed`, `unclassified_without_company_name`), so the agent still sees news when the Lite model is down |

The CHEAP call, the parse, salvage and retry steps, the fail-open on an
LLM error (`<prefix>.verification_unavailable`) and the echo check
(`<prefix>.verdicts_misaligned`) are one shared helper,
`verify_with_cheap_tier` (which calls `parsed_with_retry`), also used by
the web-search classifier below.

**Who uses it.**

Both callers go through `clean_articles(articles, ticker)`: it drops
non-English items (title plus summary, `non_english_reason`; each caller
logs the count with its own event through the `on_non_english_dropped`
callback) and runs `classify_articles` on the rest when there is a
ticker and at least one English item.

- The News tool (`ai/tools/news_tool.py::SearchNewsTool`) uses the
  classified items and caps consumer items per run. Without a ticker
  nothing is classified and every item is `unclassified`.
- The daily news digest (`ai/ingestion/digests.py`) passes the
  classified items to `stored_evidence_articles`. It stores only items
  with a trusted company_business or stock_market verdict
  (`CONFIRMED_EVIDENCE_KINDS`).
  Unclassified, unverified, consumer and opinion items are left out
  (`news_relevance.not_stored`). When any batch could not be classified
  at all, the function returns `None` and nothing is stored for that
  ticker (`news_relevance.unchecked_not_stored`, then
  `auto_ingestion.news_unverified_skipped`). No file is written, so a
  later run the same day can still store a checked digest. Reason: a
  stored document is read by later analyses as a fact source, so it must
  not contain items nobody checked.

**The NewsAPI query** (`news_relevance.build_ticker_news_query`) is the
same for the News tool and the digest: `(TICKER OR "Company Name")`,
AND-ed with at most 4 topic words left from the model's query. The
ticker, the company's name words, 4-digit years, words under three
letters and generic words (`_GENERIC_QUERY_WORDS`: "news", "latest",
"stock", month names, ...) are removed, because NewsAPI requires every
word: "AMD news September 2026" would match almost nothing. Hyphenated
words are split, because NewsAPI reads "-" as "exclude". The digest
passes an empty query, so it searches the company alone. A bare ticker
was not enough: "KO" returned boxing items.

**The web-search classifier** (`ai/tools/web_search_tool.py::classify_results`)
judges each result against the caller's own query, because `web_search`
is shared by agents with different subjects. One CHEAP call returns per
result `relevant` (true or false) and a `kind` from the same definitions
plus `reference_or_social` (a dictionary, encyclopedia or calendar page,
or a social-media post or profile). A result is kept when it is relevant
and its kind is not price_page, not_news or reference_or_social. For the
News agent's searches the prompt names the company as the research
subject. The same helper, `verify_with_cheap_tier`, applies the same
echo check and `parsed_with_retry` rules (log prefix
`search_relevance.`): unread results become `[unverified]` and are not
counted as evidence; an LLM error keeps every result `unclassified`.

**Result lines.** Both tools put a kind tag before the title
(`news_tool.kind_tag`): "[consumer product]", "[opinion/listicle]" or
"[unverified]". Dates carry the weekday ("[2026-10-01 Thu]"), so "on
Wednesday" in a text can be resolved to an exact date.

**Source catalog** (`ai/providers/newsapi.py::source_catalog`): NewsAPI's
`GET /v2/top-headlines/sources` (language en). An article with a NewsAPI
`source.id`, or a URL whose host is a catalog host or one of its
subdomains, is from a catalog outlet. Every search_news and web_search
line ends with the data field `catalog=yes` or `catalog=no` (nothing
when the catalog is unknown). The result header reads, for example,
"(12 articles: 7 company business, 2 stock market, 1 opinion/listicle, 2
consumer product, 0 unclassified; 9 from catalog outlets)"; "unverified"
is listed only when non-zero. These labels are tokens for the agent, not
prose: an answer that contains one (`TOOL_LABELS`) is rejected once
(chapter 15). Caching:

- Redis cache DB 0, key `ai:newsapi:source_catalog`, kept 7 days
  (`_CATALOG_TTL_SECONDS`);
- refreshed when the stored copy is older than 24 hours
  (`_CATALOG_REFRESH_SECONDS`); when the refresh is not possible (NewsAPI
  rate limit or error), the older copy is served
  (`newsapi.source_catalog_stale_served`) and the next try waits 15
  minutes;
- kept per process for an hour.

Any failure without a stored copy means "unknown", and nothing is
dropped because of it.

### 3.12 The ask endpoint

`domains/rag/api.py` (mounted at `/api/v1/rag`; the gateway proxies it to
ai-service with buffering off and a 180 s read timeout):

- `POST /api/v1/rag/ask` returns `{"data": RagAnswer}`.
- `POST /api/v1/rag/ask/stream` returns `text/event-stream`.

Both need the `rag:ask` permission (guests get 403). The request body
(`domains/rag/schema.py::RagAskRequest`, unknown fields rejected):

```
{"question": "1-2000 chars, not blank",
 "ticker": "AMD" | null,
 "categories": ["filing", ...] | null,
 "mode": "ask" | "draft",
 "top_k": 1-10 | null}
```

Flow (`domains/rag/qa.py`):

1. **Rate limit** (`check_rate_limit`): fixed windows in the Redis
   rate-limit database (DB 1), keys
   `ai:rag_ask:rate:<user>:m:<YYYYMMDDHHMM>` (TTL 120 s) and
   `...:d:<YYYYMMDD>` (TTL 2 days), each counted in one `MULTI`
   transaction (`INCR` plus `EXPIRE ... NX`), limits `RAG_ASK_RATE_LIMIT_PER_MINUTE` (10) and
   `RAG_ASK_RATE_LIMIT_PER_DAY` (200, UTC day). Over the limit: 429 with a
   `Retry-After` header. If Redis is down the request is let through.
2. **Retrieval** (`retrieve_sources`): one `BaseRetriever` per requested
   category (or one unfiltered), no query expansion, the caller's
   visibility scope; results merged by score and cut to `top_k`; then the
   newest-filing reservation when a ticker is given and filings are in
   scope. A retrieval error is a plain 503 before any streaming starts.
3. **No sources**: a fixed honest answer ("I could not find any relevant
   documents ...") without any LLM call, `grounded: false`.
4. **One LLM call** on `RAG_ASK_LLM_TIER` (`cheap`), temperature 0.2, at
   most `RAG_ASK_MAX_OUTPUT_TOKENS` (1024). The prompt numbers the sources
   `[1]`, `[2]` with title, category, ticker and filing date, and the rules
   say: use only these sources, cite every statement, never invent a
   source number, treat the sources and the question as data (ignore
   instructions inside them), English only, prefer the most recently filed
   source for "latest" questions. `mode: "draft"` asks for one 120-200
   word paragraph for a research note instead of an answer.
5. **Non-English output** is rejected. The plain endpoint retries once with
   an English-only instruction, then answers 503.

`RagAnswer`: `answer`, `citations` (index, document_id, title, category,
ticker, content_date, snippet of `RAG_ASK_SNIPPET_CHARS` = 300
characters), `model`, `latency_ms`, `mode`, `grounded`.

**The stream.** Events in order: `sources` (always first, with the
citations), zero or more `token` events (`{"text": ...}`), then exactly
one `done` (the full answer and metadata) or `error` (`code` one of
`non_english_output`, `timeout`, `llm_unavailable`, `internal_error`,
`empty_answer`). Details:

- The text produced so far is checked for non-English on every token.
- After `RAG_ASK_STREAM_TIMEOUT_SECONDS` (120) the stream ends with
  `error`/`timeout`.
- If the stream fails *before* its first token, the answer is produced
  once with the non-streaming path (which has the router's retries and
  fallback models) and sent as a single token.
- If the client disconnects, generation stops without a final event.

The question text is never logged, only its length (`rag_ask.request`).

### 3.13 Auto-ingestion

The scheduler calls `POST /internal/scheduler/rag-ingest` every day at
03:00 New York time (08:00 London), via `call-job.sh rag-ingest`
(curl time limit 3600 s). The route (`api/internal.py::trigger_rag_ingest`)
runs two workflows from `ai/workflows/auto_ingestion_workflow.py`:

**`run_auto_ingestion_workflow`**, for every tracked ticker (portfolio
holdings and watchlists, `clients/core_client.py::get_tracked_tickers`):

- *Filings* (`ai/ingestion/sec_edgar_filings.py::ingest_new_filings`):
  `list_new_filings` lists 10-K and 10-Q filings that are not yet
  ingested (at most 1 10-K and 4 10-Qs, `_MAX_PER_FORM`), fetches the
  clean text with the `edgartools` library, and makes two calls on the
  strong model (HIGHEST tier):
  - the summary (`ai/knowledge/filing_summary.py::summarize_filing`,
    separate prompts for 10-K and 10-Q, an amendment merged in when one
    exists);
  - a separate records-only call (`extract_filing_records`) that returns
    the structured filing records.
  The filename `{TICKER}_{FORM}_{date}.txt` is the duplicate key. The
  stored text is the summary alone. A fallback summary (a raw excerpt,
  when the LLM failed) is never stored, so the next run tries again; its
  records call is not made. After the document is created, the records
  are stored in `ai.filing_records` (fail open: a records failure is
  logged and the document stays; `scripts/backfill_filing_records.py`
  can add the row later) and the ticker's SEC caches are cleared
  (`forget_filing_caches`), so the XBRL line sees the new filing
  (chapter 18, sections 3.10-3.12). A filing that could not be stored
  makes the ticker count as failed, and its news step is skipped.
- *News:* `ai/ingestion/digests.py::generate_news_digest` builds at
  most one digest per ticker per day (`{TICKER}_news_{date}.txt`). It
  fetches up to 8 NewsAPI articles of the last 28 days, newest first
  (`sort_by="publishedAt"`, so the digest does not repeat the same
  articles every day), drops non-English ones, and keeps only the items
  that `stored_evidence_articles` confirms (3.11). A digest of fewer
  than 80 words is not stored. When the company name lookup fails, no
  NewsAPI request is sent at all, because nothing could be classified.
  Each digest costs one NewsAPI request per ticker.

Only 10-K and 10-Q filings are ingested (`_WANTED_FORMS`). 8-K current
reports (deals or management changes between quarters) are not, so such
events reach an analysis only through news.

Each new document is created through core-service's internal system
document route (`create_system_document`), so it goes through the normal
`document.uploaded` path and becomes a system document.

**`run_macro_auto_ingestion_workflow`**: once per calendar month
(`macro_auto_{YYYY-MM}.txt`), `ai/ingestion/digests.py::generate_macro_report` fetches
five FRED indicators (fed funds rate, CPI, unemployment rate, GDP, 10-year
Treasury yield) and has the Lite model write a short narrative from those
values only. Every indicator is fetched with the macro tool's
`YOY_MIN_LOOKBACK_DAYS` (430 days), so quarterly GDP, whose newest
observation is dated at the start of its quarter, is always in the window.
CPI is written as year-over-year inflation, computed with the same
reference rule as the macro tool (the observation on or before the latest
date minus 365 days); when no such observation exists the line says it is
an index level, not an inflation rate (log
`auto_ingestion.macro_year_over_year_unavailable`). An indicator with no
observations is logged (`auto_ingestion.macro_indicator_empty`) and left
out.

The same filing check also runs during an analysis
(`ai/graph/nodes.py::load_fresh_filing_context`, chapter 16): a brand-new filing is
ingested and also handed directly to that analysis's agents. The analysis
waits at most `FRESH_FILING_BUDGET_SECONDS` (600 s) for this ingest; after
that it runs without the fresh-filing context, and the ingest finishes in
the background.

### 3.14 Seed documents

The recall baseline corpus lives in one folder, the repo-root
`rag_seed_documents/`. It holds third-party documents (earnings calls,
analyst reports, news), so it is kept locally and is not committed
(`.gitignore`); a fresh checkout needs the folder copied in, or the data
comes back with `make restore`. Layout:

| Folder | Content |
|---|---|
| `rag_seed_documents/analyst_report/` | 4 files |
| `rag_seed_documents/earnings_call/` | earnings-call transcripts, prepared remarks and earnings presentations (PDF and text files, named `<TICKER>_...`) |
| `rag_seed_documents/macro_research/` | 3 files |
| `rag_seed_documents/filing/` | 5 files |
| `rag_seed_documents/news/` | 18 files |

`services/ai-service/scripts/upload_seed_documents.py` uploads them,
either as a user through the gateway, or with `--as-system` inside the
ai-service container through core-service's internal route (then they are
system documents visible to everyone). The ticker comes from the filename
prefix and the content date from a trailing `_YYYY-MM-DD`. `--dry-run`
only lists what would be sent. `--seed-root` defaults to the repo-root
folder in a checkout and to `/tmp/rag_seed_documents` in the container,
which holds no seed files: copy them in first with
`docker compose cp rag_seed_documents ai-service:/tmp/rag_seed_documents`.
The filing and news seeds are fixed files; daily ingestion keeps those
categories fresh.

core-service's duplicate check on upload is per owner. So with
`--as-system`, the script first asks core-service for the filenames of
every owner (`GET /internal/documents/filenames?category=&ticker=`,
failed documents excluded) and skips a file that is already there,
printed as "SKIP <file> (already uploaded by another owner)", also in a
dry run. Reason: without it, earnings calls that a user had already
uploaded were added again as system copies. When that list cannot be
read, the file is uploaded as before. The ticker is the filename part
before the first underscore, so keep that format; `macro_research` files
are uploaded without a ticker.

### 3.15 Evaluation tools

| Tool | Purpose |
|---|---|
| `ai/evaluation/rag_recall.py` (`generate_golden_set`) | sample indexed chunks, ask an LLM for one question whose answer is in that chunk; drop near-duplicate chunks and duplicate questions |
| `scripts/generate_rag_golden_set.py` | runs the generator in the container (Lite model), writes the JSON |
| `tests/fixtures/rag_golden_set.json` | the current golden set (909 questions: 706 earnings_call, 126 filing, 31 news, 25 analyst_report, 16 risk_analysis, 5 macro_research) |
| `ai/evaluation/rag_recall.py` (scoring) | Recall@K, Recall@1/@3, MRR for BM25-only, vector-only and fused |
| `scripts/measure_rag_recall.py` | read-only measurement of the live index against a golden set; `--sweep` is the grid search for the weights in `category_weights.json` |
| `tests/integration/test_rag_recall.py` | integration test gate on recall: Recall@5 floors for BM25, vector and fused (chapter 31, section 3.6); skipped when the golden set file is missing |
| `tests/ai-service/test_rag.py` | checks the recall arithmetic (Recall@1/@3, MRR) against hand-computed values |

How a question is scored (`ai/evaluation/rag_recall.py`): each golden-set
question runs through `BaseRetriever` three times, with `sources={"bm25"}`,
`sources={"vector"}` and both (the fused RRF path), and counts a hit when
the expected chunk id is in the top K. RAG recall is the first of the four
evaluation layers; chapter 19, section 3.11, puts it next to the others.

`measure_rag_recall.py` in detail: it reads the golden set from standard
input, rebuilds a BM25 index from Qdrant inside its own process (the
service's index is not shared), skips questions whose chunk no longer
exists (and counts them per category), embeds all questions in batches
with the "query" purpose, and scores four curves: `bm25`, `vector`,
`fused`, and `production` (category filter on, category weights applied:
what an agent tool really gets). It prints Recall@1, @5, @K and MRR@K,
overall and per category, next to the 2026-08-27 baseline (BM25 Recall@5
about 91-94%, vector about 83%, fused about 94%, fused MRR about 0.75,
measured with the older `mistral-embed`). It makes no chat-model calls,
only embedding calls; `--limit N` scores the first N questions only.

---

## 4. Configuration

From `services/ai-service/config.py` unless stated otherwise; `.env.example`
sets the embedding values explicitly.

| Setting | Default | Meaning |
|---|---|---|
| `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_URL` | `qdrant`, 6333, none | where Qdrant is |
| `QDRANT_COLLECTION_RESEARCH` | `research_documents_gemini` | the one RAG collection |
| `QDRANT_VECTOR_SIZE` | 1536 | must equal the embedding dimensions |
| `EMBEDDING_PROVIDER` | `google` | `google` or `mistral` |
| `GOOGLE_EMBEDDING_MODEL` | `gemini-embedding-2` | embedding model |
| `GOOGLE_EMBEDDING_DIMENSIONS` | 1536 | vector size |
| `EMBEDDING_REQUEST_TIMEOUT_SECONDS` | 30.0 | time limit of one Gemini embedding request; a timeout is retried like a 5xx |
| `RAG_TOP_K_RESULTS` | 5 | default number of results |
| `RAG_ASK_RATE_LIMIT_PER_MINUTE` | 10 | ask requests per user per minute |
| `RAG_ASK_RATE_LIMIT_PER_DAY` | 200 | ask requests per user per UTC day |
| `RAG_ASK_LLM_TIER` | `cheap` | tier of the answer call |
| `RAG_ASK_MAX_OUTPUT_TOKENS` | 1024 | answer length limit |
| `RAG_ASK_SNIPPET_CHARS` | 300 | citation snippet length |
| `RAG_ASK_STREAM_TIMEOUT_SECONDS` | 120.0 | stream time limit |
| `NEWSAPI_API_KEY` | empty | news digests and the News tool |
| `NEWSAPI_SEARCH_IN` | `title,description` | which article fields NewsAPI searches |
| `NEWSAPI_SORT_BY` | `relevancy` | sort order of the News tool's search (the digest always asks for `publishedAt`) |
| `NEWSAPI_RECENT_EXTRA_LIMIT` | 10 | size of the News tool's extra newest-first search |
| `NEWSAPI_RATE_LIMIT_COOLDOWN_SECONDS` | 3600.0 | pause after a NewsAPI rate limit (chapter 13) |
| `NEWS_RELEVANCE_BATCH_SIZE` | 8 | items per classifier call (calls run concurrently) |
| `NEWS_CONSUMER_MAX_ITEMS` | 2 | consumer-product items kept per agent run (never evidence, never stored) |
| `FILING_RECORDS_MAX_FILINGS` | 5 | newest filings whose records `search_filings` shows |
| `SEC_EDGAR_USER_AGENT` | empty | required by SEC EDGAR ("Company Name email") |
| `SEC_EDGAR_MAX_REQUESTS_PER_SECOND` | 8.0 | SEC request throttle (SEC's fair-access limit is 10) |
| core: `ALLOWED_DOCUMENT_EXTENSIONS` | `.pdf .txt .md` | `services/core-service/config.py` |
| core: `MAX_UPLOAD_SIZE_BYTES` | 20 MB | upload size limit |
| code: chunk sizes | see 3.3 | `ai/rag/chunking.py::_CATEGORY_CONFIG` |
| code: RRF `k` | 60 | `ai/rag/retriever.py::_DEFAULT_K` |
| code: `_PER_RANKING_OVERFETCH` | 3 | depth of each ranking before fusion |
| code: `NEWEST_FILING_SLOTS` | 2 | reserved newest-filing slots |
| code: `_VERIFY_MAX_TOKENS` / `_RETRY_MAX_TOKENS` | 4096 / 8192 | output budget of a classifier call and of its one retry (`news_relevance.py`) |
| code: `_ECHO_MIN_OVERLAP`, `_ECHO_WORDS` | 0.6, 6 | title echo check |
| code: `_STALE_FILING_THRESHOLD_DAYS` | 450 | stale flag of `search_filings` |
| code: `_STALE_THRESHOLD_DAYS` | see 3.9 | stale flags of the archive tools |
| code: digest limits | 8 articles, 28 days, 80 words | `ai/ingestion/digests.py` |
| file: `ai/rag/category_weights.json` | see 3.9 | fusion weights per category |

---

## 5. Design decisions and why

**Hybrid search instead of vectors only.** On this corpus BM25 alone
reached higher Recall@5 than vectors alone in the 2026-08-27 measurement
(about 91-94% against 83%); fusing both was best. Financial text is full
of exact terms that keyword search handles well.

**RRF instead of score blending.** BM25 scores and cosine similarities are
not on one scale; RRF uses only ranks and is known to be insensitive to
`k` between 20 and 100. Per-category weights let a category lean
towards one source without switching the other off.

**One collection, filters per need.** Alternative: a collection per
category or per agent. Reason: one collection with payload filters keeps
indexing, deletion and evaluation in one place; a new agent need is a new
retriever subclass, not a new collection.

**BM25 in memory, rebuilt from Qdrant.** Alternative: a separate search
server. Reason: the corpus is small, and Qdrant already stores every
chunk's text, so BM25 can always be rebuilt from it at startup.

**Full BM25 rebuild in a worker thread, swapped in one step.**
Alternative: keep the tokenized corpus and re-tokenize only new chunks.
Reason: `bm25s` must re-index the whole corpus anyway (document
frequencies change with every chunk), so caching tokens would save only
part of the cost and add a second copy of the corpus to keep in step. A
worker thread plus an immutable snapshot keeps the results identical to
the old in-loop rebuild (checked on 4,800 searches over a 5,000-chunk
corpus with every category, ticker and visibility combination) while the
event loop stays free.

**Event-driven ingestion.** Alternative: core-service calls ai-service
directly on upload. Reason: the upload request returns at once, the event
survives restarts (durable stream with redelivery), and the same path
serves user uploads, auto-ingestion and fresh filings.

**Embed before deleting old points.** Reason: a failed re-ingestion must
not leave a document with no chunks at all.

**Visibility enforced in three places.** Reason: a single missed filter
would leak private documents; the final payload check catches any chunk
the first two layers let through.

**No query expansion in the ask endpoint.** Reason: one extra LLM call per
question would double the cost and latency of a user-facing feature.

**An honest "no sources" answer without an LLM call.** Reason: without
sources the model could only answer from general knowledge, which the
assistant promises not to do.

**News relevance by fixed enums, not by lists.** Alternative: trust an
item whose headline names the ticker, plus lists of blocked sites, title
patterns and homonym words. Reason: in one batch the headline shortcut
alone kept 146 of 273 items, among them "1080p AMZN WEB-DL" release posts
and "UNH football"; every list only fixed the cases someone had already
seen. One classifier with fixed enums, decided in code, works for any
ticker.

**Fail open for agents, fail closed for stored digests.** An agent still
sees unclassified news when the classifier is down, because a missing
item is worse than an unchecked one during one run, and the tool marks
it. A stored digest is read by every later analysis, so it keeps only
confirmed company_business and stock_market items and is not written at
all when a batch could not be classified.

**Unverified is shown but never evidence.** Reason: in one INTC run, 8
unchecked items (laptop deals and a service dispute among them) raised
the evidence yield from 6 to 14.

**NewsAPI searches title and description, sorted by relevancy, plus a
newest-first extra.** Reason: on one AMD day, a full-content search
newest first returned 20 articles that only mentioned AMD in passing, and
the classifier kept none; title and description sorted by relevancy gave
17 of 19 kept. Relevancy ranks the whole month, so the last days can be
missed; the News tool therefore also asks for
`NEWSAPI_RECENT_EXTRA_LIMIT` newest articles and merges them (one more
request per search). The digest always asks for newest first, or every
day's digest would repeat the month's most relevant articles.

**The filing period comes from SEC, not from the summary.** Reason: a
period parsed from model-written text can be wrong; EDGAR's `reportDate`
for the named filing cannot.

---

## 6. When things go wrong

| Symptom | Likely cause | Diagnose |
|---|---|---|
| document stays `uploaded` | event still in core's outbox (Redis down) or ai-service down | `bash scripts/logs.sh ai-service grep document_uploaded_received`; `bash scripts/psql.sh -c "SELECT event_name, created_at, attempts, last_error FROM core.event_outbox WHERE published_at IS NULL;"`; core re-publishes on re-upload of a stuck document |
| ingestion slow, then `failed` and retried | embedding requests timing out | `bash scripts/logs.sh ai-service grep embedding.google_timeout_retrying` |
| document `failed` | parse error, empty PDF, embedding outage | `bash scripts/logs.sh ai-service grep rag_ingestion_failed` |
| ingestion `failed` and retried while Gemini is rate-limited | every pooled Gemini project answered 429 through all waits (`EmbeddingRateLimitedError`, transient) | `bash scripts/logs.sh ai-service grep embedding.google_pool_rate_limited_waiting`; the event is redelivered (at most 5 deliveries) |
| a `failed` document may still be found in search | cleanup of its points after the failure failed | `bash scripts/logs.sh ai-service grep rag_ingestion_failure_chunk_cleanup_failed`; compare Qdrant count with `ai.document_chunks` |
| ai-service refuses to start | vector size mismatch | startup error from `check_embedding_configuration` / `ensure_collection` |
| search finds nothing after restart | BM25 not rebuilt | `rag.bm25_rebuilt_from_qdrant` with `chunk_count` at startup |
| ingestion of a document takes a few seconds longer as the corpus grows | the full BM25 rebuild per write (off the event loop, so other requests are not held up) | `rag.indexed_document` timestamps against the document's `ready` status; corpus size from `make qdrant` |
| search works but no vector results | embedding call failing | `retriever.vector_search_skipped_embedding_failed` |
| ask returns 429 | rate limit | `Retry-After` header; Redis keys `ai:rag_ask:rate:*` |
| ask returns 503 | retrieval or LLM unavailable | `rag_ask.retrieval_failed`, `rag_ask.llm_failed` |
| deleted document still found | deletion event not processed | compare Qdrant count with `ai.document_chunks` |
| no news digest for a ticker today | classifier unavailable or company name lookup failed; or every item was left out | `bash scripts/logs.sh ai-service grep auto_ingestion.news_unverified_skipped`; `news_relevance.not_stored` lists the kinds left out |
| many "[unverified]" news or web lines | the classifier answer was not valid JSON, even after the retry | `news_relevance.unparseable_after_retry` / `search_relevance.unparseable_after_retry` with the titles |
| `catalog=` field missing on result lines | source catalog unknown (NewsAPI unreachable, no stored copy) | `newsapi.source_catalog_stale_served`; Redis key `ai:newsapi:source_catalog` |
| a filing passage shows "period: not available from SEC EDGAR" | SEC has no submissions record for that filing | `filing_tool.period_from_edgar` with `report_date` null |
| auto-ingestion did not run | scheduler or job failure | `bash scripts/logs.sh scheduler grep rag-ingest`; alert `DailyJobStale` (job `rag-ingest`) |

Read-only checks:

```
make qdrant
```

```
bash scripts/psql.sh -c "SELECT status, count(*) FROM ai.embedding_jobs GROUP BY 1;"
```

```
bash scripts/psql.sh -c "SELECT category, status, count(*) FROM core.documents GROUP BY 1, 2 ORDER BY 1, 2;"
```

```
bash scripts/psql.sh -c "SELECT count(*) AS chunk_rows, count(DISTINCT document_id) AS documents FROM ai.document_chunks;"
```

---

## 7. Demo walkthrough

1. **Show the index.**

   ```
   make qdrant
   ```

   The audience sees `research_documents_gemini` with its point count.
   Say: "One point per chunk; the same chunks are also in an in-memory
   BM25 index."

2. **Upload a document and watch it get indexed (writes data: a new
   document owned by the test owner).** Create a small text file and
   upload it through the gateway:

   ```
   cat > /tmp/amd_demo_note.txt <<'EOF'
   AMD demo note. In this demonstration document we state that the
   fictional Project Bluefin data center accelerator ships in the fourth
   quarter, according to the demo author.
   EOF
   TOKEN=$(bash scripts/token.sh owner)
   curl -sk -X POST https://localhost:8443/api/v1/research/documents \
     -H "Authorization: Bearer $TOKEN" \
     -F "file=@/tmp/amd_demo_note.txt" -F "category=analyst_report" -F "ticker=AMD"
   ```

   The response shows the document with `"status": "uploaded"`. Follow the
   ingestion:

   ```
   bash scripts/logs.sh ai-service grep rag_ingestion_complete
   bash scripts/psql.sh -c "SELECT filename, status, uploaded_at FROM core.documents WHERE filename = 'amd_demo_note.txt';"
   ```

   The status moves `uploaded -> parsing -> chunking -> embedding ->
   ready` within seconds. Say: "core-service published `document.uploaded`;
   ai-service parsed, chunked, embedded and indexed it, and reported each
   step back."

3. **Ask a question about it through the API.**

   ```
   curl -sk -X POST https://localhost:8443/api/v1/rag/ask \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"question": "When does Project Bluefin ship?", "ticker": "AMD", "categories": ["analyst_report"]}' \
     | python3 -m json.tool
   ```

   Point at the `[1]` citation in `answer`, the `citations` list with the
   snippet, `grounded: true`, and `latency_ms`. Then the streaming version:

   ```
   curl -skN -X POST https://localhost:8443/api/v1/rag/ask/stream \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"question": "What did AMD report in its latest 10-Q?", "ticker": "AMD"}'
   ```

   The audience sees `event: sources` first, then many `event: token`
   lines, then `event: done`. Say: "For 'latest 10-Q' the newest filing is
   guaranteed two places among the sources."

4. **Show visibility.** Ask the same Bluefin question with an admin token
   (`TOKEN=$(bash scripts/token.sh admin)`): the private note is not
   visible to another user, so the answer is the honest "could not find
   any relevant documents" text (unless another document matches) with
   `grounded: false`.

5. **Measure recall on a small sample (cheap: embedding calls only).**

   ```
   docker compose --env-file .env.docker exec -T ai-service \
     python -m scripts.measure_rag_recall --limit 50 < tests/fixtures/rag_golden_set.json
   ```

   The output lists Recall@1/@5/@10 and MRR for `bm25`, `vector`, `fused`
   and `production`, overall and per category, and the 2026-08-27
   baseline line. Without `--limit` it scores all 909 questions (about 46
   embedding batches).

6. **Show the news relevance decisions (read-only).** After an analysis
   or the daily ingestion:

   ```
   bash scripts/logs.sh ai-service grep news_relevance.classified
   bash scripts/logs.sh ai-service grep news_relevance.not_stored
   ```

   Each `classified` line lists the verdict counts and the dropped titles
   with their subject, kind and reasoning. Say: "The model only fills in
   fixed enums; code decides what is kept, and the digest stores only
   confirmed business and market news."

7. **Clean up (destructive: deletes the demo document).** Look up its id
   and delete it; the `document.deleted` event removes its chunks:

   ```
   bash scripts/psql.sh -c "SELECT id FROM core.documents WHERE filename = 'amd_demo_note.txt';"
   curl -sk -X DELETE https://localhost:8443/api/v1/research/documents/<document-id> \
     -H "Authorization: Bearer $(bash scripts/token.sh owner)"
   bash scripts/logs.sh ai-service grep rag.deleted_document_completely
   ```

---

## 8. Check your understanding

1. Why does AlphaMind keep both BM25 and vector search?
2. What does RRF use from each ranking, and why not the raw scores?
3. A chunk is ranked 2nd by BM25 and 4th by vectors for a `news` query.
   What is its fused score?
4. What happens to the BM25 index when ai-service restarts?
5. A user uploads a private PDF. Who can retrieve its chunks?
6. Why does `FilingRetriever` reserve two slots?
7. What does `/api/v1/rag/ask` do when nothing relevant is found?
8. Which embedding failures make a document's ingestion event redeliver?
9. A news item is about a lawsuit between this company and another one.
   Is it kept, and does it count as evidence?
10. The classifier is down during the daily ingestion. What does the
    digest store?

<details>
<summary>Answers</summary>

1. They catch different things: BM25 exact terms, vectors meaning. The
   2026-08-27 measurement showed fusing both beat either alone.
2. Only the rank position (`weight / (60 + rank)`); BM25 scores and cosine
   similarities are on different scales.
3. `0.8/62 + 0.2/64`, about 0.0160 (news weights: BM25 0.8, vector 0.2),
   for one query variant.
4. It is empty (in memory only); `rebuild_bm25_from_qdrant` refills it
   from Qdrant payloads at startup, in a worker thread like every other
   rebuild.
5. The uploader, and members of the organization if it was shared with
   one; analyses run by that user; never a system analysis or other users.
6. Ranking has no time signal, so the newest filing could be crowded out
   by an older, longer 10-K; two slots guarantee the newest quarter appears.
7. It returns a fixed honest answer without calling the LLM, with
   `grounded: false` and no citations.
8. Transient ones: timeouts, connection errors, rate limits (including
   `EmbeddingRateLimitedError`, when every pooled Gemini project keeps
   answering 429) and HTTP 408, 429, 500, 502, 503 or 504
   (`is_transient_ingestion_error`); at most 5 deliveries.
9. Yes: its subject is `party` and its kind is company_business, so it is
   kept and counts as evidence.
10. Nothing for that ticker. `stored_evidence_articles` returns `None`,
    no file is written, and a later run the same day can try again.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/ai/rag/chunking.py` | pypdf / text parsing, category-aware chunking and filters |
| `services/ai-service/ai/rag/embedding.py` | Gemini and Mistral embeddings, cosine, config check |
| `services/ai-service/ai/rag/indexing.py` | Qdrant upsert, BM25 index (snapshot swap, rebuild in a worker thread), deletion, BM25 rebuild from Qdrant |
| `services/ai-service/ai/rag/retriever.py` | multi-query expansion with KG names (`expand_query`), weighted Reciprocal Rank Fusion, `BaseRetriever`, `FilingRetriever`, newest-filing slots |
| `services/ai-service/ai/rag/visibility.py` | chunk visibility and reader scopes |
| `services/ai-service/ai/rag/category_weights.json` | fusion weights per category |
| `services/ai-service/ai/workflows/rag_ingestion_workflow.py` | ingestion pipeline and status events |
| `services/ai-service/ai/workflows/auto_ingestion_workflow.py` | daily filing/news and monthly macro ingestion |
| `services/ai-service/ai/ingestion/` | SEC EDGAR, NewsAPI digest, FRED macro report |
| `services/ai-service/ai/providers/news_relevance.py` | news relevance classifier, `verify_with_cheap_tier`, `parsed_with_retry`, `clean_articles`, `stored_evidence_articles`; structural pre-checks and the NewsAPI query builder |
| `services/ai-service/ai/tools/news_tool.py`, `web_search_tool.py` | result lines, kind tags, catalog field, result header; the web-search relevance classifier (`classify_results`, in `web_search_tool.py`) |
| `services/ai-service/ai/providers/newsapi.py` | NewsAPI client and source catalog |
| `services/ai-service/ai/tools/filing_tool.py`, `research_archive_tool.py` | agent search tools |
| `services/ai-service/event_listener.py` | `document.uploaded` / `document.deleted` handlers |
| `services/ai-service/domains/rag/` | ask API, schema, QA flow, repositories, models |
| `services/ai-service/qdrant.py` | Qdrant client and collection check |
| `services/ai-service/ai/evaluation/rag_recall.py` | golden set generation, recall |
| `services/ai-service/scripts/measure_rag_recall.py` and siblings | evaluation and maintenance scripts |
| `services/core-service/domains/research/` | upload, duplicate check, delete, status persistence |

---

## 10. Related chapters

- [16. The analysis pipeline](16-ai-analysis-pipeline.md) (fresh filing context)
- [18. Knowledge graph and memory](18-ai-knowledge-memory.md) (agent note archival into this index)
- [21. Qdrant](21-qdrant.md), [22. Object storage](22-object-storage.md), [23. Redis](23-redis.md)
- [24. Events between services](24-events.md) (`document.uploaded`, durable streams)
- [26. The scheduler and its jobs](26-scheduler-jobs.md)
- [29. Security](29-security.md) (permissions, visibility)
- [19. Workflows and backtests](19-ai-workflows-backtest.md), section 3.11 (the evaluation layers)
- [../API_SPEC.md](../API_SPEC.md), [../ERD.md](../ERD.md)
