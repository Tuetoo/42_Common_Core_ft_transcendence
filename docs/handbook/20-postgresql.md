# 20. PostgreSQL

> PostgreSQL is the system of record for AlphaMind. Every user, organization,
> portfolio, transaction, research note, analysis, recommendation and tier
> lives in one PostgreSQL 16 database called `alphamind`. The database is
> split into three schemas, `auth`, `core` and `ai`, one per backend
> service. Each service talks to its own schema through SQLAlchemy 2 in
> async mode, and each service brings its own schema up to date with
> Alembic migrations every time it starts. This chapter explains how that
> works, why it is built this way, how concurrency is kept safe with
> transactions and locks, how the database is backed up and monitored, and
> how to look inside it during a demo.

---

## 1. The big picture

There is one PostgreSQL server (the compose service `postgres`, image
`postgres:16-alpine`). Inside it there is one database, `alphamind`.
Inside that database there are three schemas. A schema is a named folder
of tables inside a database. Each backend service owns exactly one schema:

```
                    +--------------------------------------------------+
                    |  postgres (postgres:16-alpine)                    |
                    |  database "alphamind"                             |
                    |                                                   |
 auth-service ----->|  schema auth : users, user_sessions, organizations|
 (asyncpg pool)     |                organization_members, connections  |
                    |                ... + auth.alembic_version         |
                    |                                                   |
 core-service ----->|  schema core : portfolios, holdings, transactions,|
 (asyncpg pool)     |                portfolio_snapshots, documents,    |
                    |                research_notes, notifications,     |
                    |                api_keys ... + core.alembic_version|
                    |                                                   |
 ai-service ------->|  schema ai   : analyses, recommendations,         |
 (asyncpg pool)     |                recommendation_tiers, agent_runs,  |
                    |                document_chunks, backtests ...     |
                    |                + ai.alembic_version               |
                    +--------------------------------------------------+
                          ^                ^                 ^
                          |                |                 |
         postgres-exporter (metrics)   scheduler (pg_dump   Adminer / psql
         -> Prometheus                 at 02:30 New York)   (browsing, demos)
```

Three rules hold everything together:

1. **One schema per service.** A service only reads and writes its own
   schema. Other services' data is reached over HTTP, never by SQL.
2. **No foreign keys across schemas.** When a row in one schema refers to
   a row in another schema (for example `ai.analyses.user_id` refers to a
   user in `auth.users`), the column is a plain UUID, not a SQL
   `FOREIGN KEY`. Foreign keys are used freely inside one schema.
3. **Alembic owns the schema.** Tables are created and changed only by
   Alembic migrations. Every service runs `alembic upgrade head` for its
   own schema at startup.

---

## 2. Concepts you need

| Term | What it is | Why it matters here |
|---|---|---|
| Schema | A namespace inside one database. `core.portfolios` means table `portfolios` in schema `core`. | Gives each service its own private area without running three database servers. |
| `search_path` | The list of schemas PostgreSQL searches when a query names a table without a schema. | Each service sets it to `<its schema>,public`, so code can write `portfolios` instead of `core.portfolios`. |
| Connection pool | A set of open database connections that are reused instead of opened per query. | Opening a connection is slow. Each service keeps up to 20 + 10 connections. |
| asyncpg | A fast PostgreSQL driver for Python `asyncio`. | The services are async (FastAPI), so the driver must not block the event loop. |
| SQLAlchemy ORM | A library that maps Python classes ("models") to tables. | Models live in `domains/*/model.py`. Queries are written in Python. |
| Session | SQLAlchemy's unit of work: it tracks loaded objects and holds one database transaction. | One session per HTTP request. It commits at the end, or rolls back on error. |
| Transaction | A group of SQL statements that succeed or fail together (all or nothing). | A transaction that records a buy also updates the holding. Either both happen or neither. |
| Commit / rollback | Commit makes the transaction's changes permanent. Rollback throws them away. | The request's commit happens before the response is sent (section 3.4). |
| Flush | Sends pending changes to the database inside the open transaction, without committing. | Repositories flush so later queries see the new rows; the request commits once at the end. |
| Savepoint | A marker inside a transaction you can roll back to, without losing the whole transaction. SQLAlchemy calls it `begin_nested()`. | Used to recover from a duplicate idempotency key (section 3.6). |
| Row lock (`SELECT ... FOR UPDATE`) | Locks the selected rows until the transaction ends. Another transaction that wants the same lock waits. | Prevents two concurrent writes from both reading the same old value (a "lost update"). |
| Advisory lock | A lock on an arbitrary number, not on a row. `pg_advisory_xact_lock(n)` holds it until the transaction ends. | Used when there is no single row to lock, for example "the analysis queue". |
| Unique constraint | A rule that no two rows may have the same value(s) in some columns. | The last line of defence against duplicates when two requests race. |
| Upsert | `INSERT ... ON CONFLICT DO UPDATE` (or `DO NOTHING`): insert, or update/skip if the row exists. | Makes "write once per key" atomic without a read-then-write race. |
| Alembic | The migration tool for SQLAlchemy. | Changes the schema step by step, in a recorded order. |
| Alembic revision | One migration file with an id, a parent id (`down_revision`), an `upgrade()` and a `downgrade()`. | The revisions form a chain. The newest one is called "head". |
| `alembic_version` | A one-row table where Alembic writes the revision the schema is at. | Each schema has its own: `auth.alembic_version`, `core.alembic_version`, `ai.alembic_version`. |
| JSONB | PostgreSQL's binary JSON column type. | Stores flexible structures such as agent output and reflection issues. |
| TSVECTOR | A preprocessed list of words for full-text search. | `core.research_notes.search_vector` powers note search. |
| Generated column | A column whose value PostgreSQL computes from other columns of the same row. | `search_vector` and `auth.connections.pair_key` are generated. |
| GIN index | An index type for "contains" lookups over many values (words, JSON keys). | Makes full-text search over notes fast. |

---

## 3. How it works in AlphaMind

### 3.1 One database, three schemas

The `postgres` service in `docker-compose.yaml` mounts
`infra/postgres/init-schemas.sql` into `/docker-entrypoint-initdb.d/`. The
official image runs files in that folder **only once**, when the data
volume (`postgres_data`) is empty. The file is short:

```
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ai;
```

The services do not depend on this file alone: at every start,
`shared/database.py::bootstrap_schema` also runs
`CREATE SCHEMA IF NOT EXISTS "<schema>"` for its own schema.

Which schema a service uses comes from the setting `POSTGRES_SCHEMA`:

| Service | File | `POSTGRES_SCHEMA` |
|---|---|---|
| auth-service | `services/auth-service/config.py` | `auth` |
| core-service | `services/core-service/config.py` | `core` |
| ai-service | `services/ai-service/config.py` | `ai` |

The base default in `shared/config.py` is `public`; each service overrides
it in its own settings class. `.env.example` does not set it, because all
three services read the same env file and need different values.

All three services connect with the same database user (`POSTGRES_USER`
from the env file). So the schema boundary is a **convention enforced by
code and review**, not by database permissions. The cross-schema rule is
written down in `../ERD.md` section 1. Examples of cross-schema references
that are plain UUID columns:

- `core.portfolios.owner_id` and `organization_id` (users and organizations
  live in `auth`), see `services/core-service/domains/portfolio/model.py`.
- `ai.analyses.user_id` and `portfolio_id`, see
  `services/ai-service/domains/analysis/model.py`.
- `ai.document_chunks.document_id` (documents live in `core.documents`),
  see `services/ai-service/domains/rag/model.py`.
- `ai.investment_decisions.portfolio_id`, see
  `services/ai-service/domains/decision/model.py`.

Why plain UUIDs: a real foreign key would tie the schemas together. One
service could not be migrated, restored or moved to its own database
without the others. The price is that the database does not stop a
dangling reference. The services handle it with events instead: for
example, deleting a portfolio publishes the durable event
`portfolio.deleted`, and ai-service closes that portfolio's open tiers
(see [24-events.md](24-events.md)).

### 3.2 The engine and the session factory

All three services build their database objects the same way, with
helpers from `shared/database.py`. Each `services/<svc>/database.py` is
three lines:

```python
engine = create_service_engine(settings)
SessionLocal = create_session_factory(engine)
get_db = make_get_db(SessionLocal)
```

`create_service_engine(settings)` calls SQLAlchemy's
`create_async_engine` with:

- the URL from `BaseServiceSettings.assemble_database_url()`:
  `postgresql+asyncpg://<user>:<password>@<host>:<port>/<db>` (or
  `DATABASE_URL` if it is set);
- `pool_size=DB_POOL_SIZE` (default 20) and
  `max_overflow=DB_MAX_OVERFLOW` (default 10);
- `pool_pre_ping=True`: before a pooled connection is handed out, a cheap
  ping checks it is still alive (a restarted Postgres would otherwise
  give the first request a dead connection);
- `connect_args={"server_settings": {"search_path": "<schema>,public"}}`.

Why `server_settings` and not a SQLAlchemy `connect` event that runs
`SET search_path` on each new connection: with the event, some pooled
connections can end up with PostgreSQL's default search path
(`"$user", public`), and the service's tables then "disappear" for those
connections (`relation "X" does not exist`). Reproduced, the exact
trigger was not pinned down (pool pre-ping reconnects were the leading
suspect), but "hope an event fires before every query" is fragile
anyway. asyncpg's `server_settings` are sent in the connection
handshake, so the setting is in place before any query can run on that
connection.

`create_session_factory(engine)` returns an `async_sessionmaker` with:

- `expire_on_commit=False`: objects stay readable after commit. FastAPI
  serializes the response after the handler returns; with expiry on, that
  would trigger a new (forbidden, in async) lazy load.
- `autoflush=False`: SQLAlchemy does not flush automatically before every
  query; repositories call `flush()` explicitly when they need the new
  rows visible.

Connection budget (`DB_POOL_SIZE` and `DB_MAX_OVERFLOW` in
`shared/config.py`): `(DB_POOL_SIZE + DB_MAX_OVERFLOW) x replicas x 3 services` must stay below
Postgres `max_connections` (PostgreSQL's default is 100). At the defaults
with one replica each, that is at most 90 connections.

`shared/database.py` also defines:

- `Base`, the declarative base, with a naming convention for indexes and
  constraints (`ix_...`, `uq_<table>_<column>`, `ck_...`, `fk_<table>_<column>`,
  `pk_<table>`), so autogenerated migrations produce stable names;
- `count_query(session, stmt)`, which runs a real `SELECT COUNT(*)` for
  pagination, instead of loading and hydrating every matching row only
  to count them (harmless at a few dozen rows, a real cost at thousands);
- `check_database_connection(engine)`, a `SELECT 1` used by each
  service's `/health` endpoint.

### 3.3 Models

A model is a Python class that describes one table. Shared mixins live in
`shared/database.py`, next to `Base`:

- `UUIDPKMixin`: `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`. The
  database creates the id, not Python.
- `TimestampColumnsMixin`: `created_at` and `updated_at`, both
  `timestamptz DEFAULT now()`; `updated_at` is also set on update by
  SQLAlchemy (`onupdate=func.now()`).

Where the models are:

| Schema | Model files |
|---|---|
| `auth` | `services/auth-service/domains/auth/model.py`, `domains/organization/model.py`, `domains/social/model.py` |
| `core` | `services/core-service/domains/portfolio/model.py`, `domains/research/model.py`, `domains/notification/model.py`, `services/core-service/apikeys.py`, `services/core-service/outbox.py` (`event_outbox`) |
| `ai` | `services/ai-service/domains/{analysis,analytics,backtest,decision,filings,knowledge,market,memory,rag}/model.py` |

Each service has one function that imports every model file so that
`Base.metadata` knows all tables: `register_models()` in each
`services/<svc>/database.py` (used by its `ensure_schema`, its
`migrations/env.py` and its tests; one list, so two copies cannot drift
apart). A model file that is not imported there
is invisible to `Base.metadata`: its table is then "unknown" to every
schema comparison (the Alembic contract test fails), and a test that builds tables with `create_all` misses it.
Add every new model file to that function (core-service's list includes
`outbox` for `core.event_outbox`).

### 3.4 One session per request, committed before the response

Route handlers get a session through FastAPI dependency injection:

```python
async def create_analysis_internal(
    payload: AnalysisCreate, db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AnalysisRead]:
```

(`services/ai-service/api/internal.py`; every call site in the three
services uses the same form.)

`get_db` is built by `shared/database.py::make_get_db`:

```python
async def get_db():
    async with session_factory() as session:
        try:
            yield session          # the handler runs here
            await session.commit() # all of the request's writes, at once
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
```

So one request = one session = one transaction. Repositories only
`flush()`; the commit happens once, after the handler returns. The
exception is a handler that starts background work which reads the new
row: analysis create (`domains/analysis/api.py::create_and_enqueue`) and
the three backtest creates (`domains/backtest/api.py`) call
`db.commit()` themselves before they enqueue the job or start the task,
so the worker never looks for a row that is not committed yet. The
`get_db` teardown then commits an empty transaction.

**Why `scope="function"`.** A FastAPI dependency with `yield` has a
teardown (the code after `yield`). With FastAPI's default scope
(`"request"`), that teardown runs only **after the response has been sent
and after background tasks have run**. Two real problems followed from
that:

1. A client could receive `201 Created` for a write whose commit then
   failed. The client believed something was saved that was not.
2. Background tasks ran **before** the commit. core-service publishes the
   durable event `document.uploaded` as a background task. ai-service
   could receive the event and look for a document row that was not
   committed yet.

With `scope="function"`, the teardown (and the commit) runs as soon as the
handler function returns, before the response is sent and before
background tasks. The resulting order for an upload is:

```
handler runs -> flush rows -> handler returns
   -> get_db teardown: COMMIT
   -> response 201 sent
   -> background task: publish document.uploaded (Redis stream)
```

The event itself is not only in memory. The handler writes it as a row
of `core.event_outbox` in the same transaction (`outbox.enqueue`), so the
row is committed exactly when the document row is. The background task
publishes it and marks the row published. If that publish fails, the
outbox dispatcher publishes the row later (chapter 24, section 3.5).

Code that is not a request (event handlers, scheduled workflows, startup
tasks) opens its own session with `async with SessionLocal() as session:`
and commits explicitly. Example: `run_rag_ingestion_workflow` in
`services/ai-service/ai/workflows/rag_ingestion_workflow.py`.

### 3.5 Alembic migrations and how they run at startup

**What a migration is.** A migration is a Python file with an
`upgrade()` function that changes the schema (create a table, add a
column, recreate an index) and a `downgrade()` that undoes it. Each file
has a `revision` id and a `down_revision` (its parent). Alembic follows
the chain from the revision stored in `alembic_version` up to the newest
file ("head") and runs each `upgrade()` in order.

**Files per service.**

| Service | `alembic.ini` | Migrations | Chain (oldest -> head) |
|---|---|---|---|
| auth | `services/auth-service/alembic.ini` | `services/auth-service/migrations/versions/` | `f1160b000001` (baseline, creates every table) -> `f1190b000001` (adds nullable `user_sessions.revoked_reason`; old revoked rows keep NULL, treated as `rotated`) -> `f1210b000001` (drops `organization_roles` with its rows: custom organization roles were never assigned or checked, owner decision; the downgrade recreates the table empty). Head `f1210b000001` |
| core | `services/core-service/alembic.ini` | `services/core-service/migrations/versions/` | `f1160c000001` (baseline) -> `f1170c000001` (document unique indexes become `NULLS NOT DISTINCT`; unused `api_keys.owner_org_id` and `scopes` dropped, `owner_user_id` NOT NULL; refuses with a list when duplicates or ownerless keys exist) -> `f1200c000001` (adds the `event_outbox` table and its partial index; additive) -> `f1250c000001` (adds `documents.status_changed_at`, filled from `uploaded_at`). Head `f1250c000001` |
| ai | `services/ai-service/alembic.ini` | `services/ai-service/migrations/versions/` | `f1160a000001` (baseline) -> `f1170a000001` (drops `technical_indicator_cache`, `memory_references`, `rag_collections`, `retrieval_logs`, created from the ERD but never written or read; refuses if any holds rows) -> `f1210a000001` (drops the write-only `analysis_sources`, `analysis_citations`, `backtest_positions`, `backtest_trades` with their rows, owner decision; the downgrade recreates them empty) -> `f1220a000001` (adds `filing_records`) -> `f1230a000001` (adds nullable `kg_relations.kind` and `kg_relations.as_of`) -> `f1240a000001` (adds nullable `recommendations.judge_leaning` and `recommendations.judge_confidence`) -> `f1250a000001` (adds nullable `agent_memories.user_id`, the owner of a procedural-memory row). Head `f1250a000001` |

Every later migration is either additive (a new table, a nullable
column) or a drop that was decided on purpose; each downgrade undoes
exactly its own upgrade (a dropped table comes back empty).

**`migrations/env.py`.** Each service has its own. They are synchronous
and use the psycopg driver
(`settings.assemble_sync_database_url()`, `postgresql+psycopg://...`),
which is Alembic's normal mode. Two important settings in
`context.configure(...)`:

- `version_table_schema=settings.POSTGRES_SCHEMA`: the
  `alembic_version` table lives inside the service's own schema. So
  there are three independent version tables, and the three services can
  migrate at the same moment without touching each other.
- the connection runs `SET search_path TO <schema>` (only that schema,
  without `public`) before migrating, so an unqualified table name in a
  migration can only mean the service's own table.

`env.py` also excludes the `alembic_version` table from autogenerate
comparisons (`include_object=exclude_alembic_version`, one predicate in
`shared/database.py` used by all three services). Without that,
every autogenerated migration would contain a
`drop_table('alembic_version')`: dropping
Alembic's own bookkeeping table in the transaction that records the
migration can corrupt the migration state.

More `env.py` details, the same in all three services:

- Alembic is **not** made multi-schema aware (no `include_schemas=True`,
  models stay without a schema name). Instead the connection's own
  default schema is narrowed to the service's schema with the
  `search_path` above, so reflection compares "the default schema" on
  both sides with no cross-schema logic. (Alembic's multi-schema mode
  crashes inside Alembic's own comparison code with this setup.)
- `compare_type=True` and `compare_server_default=True`: `alembic check`
  and autogenerate also compare column types and server defaults.
  Without this, a quoted `"now()"` default (which freezes timestamps)
  would go unnoticed.
- After `SET search_path` the script commits explicitly before calling
  `context.begin_transaction()`. SQLAlchemy 2.0 "autobegins" a
  transaction on that first statement, and Alembic's transaction around
  an already-open one did not reliably commit: `alembic stamp head`
  reported success but the row was never stored.
- The script searches upward for the folder that contains
  `shared/database.py`, because `shared/` sits at the repository root on
  the host but next to `migrations/` inside the container.

**At startup.** Each service's `main.py` lifespan calls
`await ensure_schema(engine)` first
(`services/<svc>/database.py`), which calls
`shared/database.py::bootstrap_schema`. This runs in every
`APP_ENV`, production included:

```
bootstrap_schema(engine, schema, alembic_ini, register_models)
  1. register_models()                         # import every model
  2. CREATE SCHEMA IF NOT EXISTS "<schema>"
  3. read <schema>.alembic_version (exists? revision?)
  4. no revision but the schema has tables?    -> refuse (SchemaBootstrapError):
                                                  state unknown
  5. alembic upgrade head   (in a worker thread: asyncio.to_thread;
                             an unknown revision fails here)
  6. log "schema_bootstrap_complete" with revision_before / revision_after
```

Details worth knowing:

- An empty schema gets the baseline migration, which creates every
  table. There is no `create_all` path in the services any more.
- At head, `upgrade head` is a no-op, so restarts are fast.
- Alembic's commands are synchronous; running them with
  `asyncio.to_thread` keeps the event loop free.
- `build_alembic_config` builds the Alembic config in memory **without**
  a file name on purpose. `env.py` calls `logging.config.fileConfig`
  when a file name is set, and `fileConfig` disables every logger that
  already exists, which would silence the service's own structured logs.
- A failed bootstrap raises, so the service does not start. The compose
  healthcheck then stays unhealthy and the reason is in the service log.

**Backups from before the baselines.** A database restored from a
backup taken before the current baselines (before round 116) carries a
revision of an older migration chain that is no longer in the repository.
Alembic then stops with "Can't locate revision" and the service does not
start: such a backup cannot be restored with this version. See
`../DISASTER_RECOVERY.md` section 8.

**Tests.** The test suites build tables with `create_all` for speed (they
do not run the lifespan). One shared contract, `tests/alembic_contract.py`,
star-imported by `tests/auth-service/test_platform.py`,
`tests/core-service/test_platform.py` and `tests/ai-service/test_api.py`,
checks every schema in a throwaway schema of the test database:

- a fresh schema built by `alembic upgrade head` equals the models
  (`shared/database.py::schema_differences`: tables, columns,
  types, nullability, server defaults, indexes, unique constraints,
  foreign keys; column order is not compared, PostgreSQL cannot reorder
  columns);
- downgrade to base and upgrade again work, and a second start is a
  no-op;
- no model column has a quoted function default (`server_default="now()"`
  is stored as a constant taken at `CREATE TABLE` time);
- a schema with tables but no revision is refused.

Single migrations also have their own tests (for example the upgrade
`f1230a000001 -> f1240a000001` adds both columns and the downgrade
removes them; `f1240a000001 -> f1250a000001` adds
`agent_memories.user_id`).

**Adding a migration (development only, writes a file).** Inside the
service container, from `/app`, `alembic revision --autogenerate -m
"<message>"` compares the models with the live schema and writes a new
file under `migrations/versions/`. Read it, fix it by hand if needed, and
restart the service: the startup bootstrap applies it. The migrations in
this repository stop with a clear message instead of guessing when the
data would break the change in a way nobody decided (for example
`f1170c000001` checks for duplicate documents before it recreates the
unique indexes, and `f1170a000001` refuses to drop a table that holds
rows). A drop of data the owner decided to discard is explicit instead:
`f1210a000001` (ai) and `f1210b000001` (auth) drop their tables with the
rows. Take a backup before starting a service on a new migration.

### 3.6 Transactions, locking and duplicates

The database is the place where concurrent requests meet. The code uses
four tools: row locks, advisory locks, unique constraints and upserts.

#### Row locks (`SELECT ... FOR UPDATE`)

| Where | What is locked | Why |
|---|---|---|
| `services/core-service/domains/portfolio/service.py::_lock_portfolio_row` | the `core.portfolios` row | A BUY or WITHDRAWAL must not take cash below zero. Two outflows that each fit alone but not together would both read the same ledger and both pass. With the lock, the second waits for the first to commit, then sees it. |
| same file, `holdings.get_by_ticker(..., for_update=True)` | the `core.holdings` row of one ticker | Two writes to one holding (a buy and a split at the same time) must not overwrite each other's result. |
| same file, `record_transaction` with an idempotency key | the portfolio row, before looking the key up | A retry with the same key waits for the first attempt, then finds its row. |
| `services/ai-service/ai/workflows/tier_monitoring_workflow.py` | the `ai.recommendation_tiers` row (`session.refresh(tier, ["status"], with_for_update=True)`) | A newer recommendation can close (supersede) a tier while the 17:00 job runs. The status is re-read under the lock right before acting, so a closed tier is never executed. |
| `services/ai-service/domains/decision/repository.py` | the user's existing `ai.investment_decisions` row for an analysis | Two concurrent posts of a decision for the same analysis run one after the other. |
| `services/core-service/domains/research/repository.py` (document status update) | the `core.documents` row | Status updates from ai-service are forward-only; the lock keeps the check and the write together. |
| `services/core-service/yjs_relay.py::_load_snapshot` | the `core.research_notes` row | The first replica that opens a note seeds its collaborative state; others wait and load the same state. |

**Lock order.** `_ensure_cash_stays_non_negative` documents the rule:
every writer locks the portfolio row before any holding row
(portfolio -> holding). Two transactions that always take locks in the
same order cannot deadlock each other.

**Per-item commits in batch jobs.** The tier monitoring job commits after
each tier. A failure on one tier rolls back only that tier. It reloads
each tier by id inside the loop, because a rollback expires every object
the session had loaded.

#### Advisory locks

- **Analysis admission** (`services/ai-service/domains/analysis/queue.py::check_admission`):
  `SELECT pg_advisory_xact_lock(1120015)` (the constant
  `_ADMISSION_LOCK_KEY`), then count pending analyses and the user's active
  ones. The lock makes "count, then insert" atomic across concurrent
  requests. Limits: `ANALYSIS_QUEUE_MAX_PENDING` (default 50, else 503)
  and `ANALYSIS_MAX_ACTIVE_PER_USER` (default 3, public create only, else
  429), both in `services/ai-service/config.py`. The counts come from
  `ai.analyses`, so the limits survive a restart.
- **API key cap** (`services/core-service/apikeys.py::count_active_for_owner`):
  `SELECT pg_advisory_xact_lock(hashtext('apikeys:<owner id>'))`, so two
  concurrent key creations cannot both pass the per-user limit.
- **Backtest cap** (`services/ai-service/domains/backtest/repository.py::count_active_for_user(lock=True)`):
  `SELECT pg_advisory_xact_lock(hashtext('backtests:<user id>'))`, then
  count the user's `pending` and `running` backtests; the create route
  refuses with 429 at `BACKTEST_MAX_ACTIVE_PER_USER` (2). The lock is held
  until the new row is committed, so two concurrent creates cannot both
  pass the cap.

All three are `_xact_` locks: PostgreSQL releases them automatically at commit
or rollback.

#### Idempotency keys

An idempotency key is a client-chosen string that says "this is the same
request as before". Sending it twice must have the effect of sending it
once.

`core.transactions.idempotency_key` is `VARCHAR(64) UNIQUE`, NULL for
ordinary manual transactions. ai-service's tier monitoring sends
`idempotency_key="tier:<tier id>"` when it records a triggered tier as a
transaction. `PortfolioService.record_transaction`:

1. locks the portfolio row and looks the key up; if it exists, it returns
   the original transaction (or 409 if the key belongs to another
   portfolio) and logs `transaction_idempotent_replay`;
2. otherwise inserts inside a savepoint (`begin_nested()`);
3. if the insert still hits the unique constraint, it rolls back only
   the savepoint, loads the row that won, and applies the same check as in
   step 1 (same portfolio: return it; another portfolio: 409). This race
   is only possible across two portfolios, because the portfolio lock
   already serialises retries on one portfolio. The request's transaction
   and its portfolio lock survive the savepoint rollback.

#### Unique constraints and upserts

| Constraint | Table | Protects against |
|---|---|---|
| `uq_holding_portfolio_ticker` | `core.holdings` | two holding rows for one ticker in one portfolio |
| `uq_snapshot_portfolio_date` + `ON CONFLICT DO UPDATE` | `core.portfolio_snapshots` | a scheduled and a manual snapshot racing for the same day |
| `transactions.idempotency_key` unique | `core.transactions` | a retried automated fill applied twice |
| `uq_documents_org_ticker_category_filename`, `uq_documents_owner_ticker_category_filename` (partial, `NULLS NOT DISTINCT`) | `core.documents` | the same file uploaded twice by one tenant; `NULLS NOT DISTINCT` makes two rows with `ticker IS NULL` count as equal |
| `uq_connection_pair_key` on a generated column | `auth.connections` | A asking B and B asking A at the same moment |
| `uq_investment_decisions_user_recommendation` | `ai.investment_decisions` | two decisions by one user on one recommendation |
| `uq_decision_outcomes_decision_window` + `ON CONFLICT DO NOTHING` | `ai.decision_outcomes` | overlapping review runs scoring the same window twice |
| `recommendation_metrics.recommendation_id` + `ON CONFLICT DO NOTHING` | `ai.recommendation_metrics` | scoring one recommendation twice (`ai/workflows/review_workflow.py`) |
| `document_chunks.qdrant_point_id` unique | `ai.document_chunks` | two bookkeeping rows for one vector in Qdrant |
| `research_tags.name` unique + `ON CONFLICT` (names trimmed and lowercased first) | `core.research_tags` | two notes tagged with the same new tag at the same time; "AI" and "ai" becoming two tags |
| `two_factor_secrets.user_id` + `ON CONFLICT` | `auth.two_factor_secrets` | a double click on "set up 2FA" (the upsert never overwrites an enabled secret) |

The rule behind these: a "check first, then insert" in Python is not
safe under concurrency, because two requests can both pass the check.
The unique constraint is what really stops the duplicate; the Python
check only gives a nicer answer in the common case.

`ON DELETE CASCADE` is used inside `core`: deleting a portfolio deletes
its holdings, transactions and snapshots in the same statement.

### 3.7 Important tables per schema

This is a short map. The full column lists are in `../ERD.md`.

**`auth` (auth-service)**

| Table | Holds |
|---|---|
| `users` | email, username, password hash, `system_role`, active flag, soft-delete time |
| `user_profiles`, `user_settings` | one row per user; `user_settings.notification_preferences` is JSONB |
| `user_sessions` | refresh token hashes, revocation time and `revoked_reason` |
| `oauth_accounts`, `two_factor_secrets` | external logins and 2FA secrets |
| `organizations`, `organization_members`, `organization_invitations` | multi-tenant organizations (member roles are the column `organization_members.role`) |
| `connections` | friend requests between users, with the generated `pair_key` |

**`core` (core-service)**

| Table | Holds |
|---|---|
| `portfolios` | owner, organization, name, currency, `auto_analyze_at_open` |
| `transactions` | the ledger: buy, sell, dividend, deposit, withdrawal, stock split, adjustment; cash rows use ticker `CASH` |
| `holdings` | current quantity and average cost per ticker, kept equal to a replay of the ledger |
| `portfolio_snapshots` | one value per portfolio per day (written at market close) |
| `watchlists`, `watchlist_items` | ticker lists |
| `documents` | uploaded research files: object storage key in `storage_url`, ingestion `status` |
| `research_notes`, `research_comments`, `research_tags`, `note_tags` | collaborative notes; `research_notes.yjs_state` holds the Yjs document as bytes |
| `notifications` | user notifications |
| `api_keys` | hashed public API keys and their per-key rate limit |
| `event_outbox` | durable events (`document.uploaded`, `document.deleted`, `portfolio.deleted`) written in the same transaction as the change they announce, until they are published to Redis; `published_at` is NULL while waiting, and the partial index `ix_event_outbox_unpublished` (on `created_at`, `WHERE published_at IS NULL`) finds those rows. Published rows are deleted after 7 days (`services/core-service/outbox.py`, chapter 24) |

**`ai` (ai-service)**

| Group | Tables |
|---|---|
| Analysis pipeline | `analyses`, `recommendations` (with nullable `judge_leaning`, `judge_confidence`: the Judge's own final-round verdict before code adjustments, so the next run compares raw with raw; NULL on older rows), `recommendation_tiers`, `reflections`, `agents`, `agent_runs`, `agent_results`, `debate_sessions`, `debate_arguments` |
| RAG bookkeeping | `document_chunks` (chunk -> Qdrant point id), `embedding_jobs` (status `embedding`, `ready` or `failed` per document). Both are written by RAG ingestion and archival and removed with the document; no service code reads them. They are diagnostics for operators' SQL and for the orphan sweep of `scripts/delete_analyses.py`; the document status the user sees travels to core-service as `document.status_changed` |
| Memory and knowledge | `episodic_memories`, `agent_memories`, `agent_reports`, `kg_entities`, `kg_relations` (with nullable `kind`, `stable` or `event`, NULL counts as stable, and `as_of`, the date an event fact holds as of; used to let event facts expire, chapter 18), `ticker_names` |
| Decisions | `investment_decisions`, `decision_outcomes`, `decision_reviews` |
| Backtests | `backtests`, `backtest_metrics`, `benchmarks`, `benchmark_results` |
| Analytics | `recommendation_metrics`, `agent_metrics` |
| Market data cache | `instruments`, `price_bars` |
| Filings | `filing_records`: one row per SEC filing document (`ticker`, `filename` unique, `form`, `filing_date`, `report_date`, `records` JSONB with the structured records of the filing, `created_at`); index `ix_filing_records_ticker_filing_date`. Keyed by filename, not by document id, so a document delete does not remove it; ingesting the same filing again overwrites it (upsert by `filename`). Rebuildable with `scripts/backfill_filing_records.py` (chapter 18) |

How an analysis is stored: one `analyses` row (ticker, status,
`failure_reason`), one or more `recommendations` rows for it
(`recommendation`, `confidence`, `reasoning`, `window_expires_at`,
`accepted_at`, `debate_converged`), and for each recommendation zero or
more `recommendation_tiers` (`side`, `target_price`, `quantity`, `status`,
`triggered_at`, `triggered_price`). Tier statuses are defined in
`shared/enums.py::TierStatus` (for example
`pending_acceptance`, `accepted_awaiting_trigger`, `triggered`, `expired`,
`superseded`). The pipeline itself is explained in
[16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md).

### 3.8 JSONB, TSVECTOR and generated columns

**JSONB columns** (flexible structured data that does not need its own
table):

- `ai.agent_results.output`, `ai.reflections.issues` and `retry_agents`,
  `ai.debate_arguments.citations`, `ai.agent_memories.content`,
  `ai.filing_records.records`
- `auth.user_settings.notification_preferences`
- `core.event_outbox.payload`

**Full-text search** on research notes
(`services/core-service/domains/research/model.py::ResearchNote`):

```python
search_vector = mapped_column(
    TSVECTOR,
    Computed("to_tsvector('english', title || ' ' || coalesce(content, ''))",
             persisted=True),
)
# + Index("ix_research_notes_search_vector", "search_vector", postgresql_using="gin")
```

PostgreSQL recomputes `search_vector` whenever the title or content
changes. The repository searches with
`search_vector @@ plainto_tsquery('english', q)` and ranks with
`ts_rank` (`services/core-service/domains/research/repository.py`).

**Order-independent pair key** on friend connections
(`services/auth-service/domains/social/model.py::Connection`):

```
pair_key = LEAST(requester_id, addressee_id)::text || ':' ||
           GREATEST(requester_id, addressee_id)::text     (generated, stored)
```

(A, B) and (B, A) produce the same `pair_key`, and the unique constraint
`uq_connection_pair_key` rejects the second row. A plain "does a
connection exist?" query cannot stop two people requesting each other at
the same instant; the constraint can.

### 3.9 Healthcheck: `pg_isready -h 127.0.0.1`

`docker-compose.yaml`:

```
test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $$POSTGRES_USER -d $$POSTGRES_DB"]
interval: 5s, timeout: 5s, retries: 10
```

`pg_isready` asks the server "are you accepting connections?". Without
`-h`, it connects through the Unix socket. During the very first start,
the official image runs a **temporary** server that listens only on the
socket while it executes `/docker-entrypoint-initdb.d/` scripts. A
socket check therefore reports "healthy" during initialisation, and
the services would start against a server that is about to shut down and
restart. With `-h 127.0.0.1` the check uses TCP, which the temporary
server does not listen on, so "healthy" means the real server is up.
auth-service, core-service, ai-service, postgres-exporter, the scheduler
and Adminer all wait for `service_healthy`.

Each service's own `/health` endpoint also runs `SELECT 1`
(`check_database_connection`) and returns 503 if the database is not
reachable.

### 3.10 Backups

`infra/scheduler/backup.sh` runs from the scheduler's crontab every day at
02:30 New York time (07:30 in London most of the year):

1. `pg_dump -Fc` of the whole `alphamind` database (all three schemas)
   into `postgres.dump`. `-Fc` is PostgreSQL's custom archive format:
   compressed, and restorable table by table.
2. `pg_restore -l postgres.dump` reads the archive's table of contents,
   to prove the dump is complete and readable.
3. For each schema it reads `SELECT version_num FROM "<schema>".alembic_version`
   and records the three revisions in `manifest.json`
   (`postgres.alembic_versions`). A restore can then tell which code
   version the data belongs to.
4. Qdrant snapshots, the object storage tarball and avatars follow (see
   [21-qdrant.md](21-qdrant.md) and
   [22-object-storage.md](22-object-storage.md)), then `SHA256SUMS` and
   `manifest.json`.

The run is built in `/backups/.incomplete-<stamp>` and renamed to
`/backups/<YYYY-MM-DD_HHMM>` only when every step succeeded; a failed run
stays as `<stamp>-FAILED`. Backups older than `BACKUP_RETENTION_DAYS`
(default 7) are deleted only after a successful run, and the newest three
are always kept.

A restore (`infra/scheduler/restore.sh`, `make restore BACKUP=<name>`
for a dry run, `CONFIRM=yes` for the real one) drops the three schemas
and recreates them from the dump **in one transaction**, so an error
changes nothing. This is destructive. `make backup-check` runs only the
restore dry run. The full procedure is in `../DISASTER_RECOVERY.md`.

### 3.11 Metrics: postgres-exporter

The compose service `postgres-exporter`
(`prometheuscommunity/postgres-exporter:v0.17.1`) connects with the same
credentials as the services and exposes metrics on port 9187 inside the
Docker network. Prometheus scrapes it as job `postgres`
(`infra/prometheus/prometheus.yml`).

Used by the alert rules (`infra/prometheus/alert_rules.yml`):

- `PostgresDown`: `pg_up == 0` for 1 minute (critical). It means the
  exporter runs but cannot reach PostgreSQL.

Used by the Grafana dashboard "AlphaMind - Infrastructure & AI pipeline"
(`infra/grafana/provisioning/dashboards/infrastructure-ai-pipeline.json`):

| Panel | Expression |
|---|---|
| PostgreSQL up | `pg_up` |
| Connections | `sum(pg_stat_database_numbackends)` (compare with `max(pg_settings_max_connections)`) |
| Transactions per second | commits `sum(rate(pg_stat_database_xact_commit{datname!~"template.*"}[5m]))` and rollbacks (`pg_stat_database_xact_rollback`, same form) |
| Database size | `pg_database_size_bytes{datname!~"template.*\|postgres"}` |
| Deadlocks (per 5m) | `sum(increase(pg_stat_database_deadlocks[5m]))` |
| Cache hit ratio | blocks found in memory / all blocks read |

See [27-observability.md](27-observability.md).

### 3.12 Adminer and psql

- `make psql` / `bash scripts/psql.sh` opens `psql` inside the postgres
  container, as the container's own `POSTGRES_USER` on `POSTGRES_DB`.
  With arguments (for example `-c "SELECT ..."`) it passes them to `psql`
  unchanged. The script never reads `.env.docker`; it asks the container
  for the user and database names.
- `make adminer` starts Adminer (`adminer:4.8.1`, compose profile
  `adminer`, never started by `up`) on `http://127.0.0.1:8080` and prints
  a login link:
  `http://127.0.0.1:8080/?pgsql=postgres&username=postgres&db=alphamind&ns=core`.
  The link selects System "PostgreSQL", Server `postgres`, the user, the
  database and the schema, so you only type the `POSTGRES_PASSWORD` from
  `.env.docker`. Use the link: the plain form defaults to System "MySQL",
  which tries port 3306 and fails with "Connection refused".
- The tables are in the schemas `auth`, `core` and `ai`. Adminer opens
  the `public` schema by default, and `public` is empty, so it looks as
  if the database had no tables. Pick a schema in the "Schema" list at
  the top left, or change `ns=` in the link.
- Adminer is bound to 127.0.0.1, so other machines cannot reach it.
  **Adminer can change data.** Use its "SQL command" page for `SELECT`s
  only. `make adminer-stop` stops it.

---

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `POSTGRES_HOST` | `postgres` | database host | `shared/config.py` |
| `POSTGRES_PORT` | `5432` | database port | `shared/config.py` |
| `POSTGRES_DB` | `alphamind` | database name | `shared/config.py` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `postgres` / `postgres` (set real values in `.env.docker`) | credentials, shared by all services and the image | `shared/config.py`, `.env.example` |
| `POSTGRES_SCHEMA` | `public` (base); `auth` / `core` / `ai` per service | the service's schema, first on `search_path`, and where its `alembic_version` lives | `shared/config.py`, `services/*/config.py` |
| `DATABASE_URL` | unset | overrides the assembled async URL (`postgresql+asyncpg://...`) | `shared/config.py` |
| `SYNC_DATABASE_URL` | unset | overrides the sync URL Alembic uses (`postgresql+psycopg://...`) | `shared/config.py` |
| `DB_POOL_SIZE` | `20` | pooled connections per service process | `shared/config.py` |
| `DB_MAX_OVERFLOW` | `10` | extra connections above the pool when busy | `shared/config.py` |
| `DB_ECHO` | `False` | log every SQL statement (debugging only) | `shared/config.py` |
| `ANALYSIS_QUEUE_MAX_PENDING` | `50` | global cap on pending analyses (advisory-locked count) | `services/ai-service/config.py` |
| `ANALYSIS_MAX_ACTIVE_PER_USER` | `3` | per-user cap on pending/running analyses (public create) | `services/ai-service/config.py` |
| `BACKUP_RETENTION_DAYS` | `7` | how long daily backups are kept (newest 3 always kept) | `infra/scheduler/backup.sh`, `.env.example` |
| healthcheck | `pg_isready -h 127.0.0.1`, every 5 s, 10 retries | readiness of the server | `docker-compose.yaml` |
| image | `postgres:16-alpine` | server version | `docker-compose.yaml` |

---

## 5. Design decisions and why

**One database with three schemas, not three databases.**
Alternative: one PostgreSQL server per service. Reason: one server is
much simpler to run, back up (`pg_dump` once) and monitor on one machine,
and schemas still give each service a clear boundary. Because there are no
cross-schema foreign keys, a schema can later be moved to its own server
with a dump and restore.

**Plain UUIDs across schemas.**
Alternative: real foreign keys. Reason: keeps services independent
(separate migrations, separate restores). The consistency that foreign
keys would give is provided by events (`portfolio.deleted`,
`document.deleted`) instead. The rule is written in `../ERD.md`; a new
model must not declare a foreign key into another service's schema.

**`search_path` through asyncpg `server_settings`.**
Alternative: a `SET search_path` in a connection event. Reason: the event
approach can leave some pooled connections with the default path, and the
tables then seem to vanish. The handshake setting is in place before any
query.

**Commit before the response (`scope="function"`).**
Alternative: FastAPI's default request scope. Reason: with the default,
a client can see success for a write that failed to commit, and events
in background tasks are published before the data exists.

**Alembic alone manages the schema, at every startup.**
Alternative: `create_all` for new databases plus `alembic stamp head`.
Reason: with that, the migration chain never builds a new database, so
migrations are never tested on a fresh install; and `create_all` hides
mistakes in the models, for example a timestamp column declared with
`server_default="now()"` as a quoted string, which PostgreSQL stores as
the constant moment of `CREATE TABLE`, so every new row gets the same old
timestamp. Each service has one real baseline migration that builds its
whole schema, and tests compare the migrated schema with the models,
including server defaults.

**Refuse instead of guess.**
`bootstrap_schema` refuses a schema with tables but no revision.
Migrations refuse when the data would break
the change in a way nobody decided (`f1170a000001` refuses to drop a
table that holds rows; `f1170c000001` refuses to build unique indexes
over duplicates). A drop of data the owner decided to discard is explicit:
`f1210a000001` (ai) and `f1210b000001` (auth) drop their tables with the
rows, and their downgrades recreate the tables empty. A service that does
not start with a clear message is better than one that silently changes
data.

**Locks and constraints instead of "check in Python".**
Real races of this kind exist: a scheduled and a manual snapshot for the
same day crash the second insert; two concurrent cash outflows both pass
the balance check; a retried tier fill is recorded twice; two notes
tagged with the same new tag at once crash the second insert. Each is
prevented with a row lock, an advisory lock, a unique constraint or an
upsert, as listed in section 3.6.

**Holdings are rebuilt from the ledger.**
`_sync_holding_with_ledger` in `services/core-service/domains/portfolio/service.py`
replays the ticker's transactions in date order after every
share-moving write. Applying a transaction dated in the past (a
forgotten buy, a late split) on top of the current holding leaves
quantity and average cost different from the ledger. Example: buy 10 at
100 (January), split 2:1 (June), then a forgotten buy of 5 at 120 dated
March gives 25 shares at 64 on top of the current state, but 30 at 53.33
by replay (the correct answer).

**`pg_isready` over TCP.** Explained in section 3.9: the socket check is
healthy too early on a first start.

---

## 6. When things go wrong

| Symptom | Likely cause | What the system does | How to diagnose |
|---|---|---|---|
| A service does not become healthy after start | migration failed or bootstrap refused | lifespan raises, service exits/restarts | `make service-logs S=core-service` (or `bash scripts/logs.sh core-service`); look for `SchemaBootstrapError` or an Alembic traceback; `schema_bootstrap_complete` is the success line |
| Alembic "Can't locate revision" | database restored from a backup taken before round 116 | service does not start | restore a backup made by the current code |
| "schema 'x' has N table(s) but no Alembic revision" | tables were created by hand or `alembic_version` was lost | refuses to start | restore a backup, or inspect by hand; the service will not guess |
| `relation "..." does not exist` | wrong `search_path`, or a table missing | request fails with 500 | `bash scripts/psql.sh -c "\dt core.*"`; check `POSTGRES_SCHEMA` is not set in the shared env file |
| All requests slow, `/health` 503 with `"database": false` | Postgres down or out of connections | `pool_pre_ping` reconnects when it comes back; alert `PostgresDown` fires after 1 min | `make health`; Grafana panel "Connections" vs max connections |
| 422 "Insufficient cash to buy ..." (or to withdraw) | a BUY or WITHDRAWAL exceeds derived cash; the log event is `transaction_rejected_insufficient_cash` | rejected before any write | record a DEPOSIT first; see [12-core-service.md](12-core-service.md) |
| 409 on an idempotency key | the key was already used on another portfolio | the original transaction is not changed | `bash scripts/psql.sh -c "SELECT portfolio_id FROM core.transactions WHERE idempotency_key = 'tier:<id>';"` |
| 503 "The analysis queue is full" / 429 on analysis create | queue or per-user cap reached | rejected under the advisory lock | `bash scripts/psql.sh -c "SELECT status, count(*) FROM ai.analyses GROUP BY status;"` |
| Deadlocks panel above zero | two transactions locked in opposite order | PostgreSQL aborts one of them (it gets an error) | check the service logs at that time; the code's lock order is portfolio -> holding |
| Backup run left `<stamp>-FAILED` | a step failed (dump, snapshot, tar) | older backups are not pruned; script exits 1; heartbeat not sent | scheduler log lines `backup: ERROR: ...`; `make backups` |

---

## 7. Demo walkthrough

All commands are read-only. Run them from the repository root with the
stack up.

1. **Show the database is healthy.**
   ```
   docker compose --env-file .env.docker ps postgres
   ```
   Expected: `postgres` is `healthy`. Say: "The healthcheck is a TCP
   `pg_isready`, so 'healthy' means the real server accepts connections,
   not the temporary init server."

2. **List the schemas.**
   ```
   bash scripts/psql.sh -c "\dn"
   ```
   Expected: `ai`, `auth`, `core`, `public`. Say: "One database, one
   schema per service."

3. **List the tables of each schema.**
   ```
   bash scripts/psql.sh -c "\dt auth.*"
   bash scripts/psql.sh -c "\dt core.*"
   bash scripts/psql.sh -c "\dt ai.*"
   bash scripts/psql.sh -c "SELECT table_schema, count(*) AS tables FROM information_schema.tables WHERE table_schema IN ('auth','core','ai') AND table_type = 'BASE TABLE' GROUP BY table_schema ORDER BY 1;"
   ```
   Point out `alembic_version` in each list.

4. **Show the Alembic version tables.**
   ```
   bash scripts/psql.sh -c "SELECT 'auth' AS schema, version_num FROM auth.alembic_version UNION ALL SELECT 'core', version_num FROM core.alembic_version UNION ALL SELECT 'ai', version_num FROM ai.alembic_version;"
   ```
   Expected at the current code: `auth f1210b000001`, `core f1250c000001`,
   `ai f1250a000001`. Say: "Each service records its own migration
   revision inside its own schema, and runs `alembic upgrade head` at
   every start."
   Optional: show the startup log line:
   ```
   bash scripts/logs.sh core-service grep schema_bootstrap_complete
   ```
   This searches today's saved log file (`logs/core-service/<date>.log`,
   New York date); add a date (`YYYY-MM-DD`) as a last argument for
   another day. The line is written once per start, so search the day the
   service last started. If there is no log file for that day, the script
   stops with "no log file".

5. **Show a portfolio with its holdings and transactions.** The scripts
   use the development portfolio `d72855ea-2776-41c3-bc77-ce1a4c1435d5`
   by default (`scripts/lib.sh`); use your own id if it differs.
   ```
   bash scripts/psql.sh -c "SELECT id, name, currency, owner_id, auto_analyze_at_open FROM core.portfolios ORDER BY created_at LIMIT 5;"
   bash scripts/psql.sh -c "SELECT ticker, quantity, average_cost FROM core.holdings WHERE portfolio_id = 'd72855ea-2776-41c3-bc77-ce1a4c1435d5' ORDER BY ticker;"
   bash scripts/psql.sh -c "SELECT transaction_date::date, transaction_type, ticker, quantity, price, idempotency_key FROM core.transactions WHERE portfolio_id = 'd72855ea-2776-41c3-bc77-ce1a4c1435d5' ORDER BY transaction_date;"
   ```
   Say: "Transactions are the ledger. Holdings are kept equal to a replay
   of that ledger. Cash is not a holding; it is derived from the `CASH`
   deposit and withdrawal rows and the trades. A transaction with
   `idempotency_key` `tier:<id>` was recorded automatically by tier
   monitoring."

6. **Show an analysis with its recommendation and tiers.**
   ```
   bash scripts/psql.sh -c "SELECT id, ticker, status, failure_reason, created_at FROM ai.analyses ORDER BY created_at DESC LIMIT 5;"
   ```
   Copy one completed analysis id, then:
   ```
   bash scripts/psql.sh -c "SELECT r.id, r.recommendation, r.confidence, r.debate_converged, r.window_expires_at, r.accepted_at FROM ai.recommendations r WHERE r.analysis_id = '<analysis id>';"
   bash scripts/psql.sh -c "SELECT t.side, t.target_price, t.quantity, t.status, t.triggered_at, t.triggered_price FROM ai.recommendation_tiers t JOIN ai.recommendations r ON r.id = t.recommendation_id WHERE r.analysis_id = '<analysis id>' ORDER BY t.side, t.target_price;"
   ```
   Say: "Inside the `ai` schema these are real foreign keys. But
   `ai.analyses.portfolio_id` points into `core` and is only a plain
   UUID."

7. **Show a generated column and full-text search.**
   ```
   bash scripts/psql.sh -c "SELECT title, left(search_vector::text, 80) FROM core.research_notes LIMIT 3;"
   bash scripts/psql.sh -c "\d core.research_notes"
   ```
   Point out `search_vector ... generated always as (...) stored` and the
   GIN index.

8. **Adminer.**
   ```
   make adminer
   ```
   Open the link `make adminer` prints
   (`http://127.0.0.1:8080/?pgsql=postgres&username=postgres&db=alphamind&ns=core`)
   and type the password. Schema `core` is already selected; open
   `portfolios`, then `holdings`. Switch the "Schema" list to `ai` and
   open `analyses` to show that each service has its own schema. Show the "SQL command" page with one of
   the SELECTs above. Say: "Adminer can modify data, so I only use it for
   reading." Afterwards:
   ```
   make adminer-stop
   ```

9. **Show backups (read-only).**
   ```
   make backups
   ```
   Then open the newest `backups/<stamp>/manifest.json` and show
   `postgres.alembic_versions`.

---

## 8. Check your understanding

<details>
<summary>1. Why does `ai.analyses.user_id` have no foreign key to `auth.users`?</summary>

Because it crosses schemas. Each service owns its schema and must stay
migratable and restorable on its own. Cross-schema references are plain
UUID columns; consistency comes from events.
</details>

<details>
<summary>2. What does `scope="function"` on `Depends(get_db, ...)` change?</summary>

The dependency's teardown, which commits the session, runs when the
handler returns, before the response is sent and before background
tasks. So clients never see success for an uncommitted write, and events
published in background tasks see committed rows.
</details>

<details>
<summary>3. Where does Alembic store the current revision, and why there?</summary>

In `<schema>.alembic_version` (`version_table_schema=POSTGRES_SCHEMA` in
each `migrations/env.py`). Three independent version tables let the
three services migrate at the same time without interfering.
</details>

<details>
<summary>4. What happens at startup if a schema has tables but no `alembic_version`?</summary>

`bootstrap_schema` raises `SchemaBootstrapError` and the service does not
start. Nothing can tell which state those tables are in, so it refuses to
guess.
</details>

<details>
<summary>5. Two BUY requests arrive at the same time and each alone fits the cash balance. What prevents both from succeeding?</summary>

`_ensure_cash_stays_non_negative` locks the portfolio row with
`SELECT ... FOR UPDATE` before reading the ledger. The second request
waits until the first commits, then sees the first purchase and is
rejected if the cash is no longer enough.
</details>

<details>
<summary>6. Why is an advisory lock used for analysis admission instead of a row lock?</summary>

There is no single row that represents "the queue". The check counts
rows and then inserts a new one. `pg_advisory_xact_lock(1120015)`
serialises that count-then-insert across concurrent requests and is
released automatically at commit or rollback.
</details>

<details>
<summary>7. Why does the Postgres healthcheck use `-h 127.0.0.1`?</summary>

Without it `pg_isready` uses the Unix socket, which the image's temporary
init server answers during first-time initialisation, so the container
would be marked healthy too early. TCP is only served by the real server.
</details>

<details>
<summary>8. What makes friend requests A->B and B->A at the same instant safe?</summary>

The generated column `pair_key` (LEAST id : GREATEST id) is the same for
both directions, and the unique constraint `uq_connection_pair_key`
rejects the second insert.
</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `infra/postgres/init-schemas.sql` | first-start script: `pgcrypto`, schemas `auth`, `core`, `ai` |
| `docker-compose.yaml` (`postgres`, `postgres-exporter`, `adminer`) | image, volume, healthcheck, exporter, Adminer profile |
| `shared/config.py` | `POSTGRES_*`, `DB_*` settings, URL assembly |
| `shared/database.py` | `Base` + naming convention, `UUIDPKMixin`, `TimestampColumnsMixin`, engine factory (`search_path`), session factory, `make_get_db`, `count_query`, `check_database_connection`, `bootstrap_schema`, `schema_differences` |
| `services/*/database.py` | the service's `engine`, `SessionLocal`, `get_db`, `ensure_schema`, `register_models` |
| `services/*/alembic.ini`, `services/*/migrations/env.py` | Alembic configuration per service |
| `services/*/migrations/versions/` | the active migration chain |
| `tests/alembic_contract.py`, `tests/auth-service/test_platform.py`, `tests/core-service/test_platform.py`, `tests/ai-service/test_api.py` | the migration contract every schema meets |
| `services/*/domains/*/model.py`, `services/core-service/apikeys.py` | table definitions |
| `services/core-service/domains/portfolio/service.py` | portfolio/holding locks, idempotency keys, ledger replay |
| `services/ai-service/domains/analysis/queue.py` | admission with an advisory lock |
| `services/ai-service/ai/workflows/tier_monitoring_workflow.py` | per-tier row lock and per-tier commit |
| `infra/scheduler/backup.sh`, `infra/scheduler/restore.sh` | `pg_dump` backups and restores |
| `infra/prometheus/alert_rules.yml` | `PostgresDown` |
| `infra/grafana/provisioning/dashboards/infrastructure-ai-pipeline.json` | PostgreSQL panels |
| `scripts/psql.sh` | psql in the container |

---

## 10. Related chapters

- [01-architecture.md](01-architecture.md): where PostgreSQL sits in the system
- [02-operations.md](02-operations.md): starting, stopping, rebuilding
- [11-auth-service.md](11-auth-service.md), [12-core-service.md](12-core-service.md), [13-ai-service.md](13-ai-service.md): what each service stores
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md): analyses, recommendations and tiers
- [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md): tier monitoring and its locks
- [21-qdrant.md](21-qdrant.md): vectors that `ai.document_chunks` points to
- [24-events.md](24-events.md): how cross-schema consistency is kept with events
- [26-scheduler-jobs.md](26-scheduler-jobs.md): the backup job
- [27-observability.md](27-observability.md): exporters, alerts, dashboards
- [30-shared-library.md](30-shared-library.md): `shared/database.py` and friends
- [31-testing-quality.md](31-testing-quality.md): schema tests
- [../ERD.md](../ERD.md): every table and column
- [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md): backups, restores and the schema at startup
