# AlphaMind v4.0
# Entity Relationship Design (ERD)

Version: 4.0 

---

## 2. Database Strategy

**One Postgres instance, one schema per service** (`auth`, `core`,
`ai`), not three separate Postgres instances. This is a deliberate,
documented tradeoff for a 3-person, 6-week project:

- Each service's `shared`-based engine sets its own
  `search_path` to its own schema on every connection (see
  `shared/database.py`) — from
  application code's point of view, it's indistinguishable from having
  its own database.
- No service can query another service's schema (enforced by
  convention + code review, not by Postgres role permissions in the
  baseline — see the stretch hardening note in
  `infra/postgres/init-schemas.sql`).
- Running 3 separate Postgres instances would triple backup/monitoring/
  connection-pooling work for no functional benefit at this scale — a
  real justification is needed to pay that cost, and "more standard"
  isn't one for a 6-week student project. If the project continues past
  graduation and a service needs genuinely independent scaling of its
  data tier, splitting a schema into its own instance is a contained
  migration (`pg_dump --schema=ai` + restore elsewhere), not a rewrite.

---

## 3. Cross-Service Reference Rule

No table in one service's schema may declare a `FOREIGN KEY` into
another service's schema. Where one service's data conceptually
references another's (e.g. a `core.portfolios` row has an owner, who is
a `auth.users` row), the column is a plain `UUID`, and:

- **Reads** that need more than the ID (e.g. displaying the owner's
  username) go through that service's REST client
  (`app/clients/auth_client.py`) and a shared
  `shared.schemas.refs` model — see
  `services/*/app/clients/*.py`.
- **Referential integrity** (not letting a user be deleted while they
  still own portfolios) is enforced at the application layer, not the
  database layer — auth-service would need to check with core-service
  (or accept eventual-consistency cleanup via an event) before allowing
  a hard delete. This is a real, known tradeoff of microservices,
  not an oversight.

---

## PART 1 — `auth` schema (auth-service, Backend Lead)

### auth.users
```sql
id                UUID PK
email             VARCHAR UNIQUE NOT NULL
username          VARCHAR UNIQUE NOT NULL
password_hash     TEXT NOT NULL
system_role       VARCHAR NOT NULL DEFAULT 'user'   -- admin | user (U4 RBAC)
is_active         BOOLEAN NOT NULL DEFAULT TRUE
is_verified       BOOLEAN NOT NULL DEFAULT FALSE
last_seen_at      TIMESTAMPTZ                        -- online-status (U1)
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### auth.user_profiles
```sql
id            UUID PK
user_id       UUID FK -> auth.users.id UNIQUE        -- same-service FK, OK
first_name    VARCHAR
last_name     VARCHAR
avatar_url    TEXT
bio           TEXT
timezone      VARCHAR
country       VARCHAR
```

### auth.user_settings
```sql
id                          UUID PK
user_id                     UUID FK -> auth.users.id UNIQUE
preferred_currency          VARCHAR DEFAULT 'USD'
theme                       VARCHAR DEFAULT 'system'
default_model               VARCHAR
notification_preferences    JSONB
```

### auth.user_sessions
```sql
id             UUID PK
user_id        UUID FK -> auth.users.id
refresh_token  TEXT NOT NULL
expires_at     TIMESTAMPTZ NOT NULL
ip_address     VARCHAR
device_info    TEXT
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### auth.oauth_accounts
```sql
id                 UUID PK
user_id            UUID FK -> auth.users.id
provider           VARCHAR NOT NULL   -- google | github
provider_user_id   VARCHAR NOT NULL
UNIQUE (provider, provider_user_id)
```

### auth.two_factor_secrets
```sql
id         UUID PK
user_id    UUID FK -> auth.users.id UNIQUE
secret     TEXT NOT NULL
enabled    BOOLEAN NOT NULL DEFAULT FALSE
```

### auth.connections
```sql
id               UUID PK
requester_id     UUID FK -> auth.users.id
addressee_id     UUID FK -> auth.users.id
status           VARCHAR NOT NULL DEFAULT 'pending'  -- pending | accepted | blocked
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
responded_at     TIMESTAMPTZ
UNIQUE (requester_id, addressee_id)
```

### auth.organizations
```sql
id            UUID PK
name          VARCHAR NOT NULL
description   TEXT
owner_id      UUID FK -> auth.users.id
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### auth.organization_members
```sql
id                 UUID PK
organization_id    UUID FK -> auth.organizations.id
user_id            UUID FK -> auth.users.id
role               VARCHAR NOT NULL DEFAULT 'member'  -- owner|admin|member|viewer
joined_at          TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (organization_id, user_id)
```

### auth.organization_roles
```sql
id                 UUID PK
organization_id    UUID FK -> auth.organizations.id
role_name          VARCHAR NOT NULL
permissions        JSONB NOT NULL
```

### auth.organization_invitations
```sql
id                 UUID PK
organization_id    UUID FK -> auth.organizations.id
email              VARCHAR NOT NULL
role               VARCHAR NOT NULL DEFAULT 'member'
invited_by         UUID FK -> auth.users.id
expires_at         TIMESTAMPTZ NOT NULL
accepted_at        TIMESTAMPTZ
```

*(All foreign keys above are same-service — `auth-service` is the only
service that ever writes to this schema, so ordinary FKs are correct
and safe here.)*

---

## PART 2 — `core` schema (core-service, Backend Lead)

### core.portfolios
```sql
id                UUID PK
owner_id          UUID NOT NULL          -- plain UUID: references auth.users.id,
                                          --   resolved via app/clients/auth_client.py
organization_id   UUID                    -- plain UUID: references auth.organizations.id,
                                          --   NULL = personal portfolio
name              VARCHAR NOT NULL
currency          VARCHAR NOT NULL DEFAULT 'USD'
visibility        VARCHAR NOT NULL DEFAULT 'private'
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.holdings
```sql
id              UUID PK
portfolio_id    UUID FK -> core.portfolios.id   -- same-service FK, OK
ticker          VARCHAR NOT NULL
quantity        NUMERIC NOT NULL
average_cost    NUMERIC NOT NULL
UNIQUE (portfolio_id, ticker)
```

### core.transactions
```sql
id                  UUID PK
portfolio_id        UUID FK -> core.portfolios.id
ticker              VARCHAR NOT NULL
transaction_type    VARCHAR NOT NULL   -- buy|sell|dividend|deposit|withdrawal
quantity            NUMERIC
price               NUMERIC
transaction_date    TIMESTAMPTZ NOT NULL
```

### core.portfolio_snapshots
```sql
id              UUID PK
portfolio_id    UUID FK -> core.portfolios.id
snapshot_date   DATE NOT NULL
total_value     NUMERIC NOT NULL
cash_balance    NUMERIC NOT NULL
UNIQUE (portfolio_id, snapshot_date)
```

### core.watchlists
```sql
id          UUID PK
owner_id    UUID NOT NULL   -- plain UUID: references auth.users.id
name        VARCHAR NOT NULL
```

### core.watchlist_items
```sql
id             UUID PK
watchlist_id   UUID FK -> core.watchlists.id
ticker         VARCHAR NOT NULL
UNIQUE (watchlist_id, ticker)
```

### core.research_notes
```sql
id                 UUID PK
author_id          UUID NOT NULL   -- plain UUID: references auth.users.id
organization_id    UUID            -- plain UUID: references auth.organizations.id
title              VARCHAR NOT NULL
content            TEXT
created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.research_comments
```sql
id           UUID PK
note_id      UUID FK -> core.research_notes.id
author_id    UUID NOT NULL   -- plain UUID: references auth.users.id
content      TEXT NOT NULL
created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.research_tags
```sql
id     UUID PK
name   VARCHAR UNIQUE NOT NULL
```

### core.note_tags
```sql
note_id   UUID FK -> core.research_notes.id
tag_id    UUID FK -> core.research_tags.id
PRIMARY KEY (note_id, tag_id)
```

### core.documents
```sql
id                 UUID PK
owner_id           UUID NOT NULL   -- plain UUID: references auth.users.id
organization_id    UUID            -- plain UUID: references auth.organizations.id
filename           VARCHAR NOT NULL
document_type      VARCHAR NOT NULL      -- pdf|txt|md
storage_url        TEXT NOT NULL
status             VARCHAR NOT NULL DEFAULT 'uploaded'
                     -- uploaded|parsing|chunking|embedding|ready|failed
                     -- core-service OWNS this field but only ever
                     -- writes 'uploaded' itself; every later transition
                     -- is written here in response to ai-service's
                     -- `document.status_changed` event, never by a
                     -- direct cross-service write to this table.
uploaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.notifications
```sql
id         UUID PK
user_id    UUID NOT NULL   -- plain UUID: references auth.users.id
type       VARCHAR NOT NULL
title      VARCHAR NOT NULL
content    TEXT
read_at    TIMESTAMPTZ
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.websocket_connections
```sql
id               UUID PK
user_id          UUID NOT NULL   -- plain UUID: references auth.users.id
connection_id    VARCHAR NOT NULL
connected_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.api_keys — Public API module (W4)
```sql
id                     UUID PK
owner_user_id          UUID          -- plain UUID: references auth.users.id
owner_org_id           UUID          -- plain UUID: references auth.organizations.id
name                   VARCHAR NOT NULL
key_hash               TEXT NOT NULL UNIQUE
scopes                 JSONB
rate_limit_per_minute  INTEGER NOT NULL DEFAULT 60
last_used_at           TIMESTAMPTZ
revoked_at             TIMESTAMPTZ
created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
CHECK (owner_user_id IS NOT NULL OR owner_org_id IS NOT NULL)
```
*(Deliberately stored in `core`, not `auth`, even though it represents
an identity/credential concern — core-service is what actually
validates it on every public API request, and an extra network hop to
auth-service on every request would defeat the point of the rate
limiter being fast. A short-lived cache of the key's validity is enough
protection against staleness for a 6-week project.)*

### core.audit_logs
```sql
id             UUID PK
actor_id       UUID NOT NULL   -- plain UUID: references auth.users.id
action         VARCHAR NOT NULL
entity_type    VARCHAR NOT NULL
entity_id      UUID
metadata       JSONB
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### core.service_health
```sql
id               UUID PK
component_name   VARCHAR NOT NULL   -- core-service | postgres | redis
status           VARCHAR NOT NULL   -- healthy | degraded | down
last_check       TIMESTAMPTZ NOT NULL DEFAULT now()
```
*(Each service tracks only its own dependency health — ai-service and
auth-service have their own equivalent rows in their own schemas, not
shared here.)*

---

## PART 3 — `ai` schema (ai-service, AI Lead)

### ai.instruments
```sql
id             UUID PK
ticker         VARCHAR UNIQUE NOT NULL
name           VARCHAR
asset_type     VARCHAR NOT NULL
currency       VARCHAR NOT NULL DEFAULT 'USD'
sector         VARCHAR
exchange       VARCHAR
```

### ai.price_bars
```sql
id               UUID PK
instrument_id    UUID FK -> ai.instruments.id
interval         VARCHAR NOT NULL   -- 1d | 1h | 5m
timestamp        TIMESTAMPTZ NOT NULL
open             NUMERIC NOT NULL
high             NUMERIC NOT NULL
low              NUMERIC NOT NULL
close            NUMERIC NOT NULL
volume           NUMERIC NOT NULL
UNIQUE (instrument_id, interval, timestamp)
```

### ai.technical_indicator_cache
```sql
id               UUID PK
instrument_id    UUID FK -> ai.instruments.id
indicator        VARCHAR NOT NULL
computed_at      TIMESTAMPTZ NOT NULL
value            JSONB NOT NULL
```

### ai.rag_collections
```sql
id            UUID PK
name          VARCHAR NOT NULL
description   TEXT
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.document_chunks
```sql
id                 UUID PK
document_id        UUID NOT NULL   -- plain UUID: references core.documents.id
                                    -- (cross-service — see PART 2 note on
                                    -- core.documents.status for the event
                                    -- flow this depends on)
chunk_index        INTEGER NOT NULL
token_count        INTEGER NOT NULL
qdrant_point_id    VARCHAR NOT NULL UNIQUE
```

### ai.embedding_jobs
```sql
id                 UUID PK
document_id        UUID NOT NULL   -- plain UUID: references core.documents.id
embedding_model    VARCHAR NOT NULL
status             VARCHAR NOT NULL
created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.retrieval_logs
```sql
id                UUID PK
analysis_id       UUID FK -> ai.analyses.id
query             TEXT NOT NULL
top_k             INTEGER NOT NULL
retrieved_count   INTEGER NOT NULL
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.analyses
```sql
id            UUID PK
user_id       UUID NOT NULL   -- plain UUID: references auth.users.id
portfolio_id  UUID            -- plain UUID: references core.portfolios.id, nullable
ticker        VARCHAR NOT NULL
status        VARCHAR NOT NULL DEFAULT 'pending'
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.analysis_sources
```sql
id                  UUID PK
analysis_id         UUID FK -> ai.analyses.id
source_type         VARCHAR NOT NULL
source_reference    TEXT NOT NULL
```

### ai.analysis_citations
```sql
id              UUID PK
analysis_id     UUID FK -> ai.analyses.id
document_id     UUID NOT NULL   -- plain UUID: references core.documents.id
chunk_id        UUID FK -> ai.document_chunks.id
citation_text   TEXT NOT NULL
```

### ai.recommendations
```sql
id               UUID PK
analysis_id      UUID FK -> ai.analyses.id
recommendation   VARCHAR NOT NULL   -- buy|hold|sell
confidence       NUMERIC NOT NULL
reasoning        TEXT NOT NULL
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.reflections
```sql
id            UUID PK
analysis_id   UUID FK -> ai.analyses.id
verified      BOOLEAN NOT NULL
issues        JSONB
```

### ai.agents
```sql
id            UUID PK
name          VARCHAR NOT NULL
description   TEXT
version       VARCHAR NOT NULL
active        BOOLEAN NOT NULL DEFAULT TRUE
```

### ai.agent_runs
```sql
id               UUID PK
analysis_id      UUID FK -> ai.analyses.id
agent_id         UUID FK -> ai.agents.id
status           VARCHAR NOT NULL
started_at       TIMESTAMPTZ
completed_at     TIMESTAMPTZ
duration_ms      INTEGER
```

### ai.agent_results
```sql
id             UUID PK
agent_run_id   UUID FK -> ai.agent_runs.id
score          NUMERIC
summary        TEXT
output         JSONB
```

### ai.agent_steps
```sql
id             UUID PK
agent_run_id   UUID FK -> ai.agent_runs.id
step_name      VARCHAR NOT NULL
input_data     JSONB
output_data    JSONB
duration_ms    INTEGER
```

### ai.agent_dependencies
```sql
parent_agent_id   UUID FK -> ai.agents.id
child_agent_id    UUID FK -> ai.agents.id
PRIMARY KEY (parent_agent_id, child_agent_id)
```

### ai.agent_memories
```sql
id            UUID PK
agent_id      UUID FK -> ai.agents.id
memory_type   VARCHAR NOT NULL
content       TEXT NOT NULL
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.memory_references
```sql
id            UUID PK
memory_id     UUID FK -> ai.agent_memories.id
analysis_id   UUID FK -> ai.analyses.id
```

### ai.episodic_memories
```sql
id            UUID PK
user_id       UUID NOT NULL   -- plain UUID: references auth.users.id
event_type    VARCHAR NOT NULL
content       TEXT
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.prompt_templates
```sql
id            UUID PK
name          VARCHAR NOT NULL
version       VARCHAR NOT NULL
prompt        TEXT NOT NULL
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.prompt_executions
```sql
id             UUID PK
prompt_id      UUID FK -> ai.prompt_templates.id
analysis_id    UUID FK -> ai.analyses.id
model_used     VARCHAR NOT NULL
token_count    INTEGER
```

### ai.prompt_versions
```sql
id            UUID PK
prompt_id     UUID FK -> ai.prompt_templates.id
version       VARCHAR NOT NULL
content       TEXT NOT NULL
```

### ai.debate_sessions
```sql
id             UUID PK
analysis_id    UUID FK -> ai.analyses.id
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.debate_arguments
```sql
id                   UUID PK
debate_session_id    UUID FK -> ai.debate_sessions.id
side                 VARCHAR NOT NULL   -- bull|bear
content              TEXT NOT NULL
```

### ai.investment_decisions
```sql
id                   UUID PK
user_id              UUID NOT NULL   -- plain UUID: references auth.users.id
recommendation_id    UUID FK -> ai.recommendations.id
action_taken         VARCHAR NOT NULL   -- followed|ignored|partial
created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.decision_outcomes
```sql
id                 UUID PK
decision_id        UUID FK -> ai.investment_decisions.id
evaluation_days    INTEGER NOT NULL
strategy_return    NUMERIC
benchmark_return   NUMERIC
alpha              NUMERIC
outperformed       BOOLEAN
evaluated_at       TIMESTAMPTZ
```

### ai.decision_reviews
```sql
id                UUID PK
decision_id       UUID FK -> ai.investment_decisions.id
review_text       TEXT
lessons_learned   TEXT
```

### ai.backtests
```sql
id                  UUID PK
user_id             UUID NOT NULL   -- plain UUID: references auth.users.id
strategy_name       VARCHAR NOT NULL
benchmark_symbol    VARCHAR NOT NULL
start_date          DATE NOT NULL
end_date            DATE NOT NULL
initial_capital     NUMERIC NOT NULL
ending_capital      NUMERIC
strategy_return     NUMERIC
benchmark_return    NUMERIC
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.backtest_positions
```sql
id             UUID PK
backtest_id    UUID FK -> ai.backtests.id
ticker         VARCHAR NOT NULL
quantity       NUMERIC NOT NULL
entry_price    NUMERIC NOT NULL
```

### ai.backtest_trades
```sql
id             UUID PK
backtest_id    UUID FK -> ai.backtests.id
ticker         VARCHAR NOT NULL
side           VARCHAR NOT NULL
quantity       NUMERIC NOT NULL
price          NUMERIC NOT NULL
trade_date     TIMESTAMPTZ NOT NULL
```

### ai.backtest_metrics
```sql
id              UUID PK
backtest_id     UUID FK -> ai.backtests.id
sharpe_ratio    NUMERIC
sortino_ratio   NUMERIC
drawdown        NUMERIC
win_rate        NUMERIC
```

### ai.benchmarks
```sql
id            UUID PK
symbol        VARCHAR UNIQUE NOT NULL
name          VARCHAR NOT NULL
asset_type    VARCHAR NOT NULL
currency      VARCHAR NOT NULL
description   TEXT
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.benchmark_results
```sql
id                    UUID PK
backtest_id           UUID FK -> ai.backtests.id
benchmark_id          UUID FK -> ai.benchmarks.id
strategy_return       NUMERIC
benchmark_return      NUMERIC
alpha                 NUMERIC
beta                  NUMERIC
information_ratio     NUMERIC
tracking_error        NUMERIC
outperformed          BOOLEAN
created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.portfolio_metrics
```sql
id              UUID PK
portfolio_id    UUID NOT NULL   -- plain UUID: references core.portfolios.id
sharpe_ratio    NUMERIC
sortino_ratio   NUMERIC
drawdown        NUMERIC
```

### ai.recommendation_metrics
```sql
id                   UUID PK
recommendation_id    UUID FK -> ai.recommendations.id
accuracy_score       NUMERIC
```

### ai.agent_metrics
```sql
id             UUID PK
agent_id       UUID FK -> ai.agents.id
avg_score      NUMERIC
success_rate   NUMERIC
```

### ai.retrieval_metrics
```sql
id                UUID PK
analysis_id       UUID FK -> ai.analyses.id
precision_score   NUMERIC
recall_score      NUMERIC
```

### ai.portfolio_evaluations
```sql
id                  UUID PK
portfolio_id        UUID NOT NULL   -- plain UUID: references core.portfolios.id
benchmark_symbol    VARCHAR NOT NULL
portfolio_return    NUMERIC
benchmark_return    NUMERIC
alpha               NUMERIC
beta                NUMERIC
sharpe_ratio        NUMERIC
sortino_ratio       NUMERIC
maximum_drawdown    NUMERIC
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.model_providers
```sql
id                    UUID PK
name                  VARCHAR NOT NULL
provider              VARCHAR NOT NULL   -- openai|anthropic|google|mistral
cost_per_1k_tokens    NUMERIC
active                BOOLEAN NOT NULL DEFAULT TRUE
```

### ai.model_usage_logs
```sql
id             UUID PK
provider_id    UUID FK -> ai.model_providers.id
analysis_id    UUID FK -> ai.analyses.id
tokens_used    INTEGER NOT NULL
cost           NUMERIC NOT NULL
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### ai.cost_tracking
```sql
id             UUID PK
provider_id    UUID FK -> ai.model_providers.id
daily_cost     NUMERIC NOT NULL DEFAULT 0
monthly_cost   NUMERIC NOT NULL DEFAULT 0
```

### ai.service_health
```sql
id               UUID PK
component_name   VARCHAR NOT NULL   -- ai-service | postgres | redis | qdrant
status           VARCHAR NOT NULL
last_check       TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 4. Database Statistics

| Metric | v3.0 | v4.0 |
|---|---|---|
| Tables | 62 | 62 (unchanged — regrouped, not resized) |
| Schemas | 1 (implicit `public`) | 3 (`auth`, `core`, `ai`) |
| Services | 0 (monolith) | 3 |
| Cross-schema references converted from FK to plain UUID | 0 | 19 |

---

## 5. Architecture Compatibility

- ✅ 3 independent FastAPI services + Nginx gateway
- ✅ One Postgres instance, one schema per service, no cross-schema FKs
- ✅ Redis (cache / rate limiting / cross-service Pub/Sub)
- ✅ Qdrant (ai-service only)
- ✅ REST + Redis Pub/Sub inter-service communication
- ✅ `shared/` — shared Python source, no packaging step
- ✅ LangGraph multi-agent workflows, RAG, OAuth 2.0, 2FA, RBAC,
  Organizations, Public API with key-based rate limiting, Decision
  tracking + backtesting

---

# END OF ERD.md
# Version 4.0