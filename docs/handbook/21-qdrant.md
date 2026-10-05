# 21. Qdrant (vector database)

> Qdrant is the vector database behind AlphaMind's research search. Every
> research document (an uploaded filing, a news summary, an archived agent
> note) is cut into chunks, each chunk is turned into a list of 1536
> numbers by the `gemini-embedding-2` model, and each list is stored in
> Qdrant as one "point" together with the chunk's text and metadata. When
> an agent searches, Qdrant returns the points whose vectors are closest
> to the query's vector, filtered to the right ticker, category and, most
> importantly, to the documents the user is allowed to read. This chapter
> explains the concepts, the one collection AlphaMind uses, how points are
> written and read, how visibility filtering works, and how Qdrant is
> backed up, checked and inspected. The full retrieval logic (BM25,
> fusion, query expansion) is in [17-ai-rag.md](17-ai-rag.md).

---

## 1. The big picture

```
 core-service                         ai-service                              qdrant (v1.18.3)
 ------------                         ----------                              ----------------
 upload document  --document.uploaded-->  rag_ingestion_workflow
 (bytes in S3,       (Redis stream)       load -> parse -> chunk
  row in core.documents)                  embed (gemini-embedding-2, 1536)
                                          index_chunks()  ------- upsert ---->  collection
                                          + ai.document_chunks rows             "research_documents_gemini"
                                                                                 point = id + vector
 agent tools (search_filings, ...)                                                       + payload
   -> BaseRetriever.retrieve()  ----------- query_points(vector, filter) ---->   (cosine, HNSW)
      (BM25 in memory + vector,  <--------- top points + payloads -----------
       fused with RRF)

 delete document --document.deleted-->  delete_document_chunks() -- delete by filter -->

 scheduler 02:30 New York: backup.sh -- POST /collections/<c>/snapshots, download, DELETE -->
```

Key facts:

- One Qdrant server, the compose service `qdrant`, image
  `qdrant/qdrant:v1.18.3`, data in the volume `qdrant_data`.
- Its REST API (port 6333) is reachable by the services on the Docker
  network, and on the host **only** at `127.0.0.1:6333` (dashboard at
  `http://127.0.0.1:6333/dashboard`).
- Only ai-service talks to Qdrant (plus the backup and restore scripts).
- One collection for everything, `research_documents_gemini`, with
  1536-dimensional vectors and cosine distance.
- Qdrant holds the vectors **and** the chunk text. PostgreSQL only keeps
  bookkeeping (`ai.document_chunks`, `ai.embedding_jobs`); see
  [20-postgresql.md](20-postgresql.md).

---

## 2. Concepts you need

| Term | Plain explanation | In AlphaMind |
|---|---|---|
| Embedding | A list of numbers (a vector) that represents the meaning of a text. Texts with similar meaning get vectors that point in similar directions. | Produced by Google's `gemini-embedding-2` with 1536 numbers per text (`ai/rag/embedding.py`). |
| Dimension | How many numbers are in a vector. | 1536 (`QDRANT_VECTOR_SIZE`, `GOOGLE_EMBEDDING_DIMENSIONS`). All vectors in one collection must have the same dimension. |
| Cosine similarity | A score for how similar two vectors' directions are: 1 = same direction, 0 = unrelated. Length does not matter, only direction. | The collection's distance is `COSINE` (`qdrant.py::ensure_collection`). Vectors are also L2-normalised (scaled to length 1) by the code. |
| Vector database | A database that stores vectors and answers "which stored vectors are closest to this one?" quickly. | Qdrant. |
| Collection | Qdrant's equivalent of a table: a named set of points with one vector configuration. | `research_documents_gemini`. |
| Point | One entry in a collection: an `id`, a `vector` and a `payload`. | One chunk of one document. |
| Payload | A JSON object stored with the point. It can be returned with results and used in filters. | Chunk text, ticker, category, document id, visibility fields (section 3.3). |
| Payload filter | A condition on payload fields that results must satisfy (`must`, `should`, `must_not`). | Category, ticker and visibility are filters on every search. |
| HNSW | "Hierarchical Navigable Small World": a graph index over the vectors. Each point is linked to some near neighbours on several layers; a search walks the graph toward the query instead of comparing with every point. It is approximate but very fast. | Qdrant's default index; AlphaMind uses Qdrant's default settings. |
| Upsert | Insert a point, or replace it if a point with the same id exists. | Re-indexing a document overwrites its chunks because point ids are deterministic. |
| Scroll | Read points page by page (no vector search), optionally filtered. | Used to rebuild the BM25 index at startup and to find a document's chunks before deleting them. |
| Snapshot | A file that contains a whole collection (vectors, payloads, index) and can be restored. | Taken nightly by `infra/scheduler/backup.sh`. |
| BM25 | A classic keyword-based ranking function. | Kept in ai-service's memory and rebuilt from Qdrant payloads; see [17-ai-rag.md](17-ai-rag.md). |

Why vectors at all: a keyword search finds "revenue" only when the text
says "revenue". A vector search also finds "net sales grew 12%", because
the meanings are close. AlphaMind uses both and fuses the two rankings.

---

## 3. How it works in AlphaMind

### 3.1 The client and the collection

`services/ai-service/qdrant.py`:

- `get_qdrant_client()` creates one `AsyncQdrantClient` per process with
  `url=settings.assemble_qdrant_url()` and `api_key=QDRANT_API_KEY or
  None`. The URL is `QDRANT_URL` if set, otherwise
  `http://<QDRANT_HOST>:<QDRANT_PORT>` (default `http://qdrant:6333`).
  The key is sent only when it is set; the compose Qdrant needs none.
- `ensure_collection()` runs at ai-service startup. If the collection
  does not exist, it creates it with
  `VectorParams(size=QDRANT_VECTOR_SIZE, distance=COSINE)`. If it exists
  with a **different** vector size, it raises `RuntimeError` and the
  service does not start ("point QDRANT_COLLECTION_RESEARCH at a
  collection built for this model").
- `check_qdrant_connection()` calls `get_collections()`; it is part of
  ai-service's `/health` (`"qdrant": true/false`).
- `close_qdrant_client()` closes the client at shutdown.

The collection is created with Qdrant's default HNSW settings, and the
code creates no payload indexes. Filtering still works on any payload
field; at this project's corpus size the cost of filtering without a
payload index is small.

The startup order in `services/ai-service/main.py` lifespan is:

```
ensure_schema(engine)                 # PostgreSQL migrations
check_embedding_configuration()       # provider and vector size agree
ensure_collection()                   # Qdrant collection exists, size matches
rebuild_bm25_from_qdrant()            # in-memory keyword index from payloads
... durable event listener, analysis queue ...
```

`ai/rag/embedding.py::check_embedding_configuration` stops startup if
`EMBEDDING_PROVIDER` is not `mistral` or `google`, or if the provider's
vector size (`GOOGLE_EMBEDDING_DIMENSIONS` for google, 1024 for mistral)
differs from `QDRANT_VECTOR_SIZE`. Both checks exist because a mismatch
would otherwise fail every single upsert and query later, far from the
cause. The Mistral path still works (`EMBEDDING_PROVIDER=mistral`,
`QDRANT_VECTOR_SIZE=1024` and a collection filled for that model).

### 3.2 The embedding model

Settings in `services/ai-service/config.py`:

```
EMBEDDING_PROVIDER = "google"
GOOGLE_EMBEDDING_MODEL = "gemini-embedding-2"
GOOGLE_EMBEDDING_DIMENSIONS = 1536
QDRANT_VECTOR_SIZE = 1536
QDRANT_COLLECTION_RESEARCH = "research_documents_gemini"
```

How texts are embedded (`ai/rag/embedding.py`):

- `gemini-embedding-2` takes the task inside the text. Documents are sent
  as `"title: none | text: <chunk>"`, queries as
  `"task: search result | query: <question>"`. (`gemini-embedding-001`
  takes a separate `task_type` parameter instead; the code supports
  both.) A query must be embedded with the query form: the two forms give
  different vectors, which is why `scripts/measure_rag_recall.py` embeds
  its questions with `embed_query`.
- Every text is sent as its own `Content`, because a plain list of
  strings would be aggregated into one vector by this model.
- Batches are capped at 20 inputs and about 15,000 estimated tokens
  (`_GOOGLE_BATCH_MAX_INPUTS`, `_GOOGLE_BATCH_TOKEN_BUDGET`). The free
  tier counts every input against per-project limits (100 requests and
  30K tokens per minute, 1K requests per day); a 50-chunk batch uses
  about 28K tokens, almost a whole minute's budget.
- Requests rotate across the pooled Google projects; on 429 or 5xx the
  code waits 5, 10, 20, 40 seconds between rounds
  (`_GOOGLE_RETRY_WAITS_SECONDS`). A request with no answer after
  `EMBEDDING_REQUEST_TIMEOUT_SECONDS` (30 s) counts like a 5xx (log
  `embedding.google_timeout_retrying`); the last timeout raises
  `TimeoutError`.
- Every vector is L2-normalised (`l2_normalise`), a no-op for
  `gemini-embedding-2`, which already returns unit-length vectors.

Vectors from different models are not comparable. A collection can only
hold one model's vectors. That is why the collection name contains the
model family and why switching models means a new collection
(section 3.7).

### 3.3 How points are written (`ai/rag/indexing.py`)

`index_chunks(...)` is the single write path. Callers:

- `ai/workflows/rag_ingestion_workflow.py::run_rag_ingestion_workflow`:
  for a document from core-service (user upload or system document),
  triggered by the durable event `document.uploaded`;
- `ai/knowledge/archival.py`: for an agent's archived note at the end of
  an analysis (`document_id` is the new `ai.agent_reports` id, not a
  `core.documents` id).

**Point id.** `chunk_point_id(document_id, chunk_index)` =
`uuid5(NAMESPACE_URL, "<document_id>:<chunk_index>")`. The same document
and chunk number always give the same id, so indexing a document again
**overwrites** its points instead of duplicating them.

`index_chunks` takes the embeddings as an argument instead of computing
them, so a caller can batch embedding calls across several documents
before any write happens.

**Payload fields** written on every point (verified in `index_chunks`):

| Field | Value | Used for |
|---|---|---|
| `document_id` | the `core.documents.id` (or `ai.agent_reports.id` for agent notes) | deleting a document's chunks; citations |
| `chunk_id` | same as the point id | BM25 rebuild and mapping |
| `chunk_index` | position of the chunk in the document | ordering |
| `text` | the chunk text | returned to agents; BM25 rebuild; re-embedding |
| `ticker` | e.g. `AMD`, or null for macro research | filter |
| `category` | `filing`, `analyst_report`, `earnings_call`, `news`, `macro_research`, `risk_analysis` | filter |
| `created_at` | ISO time of indexing (defaults to now) | stored so recency could be used later; retrieval scoring has no recency term today |
| `source` | file name without extension (e.g. `AMD_10-Q_2026-08-05`) or `agent:<role>` for agent notes | shown in search results |
| `content_date` | the date the content is about (filing date, report date), when known | shown in results; stale flags |
| `owner_id` | uploading user (or the analysis user for an agent note) | visibility |
| `organization_id` | the document's organization, or null | visibility |
| `is_system` | true for system documents (auto-ingestion) and notes of analyses without a user | visibility |

Chunk metadata is merged into the payload too: upload chunks carry
`ticker` and `content_date`, agent-note chunks carry `agent_role`. The
three visibility fields are applied **last**, so chunk metadata can never
override who may read a chunk.

Write order in the ingestion workflow: delete the document's old points
(`delete_document_chunks`) so that a re-chunk with fewer chunks leaves no
stale high-numbered chunks, then `client.upsert(...)` the new points
(one retry after 1 s, `_UPSERT_RETRY_DELAY_SECONDS`, on
`ResponseHandlingException`, for example "Server disconnected without
sending a response" when Qdrant closed a reused connection; safe because
point ids are deterministic; logged as `rag.qdrant_upsert_retrying`; a
second failure raises), then add them to BM25, then write the
`ai.document_chunks` rows (`record_document_chunks`, flush only, the
caller commits) and mark the `ai.embedding_jobs` row `ready`.

**BM25 is rebuilt on every write, in a worker thread.** The `bm25s`
library has no incremental update: it indexes a whole corpus at once.
`BM25Index` keeps every chunk text in memory and rebuilds its index on
each write; `add_many` does it once per document, not once per chunk.
The cost grows linearly with the corpus (about 1.3-1.7 s for 5,000
chunks of 400 words), so `index_chunks`, `delete_document_chunks` and
`rebuild_bm25_from_qdrant` run `add_many` / `remove_many` through
`asyncio.to_thread`: the event loop keeps serving other requests during
a rebuild. A lock serialises writers, and each rebuild swaps in a new
immutable snapshot (model, chunk-id order, scope and visibility maps) in
one assignment, so a concurrent search uses either the old or the new
index, never a mix. Search results are the same as with a rebuild on the
loop. If the corpus grows enough for the rebuild time itself to matter,
batch writes further before changing library.

**Deleting.** `delete_document_chunks(document_id)` scrolls the points
with `document_id == <id>` (256 per page) to collect their `chunk_id`s,
deletes them in Qdrant with a `FilterSelector`, then removes them from
BM25. `delete_document_completely` also deletes the two PostgreSQL
bookkeeping tables' rows. It runs when ai-service receives the durable
event `document.deleted` (see [24-events.md](24-events.md)); without it,
a deleted document's chunks would stay searchable in Qdrant under an id
that no longer exists. The `chunk_id`s are read from Qdrant before the
delete because the BM25 entries do not store `document_id`, only
`chunk_id -> text`. `delete_document_completely` takes the caller's
session and does not commit, so an event handler or a cleanup script
controls the commit boundary; it returns a `DeletionResult` with what
was removed. `ai.filing_records` is keyed by filename, not by document
id, and is not deleted here.

### 3.4 How points are read (`ai/rag/retriever.py`)

`BaseRetriever.retrieve()` (subclasses such as `FilingRetriever` set the
category and `top_k`):

1. Resolve the caller's visibility scope (section 3.5).
2. Build the Qdrant filter (`_build_filter`): `must` = category match
   (when the retriever has a category), ticker match (when a ticker is
   given), and the visibility condition.
3. Embed all query variants in one embedding request, then for each
   variant: BM25 search in memory, and a vector search
   `client.query_points(collection_name=..., query=<vector>,
   limit=top_k * 3, query_filter=<filter>)`. The over-fetch factor is
   `_PER_RANKING_OVERFETCH = 3`; `top_k` defaults to `RAG_TOP_K_RESULTS`
   (5).
4. Fuse the rankings with RRF (reciprocal rank fusion).
5. For BM25-only hits, fetch payloads with `client.retrieve(ids=...)`.
6. Check every payload again (category, ticker, visibility) before it
   is returned, as a final guard.

If the query embedding fails (provider overloaded), a default search
continues with BM25 alone and logs
`retriever.vector_search_skipped_embedding_failed`. Details of steps 3-6
are in [17-ai-rag.md](17-ai-rag.md).

### 3.5 Visibility filtering (`ai/rag/visibility.py`)

A user's uploaded documents must not appear in another user's analysis.
The rule: a caller may read a chunk when

```
is_system == true
OR the chunk is "legacy" (neither owner_id nor is_system is present)
OR owner_id == the caller's user id
OR organization_id is one of the caller's organizations
```

An analysis without a user (a scheduled system analysis) sees system
chunks only (`SYSTEM_ONLY_SCOPE`).

In Qdrant this becomes one nested filter, ANDed with category and ticker
(`VisibilityScope.qdrant_condition`):

```
must:
  - category == <category>
  - ticker == <ticker>
  - should:                                   # at least one must match
      - is_system == true
      - must: [ is_empty(owner_id), is_empty(is_system) ]   # legacy chunk
      - owner_id == <user id>
      - organization_id in [<org ids>]
```

The same rule is applied three times: in the Qdrant filter, in the BM25
ranking mask, and on every returned payload. All three treat a legacy
chunk as readable by everyone. `index_chunks` requires a `visibility`
argument (a `ChunkVisibility`; there is no default), so every new chunk
carries visibility fields and only chunks written before these fields
existed are legacy.

Where the visibility comes from at write time
(`ChunkVisibility.from_upload_event`): the `owner_id` and
`organization_id` in the `document.uploaded` event. The owner
`00000000-0000-0000-0000-0000000005ec` (`shared.schemas.SYSTEM_DOCUMENT_OWNER_ID`, used
by the automated ingestion job) marks a system document. A missing owner
**fails closed**: the chunk gets no owner and `is_system=false`, so only
organization members could match it.

Where the organizations come from at read time
(`resolve_visibility_scope`): auth-service's
`/internal/users/{id}/organizations` through
`shared/org_membership_cache.py` (one Redis key per user in DB 0, 60 s
TTL, deleted by auth-service whenever a membership changes, so a removed
member loses access at once; see [23-redis.md](23-redis.md)). There is
no in-process cache, because a membership change could not clear it. If
auth-service fails, the scope falls back to system + own documents (fail
closed) and logs `rag_visibility.org_lookup_failed_fail_closed`.

An analysis does not resolve its scope up front. `LazyVisibilityScope`
resolves it at the first RAG search of an agent (`visibility_for_state`
builds one per agent from `state.user_id`; an empty user id means
system only) and keeps it for that agent's later searches, so agents
that never search cost no auth-service call. Ids are compared in one
canonical form (`_norm_id`: lowercase, hyphenated UUID).

### 3.6 Startup rebuild of BM25

BM25 lives in ai-service's memory and is lost on restart.
`rebuild_bm25_from_qdrant()` scrolls the whole collection (256 points per
page, payloads only, no vectors) and rebuilds it from each point's
`chunk_id`, `text`, `category`, `ticker` and visibility, in one write
that runs in a worker thread like every other rebuild. Each BM25 entry
keeps its category and ticker so BM25 ranks only within a retriever's
scope (ranking the whole corpus and filtering afterwards let other
tickers push in-scope chunks out of the top results), and its
visibility so BM25 ranks only among chunks the caller may read; results
with a score of 0 or less are dropped, so RRF never credits them by
rank. The startup log line `bm25_index_rebuilt` (and
`rag.bm25_rebuilt_from_qdrant`) shows the chunk count. So Qdrant is the single durable source for both halves of
the hybrid search. After a Qdrant restore, restart ai-service so BM25 is
rebuilt (`../DISASTER_RECOVERY.md` section 4.2).

### 3.7 Re-embedding and comparing models

**`services/ai-service/scripts/reembed_collection.py`** copies a
collection into a new one with vectors from a Gemini model: same point
ids, same payloads, new vectors. Because every chunk's text is in its
payload, nothing is re-read from object storage and nothing in
PostgreSQL changes (`ai.document_chunks.qdrant_point_id` stays valid).

```
docker compose --env-file .env.docker exec -T ai-service \
    python -m scripts.reembed_collection --source <live collection> \
    --target <new collection> --model <model> --dims <dims>
```

- Preview by default (prints counts, writes nothing); `--confirm` writes
  (creates the target collection if needed and upserts). **`--confirm` is
  a write.**
- `--source` is required (there is no default source collection; a
  switch copies from the live collection). `--target`,
  `--model`, `--dims` default to the current settings, which suits the
  catch-up run after a switch. `--all` re-embeds points already in the
  target.
- It aborts if source and target are the same, if the source does not
  exist, or if the target exists with another vector size.
- Process: first run it as a preview, then with `--confirm`, before
  switching (so there is no gap without vectors). Then set
  `EMBEDDING_PROVIDER`, `GOOGLE_EMBEDDING_MODEL`,
  `GOOGLE_EMBEDDING_DIMENSIONS`, `QDRANT_VECTOR_SIZE` and
  `QDRANT_COLLECTION_RESEARCH` in `.env.docker` and recreate ai-service.
  Finally run it once more with `--confirm`: it copies the points that
  were indexed into the old collection between the copy and the switch
  (points already in the target are skipped unless `--all`). The source
  collection is never modified and serves as the rollback copy.

For a model switch, measure the new collection with
`scripts/measure_rag_recall.py` after `reembed_collection.py` and compare
it with the live one before switching.

Why `gemini-embedding-2` at 1536 dimensions: on the project's own golden
set (922 questions, vector search only, same ticker and category) it had
the best recall of the candidates (Recall@1 0.631, Recall@5 0.927,
MRR@10 0.762, against 0.552 / 0.907 / 0.702 for `mistral-embed` and
0.592 / 0.918 / 0.732 for `gemini-embedding-001`).

### 3.8 Snapshots in backups

`infra/scheduler/backup.sh` (daily 02:30 New York, 07:30 London) for each
collection listed by `GET /collections`:

1. `POST /collections/<c>/snapshots?wait=true` (Qdrant writes a snapshot
   file inside its own storage);
2. downloads it to `/backups/<stamp>/qdrant/<c>.snapshot`;
3. `DELETE /collections/<c>/snapshots/<name>` so snapshots do not pile up
   in the `qdrant_data` volume. If the run fails between 1 and 3, the
   cleanup trap still tries to delete it.

`QDRANT_API_KEY` is sent as an `api-key` header only when set (the
compose Qdrant runs without one).

Restore (`infra/scheduler/restore.sh`, **destructive**, dry run by
default): each snapshot is uploaded with
`POST /collections/<name>/snapshots/upload?priority=snapshot`, which
replaces that collection.

### 3.9 Healthcheck through bash `/dev/tcp`

`docker-compose.yaml`:

```
test: ["CMD", "bash", "-c", 'exec 3<>/dev/tcp/127.0.0.1/6333 && printf "GET /readyz HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3 && read -r status <&3 && [[ "$$status" == *" 200 "* ]]']
interval: 10s, timeout: 5s, retries: 10, start_period: 20s
```

- `/readyz` is Qdrant's readiness endpoint: it answers 200 when the
  server is ready to serve requests.
- The Qdrant image has `bash` but no `curl` or `wget`. Bash can open a
  TCP connection itself through the special path `/dev/tcp/<host>/<port>`.
  The check opens it as file descriptor 3, writes a minimal HTTP request,
  reads the status line, and succeeds only if it contains ` 200 `.
- `$$` in compose is a literal `$`.
- ai-service and the scheduler wait for `qdrant` to be `service_healthy`,
  so a Qdrant restart does not make ai-service fail its startup checks a
  few times until Qdrant is ready.

### 3.10 The dashboard and the script

- Dashboard: `http://127.0.0.1:6333/dashboard` (Qdrant's built-in web
  UI). It lists collections, shows a collection's configuration and point
  count, lets you browse points with their payloads, and has a console for
  REST calls. It has no login in this stack, which is why the port is
  bound to 127.0.0.1 only.
- `make qdrant` / `bash scripts/qdrant.sh`:
  - `bash scripts/qdrant.sh` lists collections with exact point counts;
  - `bash scripts/qdrant.sh info <name>` prints the collection description;
  - `bash scripts/qdrant.sh count <name>` prints the exact point count.
  It uses `QDRANT_LOCAL_URL` (default `http://127.0.0.1:6333`).

---

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `QDRANT_HOST` | `qdrant` | host name of Qdrant | `services/ai-service/config.py` |
| `QDRANT_PORT` | `6333` | REST port | `services/ai-service/config.py` |
| `QDRANT_URL` | unset (`.env.example`: `http://qdrant:6333`) | full URL; wins over host/port | `services/ai-service/config.py` |
| `QDRANT_COLLECTION_RESEARCH` | `research_documents_gemini` | the one RAG collection | `services/ai-service/config.py` |
| `QDRANT_VECTOR_SIZE` | `1536` | vector size; must equal the provider's dimension | `services/ai-service/config.py` |
| `EMBEDDING_PROVIDER` | `google` | `google` or `mistral` | `services/ai-service/config.py` |
| `GOOGLE_EMBEDDING_MODEL` | `gemini-embedding-2` | Gemini embedding model | `services/ai-service/config.py` |
| `GOOGLE_EMBEDDING_DIMENSIONS` | `1536` | requested output size | `services/ai-service/config.py` |
| `EMBEDDING_REQUEST_TIMEOUT_SECONDS` | `30.0` | time limit of one Gemini embedding request | `services/ai-service/config.py` |
| `RAG_TOP_K_RESULTS` | `5` | default results per retriever (vector query fetches 3x) | `services/ai-service/config.py` |
| `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` | `.env.example`, and therefore the running stack: `0.85` (calibrated for `gemini-embedding-2`). If the variable is unset (code default `None`) the code falls back to `0.90` (`_NEAR_DUPLICATE_SEMANTIC_THRESHOLD` in `ai/knowledge/archival.py`), the value chosen for `mistral-embed` | cosine threshold at or above which a new agent note supersedes a near-duplicate one (compared with the latest 3 notes of the same category and ticker) | `services/ai-service/config.py`, `.env.example` |
| `QDRANT_API_KEY` | empty | sent as the `api-key` header by ai-service's client and by `backup.sh`/`restore.sh`, only when set | `services/ai-service/config.py`, `.env.example` |
| `QDRANT_LOCAL_URL` | `http://127.0.0.1:6333` | URL for `scripts/qdrant.sh` | `scripts/qdrant.sh` |
| image / port | `qdrant/qdrant:v1.18.3`, `127.0.0.1:6333:6333` | server version, host binding | `docker-compose.yaml` |

The image tag is pinned to match the `qdrant-client` minor version in
`uv.lock` (client 1.18.0, server 1.18.3).

---

## 5. Design decisions and why

**One collection for all categories and tickers, filtered per query.**
Alternative: one collection per category or per ticker. Reason: one
vector configuration, one backup file, one BM25 rebuild, and filters do
the scoping. `../ERD.md` section 6 describes the same choice, and
chapter 17 explains how each retriever sets its filters.

**Text kept in the payload.**
Alternative: store only vectors in Qdrant and text in PostgreSQL.
Reason: a search result is usable without a second database round trip,
BM25 can be rebuilt from Qdrant alone, and a model switch can re-embed
without re-reading object storage.

**Deterministic point ids.**
Alternative: random ids. Reason: re-ingesting a document (a retry after
failure, a re-upload) overwrites instead of duplicating, and
`ai.document_chunks.qdrant_point_id` stays stable across re-embedding.

**Fail on a size mismatch, never recreate.**
Alternative: drop and recreate a collection with the wrong size. Reason:
recreating silently deletes every vector. Startup stops with a
message that names `reembed_collection.py`.

**Visibility in the payload and in the filter, three times.**
Reason: Qdrant applies the filter during search, so other users' chunks
never take up result slots; the BM25 mask does the same for keyword
search; the final payload check catches a chunk that slipped through
the first two layers (for example a BM25 entry whose payload changed).
It does not protect against a chunk written without visibility fields:
such a legacy chunk is allowed by all three layers, which is why
`index_chunks` requires a visibility.

**Gemini instead of Mistral for embeddings.**
Mistral's embedding endpoint can stay overloaded for days ("backend out
of capacity" on most requests); ingestion then stops and searches lose
their vector half. Gemini embeddings go through the same pool of Google
projects as the chat models. The model was chosen by measuring recall on
the project's own golden set, not from public benchmarks.

**Healthcheck via `/readyz` with bash.** The image has no HTTP client;
bash's `/dev/tcp` is enough for one GET, and dependent services start
only when Qdrant is really ready.

**Loopback-only port.** Qdrant runs without authentication here. The host
port is bound to `127.0.0.1`, so only the local machine can reach the
API and dashboard.

---

## 6. When things go wrong

| Symptom | Likely cause | What the system does | How to diagnose |
|---|---|---|---|
| ai-service fails at startup: "holds N-dim vectors but the configured embedding model produces M" | collection built for another model | `ensure_collection` raises | `bash scripts/qdrant.sh info research_documents_gemini`; check `QDRANT_COLLECTION_RESEARCH` and `QDRANT_VECTOR_SIZE` |
| ai-service fails at startup: "QDRANT_VECTOR_SIZE=... does not match EMBEDDING_PROVIDER" | settings changed only partly | `check_embedding_configuration` raises | set the provider, dimensions and vector size together |
| `/health` of ai-service shows `"qdrant": false` | Qdrant down or not ready | 503 from `/health`; compose marks ai-service unhealthy | `docker compose --env-file .env.docker ps qdrant`; `bash scripts/qdrant.sh` |
| Searches return only keyword-style results | embedding provider failing | vector search skipped, BM25 only | log `retriever.vector_search_skipped_embedding_failed` |
| A document stays in `embedding` or ends `failed` | embedding errors during ingestion | transient errors (429/5xx/timeouts, including `EmbeddingRateLimitedError` when every pooled Gemini project keeps answering 429) mark it failed and leave the event for redelivery (up to 5 deliveries); permanent errors just mark it failed. A failed run that already touched the index deletes the document's points again, so a `failed` document is not searchable (log `rag_ingestion_failure_chunk_cleanup_failed` if that delete fails) | `bash scripts/psql.sh -c "SELECT document_id, embedding_model, status, created_at FROM ai.embedding_jobs ORDER BY created_at DESC LIMIT 10;"`; ai-service logs |
| A deleted document still appears in results | `document.deleted` not processed | the durable event is retried | count its points (demo step 5); `bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.deleted` |
| Keyword search empty right after a Qdrant restore | BM25 still holds the old corpus | none | restart ai-service; look for `bm25_index_rebuilt` with the expected `chunk_count` |
| Backup log "snapshot of collection ... failed" | Qdrant unreachable or out of disk | backup run fails, older backups kept | scheduler log; free disk on the Docker VM |

---

## 7. Demo walkthrough

All steps are read-only.

1. **Qdrant is up and healthy.**
   ```
   docker compose --env-file .env.docker ps qdrant
   curl -s http://127.0.0.1:6333/readyz
   ```
   Say: "The container healthcheck makes exactly this `/readyz` request,
   with bash's `/dev/tcp`, because the image has no curl."

2. **List collections and counts.**
   ```
   bash scripts/qdrant.sh
   ```
   Expected: `research_documents_gemini   <N> points`.

3. **Show the collection configuration.**
   ```
   bash scripts/qdrant.sh info research_documents_gemini
   ```
   Point at `"size": 1536`, `"distance": "Cosine"`, the `hnsw_config`
   block (default values) and `points_count`. Say: "1536 numbers per chunk
   from gemini-embedding-2; cosine compares directions; HNSW is the graph
   index that makes the search fast."

4. **Show one point's payload (no vector).**
   ```
   curl -s -X POST http://127.0.0.1:6333/collections/research_documents_gemini/points/scroll \
     -H "Content-Type: application/json" \
     -d '{"limit": 1, "with_payload": true, "with_vector": false}' | python3 -m json.tool
   ```
   Point at `document_id`, `ticker`, `category`, `source`, `owner_id`,
   `organization_id`, `is_system` and `text`.

5. **Count points with a filter, the same way a search filters.**
   ```
   curl -s -X POST http://127.0.0.1:6333/collections/research_documents_gemini/points/count \
     -H "Content-Type: application/json" \
     -d '{"exact": true, "filter": {"must": [{"key": "category", "match": {"value": "filing"}}, {"key": "is_system", "match": {"value": true}}]}}'
   ```
   Change `filing` to `news`, or add `{"key": "ticker", "match":
   {"value": "AMD"}}`. Say: "Every search carries a filter like this,
   plus the visibility condition."

6. **Link a point to PostgreSQL.**
   ```
   bash scripts/psql.sh -c "SELECT document_id, chunk_index, token_count, qdrant_point_id FROM ai.document_chunks ORDER BY document_id, chunk_index LIMIT 5;"
   ```
   Say: "PostgreSQL only keeps bookkeeping: which chunk of which document
   lives under which Qdrant point id. The text and vector are only in
   Qdrant."

7. **Dashboard.** Open `http://127.0.0.1:6333/dashboard`. Open the
   collection, show its info and browse a few points. Say: "Bound to
   127.0.0.1; there is no login, so it must not be exposed."

8. **Optional: preview a re-embedding (writes nothing).**
   ```
   docker compose --env-file .env.docker exec -T ai-service \
       python -m scripts.reembed_collection --source research_documents_gemini --target demo_preview_collection
   ```
   It prints `=== PREVIEW (read-only) ===` and the counts. Do **not** add
   `--confirm` in a demo.

9. **Backups contain snapshots.**
   ```
   make backups
   ls backups/<newest>/qdrant/
   ```

---

## 8. Check your understanding

<details>
<summary>1. What is stored in one Qdrant point?</summary>

An id (uuid5 of document id and chunk index), a 1536-number vector, and a
payload with the chunk text, document id, ticker, category, source,
dates and the visibility fields `owner_id`, `organization_id`,
`is_system`.
</details>

<details>
<summary>2. Why does re-indexing a document not create duplicates?</summary>

The point id is computed from `document_id` and `chunk_index`, so the
same chunk gets the same id and `upsert` replaces it. The workflow also
deletes the document's old points first, so fewer chunks leave nothing
behind.
</details>

<details>
<summary>3. What does cosine distance measure, and why normalise vectors?</summary>

The angle between two vectors (similar direction = similar meaning).
With unit-length vectors, cosine similarity is a plain dot product and
vector length cannot distort the score.
</details>

<details>
<summary>4. Which chunks can an analysis by user U, member of organization O, read?</summary>

System chunks, legacy chunks without visibility fields, chunks with
`owner_id == U`, and chunks with `organization_id == O`.
</details>

<details>
<summary>5. Why must the collection change when the embedding model changes?</summary>

Vectors from different models live in different spaces and are not
comparable, and a collection's vector size is fixed at creation. A new
collection is filled with `reembed_collection.py` and the settings are
switched together.
</details>

<details>
<summary>6. Why is the healthcheck written with `/dev/tcp`?</summary>

The Qdrant image has bash but no curl or wget. Bash can open a TCP socket
through `/dev/tcp/host/port` and send a minimal HTTP GET to `/readyz`.
</details>

<details>
<summary>7. What must happen after restoring Qdrant from a snapshot?</summary>

Restart ai-service, so the in-memory BM25 index is rebuilt from the
restored payloads.
</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `docker-compose.yaml` (`qdrant`) | image, volume, loopback port, `/readyz` healthcheck |
| `services/ai-service/config.py` | Qdrant and embedding settings |
| `services/ai-service/qdrant.py` | client, `ensure_collection`, health check, close |
| `services/ai-service/ai/rag/embedding.py` | embedding providers, batching, `check_embedding_configuration` |
| `services/ai-service/ai/rag/indexing.py` | `chunk_point_id`, `index_chunks`, `delete_document_chunks`, `delete_document_completely`, `rebuild_bm25_from_qdrant`, `BM25Index` |
| `services/ai-service/ai/rag/retriever.py` | retrievers, Qdrant filters, `query_points` |
| `services/ai-service/ai/rag/visibility.py` | `ChunkVisibility`, `VisibilityScope`, scope resolution |
| `services/ai-service/ai/workflows/rag_ingestion_workflow.py` | ingestion pipeline that writes points |
| `services/ai-service/ai/knowledge/archival.py` | agent notes indexed as points |
| `services/ai-service/domains/rag/model.py` | `ai.document_chunks`, `ai.embedding_jobs` |
| `services/ai-service/scripts/reembed_collection.py` | copy a collection with new vectors |
| `infra/scheduler/backup.sh`, `infra/scheduler/restore.sh` | snapshots |
| `scripts/qdrant.sh` | collections, info, counts |

---

## 10. Related chapters

- [17-ai-rag.md](17-ai-rag.md): chunking, BM25, RRF fusion, query expansion, retrievers
- [18-ai-knowledge-memory.md](18-ai-knowledge-memory.md): archived agent notes and near-duplicates
- [14-ai-llm-layer.md](14-ai-llm-layer.md): the Google key pool also used for embeddings
- [20-postgresql.md](20-postgresql.md): `ai.document_chunks` and `ai.embedding_jobs`
- [22-object-storage.md](22-object-storage.md): where the original document bytes live
- [24-events.md](24-events.md): `document.uploaded` and `document.deleted`
- [26-scheduler-jobs.md](26-scheduler-jobs.md): nightly backups and RAG ingestion
- [29-security.md](29-security.md): tenant isolation
- [../ERD.md](../ERD.md) section 6, [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md)
