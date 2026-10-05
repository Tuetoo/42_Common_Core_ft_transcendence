# AlphaMind — Entity Relationship Documentation

This document describes the schema as implemented in code: every table
below has a SQLAlchemy model behind it.

Schema changes are Alembic migrations only. Each service's chain starts at
a baseline that builds its whole schema, followed by a few later
migrations:

| Service | Chain (oldest -> head) |
|---|---|
| auth | `f1160b000001` (baseline) -> `f1190b000001` (`user_sessions.revoked_reason`) -> `f1210b000001` (drops `organization_roles`) |
| core | `f1160c000001` (baseline) -> `f1170c000001` (document unique indexes `NULLS NOT DISTINCT`; `api_keys` owned by a user only) -> `f1200c000001` (`event_outbox`) -> `f1250c000001` (`documents.status_changed_at`) |
| ai | `f1160a000001` (baseline) -> `f1170a000001` (drops four unused tables) -> `f1210a000001` (drops four write-only tables) -> `f1220a000001` (`filing_records`) -> `f1230a000001` (`kg_relations.kind`, `as_of`) -> `f1240a000001` (`recommendations.judge_leaning`, `judge_confidence`) -> `f1250a000001` (`agent_memories.user_id`) |

The files are under `services/<service>/migrations/versions/`; details in
`handbook/20-postgresql.md` section 3.5. `migrations/archive/` holds an
older chain that is used only to convert a database restored from an old
backup (`DISASTER_RECOVERY.md` section 8).

## 1. Cross-Service Reference Rule

Each service owns one Postgres schema (`auth`, `core`, `ai`) in a single shared
Postgres instance. **No table in one schema may declare a real `FOREIGN KEY`
into another schema.** Cross-service references (e.g. `ai.analyses.user_id`
pointing conceptually at `auth.users.id`) are always a plain UUID column, never
a SQL foreign key. Integrity across the boundary is kept by the
application (internal API checks and events such as `portfolio.deleted`).

## 2. `auth` schema (auth-service)

**users** — `id`, `email` (unique), `username` (unique), `password_hash`,
`system_role` (default `"user"`; one of `admin`/`moderator`/`user`/`guest`,
permissions per role in `shared/permissions.py`), `is_active`,
`is_verified`, `last_seen_at`, `deleted_at` (nullable), timestamps. `deleted_at` marks an admin's soft
delete: the row stays so other services' plain-UUID references keep
resolving, `is_active` is false, `email`/`username` are replaced by
`deleted-<id>@deleted.alphamind.local` / `deleted_<id>`, the password by a
random one, and the user's profile fields, OAuth accounts, 2FA secret,
connections, non-owner organization memberships and sessions are removed.
A deleted account can never log in again. The live `system_role` /
`is_active` of every user is mirrored in Redis (`auth:user_state:{id}`,
`shared/user_state.py`) so every service applies changes at once.

**user_profiles** — `id`, `user_id` (FK → users, 1:1), `first_name`,
`last_name`, `avatar_url`, `bio`, `timezone`, `country`.

**user_settings** — `id`, `user_id` (FK → users, 1:1), `preferred_currency`,
`theme`, `default_model`, `notification_preferences` (JSONB).

**user_sessions** — `id`, `user_id` (FK → users), `refresh_token_hash`
(unique; the refresh token itself is never stored), `expires_at`,
`ip_address`, `device_info`, `created_at`, `revoked_at`, `revoked_reason`
(nullable: `rotated` / `logout` / `revoked_all` / `reuse`; a revoked row
without a reason is treated as `rotated`). Only a `rotated` (or NULL)
token presented again revokes every session of the user; a logged-out
token just gets a 401. A new session deletes the user's sessions that
expired more than 7 days ago.

**oauth_accounts** — `id`, `user_id` (FK → users), `provider`,
`provider_user_id`.

**two_factor_secrets** — `id`, `user_id` (FK → users, 1:1), `secret`,
`enabled`, `last_used_step` (the last accepted TOTP step; a code of that
step or earlier is rejected, so it cannot be replayed). `secret` holds the
encrypted value (`fernet:<token>`, see
`services/auth-service/security.py`); a value without the prefix is a
plain-text secret, encrypted on the user's next successful 2FA check.

**organizations** — `id`, `name`, `description`, `owner_id` (plain UUID →
`users.id`, same-schema so this one *is* a real FK), timestamps.

**organization_members** — `id`, `organization_id` (FK), `user_id` (FK →
users), `role` (default `"member"`; one of the fixed roles `owner`,
`admin`, `member`, `viewer`), `joined_at`. There are no custom
organization roles.

**organization_invitations** — `id`, `organization_id` (FK), `email`, `role`,
`invited_by` (FK → users), `expires_at`, `accepted_at`. There is no
status column. An invitation with `role = 'owner'` cannot be accepted
(409).

**connections** — `id`, `requester_id` / `addressee_id` (FK → users),
`pair_key` (deterministic, order-independent pair identifier — lets a unique
constraint prevent duplicate connection requests in either direction without
a self-referential composite unique index), `status` (default `"pending"`),
`created_at`, `responded_at`, `blocked_by` (FK → users, `ON DELETE SET
NULL`: who blocked, the only user who may unblock).

## 3. `core` schema (core-service)

**portfolios** — `id`, `owner_id` (plain UUID, cross-schema), `organization_id`
(plain UUID, nullable), `name`, `currency` (default `"USD"`; validated
against the `Currency` enum `USD`/`EUR`/`GBP` at creation and used by
`GET /api/v1/portfolios/{id}/snapshots` for a display-only conversion
overlay, see `docs/API_SPEC.md` §3), `visibility` (default `"private"`),
`auto_analyze_at_open` (bool: re-analyze the holdings at market open),
timestamps.

**holdings** — `id`, `portfolio_id` (FK), `ticker`, `quantity` (NUMERIC),
`average_cost` (NUMERIC). This table is only a cache of the replayed
transaction ledger: holdings change only through ledger rows (the holding
API routes record `adjustment` transactions instead of writing here).

**transactions** — `id`, `portfolio_id` (FK), `ticker`, `transaction_type`
(`buy`/`sell`/`dividend`/`deposit`/`withdrawal`/`stock_split`/`adjustment`),
`quantity`,
`price`, `dividend_per_share` (only meaningful for `dividend`; a
dedicated column rather than overloading `price`), `split_ratio` (only
meaningful for `stock_split`, for the same reason; one real value covers a
forward split, a reverse split, and a stock dividend, since all three are
mechanically identical ratio-based rescales of an existing holding with
zero cash effect), `transaction_date`, `idempotency_key` (automated
fills). **`adjustment`**: shares moved in
(positive `quantity`) or out (negative `quantity`) at a per-share cost basis
(`price`, >= 0, required for shares in, may be NULL for shares out), with
**no cash effect** -- a transfer from another broker, a correction or an
opening position. Shares in move the average cost like a buy; shares out
leave it unchanged and cannot exceed the held quantity. Backtests and the
history export treat it as an external flow, never as return.
`transaction_type` is a plain VARCHAR and `quantity`/`price` are
unconstrained NUMERIC; the allowed values are checked by the application.

**portfolio_snapshots** — `id`, `portfolio_id` (FK), `snapshot_date`,
`total_value`, `cash_balance`.

**watchlists** — `id`, `owner_id` (plain UUID, cross-schema), `name`.

**watchlist_items** — `id`, `watchlist_id` (FK), `ticker`, unique on
`(watchlist_id, ticker)`.

**documents** — `id`, `owner_id` (plain UUID), `organization_id` (plain UUID,
nullable), `filename`, `document_type` (`pdf`/`txt`/`md` — file format),
**`category`** (`filing`/`analyst_report`/`earnings_call`/`news`/
`macro_research` — RAG-layer classification, a different axis from
`document_type`; user-supplied at upload since there's no reliable way to
infer document semantics from bytes alone), **`ticker`** (nullable — not
every category is about one company), `storage_url` (an object *key* into
S3-compatible object storage via `shared/storage.py`, not a fetchable URL — see §6 for why), and
`status` (`uploaded`→`parsing`→`chunking`→`embedding`→`ready`/`failed`,
driven by ai-service's ingestion workflow), `uploaded_at` (default `now()`),
`status_changed_at` (default `now()`, set on every applied status change;
the stuck-document rule measures from it),
`content_date` (nullable: the date of the content itself, for example a
filing's SEC filing date, set by automated ingestion). `category` and
`ticker` drive `ai/rag/chunking.py`'s category-aware chunking and
`ai/rag/retriever.py`'s ticker-scoped filtering. Nullable `title`
(VARCHAR(256), display title;
the list sorts by `coalesce(title, filename)`) and `source_url` (TEXT, where
a system document came from). A system document has the well-known owner
`00000000-0000-0000-0000-0000000005ec` (`is_system` in the API response is
derived from it, not stored). Two partial unique indexes stop a duplicate
upload per tenant: `(organization_id, ticker, category, filename)` where
`organization_id IS NOT NULL`, and `(owner_id, ticker, category, filename)`
where it is NULL; both are `NULLS NOT DISTINCT`, so a document without a
ticker (macro research) is covered too.

**research_notes** — `id`, `author_id` (plain UUID), `organization_id`
(nullable), `title`, `content`, `search_vector` (Postgres `TSVECTOR`,
generated column, full-text search), `yjs_state` (bytea — the collaborative
editing CRDT state), timestamps, plus the moderation and sentiment
columns below. Yjs is the source of truth for the body: the Yjs document's `Y.Text` named `"content"` is written back to
`content` (and so to `search_vector`) at every persist; title and tags are
not part of the Yjs document.

**research_comments** — `id`, `note_id` (FK), `author_id` (plain UUID),
`content`, `created_at`, `updated_at` (nullable: set when the author
edits the comment, NULL if never edited), plus the moderation and
sentiment columns below.

Moderation and sentiment columns, on both `research_notes` and
`research_comments`:
`is_hidden` (BOOLEAN NOT NULL, default false), `hidden_by` (plain UUID of the
moderator), `hidden_at` — set by a moderator; hidden rows are left out of
every list, search and detail except for their author and moderators —
and `sentiment_label` (VARCHAR(16): `positive`/`neutral`/`negative`),
`sentiment_score` (DOUBLE PRECISION, -1..1), `sentiment_model`
(VARCHAR(128)), `sentiment_analyzed_at` — all NULL until ai-service
(`POST /internal/sentiment`) has analysed the current text; rows without
a sentiment are filled by `scripts/backfill_sentiment.py --apply` or at
their next edit.

**research_tags** — `id`, `name` (unique). **note_tags** — plain many-to-many
join table, `note_id` + `tag_id` (both FK).

**notifications** — `id`, `user_id` (plain UUID), `type`, `title`, `content`,
`read_at`, `created_at`. `type` is a plain string
(`shared/enums.py::NotificationType`), so a new type needs no
migration.

**api_keys** — `id`, `owner_user_id` (plain UUID, NOT NULL, indexed), `name`,
`key_hash` (unique), `rate_limit_per_minute`, `last_used_at`, `revoked_at`,
`created_at`. A key always belongs to one user; there is no organization
owner and no scopes.

**event_outbox** (migration `f1200c000001`) — transactional outbox for
core-service's durable events. `id` (UUID PK, default `gen_random_uuid()`),
`event_name` (VARCHAR(100), NOT NULL), `payload` (JSONB, NOT NULL),
`created_at` (TIMESTAMPTZ, NOT NULL, default `now()`), `published_at`
(TIMESTAMPTZ, NULL until the event reached Redis), `attempts` (INTEGER,
NOT NULL, default 0), `last_error` (TEXT, nullable). Partial index
`ix_event_outbox_unpublished` on `created_at` `WHERE published_at IS NULL`.
`document.uploaded`, `document.deleted` and `portfolio.deleted` are
inserted in the same transaction as the change they announce
(`outbox.py::enqueue`), published after the response, and retried by the
outbox dispatcher (every 5 s, rows unpublished for more than 10 s, oldest
first, `FOR UPDATE SKIP LOCKED`) until published. Rows published more than
7 days ago are deleted. No foreign keys: the payload carries the ids.

## 4. `ai` schema (ai-service)

A tier cancelled because its portfolio was deleted (`portfolio.deleted`
event) uses the terminal status `expired` with a note naming the reason;
there is no separate cancelled status.

### Analysis pipeline

**analyses** — `id`, `user_id` (plain UUID, **nullable** — a system-triggered
batch analysis, e.g. core-service's scheduled market-open re-analysis job,
has no owning user), `portfolio_id` (plain UUID, nullable), `ticker`, `status`
(`pending`→`running`→`completed`/`failed`), `failure_reason` (nullable
text), `created_at`. Indexed on
`(user_id, created_at)` and `(ticker, created_at)` (the latter backs the hot
"latest recommendation for this ticker" lookup).

**recommendations** — `id`, `analysis_id` (FK), `recommendation`
(`buy`/`hold`/`sell`), `confidence`, `judge_leaning` and
`judge_confidence` (nullable: the Judge's own verdict before the code's
adjustments; NULL when there was no verdict), `reasoning`,
`confidence_adjustment_note`
(nullable — the human-readable explanation for *why* confidence was adjusted,
never just a bare number), `tier_generation_note` (nullable: why no tiers
were made, and which earlier unfilled tiers this recommendation closed),
`debate_converged` (bool), `window_expires_at` (nullable: the end of the
window in which its tiers may trigger, fixed at generation; NULL for HOLD
and without a portfolio), `accepted_at` (nullable: the user accepted all
its tiers at once), `created_at`.

**recommendation_tiers** — `id`, `recommendation_id` (FK → recommendations,
indexed), `side` (`buy`/`sell`), `target_price` and `quantity` (NUMERIC),
`status` (indexed; `pending_acceptance` → `accepted_awaiting_trigger` →
`triggered`, or `expired` / `superseded` / `declined` /
`execution_failed`), `triggered_at`, `triggered_price`, `created_at`. The
daily tier-monitoring job checks accepted tiers against the session's
daily bar and sends a triggered tier to core-service as an order; there is
no per-tier acceptance (see `recommendations.accepted_at`).

**reflections** — `id`, `analysis_id` (FK), `round_number`, `verified` (bool),
`issues` (JSONB), `retry_agents` (JSONB).

**agents** — small catalog table, one row per `AgentRole`, upserted lazily
(not seeded) since the set of roles is defined in code and rarely changes:
`id`, `name` (unique), `description`, `version`, `active`.

**agent_runs** — `id`, `analysis_id` (FK), `agent_id` (FK → agents), `status`,
`started_at`/`completed_at` (the agent's own start and end time; the
persistence time when the agent output carries none), `duration_ms`
(NULL when either time is missing).

**agent_results** — `id`, `agent_run_id` (FK, unique — one result per run),
`score`, `summary`, `output` (JSONB, the raw scratchpad).

**debate_sessions** — `id`, `analysis_id` (FK), `created_at`.

**debate_arguments** — `id`, `debate_session_id` (FK), `side` (`bull`/`bear`),
`content`, `round_number`, `citations` (JSONB); together they let the
debate transcript be rebuilt from `AnalysisState.debate.arguments`. These
are the only stored citations of an analysis.

### RAG (retrieval-augmented generation)

The vector data lives in Qdrant; these tables are the Postgres-side
bookkeeping.

**document_chunks** — `id`, `document_id` (plain UUID → `core.documents.id`),
`chunk_index`, `token_count`, `qdrant_point_id` (unique — the actual
addressable Qdrant point this row's text lives under). This table's entire
purpose is letting a citation resolve back to "where the vector actually is"
without querying Qdrant just to check existence.

**embedding_jobs** — `id`, `document_id` (plain UUID), `embedding_model`,
`status`, `created_at`. One row per ingestion run.

### Memory (L2 episodic / L4 procedural)

See `handbook/18-ai-knowledge-memory.md` for the full L0–L4 breakdown; only L2 and L4
persist to Postgres (L0/L1 are in-process/graph-state only, L3 *is* the RAG
section above).

**episodic_memories** (L2) — `id`, `user_id` (plain UUID, nullable),
`analysis_id` (FK → analyses), `ticker`, `recommendation`, `confidence`,
`outcome` (nullable string, `NULL` until a review workflow evaluates real
performance: "too recent to know yet"), `outcome_return`
(nullable float, the ticker's realized return over the review window),
`summary` (stored, never shown to the Judge), `created_at`.
Indexed on `(ticker, created_at)` — one row written per completed analysis,
read back by `episodic_memory_node` (the requesting user's own rows plus
system rows) as the Judge's track record. The ticker-keyed shape answers
"what did we conclude about this ticker before" without joins.

**agent_memories** (L4) — `id`, `agent_id` (FK → agents), `ticker` (nullable),
`user_id` (nullable: the owner of the analysis that wrote the row; null for
an analysis without an owner), `memory_type` (default `"trajectory"`),
`score`, `content` (JSONB — the model-chosen tool calls (marked
`model_steps_only`) + final answer, read back as few-shot prompt text, never
queried by its internal fields), `created_at`. Written for a clean agent run
scoring ≥ 0.7 that made at least one tool call of its own; read back per
role and owner only (same ticker first, then the same owner's other
tickers — see `ai/memory/procedural.py`) before that role's next run.

### Knowledge (agent reports and knowledge graph)

**agent_reports** — `id`, `analysis_id` (FK → analyses, indexed),
`agent_role`, `ticker` (nullable, indexed), `category`, `content`,
`created_at`. One archived agent answer, also indexed into the RAG
collection (`ai/knowledge/archival.py::archive_agent_report`).

**kg_entities** — `id`, `name` (as extracted, for display),
`normalized_name` (`name.strip().upper()`, the dedup key), `entity_type`,
`created_at`; unique on `(normalized_name, entity_type)`.

**kg_relations** — `id`, `source_entity_id` / `target_entity_id` (FK →
kg_entities, indexed), `relation_type`, `source_report_id` (FK →
agent_reports: the report the relation was extracted from), `confidence`
(nullable float), `kind` (nullable: `stable` or `event`; NULL counts as
stable), `as_of` (nullable date: the date an event fact holds as of),
`created_at`.

**filing_records** — `id`, `ticker`, `filename` (unique), `form`,
`filing_date`, `report_date` (nullable), `records` (JSONB: the structured
records of one SEC filing: legal matters, transactions, guidance, unusual
items, commitments, results), `created_at`; indexed on
`(ticker, filing_date)`. One row per filing, keyed by filename, not by
document id (`handbook/18-ai-knowledge-memory.md` section 3.11).

**ticker_names** — `id`, `ticker` (unique), `official_name`, `created_at`.
The company name for a ticker (from SEC), used by the news queries and KG
naming.

### Decision tracking

**investment_decisions** — `id`, `user_id` (plain UUID), `portfolio_id`
(plain UUID, nullable), `recommendation_id` (FK → recommendations),
`ticker`, `action_taken` (`followed`/`ignored`/`partial`), `notes`
(nullable), `created_at`; unique on `(user_id, recommendation_id)`.

**decision_outcomes** (model `DecisionOutcome`) — `id`, `decision_id` (FK), `evaluation_days` (the
horizon this row evaluates — a decision accumulates one row per horizon
reached, e.g. 7/30/90 days, not one row total), `strategy_return`,
`benchmark_return`, `alpha`, `outperformed`, `evaluated_at`. A row is
written only when both returns are known; a missing price
series leaves the window unscored and the next review run tries again.

**decision_reviews** — `id`, `decision_id` (FK), `review_text`,
`lessons_learned`. Written by `POST /api/v1/decisions/{id}/review` (one
row per call). Re-posting the decision deletes its reviews
together with its `decision_outcomes` rows.

### Backtesting

**backtests** — `id`, `user_id` (plain UUID, nullable), `strategy_name`,
`benchmark_symbol`, `start_date`, `end_date`, `initial_capital`,
`ending_capital`, `strategy_return`, `benchmark_return`, **`status`**
(pending/running/completed/failed: a backtest runs as a detached
background task, so it exposes its progress like `analyses.status`),
`created_at`. The single-ticker and recommendation replays replay every
triggered tier (FIFO lots, open positions marked to market) in memory;
per-trade and per-position rows are not stored.

**backtest_metrics** — `id`, `backtest_id` (FK, unique), `sharpe_ratio`,
`sortino_ratio`, `drawdown`, `win_rate`.

**benchmarks** — `id`, `symbol` (unique), `name`, `asset_type`, `currency`
(default `"USD"`), `description`, `created_at`. Upserted lazily per symbol
encountered, same pattern as the `agents` catalog table.

**benchmark_results** — `id`, `backtest_id` (FK), `benchmark_id` (FK),
`strategy_return`, `benchmark_return`, `alpha`, `beta`, `information_ratio`,
`tracking_error`, `outperformed`, `created_at`.

### Analytics (portfolio/agent performance rollups)

**recommendation_metrics** — `id`, `recommendation_id` (FK →
recommendations, same-schema, a legitimate FK), `accuracy_score`. Written
once per recommendation by the review workflow at a single fixed evaluation
checkpoint (this table has no `evaluation_days` column, unlike
`decision_outcomes` — recommendations are scored once, not re-evaluated at
multiple horizons; that asymmetry is intentional, not an oversight).

**agent_metrics** — `id`, `agent_id` (FK → agents, legitimate same-schema
FK), `avg_score`, `success_rate`; unique on `agent_id`. Upserted by the
nightly review (`run_agent_metrics_rollup`): over the agent's completed
and failed runs, the mean `agent_results.score` and the share of
completed runs.

There is no stored portfolio metric: portfolio performance is computed on
request by the portfolio backtest.

### Market data cache

**instruments** — `id`, `ticker` (unique), `name`, `asset_type`, `currency`
(default `USD`), `sector`, `exchange`: ticker metadata cache.
**price_bars** — `id`, `instrument_id` (FK), `interval`, `timestamp`,
`open`, `high`, `low`, `close`, `volume` (NUMERIC), `fetched_at` (default
`now()`, read by the cache's freshness check); unique on `(instrument_id,
interval, timestamp)`. A daily bar is stamped at midnight New York and
stored only after its session closed; a stored bar
fetched before its 16:00 ET close is not used as the close. Agents read bars cache-first
(`ai/tools/market_data_cache.py`), and a miss or stale read refreshes them
from the market data provider
(`domains/market/service.py::refresh_from_external_provider`). Indicators
are computed on the fly (`ai/tools/indicators.py`); they are not stored.

## 5. Object storage (not Postgres)

Research documents' raw bytes live in S3-compatible object storage, not
in any database: SeaweedFS in docker compose (service `objectstore`, also
reachable under the network alias `minio`), a real AWS S3 or Cloudflare R2
endpoint in production, with the same code either way
(`shared/storage.py`). `core.documents.storage_url` holds the object
*key*, not a browsable URL: `GET /api/v1/research/documents/{id}/content`
is the only way to retrieve the bytes (there is no directly linkable file
URL).

## 6. Vector storage (not Postgres)

Chunk embeddings live in Qdrant (one collection,
`settings.QDRANT_COLLECTION_RESEARCH` — deliberately one collection for every
category/ticker, filtered per-query, not one collection per category; see
`handbook/21-qdrant.md` section 5). `ai.document_chunks` is Postgres-side bookkeeping only, mapping
a chunk's identity to its Qdrant point ID — the payload text/vector itself is
never duplicated into Postgres.
