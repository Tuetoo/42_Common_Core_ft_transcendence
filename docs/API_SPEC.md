# AlphaMind v2.3 Enterprise
# API Specification

Version: 2.3

---

# 1. API Design Principles

## Architecture Style

REST First

Future:

```text
REST
+
WebSocket
+
Event Driven
```

---

## API Versioning

```http
/api/v1
```

Example:

```http
/api/v1/auth/login
/api/v1/portfolio
/api/v1/analysis/run
```

---

## Response Format

All APIs return:

```json
{
  "success": true,
  "data": {},
  "message": null
}
```

---

## Error Format

```json
{
  "success": false,
  "error": {
    "code": "INVALID_CREDENTIALS",
    "message": "Email or password incorrect"
  }
}
```

---

# Workflow Architecture

AlphaMind separates its backend into two complementary API layers.

## REST Layer

Responsible for resource management.

Typical operations include:

- User Management
- Portfolio Management
- Organizations
- Documents
- Transactions
- Research Notes

REST APIs follow standard CRUD principles.

---

## AI Workflow Layer

Responsible for orchestrating long-running AI analysis tasks.

Instead of manipulating resources directly, Workflow APIs manage the lifecycle of AI executions.

Workflow operations include:

- Start Analysis
- Monitor Progress
- Pause Execution
- Resume Execution
- Cancel Execution
- View Execution Timeline
- Inspect Agent States
- View Intermediate Results
- Retrieve Final Recommendation

This separation keeps business resources independent from AI execution logic while improving scalability and observability.

---

# 2. Authentication APIs

Base Path

```http
/api/v1/auth
```

---

## Register

POST

```http
/auth/register
```

Request

```json
{
  "email":"user@email.com",
  "username":"john",
  "password":"secret"
}
```

Response

```json
{
  "user_id":"uuid"
}
```

---

## Login

POST

```http
/auth/login
```

Request

```json
{
  "email":"user@email.com",
  "password":"secret"
}
```

Response

```json
{
  "access_token":"...",
  "refresh_token":"..."
}
```

---

## Refresh Token

POST

```http
/auth/refresh
```

---

## Logout

POST

```http
/auth/logout
```

---

## Forgot Password

POST

```http
/auth/forgot-password
```

---

## Reset Password

POST

```http
/auth/reset-password
```

---

# 3. OAuth APIs

---

## Google Login

GET

```http
/auth/oauth/google
```

---

## Google Callback

GET

```http
/auth/oauth/google/callback
```

---

## GitHub Login

GET

```http
/auth/oauth/github
```

---

## GitHub Callback

GET

```http
/auth/oauth/github/callback
```

---

# 4. Two Factor Authentication APIs

---

## Enable 2FA

POST

```http
/auth/2fa/enable
```

Response

```json
{
  "qr_code":"..."
}
```

---

## Verify 2FA

POST

```http
/auth/2fa/verify
```

---

## Disable 2FA

POST

```http
/auth/2fa/disable
```

---

# 5. User APIs

Base

```http
/api/v1/users
```

---

## Current User

GET

```http
/users/me
```

---

## Update Profile

PUT

```http
/users/me
```

---

## Update Settings

PUT

```http
/users/settings
```

---

## Upload Avatar

POST

```http
/users/avatar
```

Multipart upload.

---

# 6. Organization APIs

Base

```http
/api/v1/organizations
```

---

## Create Organization

POST

```http
/organizations
```

---

## List Organizations

GET

```http
/organizations
```

---

## Get Organization

GET

```http
/organizations/{id}
```

---

## Update Organization

PUT

```http
/organizations/{id}
```

---

## Delete Organization

DELETE

```http
/organizations/{id}
```

---

## Invite Member

POST

```http
/organizations/{id}/invite
```

Request

```json
{
  "email":"user@email.com",
  "role":"ANALYST"
}
```

---

## Accept Invitation

POST

```http
/invitations/{id}/accept
```

---

## Remove Member

DELETE

```http
/organizations/{id}/members/{member_id}
```

---

# 7. Portfolio APIs

Base

```http
/api/v1/portfolios
```

---

## Create Portfolio

POST

```http
/portfolios
```

---

## List Portfolios

GET

```http
/portfolios
```

---

## Portfolio Details

GET

```http
/portfolios/{id}
```

---

## Update Portfolio

PUT

```http
/portfolios/{id}
```

---

## Delete Portfolio

DELETE

```http
/portfolios/{id}
```

---

# 8. Holdings APIs

---

## Add Holding

POST

```http
/portfolios/{id}/holdings
```

Request

```json
{
  "ticker":"NVDA",
  "quantity":10,
  "average_cost":120
}
```

---

## Update Holding

PUT

```http
/holdings/{id}
```

---

## Delete Holding

DELETE

```http
/holdings/{id}
```

---

## List Holdings

GET

```http
/portfolios/{id}/holdings
```

---

# 9. Transactions APIs

---

## Add Transaction

POST

```http
/transactions
```

---

## List Transactions

GET

```http
/transactions
```

---

## Transaction Detail

GET

```http
/transactions/{id}
```

---

# 10. Watchlist APIs

---

## Create Watchlist

POST

```http
/watchlists
```

---

## List Watchlists

GET

```http
/watchlists
```

---

## Add Watchlist Item

POST

```http
/watchlists/{id}/items
```

---

## Remove Watchlist Item

DELETE

```http
/watchlists/{id}/items/{item_id}
```

---

# 11. Market Data APIs

Base

```http
/api/v1/market
```

---

## Stock Quote

GET

```http
/market/quote/{ticker}
```

Response

```json
{
  "ticker":"NVDA",
  "price":145.22
}
```

---

## Historical Prices

GET

```http
/market/history/{ticker}
```

Query

```http
?start=2024-01-01
&end=2025-01-01
```

---

## Technical Indicators

GET

```http
/market/indicators/{ticker}
```

Response

```json
{
  "rsi":65,
  "macd":1.4,
  "ema50":120
}
```


---

## Available Benchmarks

GET

```http
/market/benchmarks
```

Response

```json
[
  {
    "symbol": "SPY",
    "name": "SPDR S&P500 ETF"
  },
  {
    "symbol": "QQQ",
    "name": "Invesco NASDAQ100 ETF"
  },
  {
    "symbol": "DIA",
    "name": "SPDR Dow Jones ETF"
  }
]
```

---

## Benchmark Historical Prices

GET

```http
/market/benchmarks/{symbol}/history
```

Query

```http
?start=2024-01-01
&end=2025-01-01
```

Response

```json
{
  "symbol": "SPY",
  "prices": []
}
```

---

# 12. Research APIs

Base

```http
/api/v1/research
```

---

## Create Note

POST

```http
/research/notes
```

---

## List Notes

GET

```http
/research/notes
```

---

## Note Detail

GET

```http
/research/notes/{id}
```

---

## Update Note

PUT

```http
/research/notes/{id}
```

---

## Delete Note

DELETE

```http
/research/notes/{id}
```

---

# 13. Research Comment APIs

---

## Add Comment

POST

```http
/research/notes/{id}/comments
```

---

## List Comments

GET

```http
/research/notes/{id}/comments
```

---

# 14. Document APIs

Base

```http
/api/v1/documents
```

---

## Upload Document

POST

```http
/documents/upload
```

Multipart form.

---

## List Documents

GET

```http
/documents
```

---

## Get Document

GET

```http
/documents/{id}
```

---

## Delete Document

DELETE

```http
/documents/{id}
```

---

## Document Status

GET

```http
/documents/{id}/status
```

Response

```json
{
  "status":"INDEXED"
}
```

---

# 15. RAG APIs

Base

```http
/api/v1/rag
```

---

## Search Documents

POST

```http
/rag/search
```

Request

```json
{
  "query":"Tesla revenue growth"
}
```

---

## Ask Knowledge Base

POST

```http
/rag/ask
```

Request

```json
{
  "question":"What are Tesla's risks?"
}
```

---

Response

```json
{
  "answer":"...",
  "citations":[]
}
```

---

# 16. AI Analysis APIs

Base

```http
/api/v1/analysis
```

---

## Run Analysis

POST

```http
/analysis/run
```

Request

```json
{
  "ticker": "NVDA",
  "benchmark": "SPY",
  "risk_level": "MEDIUM",
  "time_horizon": "LONG_TERM",
  "enable_debate": true,
  "enable_reflection": true
}
```

---

Response

```json
{
  "analysis_id":"uuid"
}
```

---

## Analysis Status

GET

```http
/analysis/{id}/status
```

---

## Analysis Result

GET

```http
/analysis/{id}
```

Response

```json
{
  "recommendation": "BUY",
  "confidence": 0.91,
  "benchmark": "SPY",
  "bull_summary": "...",
  "bear_summary": "...",
  "reflection_score": 0.93
}
```

---

## Analysis History

GET

```http
/analysis/history
```

---

## AI Workflow

The Analysis Service is implemented as an asynchronous workflow managed by LangGraph.

Workflow

```text
User Request

↓

Workflow Created

↓

Supervisor Agent

↓

Parallel AI Agents

↓

Debate Engine

↓

Reflection

↓

Benchmark Evaluation

↓

Recommendation Generated

↓

Workflow Completed
```

Each workflow execution receives a unique Analysis ID.

The frontend can monitor workflow execution using Workflow APIs and WebSocket events.


# 16.5 AI Workflow APIs

Base

```http
/api/v1/workflows
```

---

## Get Workflow

GET

```http
/workflows/{workflow_id}
```

Returns current workflow status.

---

## Workflow Timeline

GET

```http
/workflows/{workflow_id}/timeline
```

Returns chronological execution history.

Example

```json
[
  {
    "agent":"Market Agent",
    "status":"COMPLETED",
    "duration_ms":1420
  },
  {
    "agent":"News Agent",
    "status":"RUNNING"
  }
]
```

---

## Workflow Events

GET

```http
/workflows/{workflow_id}/events
```

Returns all workflow events.

---

## Workflow Agents

GET

```http
/workflows/{workflow_id}/agents
```

Returns execution status of every agent.

---

## Pause Workflow

POST

```http
/workflows/{workflow_id}/pause
```

---

## Resume Workflow

POST

```http
/workflows/{workflow_id}/resume
```

---

## Cancel Workflow

POST

```http
/workflows/{workflow_id}/cancel
```

---

## Workflow Graph

GET

```http
/workflows/{workflow_id}/graph
```

Returns LangGraph execution graph.

---

## Workflow Metrics

GET

```http
/workflows/{workflow_id}/metrics
```

Response

```json
{
  "runtime_ms": 3242,
  "agent_count": 11,
  "parallel_tasks": 4,
  "llm_calls": 18,
  "token_usage": 42133
}
```


---

# 17. Agent APIs

Base

```http
/api/v1/agents
```

---

## List Agents

GET

```http
/agents
```

---

## Agent Detail

GET

```http
/agents/{id}
```

---

## Agent Metrics

GET

```http
/agents/{id}/metrics
```

---

## Agent Runs

GET

```http
/agents/{id}/runs
```

---

# 18. Debate APIs

Base

```http
/api/v1/debates
```

---

## Debate Detail

GET

```http
/debates/{id}
```

---

## Bull Case

GET

```http
/debates/{id}/bull
```

---

## Bear Case

GET

```http
/debates/{id}/bear
```

---

# 19. Recommendation APIs

Base

```http
/api/v1/recommendations
```

---

## List Recommendations

GET

```http
/recommendations
```

---

## Recommendation Detail

GET

```http
/recommendations/{id}
```

---

## Recommendation Metrics

GET

```http
/recommendations/{id}/metrics
```

---

# 20. Decision Tracking APIs

Base

```http
/api/v1/decisions
```

---

## Create Decision

POST

```http
/decisions
```

Request

```json
{
  "recommendation_id":"uuid",
  "action":"BUY"
}
```

---

## Decision Detail

GET

```http
/decisions/{id}
```

---

## Decision Outcome

GET

```http
/decisions/{id}/outcome
```

---

# 21. Backtesting APIs

Base

```http
/api/v1/backtests
```

---

## Create Backtest

POST

```http
/backtests
```

Request

```json
{
  "strategy": "AlphaMind",
  "ticker": "NVDA",
  "benchmark": "SPY",
  "start": "2020-01-01",
  "end": "2025-01-01",
  "initial_capital": 100000
}
```

---

## Backtest Status

GET

```http
/backtests/{id}/status
```

---

## Backtest Result

GET

```http
/backtests/{id}
```

---

## Backtest Trades

GET

```http
/backtests/{id}/trades
```

---

## Benchmark Comparison

GET

```http
/backtests/{id}/benchmark
```

Response

```json
{
  "strategy_return": 18.4,
  "benchmark_return": 13.2,
  "alpha": 5.2,
  "beta": 1.03,
  "sharpe_ratio": 1.82,
  "sortino_ratio": 2.14,
  "maximum_drawdown": -8.4,
  "outperformed": true
}
```

---

# 22. Analytics APIs

Base

```http
/api/v1/analytics
```

---

## Portfolio Metrics

GET

```http
/analytics/portfolio/{id}
```

---

## Recommendation Metrics

GET

```http
/analytics/recommendations
```

---

## Agent Metrics

GET

```http
/analytics/agents
```

---

## AI Accuracy Dashboard

GET

```http
/analytics/ai
```

---

## Portfolio Evaluation

GET

```http
/analytics/portfolio/{id}/evaluation
```

Response

```json
{
  "portfolio_return": 21.3,
  "benchmark_return": 15.7,
  "alpha": 5.6,
  "beta": 0.94,
  "sharpe_ratio": 1.91,
  "sortino_ratio": 2.47,
  "maximum_drawdown": -7.2
}
```

---

## Benchmark Performance

GET

```http
/analytics/benchmarks
```

Response

```json
{
  "benchmarks": [
    {
      "symbol": "SPY",
      "annual_return": 12.4
    },
    {
      "symbol": "QQQ",
      "annual_return": 16.2
    }
  ]
}
```

---

# 23. Notification APIs

Base

```http
/api/v1/notifications
```

---

## List Notifications

GET

```http
/notifications
```

---

## Mark As Read

POST

```http
/notifications/{id}/read
```

---

## Mark All As Read

POST

```http
/notifications/read-all
```

---

# 24. WebSocket APIs

Endpoint

```text
ws://host/api/v1/ws
```

---

## Authentication Event

```json
{
  "event":"AUTH",
  "token":"jwt"
}
```

---

## Subscribe Portfolio

```json
{
  "event":"SUBSCRIBE",
  "channel":"portfolio:uuid"
}
```

---

## Subscribe Organization

```json
{
  "event":"SUBSCRIBE",
  "channel":"org:uuid"
}
```

---

## Subscribe Analysis

```json
{
  "event":"SUBSCRIBE",
  "channel":"analysis:uuid"
}
```

---

## Subscribe Workflow

```json
{
  "event":"SUBSCRIBE",
  "channel":"workflow:uuid"
}
```

---

# 25. WebSocket Events

## Analysis Progress

```json
{
  "event":"ANALYSIS_PROGRESS",
  "progress":50
}
```

---

## Analysis Completed

```json
{
  "event":"ANALYSIS_COMPLETED",
  "analysis_id":"uuid"
}
```

---

## Notification Created

```json
{
  "event":"NOTIFICATION_CREATED"
}
```

---

## Document Indexed

```json
{
  "event":"DOCUMENT_INDEXED"
}
```

---

## Benchmark Evaluation Completed

```json
{
  "event": "BENCHMARK_COMPLETED",
  "backtest_id": "uuid"
}
```

---

## Portfolio Evaluation Completed

```json
{
  "event": "PORTFOLIO_EVALUATED",
  "portfolio_id": "uuid"
}
```

---

## Workflow Started

```json
{
  "event":"WORKFLOW_STARTED"
}
```

---

## Agent Started

```json
{
  "event":"AGENT_STARTED",
  "agent":"Market Agent"
}
```

---

## Agent Finished

```json
{
  "event":"AGENT_FINISHED",
  "agent":"News Agent"
}
```

---

## Workflow Paused

```json
{
  "event":"WORKFLOW_PAUSED"
}
```

---

## Workflow Resumed

```json
{
  "event":"WORKFLOW_RESUMED"
}
```

---

## Workflow Cancelled

```json
{
  "event":"WORKFLOW_CANCELLED"
}
```

---

## Workflow Completed

```json
{
  "event":"WORKFLOW_COMPLETED",
  "workflow_id":"uuid"
}
```

---

# 26. Admin APIs

Base

```http
/api/v1/admin
```

---

## List Users

GET

```http
/admin/users
```

---

## Disable User

POST

```http
/admin/users/{id}/disable
```

---

## Service Health

GET

```http
/admin/health
```

---

## System Metrics

GET

```http
/admin/metrics
```

---

## Feature Flags

GET

```http
/admin/feature-flags
```

---

# 27. AI Cost Management APIs

Base

```http
/api/v1/costs
```

---

## Daily Cost

GET

```http
/costs/daily
```

---

## Monthly Cost

GET

```http
/costs/monthly
```

---

## Model Usage

GET

```http
/costs/models
```

---

# 28. Prompt Management APIs

Base

```http
/api/v1/prompts
```

---

## List Prompts

GET

```http
/prompts
```

---

## Create Prompt

POST

```http
/prompts
```

---

## Update Prompt

PUT

```http
/prompts/{id}
```

---

## Prompt Versions

GET

```http
/prompts/{id}/versions
```

---

# 29. Health APIs

## Readiness

GET

```http
/health/ready
```

---

## Liveness

GET

```http
/health/live
```

---

## Dependency Health

GET

```http
/health/dependencies
```

Response

```json
{
  "postgres":"UP",
  "redis":"UP",
  "qdrant":"UP",
  "openai":"UP"
}
```

---

# 30. API Statistics

## API Categories

```text
REST APIs

≈120

Workflow APIs

≈10

WebSocket Events

20+
```

---

## Services Covered

```text
Gateway

Auth

Portfolio

Research

AI

Analytics

Notifications
```

---

Compatible With

✅ FastAPI

✅ OpenAPI

✅ Swagger

✅ SQLAlchemy

✅ PostgreSQL

✅ Redis

✅ Qdrant

✅ LangGraph

✅ Multi-Agent

✅ RAG

✅ AsyncIO

✅ Pydantic v2

✅ WebSocket

✅ OAuth

✅ 2FA

✅ Benchmark Evaluation

✅ AsyncIO

✅ Workflow Engine

✅ LangGraph State Machine

✅ Event Streaming

---

---

# API Layer Summary

AlphaMind exposes two complementary API layers.

## REST API

Designed for standard business resources.

Examples:

- Users
- Portfolios
- Documents
- Organizations
- Transactions

---

## Workflow API

Designed for AI orchestration.

Examples:

- AI Analysis
- Workflow Execution
- Agent Monitoring
- Timeline Inspection
- Benchmark Evaluation

---

This architecture cleanly separates resource management from AI execution, making AlphaMind scalable, observable, and extensible for future multi-agent capabilities.


# END OF API_SPEC.md
# Version 2.3 Enterprise