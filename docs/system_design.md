# AlphaMind v4.0
# system_design.md (Interview-Prep Q&A)

Version: 4.0 


---

# 1. What Are You Building?

Question:

```text
What is AlphaMind?
```

Answer:

```text
An AI-powered investment research platform. Users manage portfolios,
upload research documents, and run multi-agent AI analyses that
produce transparent, cited BUY/HOLD/SELL recommendations — backed by a
Bull/Bear debate and a self-critique (Reflection) step before anything
is shown to the user. Recommendations are tracked against real market
outcomes and backtested against real benchmarks (SPY/QQQ/DIA/VT), so
the system's own track record is measurable, not just claimed.
```

---

# 2. System Architecture Overview

Question:

```text
Describe your architecture.
```

Answer:

```text
AlphaMind is 3 independently deployable microservices behind an Nginx
gateway, sharing one Postgres instance (one schema per service) and one
Redis instance.

auth-service   — identity: users, OAuth, 2FA, RBAC, organizations,
                 connections/presence. The only service that issues
                 JWTs, hashes passwords, or checks TOTP codes.

core-service   — portfolio, research (incl. document upload), 
                 notification, and the WebSocket gateway to the
                 frontend (the only service holding live connections).

ai-service     — the multi-agent engine: LangGraph orchestration, RAG,
                 multi-LLM router, debate/reflection, recommendation,
                 backtesting, market data, AI-driven analytics.

They share exactly one thing in code: `shared`, an
shared Python source (no packaging step) with config/DB/Redis/JWT-verification/
exceptions/logging/events/enums/base-schemas — the wire contracts, not
business logic.

Communication:
- REST for synchronous needs (e.g. core-service asking auth-service for
  a username), through typed, cached clients that raise a specific
  UpstreamServiceError on failure.
- Redis Pub/Sub for asynchronous cross-service signals (e.g. ai-service
  announcing an analysis completed), via a registered EventName.
```

```text
Architecture Diagram

Frontend
  │
  ▼
Nginx Gateway (routing + WebSocket upgrade, config only, no custom code)
  │
  ├── auth-service ──┐
  ├── core-service ──┼── shared (shared contracts)
  └── ai-service ────┘
        │
  ┌─────┼─────────┐
  ▼     ▼          ▼
Postgres Redis   Qdrant (ai-service only)
(3 schemas)
```

---

# 3. Why FastAPI Instead of Django?

*(unchanged from v3.0)*

```text
FastAPI gives native async support, automatic OpenAPI generation from
Pydantic models, and a lighter footprint per service — important when
running 3 of them. Django's batteries-included ORM/admin/templating
would be mostly unused weight in a service that's purely a JSON API.
```

---

# 4. Why Python for the AI Service Specifically?

```text
LangGraph, the LLM SDKs (OpenAI, Anthropic, Google, Mistral), and the
RAG/embedding ecosystem are all Python-first. Since only ai-service
needs these, isolating them there also means auth-service and
core-service's dependency trees stay small and their Docker images
build faster.
```

---

# 5. Why PostgreSQL?

```text
Strong relational guarantees for financial data (transactions, holdings)
and JSONB support for flexible fields (agent outputs, permissions).
One instance, 3 schemas — see Q8.
```

---

# 6. Why Redis for Both Caching and Cross-Service Events?

```text
It's already a required piece of infrastructure for caching and rate
limiting; using its Pub/Sub for cross-service events avoids introducing
a 4th piece of infrastructure (e.g. RabbitMQ/Kafka) whose durability
guarantees the project doesn't actually need — an event like "analysis
completed" is fine to lose if a service is down for a fraction of a
second during a demo; a payment wouldn't be, but this system has none.
```

---

# 7. Why Qdrant Over pgvector?

```text
Qdrant is purpose-built for vector search at scale with clean async
Python client support and constant-time approximate nearest-neighbor
search; pgvector would work for the current data size but would 
add vector index load onto the same Postgres instance the other two
services depend on for their own(unrelated) queries — keeping it a
separate, ai-service-owned system isolates that load.
```

---

# 8. Database Strategy: Why One Postgres Instance With 3 Schemas, Not 3 Instances?

```text
Each service's connection sets its own `search_path` to its own schema
(auth/core/ai), so from application code it behaves like 3 separate
databases — no service can accidentally query into another's tables.
Running 3 actual Postgres instances would triple backup/monitoring/
connection-pool operational work for no functional benefit at this
scale. If a service later needs genuinely independent scaling of its
data tier, splitting its schema out is a contained `pg_dump`/restore,
not a rewrite. See ERD.md v4.0 §2 for the full reasoning.
```

---

# 9. What Happens If a Service References Another Service's Data?

```text
The referencing column is a plain UUID, never a cross-schema foreign
key (Postgres doesn't meaningfully enforce those across schemas the way
teams sometimes assume, and doing so would recreate a hidden coupling
between services that are supposed to be independently deployable).
Reads that need more than the ID go through that service's REST client
and a shared reference schema (`UserRef`, `AnalysisRef`, ...).
Referential integrity across the boundary (e.g. not letting a user with
active portfolios be hard-deleted) is enforced at the application
layer — a known, accepted tradeoff of microservices.
```

---

# 10. Why a Shared Library Instead of Copy-Pasting Common Code Into Each Service?

```text
Two services independently defining "what a UserRef looks like" will
drift the moment one of them changes a field — exactly the kind of bug
that only shows up at runtime, cross-service, and is painful to trace.
A shared, versioned library makes the drift visible as a diff in one
place, reviewed once.
```

---

# 11. What's the Downside of the Shared Library Approach?

```text
Every service needs to rebuild/redeploy if shared changes,
even if that specific service's own code didn't — partially undoing the
"independent deployability" benefit of microservices for library-level
changes. Mitigated by keeping the library intentionally small (wire
contracts and infra glue, not business logic) so it changes rarely.
```

---

# 12. Why Not a Custom-Coded API Gateway?

```text
Nginx path-based routing + WebSocket upgrade handling covers everything
the gateway needs to do at this scale: route by path prefix, terminate
TLS, forward the WebSocket upgrade. Writing and maintaining a 4th
FastAPI service just to proxy requests would be extra surface area with
no functional gain — config beats code when config is sufficient.
```

---

# 13. Where Is Rate Limiting Enforced, Gateway or Service?

```text
In core-service (PublicAPIRateLimitMiddleware, Redis-backed, keyed by
X-API-Key), not in Nginx. Nginx's own rate limiting is IP-based, which
is the wrong granularity for an API-key-scoped module requirement (it
would rate-limit an entire shared office/NAT IP together). The gateway's
job is routing, not this specific module's business rule.
```

---

# 14. Why Does Only auth-service Issue Tokens?

```text
A single, obvious source of truth for identity. If token issuance were
duplicated or delegated, a bug in any other service could forge a valid
session. Every other service can only verify (stateless HS256 check
against a shared secret) — verification requires no database lookup and
no call to auth-service, so it doesn't become a bottleneck or a single
point of failure for every authenticated request across the system.
```

---

# 15. What If auth-service Is Down? Does Everything Stop?

```text
Existing sessions keep working — JWT verification is stateless, so
core-service and ai-service can still authenticate already-issued
tokens without auth-service being reachable. New logins/registrations
would fail (as they should — auth-service being down IS an outage for
new sessions specifically). Calls that need a UserRef beyond the JWT's
claims (e.g. displaying a username) degrade to a cached or placeholder
value rather than failing the whole request — this is deliberately
demonstrated live during evaluation (see ROADMAP.md v4.0 Week 5).
```

---

# 16. How Does Real-Time Work Across 3 Processes?

```text
Only core-service holds WebSocket connections. When ai-service finishes
an analysis step, it publishes to Redis (not a WebSocket message — it
has no notion of a live connection at all); core-service's background
CrossServiceSubscriber receives it and rebroadcasts it over WebSocket,
wrapped in the shared WebSocketEnvelope. The frontend never knows or
cares which backend process actually did the work.
```

---

# 17. Why LangGraph for Agent Orchestration?

*(unchanged from v3.0 — see technical_design_doc.md v4.0 and Agents.md
v4.0 for the full agent design)*

```text
Explicit graph-based orchestration (nodes, edges, shared typed state)
over ad hoc chained LLM calls: deterministic control flow, resumability,
and a debuggable execution trace — important for an explainable-AI
product and for demoing "what the agent actually did" during evaluation.
```

---

# 18. How Does RAG Work?

```text
Document uploaded in core-service -> document.uploaded event (Redis) ->
ai-service loads/parses/chunks -> embeds -> indexes in Qdrant (payload:
document_id, chunk_id; full text stays in Postgres, never duplicated
into Qdrant) -> document.status_changed event back to core-service.
At query time, ai-service embeds the query, retrieves top-k chunks from
Qdrant, fetches their text from its own `ai.document_chunks` (or via
core-service if needed), and cites them in the agent's output.
```

---

# 19. Why AI Workflow APIs Instead of Plain CRUD for Analysis?

```text
An analysis isn't a simple resource with a fixed shape — it's a
multi-step process with a live status. ai-service exposes it as a
workflow (start -> poll/subscribe for live steps -> final result), not
a single POST/GET pair, which maps naturally onto the WebSocket
relay described in Q16.
```

---

# 20. Why 3 Services and Not More (or Fewer)?

```text
Fewer than 3 (i.e. a monolith) was the team's earlier direction, but
the team confirmed the original microservices intent, so the tradeoff
became "how many services, not whether." More than 3 (the original
plan had 7: Gateway, Auth, Portfolio, Market, AI, Analytics,
Notification) means more Dockerfiles, more inter-service calls for
things that don't need to cross a boundary, and more coordination
overhead for 2 backend-side developers to carry — for the SAME point
value (the official module doesn't score by service count). 3 services
map cleanly onto "identity", "everyday CRUD", and "AI", which are also
genuinely different enough in their dependencies (auth-service is
tiny and stable; ai-service needs LangGraph/LLM SDKs/Qdrant that the
others don't) to justify being separate deployables.
```

---

# 21. What Would You Gain by Splitting Further Later?

```text
Splitting `research`'s document-CRUD out of core-service, or splitting
`market`/`analytics` out of ai-service, would let those scale/deploy
independently of the rest of their current service. Nothing about the
current design blocks this — the domain folders inside each service
already have clean internal boundaries (7-file pattern), and the "no
cross-schema FK, resolve via a client + shared ref schema" discipline
already used for cross-SERVICE boundaries is exactly what a future
split would need for the newly-separated boundary too.
```

---

# 22. How Would You Scale to 1,000 Users?

```text
Each of the 3 services scales horizontally and independently behind the
gateway (add replicas, Nginx upstream already supports multiple
backends per service). ai-service is the most likely bottleneck (LLM
calls are slow and $-costed) — scale it independently of the other two,
and add a queue in front of it if analysis requests start to back up
beyond what synchronous request/response comfortably handles.
```

---

# 23. How Do You Handle AI Cost?

```text
model_usage_logs / cost_tracking (ai schema) record tokens and cost per
provider per analysis; the LLM router can pick a cheaper/faster model
for lower-stakes agents (e.g. Sentiment) and reserve the strongest model
for Judge/Reflection. Visible on the ai-service Grafana dashboard.
```

---

# 24. What's Your Security Model?

```text
JWT (HS256, shared secret across services for this project's scale — a
documented path to RS256 exists, see COMMON_LIBRARY_README.md), OAuth
2.0, TOTP 2FA, RBAC (system + organization roles), rate-limited public
API with per-key scoping, HTTPS at the gateway for any external traffic,
plaintext only between containers on the internal Docker network (per
the subject's own carve-out for internal traffic).
```

---

# 25. What's the Most Innovative Part of This Project?

```text
The debate/reflection engine (Modules of Choice C1): instead of a
single LLM call producing an opaque recommendation, 8 specialist agents
analyze in parallel, a Bull and a Bear agent build opposing cases from
that analysis, a Judge weighs them, and a Reflection agent checks
evidence sufficiency and confidence calibration before anything reaches
the user — an auditable reasoning trail, not a black box.
```

---

# 26. What Was the Biggest Technical Challenge?

```text
Getting the service boundaries right without either (a) turning every
interaction into a slow network round trip, or (b) quietly recreating a
monolith through excessive cross-service chatter. The resolution:
REST only for things that genuinely need a synchronous answer (and
always cached), events for everything else, and a hard rule against any
cross-schema foreign key to keep the boundary honest at the data layer
too.
```

---

# 27. What Would You Do Differently With More Time?

```text
RS256 (asymmetric) JWT instead of a shared HS256 secret; a real message
queue (with durability/retry) instead of best-effort Redis Pub/Sub for
cross-service events; splitting research and market/analytics into
their own services as discussed in Q21; a service mesh or at least
mTLS between services if this ever left a trusted Docker network.
```

---

# 28. Team Rule

```text
Every team member must be able to explain, at a high level:

    The 3-service architecture and why REST vs Redis Pub/Sub is chosen
      per interaction
    shared and why it exists
    The database strategy (one instance, one schema per service)
    Authentication (JWT verify vs issue split)
    RAG
    LangGraph / Agent Orchestration
    Backtesting against a real benchmark
    Deployment (docker compose up, 9 containers)

Whoever owns a given service (ROADMAP.md v4.0 §3) must be able to
explain it in depth; everyone must be able to correctly explain every
other part at a high level, per the subject's requirement that "all
team members must be able to explain the project and their
contributions."
```

---


# END OF system_design.md
# Version 4.0
