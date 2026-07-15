# AlphaMind v4.0
# EVAL_BLUEPRINT.md

Version: 4.0

---

# 1. Objective

```text
Maximize evaluator perception
Maximize module coverage
Reduce evaluation risk
Clear the 14-point mandatory minimum with a wide safety margin, toward
the honestly-scored 34-point baseline in MODULE_MAPPING.md v4.0
Demonstrate the microservices architecture as a genuine strength, not
just a checkbox — the demo script below deliberately shows
inter-service behavior, not just end-user features.
```

---

# 2. Evaluator Mental Model

```text
An evaluator forms an impression in the first 2 minutes. Priorities,
in order:

1. Does it run? (docker compose up, one command, all 9 containers)
2. Can the team explain what they built?
3. Do the claimed modules actually work when demoed?
4. Is there something genuinely interesting beyond the checklist?
   (AlphaMind's answer: the debate/reflection engine, and the fact
   that killing one service doesn't take down the other two.)
```

---

# 3. Target Score Strategy

```text
Official minimum: 14
Safe pass: 20
AlphaMind baseline (fully built): 34
  (bonus above 14 is capped at +5 per the official subject — build
  past 14 for demo-day resilience and product quality, not because the
  formal score scales linearly past the cap)
```

---

# 4. Pre-Demo Setup Checklist

```text
[ ] Fresh clone, docker compose up — confirm all 9 containers healthy
[ ] Seed data: at least one user, one organization, one portfolio with
    holdings, one uploaded (already-processed) document, one completed
    analysis with a recommendation, one backtest report
[ ] Grafana dashboards open in 3 tabs (one per service)
[ ] A terminal ready to `docker compose stop auth-service` for the
    resilience demo (Section 15)
[ ] GitHub Actions tab open, showing 3 green pipelines
```

---

# 5. Demo Script — Identity (auth-service)

```text
1. Register a new user -> show email/password validation
2. Log in -> show JWT issued (decode it live, point out `sub`, `role`)
3. Enable 2FA -> scan QR -> verify code -> log out -> log back in with
   2FA challenge
4. OAuth login with Google/GitHub
5. Create an organization -> invite the seed user -> show role-based
   view differences (owner vs member)
6. Add a connection -> show the other user's online status update live
```

# 6. Demo Script — Portfolio & Research (core-service)

```text
1. Create a portfolio, add 3 holdings
2. Point out: PortfolioRead.owner.username is NOT a local database
   column — open core-service's logs live and show the actual HTTP
   call to auth-service's /internal/users/{id} happening, then show it
   NOT happening on a second identical request (cache hit)
3. Upload a 10-K PDF -> show status: uploaded -> (wait a few seconds,
   or show ai-service's logs reacting to the Redis event) -> ready
4. Ask a question about the document -> get a cited answer
5. Advanced search: filter/sort/paginate research notes
```

# 7. Demo Script — Multi-Agent AI (ai-service)

```text
1. Request a full analysis on a real ticker
2. Watch the live workflow timeline over WebSocket — explicitly narrate:
   "ai-service is doing this work; core-service, a completely different
   process, is what's actually holding this browser connection open —
   it's relaying what ai-service published to Redis"
3. Show the 8 data/analysis agents completing (parallel where possible)
4. Show the Bull case, the Bear case, the Judge's verdict
5. Show the Reflection agent's self-check
6. Show the final recommendation: action, confidence, reasoning,
   citations back to the uploaded document
```

# 8. Demo Script — Decision Tracking & Backtesting (ai-service)

```text
1. Mark a past recommendation as "followed"
2. Show its outcome evaluation against real market data
3. Run a backtest against SPY -> show Sharpe/Sortino/drawdown/win-rate
4. Show the analytics dashboard: recommendation accuracy over time,
   agent performance breakdown, CSV export
```

# 9. Demo Script — Public API (core-service, Module W4)

```text
1. curl the public API without X-API-Key -> 401
2. curl with a valid key -> 200, show the Swagger docs for these
   endpoints (auto-generated from Pydantic models)
3. Hit it >60 times in under a minute -> 429, show the Redis key
   backing the counter
```

# 10. Demo Script — Real-Time Resilience

```text
Already covered inline in Sections 6–7 (cache hit/miss, cross-service
WebSocket relay). This is the connective tissue of the whole demo —
don't save it for one isolated "and here's our architecture" slide;
show it live, at the exact moment it's happening, throughout.
```

# 11. Demo Script — Infrastructure

```text
1. docker compose up from a clean clone — narrate the 9 containers as
   they come up
2. Open all 3 Grafana dashboards — show live request metrics as you
   perform the demo actions above
3. Open each service's /health
4. Show 3 separate Swagger UIs (/docs/auth, /docs/core, /docs/ai) —
   point out they're independently generated from each service's own
   Pydantic schemas
5. Show GitHub Actions: point at a merged PR that touched only
   ai-service and show only ai-service's + the shared-library
   pipelines ran, not auth-service's/core-service's
```

# 12. Demo Script — Service Independence (the signature moment)

```text
1. docker compose stop auth-service
2. Show that an ALREADY-LOGGED-IN user can still use core-service and
   ai-service (JWT verification is stateless — no call to auth-service
   needed for an existing session)
3. Show that a NEW login attempt correctly fails (auth-service is the
   identity source of truth — this SHOULD fail)
4. Show that a request needing a UserRef (e.g. viewing a portfolio)
   degrades gracefully (cached value, or an explicit placeholder) 
   instead of 500ing the whole request
5. docker compose start auth-service -> show everything recovers
   without restarting the other two services
```

This is the single highest-value 90 seconds of the demo — it's the
concrete, undeniable proof the "microservices" claim (Devops D2, 2 pts)
is real, not just a diagram.

---

# 13. High-Value Modules (build/rehearse these first)

```text
Tier S:
    OAuth, 2FA, WebSocket (cross-service relay), Public API,
    Backend as microservices (D2 — see Section 12),
    Monitoring (3 services), Multi-Agent AI (RAG + LLM interface +
    Modules of Choice C1)

Tier A:
    Advanced Search, Backtesting/Decision Tracking, Analytics Dashboard,
    CI/CD (C3)

Tier B:
    Watchlists, Notifications, Profile/Connections (U1)
```

---

# 14. Common Reasons Teams Lose Points

```text
Claiming a module not in the official list (e.g. "Database", "Docker",
  "CI/CD" as standalone modules — CI/CD here is correctly claimed
  through Modules of Choice with justification, not as a standalone item)

Claiming a specific stack for a module while running something else
  (e.g. claiming ELK while running only structured JSON logs with no
  real Elasticsearch/Logstash/Kibana — not claimed in this project)

Claiming "microservices" without being able to show independent
  deployability — mitigated here by Section 12's live demo

A service crashing during the demo because a cross-service call wasn't
  cached/error-handled — mitigated by the mandatory 4-step client
  pattern in every app/clients/*.py (see COMMON_LIBRARY_README.md)
```

---

# 15. Likely Evaluator Questions & Answers

```text
Q: Why 3 services and not more/fewer?
A: See system_design.md v4.0 Q20 — maps cleanly onto genuinely
   different dependency needs (identity vs CRUD vs AI/LLM), and more
   services wasn't going to score differently under the official rules.

Q: What happens if a service goes down?
A: Live demo — see Section 12.

Q: How do services find each other?
A: Docker Compose service names as hostnames on the shared network
   (see BaseServiceSettings.AUTH_SERVICE_URL etc.) — no separate
   service registry needed at this scale.

Q: Why one Postgres instance instead of three?
A: See ERD.md v4.0 §2 and system_design.md v4.0 Q8 — schema-per-service
   gives the same application-level isolation without tripling
   operational overhead for a 6-week project.

Q: How do you keep the shared library from becoming a bottleneck?
A: It's intentionally small — wire contracts and infra glue only, never
   business logic — so it changes rarely; every service still owns its
   real logic independently.

Q: Why doesn't the AI engine talk to the database that stores
   portfolios?
A: It doesn't need to, and shouldn't — cross-service data needs go
   through a typed, cached REST call (ai_client.py /
   core_client.py), never a database join across schemas.
```

---

# 16. Final Point Outcome

```text
Official minimum: 14
Safe target: 20
AlphaMind baseline (fully built): 34
```

---

# 17. AlphaMind Evaluation Formula (unchanged principle from v3.0)

```text
Perceived Quality = Working Demo × Explanation Clarity × Architecture
Coherence

A working demo with a clear explanation of a coherent architecture
consistently outperforms a longer feature list with any one of those
three missing.
```

---

# 18. Final Mission

```text
Show a real, working, honestly-scoped 3-service platform. Every module
claimed must be demoed; every architectural claim (especially
"microservices") must be shown working, not just described.
```

---

# END OF EVAL_BLUEPRINT.md
# Version 4.0
