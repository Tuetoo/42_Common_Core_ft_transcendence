# AlphaMind v3.0
# Entity Relationship Design (ERD)

Version: 3.0 (supersedes 2.3 — see "Change Log vs v2.3" at the end)

---

## 1. What changed vs v2.3

- **No table was removed.** All product functionality from v2.3 is still
  here — this update only fixes gaps and mislabeling, it does not cut scope.
- Two tables were **added** because they were required by modules the team
  already committed to but the old ERD never actually modeled:
  - `api_keys` — the Web module "Public API" (secured API key + rate
    limiting) cannot be demoed without a real API key table.
  - `connections` — the User Management "Standard user management" Major
    explicitly requires "users can add other users as friends and see their
    online status"; a `last_seen_at` column was added to `users` to support
    the online-status half of that requirement.
- The 18 "domains" are now grouped to match `MODULE_MAPPING.md v3.0`
  exactly, and each domain header states which module(s) it exists to
  support, plus its **owner** (Backend / AI / Frontend-consumed).
- `service_health` is repurposed for a single backend process + its 3
  infra dependencies (Postgres/Redis/Qdrant), matching the simplified
  monolith architecture instead of the old fake microservices list.
- "Architecture Compatibility" (Section 24) no longer claims
  "Microservices."

---

## 2. Design Goals

The schema supports, end to end:

- Authentication, OAuth, 2FA, RBAC, Organizations
- Portfolio management (holdings, transactions, watchlists, snapshots)
- Research workspace + document upload/RAG ingestion
- Multi-agent AI analysis (LangGraph), debate, reflection
- Decision tracking and backtesting against real benchmarks
- Analytics dashboards (portfolio, agent, recommendation, retrieval)
- Real-time notifications and WebSocket presence
- Auditability and basic system observability

---

## 3. Database Domains (regrouped, mapped to MODULE_MAPPING.md v3.0)

| Domain | Supports (module code) | Owner |
|---|---|---|
| AUTH | Mandatory user management + U2 (OAuth) + U3 (2FA) | Backend |
| SOCIAL (new, small) | U1 (connections + online status) | Backend |
| ORGANIZATION | U4 (advanced permissions) + U5 (organization system) | Backend |
| PORTFOLIO | Mandatory + core product | Backend |
| RESEARCH | Mandatory + W6 (file upload) | Backend |
| DOCUMENT | W6 (file upload) + A1 (RAG) | Backend / AI |
| RAG | A1 (RAG major) | AI |
| AI ANALYSIS | A1/A2 (RAG, LLM interface) + C1 (debate/reflection) | AI |
| AGENT | C1 (multi-agent engine) | AI |
| AI MEMORY | Supports C1 (agent long-term context) — engineering quality, not separately scored | AI |
| PROMPT MANAGEMENT | Supports A2 (LLM interface) — engineering quality, not separately scored | AI |
| DEBATE | C1 (debate/reflection Major) | AI |
| DECISION TRACKING | C2 (decision tracking Minor) | AI |
| BACKTEST | C2 (backtesting Minor) | AI |
| BENCHMARK | C2 (backtesting Minor) | AI |
| MODEL ROUTING | Supports A2 (LLM interface: cost/usage per provider) | AI |
| ANALYTICS | N1 (analytics dashboard Major) | AI (data) |
| NOTIFICATION | W5 (notification system) + W3 (real-time) | Backend |
| SYSTEM | D2 (health check) | Backend |
| AUDIT | Supports U4/U5 accountability — engineering quality, not separately scored | Backend |
| MARKET | Supports A1/A2/C1 (agents' data source) — engineering quality, not separately scored (see §24.5 addendum) | AI |

---

## 4. High-Level Domain Relationship

```
User
 ├── Connections (friends + online status)
 ├── Portfolio
 │      ├── Holdings
 │      ├── Transactions
 │      ├── Watchlists
 │      └── Portfolio Metrics / Evaluations
 ├── Research
 │      ├── Documents -> Chunks -> Qdrant (RAG)
 │      ├── Notes / Comments / Tags
 │      └── Analyses
 ├── AI Analysis
 │      ├── Agent Runs -> Agent Results / Steps
 │      ├── Debate Sessions -> Arguments
 │      ├── Reflections
 │      └── Recommendations -> Decision Tracking -> Outcomes
 ├── Backtests -> Positions / Trades / Metrics -> Benchmark Results
 └── Notifications

Organization
 ├── Members (role-scoped)
 ├── Shared Portfolios
 ├── Shared Research
 └── Shared Analyses

API Key (belongs to a User or an Organization)
 └── used by the Public API module (rate-limited, logged)
```

---

## 5. AUTH DOMAIN
*(Backend — mandatory user management, U2 OAuth, U3 2FA)*

### users
```sql
id                UUID PK
email             VARCHAR UNIQUE NOT NULL
username          VARCHAR UNIQUE NOT NULL
password_hash     TEXT NOT NULL
system_role       VARCHAR NOT NULL DEFAULT 'user'   -- admin | user (U4 RBAC)
is_active         BOOLEAN NOT NULL DEFAULT TRUE
is_verified       BOOLEAN NOT NULL DEFAULT FALSE
last_seen_at      TIMESTAMPTZ                        -- NEW: online-status (U1)
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### user_profiles
```sql
id            UUID PK
user_id       UUID FK -> users.id UNIQUE
first_name    VARCHAR
last_name     VARCHAR
avatar_url    TEXT                -- default avatar applied at service layer if NULL
bio           TEXT
timezone      VARCHAR
country       VARCHAR
```

### user_settings
```sql
id                          UUID PK
user_id                     UUID FK -> users.id UNIQUE
preferred_currency          VARCHAR DEFAULT 'USD'
theme                       VARCHAR DEFAULT 'system'
default_model               VARCHAR                -- default LLM provider preference
notification_preferences    JSONB
```

### user_sessions
```sql
id             UUID PK
user_id        UUID FK -> users.id
refresh_token  TEXT NOT NULL
expires_at     TIMESTAMPTZ NOT NULL
ip_address     VARCHAR
device_info    TEXT
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### oauth_accounts
```sql
id                 UUID PK
user_id            UUID FK -> users.id
provider           VARCHAR NOT NULL   -- google | github
provider_user_id   VARCHAR NOT NULL
UNIQUE (provider, provider_user_id)
```

### two_factor_secrets
```sql
id         UUID PK
user_id    UUID FK -> users.id UNIQUE
secret     TEXT NOT NULL
enabled    BOOLEAN NOT NULL DEFAULT FALSE
```

### api_keys  — **NEW, required by Web module W4 (Public API)**
```sql
id               UUID PK
owner_user_id    UUID FK -> users.id NULL
owner_org_id     UUID FK -> organizations.id NULL
name             VARCHAR NOT NULL
key_hash         TEXT NOT NULL UNIQUE      -- store a hash, never the raw key
scopes           JSONB                     -- e.g. ["portfolio:read", "research:read"]
rate_limit_per_minute  INTEGER NOT NULL DEFAULT 60
last_used_at     TIMESTAMPTZ
revoked_at       TIMESTAMPTZ
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
CHECK (owner_user_id IS NOT NULL OR owner_org_id IS NOT NULL)
```

---

## 6. SOCIAL DOMAIN — **NEW, required by User Management Major U1**
*(Backend — lightweight "friends" feature; not the Web category's full chat)*

### connections
```sql
id               UUID PK
requester_id     UUID FK -> users.id
addressee_id     UUID FK -> users.id
status           VARCHAR NOT NULL DEFAULT 'pending'  -- pending | accepted | blocked
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
responded_at     TIMESTAMPTZ
UNIQUE (requester_id, addressee_id)
```

Online status itself does not need its own table: it is derived from
`users.last_seen_at` (updated on every authenticated request/WS heartbeat)
plus the live `websocket_connections` table (Section 16).

---

## 7. ORGANIZATION DOMAIN
*(Backend — U4 advanced permissions, U5 organization system)*

### organizations
```sql
id            UUID PK
name          VARCHAR NOT NULL
description   TEXT
owner_id      UUID FK -> users.id
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### organization_members
```sql
id                 UUID PK
organization_id    UUID FK -> organizations.id
user_id            UUID FK -> users.id
role               VARCHAR NOT NULL DEFAULT 'member'  -- owner|admin|member|viewer
joined_at          TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (organization_id, user_id)
```

### organization_roles
```sql
id                 UUID PK
organization_id    UUID FK -> organizations.id
role_name          VARCHAR NOT NULL
permissions        JSONB NOT NULL   -- e.g. {"portfolio.create": true, "research.delete": false}
```

### organization_invitations
```sql
id                 UUID PK
organization_id    UUID FK -> organizations.id
email              VARCHAR NOT NULL
role               VARCHAR NOT NULL DEFAULT 'member'
invited_by         UUID FK -> users.id
expires_at         TIMESTAMPTZ NOT NULL
accepted_at        TIMESTAMPTZ
```

---

## 8. PORTFOLIO DOMAIN
*(Backend — core product, mandatory multi-user data)*

### portfolios
```sql
id                UUID PK
owner_id          UUID FK -> users.id
organization_id   UUID FK -> organizations.id NULL   -- NULL = personal portfolio
name              VARCHAR NOT NULL
currency          VARCHAR NOT NULL DEFAULT 'USD'
visibility        VARCHAR NOT NULL DEFAULT 'private'  -- private|organization
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### holdings
```sql
id              UUID PK
portfolio_id    UUID FK -> portfolios.id
ticker          VARCHAR NOT NULL
quantity        NUMERIC NOT NULL
average_cost    NUMERIC NOT NULL
UNIQUE (portfolio_id, ticker)
```

### transactions
```sql
id                  UUID PK
portfolio_id        UUID FK -> portfolios.id
ticker              VARCHAR NOT NULL
transaction_type    VARCHAR NOT NULL   -- buy|sell|dividend|deposit|withdrawal
quantity            NUMERIC
price               NUMERIC
transaction_date    TIMESTAMPTZ NOT NULL
```

### portfolio_snapshots
```sql
id              UUID PK
portfolio_id    UUID FK -> portfolios.id
snapshot_date   DATE NOT NULL
total_value     NUMERIC NOT NULL
cash_balance    NUMERIC NOT NULL
UNIQUE (portfolio_id, snapshot_date)
```

### watchlists
```sql
id          UUID PK
owner_id    UUID FK -> users.id
name        VARCHAR NOT NULL
```

### watchlist_items
```sql
id             UUID PK
watchlist_id   UUID FK -> watchlists.id
ticker         VARCHAR NOT NULL
UNIQUE (watchlist_id, ticker)
```

---

## 9. RESEARCH DOMAIN
*(Backend — mandatory + supports W6/W7)*

### research_notes
```sql
id                 UUID PK
author_id          UUID FK -> users.id
organization_id    UUID FK -> organizations.id NULL
title              VARCHAR NOT NULL
content            TEXT
created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
```

### research_comments
```sql
id           UUID PK
note_id      UUID FK -> research_notes.id
author_id    UUID FK -> users.id
content      TEXT NOT NULL
created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
```

### research_tags
```sql
id     UUID PK
name   VARCHAR UNIQUE NOT NULL
```

### note_tags
```sql
note_id   UUID FK -> research_notes.id
tag_id    UUID FK -> research_tags.id
PRIMARY KEY (note_id, tag_id)
```

---

## 10. DOCUMENT DOMAIN
*(Backend ingest orchestration + AI processing — W6, A1)*

### documents
```sql
id                 UUID PK
owner_id           UUID FK -> users.id
organization_id    UUID FK -> organizations.id NULL
filename           VARCHAR NOT NULL
document_type      VARCHAR NOT NULL      -- pdf|txt|md
storage_url        TEXT NOT NULL
status             VARCHAR NOT NULL DEFAULT 'uploaded'  -- uploaded|parsing|chunking|embedding|ready|failed
uploaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

### document_chunks
```sql
id                 UUID PK
document_id        UUID FK -> documents.id
chunk_index        INTEGER NOT NULL
token_count        INTEGER NOT NULL
qdrant_point_id    VARCHAR NOT NULL UNIQUE
```

### document_processing_jobs
```sql
id               UUID PK
document_id      UUID FK -> documents.id
status           VARCHAR NOT NULL
started_at       TIMESTAMPTZ
completed_at     TIMESTAMPTZ
error_message    TEXT
```

---

## 11. RAG DOMAIN
*(AI — A1)*

### rag_collections
```sql
id            UUID PK
name          VARCHAR NOT NULL
description   TEXT
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### rag_documents
```sql
id              UUID PK
collection_id   UUID FK -> rag_collections.id
document_id     UUID FK -> documents.id
```

### embedding_jobs
```sql
id                 UUID PK
document_id        UUID FK -> documents.id
embedding_model    VARCHAR NOT NULL
status             VARCHAR NOT NULL
created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
```

### retrieval_logs
```sql
id                UUID PK
analysis_id       UUID FK -> analyses.id
query             TEXT NOT NULL
top_k             INTEGER NOT NULL
retrieved_count   INTEGER NOT NULL
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 12. AI ANALYSIS DOMAIN
*(AI — A1, A2, C1)*

### analyses
```sql
id            UUID PK
user_id       UUID FK -> users.id
ticker        VARCHAR NOT NULL
status        VARCHAR NOT NULL DEFAULT 'pending'  -- pending|running|completed|failed|cancelled
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### analysis_sources
```sql
id                  UUID PK
analysis_id         UUID FK -> analyses.id
source_type         VARCHAR NOT NULL
source_reference    TEXT NOT NULL
```

### analysis_citations
```sql
id              UUID PK
analysis_id     UUID FK -> analyses.id
document_id     UUID FK -> documents.id
chunk_id        UUID FK -> document_chunks.id
citation_text   TEXT NOT NULL
```

### recommendations
```sql
id               UUID PK
analysis_id      UUID FK -> analyses.id
recommendation   VARCHAR NOT NULL   -- buy|hold|sell
confidence       NUMERIC NOT NULL   -- 0..1
reasoning        TEXT NOT NULL
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
```

### reflections
```sql
id            UUID PK
analysis_id   UUID FK -> analyses.id
verified      BOOLEAN NOT NULL
issues        JSONB
```

---

## 13. AGENT DOMAIN
*(AI — C1)*

### agents
```sql
id            UUID PK
name          VARCHAR NOT NULL     -- Market, News, Sentiment, Filing, Technical,
                                    -- Fundamental, Risk, Macro, Bull, Bear, Judge,
                                    -- Reflection, Supervisor
description   TEXT
version       VARCHAR NOT NULL
active        BOOLEAN NOT NULL DEFAULT TRUE
```

### agent_runs
```sql
id               UUID PK
analysis_id      UUID FK -> analyses.id
agent_id         UUID FK -> agents.id
status           VARCHAR NOT NULL   -- pending|running|completed|failed|skipped
started_at       TIMESTAMPTZ
completed_at     TIMESTAMPTZ
duration_ms      INTEGER
```

### agent_results
```sql
id             UUID PK
agent_run_id   UUID FK -> agent_runs.id
score          NUMERIC
summary        TEXT
output         JSONB
```

### agent_steps
```sql
id             UUID PK
agent_run_id   UUID FK -> agent_runs.id
step_name      VARCHAR NOT NULL
input_data     JSONB
output_data    JSONB
duration_ms    INTEGER
```

### agent_dependencies
```sql
parent_agent_id   UUID FK -> agents.id
child_agent_id    UUID FK -> agents.id
PRIMARY KEY (parent_agent_id, child_agent_id)
```

---

## 14. AI MEMORY DOMAIN
*(AI — supports C1, not separately scored)*

### agent_memories
```sql
id            UUID PK
agent_id      UUID FK -> agents.id
memory_type   VARCHAR NOT NULL
content       TEXT NOT NULL
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### memory_references
```sql
id            UUID PK
memory_id     UUID FK -> agent_memories.id
analysis_id   UUID FK -> analyses.id
```

### episodic_memories
```sql
id            UUID PK
user_id       UUID FK -> users.id
event_type    VARCHAR NOT NULL
content       TEXT
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 15. PROMPT MANAGEMENT DOMAIN
*(AI — supports A2, not separately scored)*

### prompt_templates
```sql
id            UUID PK
name          VARCHAR NOT NULL
version       VARCHAR NOT NULL
prompt        TEXT NOT NULL
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### prompt_executions
```sql
id             UUID PK
prompt_id      UUID FK -> prompt_templates.id
analysis_id    UUID FK -> analyses.id
model_used     VARCHAR NOT NULL
token_count    INTEGER
```

### prompt_versions
```sql
id            UUID PK
prompt_id     UUID FK -> prompt_templates.id
version       VARCHAR NOT NULL
content       TEXT NOT NULL
```

---

## 16. DEBATE DOMAIN
*(AI — C1)*

### debate_sessions
```sql
id             UUID PK
analysis_id    UUID FK -> analyses.id
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### debate_arguments
```sql
id                   UUID PK
debate_session_id    UUID FK -> debate_sessions.id
side                 VARCHAR NOT NULL   -- bull|bear
content              TEXT NOT NULL
```

---

## 17. DECISION TRACKING DOMAIN
*(AI — C2)*

### investment_decisions
```sql
id                   UUID PK
user_id              UUID FK -> users.id
recommendation_id    UUID FK -> recommendations.id
action_taken         VARCHAR NOT NULL   -- followed|ignored|partial
created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
```

### decision_outcomes
```sql
id                 UUID PK
decision_id        UUID FK -> investment_decisions.id
evaluation_days    INTEGER NOT NULL
strategy_return    NUMERIC
benchmark_return   NUMERIC
alpha              NUMERIC
outperformed       BOOLEAN
evaluated_at       TIMESTAMPTZ
```

### decision_reviews
```sql
id                UUID PK
decision_id       UUID FK -> investment_decisions.id
review_text       TEXT
lessons_learned   TEXT
```

---

## 18. BACKTEST DOMAIN
*(AI — C2)*

### backtests
```sql
id                  UUID PK
user_id             UUID FK -> users.id
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

### backtest_positions
```sql
id             UUID PK
backtest_id    UUID FK -> backtests.id
ticker         VARCHAR NOT NULL
quantity       NUMERIC NOT NULL
entry_price    NUMERIC NOT NULL
```

### backtest_trades
```sql
id             UUID PK
backtest_id    UUID FK -> backtests.id
ticker         VARCHAR NOT NULL
side           VARCHAR NOT NULL   -- buy|sell
quantity       NUMERIC NOT NULL
price          NUMERIC NOT NULL
trade_date     TIMESTAMPTZ NOT NULL
```

### backtest_metrics
```sql
id              UUID PK
backtest_id     UUID FK -> backtests.id
sharpe_ratio    NUMERIC
sortino_ratio   NUMERIC
drawdown        NUMERIC
win_rate        NUMERIC
```

---

## 19. BENCHMARK DOMAIN
*(AI — C2)*

### benchmarks
```sql
id            UUID PK
symbol        VARCHAR UNIQUE NOT NULL   -- SPY, QQQ, DIA, VT
name          VARCHAR NOT NULL
asset_type    VARCHAR NOT NULL
currency      VARCHAR NOT NULL
description   TEXT
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### benchmark_results
```sql
id                    UUID PK
backtest_id           UUID FK -> backtests.id
benchmark_id          UUID FK -> benchmarks.id
strategy_return       NUMERIC
benchmark_return      NUMERIC
alpha                 NUMERIC
beta                  NUMERIC
information_ratio     NUMERIC
tracking_error        NUMERIC
outperformed          BOOLEAN
created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 20. ANALYTICS DOMAIN
*(AI/data — N1)*

### portfolio_metrics
```sql
id              UUID PK
portfolio_id    UUID FK -> portfolios.id
sharpe_ratio    NUMERIC
sortino_ratio   NUMERIC
drawdown        NUMERIC
```

### recommendation_metrics
```sql
id                   UUID PK
recommendation_id    UUID FK -> recommendations.id
accuracy_score       NUMERIC
```

### agent_metrics
```sql
id             UUID PK
agent_id       UUID FK -> agents.id
avg_score      NUMERIC
success_rate   NUMERIC
```

### retrieval_metrics
```sql
id                UUID PK
analysis_id       UUID FK -> analyses.id
precision_score   NUMERIC
recall_score      NUMERIC
```

### portfolio_evaluations
```sql
id                  UUID PK
portfolio_id        UUID FK -> portfolios.id
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

---

## 21. MODEL ROUTING DOMAIN
*(AI — supports A2, not separately scored)*

### model_providers
```sql
id                    UUID PK
name                  VARCHAR NOT NULL
provider              VARCHAR NOT NULL   -- openai|anthropic|google|mistral
cost_per_1k_tokens    NUMERIC
active                BOOLEAN NOT NULL DEFAULT TRUE
```

### model_usage_logs
```sql
id             UUID PK
provider_id    UUID FK -> model_providers.id
analysis_id    UUID FK -> analyses.id
tokens_used    INTEGER NOT NULL
cost           NUMERIC NOT NULL
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

### cost_tracking
```sql
id             UUID PK
provider_id    UUID FK -> model_providers.id
daily_cost     NUMERIC NOT NULL DEFAULT 0
monthly_cost   NUMERIC NOT NULL DEFAULT 0
```

---

## 22. NOTIFICATION DOMAIN
*(Backend — W5, W3)*

### notifications
```sql
id         UUID PK
user_id    UUID FK -> users.id
type       VARCHAR NOT NULL   -- see shared.enums.notification.NotificationType
title      VARCHAR NOT NULL
content    TEXT
read_at    TIMESTAMPTZ
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

### websocket_connections
```sql
id               UUID PK
user_id          UUID FK -> users.id
connection_id    VARCHAR NOT NULL
connected_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 23. SYSTEM DOMAIN
*(Backend — D2, simplified for the monolith)*

### system_events
```sql
id            UUID PK
event_type    VARCHAR NOT NULL
payload       JSONB
created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
```

### feature_flags
```sql
id           UUID PK
flag_name    VARCHAR UNIQUE NOT NULL
enabled      BOOLEAN NOT NULL DEFAULT FALSE
```

### service_health — repurposed for the monolith + its infra dependencies
```sql
id               UUID PK
component_name   VARCHAR NOT NULL   -- backend | postgres | redis | qdrant
status           VARCHAR NOT NULL   -- healthy | degraded | down
last_check       TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 24. AUDIT DOMAIN
*(Backend — supports U4/U5 accountability, not separately scored)*

### audit_logs
```sql
id             UUID PK
actor_id       UUID FK -> users.id
action         VARCHAR NOT NULL
entity_type    VARCHAR NOT NULL
entity_id      UUID
metadata       JSONB
created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## 24.5 MARKET DOMAIN — **ADDENDUM, added in this review**
*(AI Lead — see ROADMAP.md v3.0 §3. Gap found during the team-ownership
review: `backend/app/domains/market/` already existed in the codebase
and is referenced throughout `MODULE_MAPPING.md`/`PROJECT_STRUCTURE.md`,
but no tables were ever defined for it. Numbered 24.5, not 25, to avoid
renumbering every section after it.)*

### instruments
```sql
id             UUID PK
ticker         VARCHAR UNIQUE NOT NULL
name           VARCHAR
asset_type     VARCHAR NOT NULL   -- see shared.enums.market.AssetType
currency       VARCHAR NOT NULL DEFAULT 'USD'
sector         VARCHAR
exchange       VARCHAR
```

### price_bars
```sql
id               UUID PK
instrument_id    UUID FK -> instruments.id
interval         VARCHAR NOT NULL   -- 1d | 1h | 5m
timestamp        TIMESTAMPTZ NOT NULL
open             NUMERIC NOT NULL
high             NUMERIC NOT NULL
low              NUMERIC NOT NULL
close            NUMERIC NOT NULL
volume           NUMERIC NOT NULL
UNIQUE (instrument_id, interval, timestamp)
```

### technical_indicator_cache
```sql
id               UUID PK
instrument_id    UUID FK -> instruments.id
indicator        VARCHAR NOT NULL   -- RSI | MACD | EMA | SMA | BOLLINGER | ATR
computed_at      TIMESTAMPTZ NOT NULL
value            JSONB NOT NULL
```

Note: `price_bars` and `technical_indicator_cache` are read-heavy,
short-lived data — real-time quotes are additionally cached in Redis
(`core/redis.py`'s cache DB, 30s TTL) rather than round-tripping to
Postgres on every request; Postgres holds the durable historical record
these caches are refreshed from.

---

## 25. Database Statistics


| Metric | v2.3 | v3.0 |
|---|---|---|
| Tables | 57 | 62 (`api_keys`, `connections` added in v3.0; `instruments`, `price_bars`, `technical_indicator_cache` added in this review's MARKET addendum) |
| Domains | 18 | 21 (`SOCIAL` added; `BENCHMARK` promoted to its own numbered section; `MARKET` added as §24.5 addendum) |
| Estimated relationships | 120+ | 120+ |

---

## 26. Architecture Compatibility

This schema supports:

- ✅ FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL
- ✅ Redis (cache / rate limiting / pub-sub)
- ✅ Qdrant (RAG vector search)
- ✅ LangGraph multi-agent workflows
- ✅ RAG pipeline with citation tracking
- ✅ WebSocket real-time updates + presence
- ✅ OAuth 2.0 + 2FA (TOTP)
- ✅ RBAC + Organizations
- ✅ Public API with API-key rate limiting
- ✅ Decision tracking + backtesting vs benchmark
- ✅ Modular monolith deployment (single backend container)
- ⏸ Microservices — **not currently targeted**; the schema does not block a
  future split (no cross-domain foreign keys were removed to make this
  possible), but the baseline plan deploys as one backend service.

---

## 27. Change Log vs v2.3

1. Added `api_keys` (required by Web module W4, previously undefined).
2. Added `connections` + `users.last_seen_at` (required by User Management
   Major U1, previously undefined).
3. Regrouped all 20 domains explicitly against `MODULE_MAPPING.md v3.0`
   module codes, with an owner (Backend/AI) per domain.
4. Repurposed `service_health` from a multi-microservice registry to a
   single-backend + 3-infra-dependency health table, matching the
   simplified monolith architecture.
5. Removed the "✅ Microservices" claim from Architecture Compatibility;
   replaced with an explicit "not currently targeted" note.
6. Promoted the former "16.5 Benchmark Domain" to a first-class numbered
   section (19) for clarity — no structural change to its tables.

---

# END OF ERD.md
# Version 3.0