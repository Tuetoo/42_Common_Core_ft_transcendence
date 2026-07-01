# AlphaMind v2.3 Enterprise
# Entity Relationship Design (ERD)

Version: 2.3 Enterprise Edition

---

# 1. ERD Design Goals

This version extends the MVP schema into an enterprise-grade architecture capable of supporting:

- Multi-Agent AI
- LangGraph Workflows
- RAG Pipelines
- AI Evaluation
- Organization Collaboration
- Audit & Compliance
- Cost Monitoring
- Model Routing
- Prompt Versioning
- Agent Memory
- Backtesting
- Future Broker Integration

---

# 2. Database Domains

```text
AUTHENTICATION

USER MANAGEMENT

ORGANIZATIONS

PORTFOLIOS

RESEARCH

DOCUMENTS

RAG

AI ANALYSIS

AGENT SYSTEM

PROMPT MANAGEMENT

AI MEMORY

DECISION TRACKING

BACKTESTING

ANALYTICS

NOTIFICATIONS

SYSTEM MANAGEMENT

AUDIT
```

---

# 3. High-Level Domain Relationship

```text
User
 │
 ├── Portfolio
 │      ├── Holdings
 │      ├── Transactions
 │      └── Portfolio Metrics
 │
 ├── Research
 │      ├── Documents
 │      ├── Notes
 │      └── Analyses
 │
 ├── AI Analysis
 │      ├── Agent Runs
 │      ├── Recommendations
 │      ├── Debate Sessions
 │      └── Reflections
 │
 ├── Decisions
 │      └── Outcomes
 │
 └── Notifications


Organization
 │
 ├── Members
 ├── Shared Portfolios
 ├── Shared Research
 └── Shared Analyses
```

---

# 4. AUTH DOMAIN

## users

```sql
id UUID PK
email VARCHAR UNIQUE
username VARCHAR UNIQUE
password_hash TEXT
is_active BOOLEAN
is_verified BOOLEAN
created_at TIMESTAMP
updated_at TIMESTAMP
```

---

## user_profiles

```sql
id UUID PK
user_id UUID FK
first_name VARCHAR
last_name VARCHAR
avatar_url TEXT
bio TEXT
timezone VARCHAR
country VARCHAR
```

---

## user_settings

```sql
id UUID PK
user_id UUID FK
preferred_currency VARCHAR
theme VARCHAR
default_model VARCHAR
notification_preferences JSONB
```

---

## user_sessions

```sql
id UUID PK
user_id UUID FK
refresh_token TEXT
expires_at TIMESTAMP
ip_address VARCHAR
device_info TEXT
```

---

## oauth_accounts

```sql
id UUID PK
user_id UUID FK
provider VARCHAR
provider_user_id VARCHAR
```

---

## two_factor_secrets

```sql
id UUID PK
user_id UUID FK
secret TEXT
enabled BOOLEAN
```

---

# 5. ORGANIZATION DOMAIN

## organizations

```sql
id UUID PK
name VARCHAR
description TEXT
owner_id UUID FK
created_at TIMESTAMP
```

---

## organization_members

```sql
id UUID PK
organization_id UUID FK
user_id UUID FK
role VARCHAR
joined_at TIMESTAMP
```

---

## organization_roles

```sql
id UUID PK
organization_id UUID FK
role_name VARCHAR
permissions JSONB
```

---

## organization_invitations

```sql
id UUID PK
organization_id UUID FK
email VARCHAR
role VARCHAR
invited_by UUID FK
expires_at TIMESTAMP
```

---

# 6. PORTFOLIO DOMAIN

## portfolios

```sql
id UUID PK
owner_id UUID FK
organization_id UUID FK NULL
name VARCHAR
currency VARCHAR
visibility VARCHAR
created_at TIMESTAMP
```

---

## holdings

```sql
id UUID PK
portfolio_id UUID FK
ticker VARCHAR
quantity NUMERIC
average_cost NUMERIC
```

---

## transactions

```sql
id UUID PK
portfolio_id UUID FK
ticker VARCHAR
transaction_type VARCHAR
quantity NUMERIC
price NUMERIC
transaction_date TIMESTAMP
```

---

## portfolio_snapshots

Historical portfolio value.

```sql
id UUID PK
portfolio_id UUID FK
snapshot_date DATE
total_value NUMERIC
cash_balance NUMERIC
```

---

## watchlists

```sql
id UUID PK
owner_id UUID FK
name VARCHAR
```

---

## watchlist_items

```sql
id UUID PK
watchlist_id UUID FK
ticker VARCHAR
```

---

# 7. RESEARCH DOMAIN

## research_notes

```sql
id UUID PK
author_id UUID FK
organization_id UUID FK
title VARCHAR
content TEXT
```

---

## research_comments

```sql
id UUID PK
note_id UUID FK
author_id UUID FK
content TEXT
```

---

## research_tags

```sql
id UUID PK
name VARCHAR UNIQUE
```

---

## note_tags

```sql
note_id UUID FK
tag_id UUID FK
```

---

# 8. DOCUMENT DOMAIN

## documents

```sql
id UUID PK
owner_id UUID FK
organization_id UUID FK
filename VARCHAR
document_type VARCHAR
storage_url TEXT
status VARCHAR
uploaded_at TIMESTAMP
```

---

## document_chunks

```sql
id UUID PK
document_id UUID FK
chunk_index INTEGER
token_count INTEGER
qdrant_point_id VARCHAR
```

---

## document_processing_jobs

Tracks ingestion jobs.

```sql
id UUID PK
document_id UUID FK
status VARCHAR
started_at TIMESTAMP
completed_at TIMESTAMP
error_message TEXT
```

---

# 9. RAG DOMAIN

## rag_collections

Logical vector collections.

```sql
id UUID PK
name VARCHAR
description TEXT
created_at TIMESTAMP
```

---

## rag_documents

```sql
id UUID PK
collection_id UUID FK
document_id UUID FK
```

---

## embedding_jobs

```sql
id UUID PK
document_id UUID FK
embedding_model VARCHAR
status VARCHAR
created_at TIMESTAMP
```

---

## retrieval_logs

Tracks retrieval quality.

```sql
id UUID PK
analysis_id UUID FK
query TEXT
top_k INTEGER
retrieved_count INTEGER
```

---

# 10. AI ANALYSIS DOMAIN

## analyses

```sql
id UUID PK
user_id UUID FK
ticker VARCHAR
status VARCHAR
created_at TIMESTAMP
```

---

## analysis_sources

Tracks evidence.

```sql
id UUID PK
analysis_id UUID FK
source_type VARCHAR
source_reference TEXT
```

---

## analysis_citations

```sql
id UUID PK
analysis_id UUID FK
document_id UUID FK
chunk_id UUID FK
citation_text TEXT
```

---

## recommendations

```sql
id UUID PK
analysis_id UUID FK
recommendation VARCHAR
confidence NUMERIC
reasoning TEXT
```

---

## reflections

```sql
id UUID PK
analysis_id UUID FK
verified BOOLEAN
issues JSONB
```

---

# 11. AGENT DOMAIN

## agents

Master registry.

```sql
id UUID PK
name VARCHAR
description TEXT
version VARCHAR
active BOOLEAN
```

---

Examples:

```text
Market Agent

News Agent

Risk Agent

Bull Agent

Bear Agent

Judge Agent

Reflection Agent
```

---

## agent_runs

One execution instance.

```sql
id UUID PK
analysis_id UUID FK
agent_id UUID FK
status VARCHAR
started_at TIMESTAMP
completed_at TIMESTAMP
duration_ms INTEGER
```

---

## agent_results

```sql
id UUID PK
agent_run_id UUID FK
score NUMERIC
summary TEXT
output JSONB
```

---

## agent_steps

LangGraph visibility.

```sql
id UUID PK
agent_run_id UUID FK
step_name VARCHAR
input_data JSONB
output_data JSONB
duration_ms INTEGER
```

---

## agent_dependencies

```sql
parent_agent_id UUID FK
child_agent_id UUID FK
```

---

# 12. AI MEMORY DOMAIN

## agent_memories

Long-term memory.

```sql
id UUID PK
agent_id UUID FK
memory_type VARCHAR
content TEXT
created_at TIMESTAMP
```

---

## memory_references

```sql
id UUID PK
memory_id UUID FK
analysis_id UUID FK
```

---

## episodic_memories

```sql
id UUID PK
user_id UUID FK
event_type VARCHAR
content TEXT
```

---

# 13. PROMPT MANAGEMENT DOMAIN

## prompt_templates

```sql
id UUID PK
name VARCHAR
version VARCHAR
prompt TEXT
created_at TIMESTAMP
```

---

## prompt_executions

```sql
id UUID PK
prompt_id UUID FK
analysis_id UUID FK
model_used VARCHAR
token_count INTEGER
```

---

## prompt_versions

```sql
id UUID PK
prompt_id UUID FK
version VARCHAR
content TEXT
```

---

# 14. DEBATE DOMAIN

## debate_sessions

```sql
id UUID PK
analysis_id UUID FK
created_at TIMESTAMP
```

---

## debate_arguments

```sql
id UUID PK
debate_session_id UUID FK
side VARCHAR
content TEXT
```

---

# 15. DECISION TRACKING DOMAIN

## investment_decisions

```sql
id UUID PK
user_id UUID FK
recommendation_id UUID FK
action_taken VARCHAR
created_at TIMESTAMP
```

---

## decision_outcomes

```sql
id UUID PK

decision_id UUID FK

evaluation_days INTEGER

strategy_return NUMERIC

benchmark_return NUMERIC

alpha NUMERIC

outperformed BOOLEAN

evaluated_at TIMESTAMP
```

---

## decision_reviews

AI self-evaluation.

```sql
id UUID PK
decision_id UUID FK
review_text TEXT
lessons_learned TEXT
```

---

# 16. BACKTEST DOMAIN

## backtests

```sql
id UUID PK

user_id UUID FK

strategy_name VARCHAR

benchmark_symbol VARCHAR

start_date DATE

end_date DATE

initial_capital NUMERIC

ending_capital NUMERIC

strategy_return NUMERIC

benchmark_return NUMERIC

created_at TIMESTAMP
```

---

## backtest_positions

```sql
id UUID PK
backtest_id UUID FK
ticker VARCHAR
quantity NUMERIC
entry_price NUMERIC
```

---

## backtest_trades

```sql
id UUID PK
backtest_id UUID FK
ticker VARCHAR
side VARCHAR
quantity NUMERIC
price NUMERIC
trade_date TIMESTAMP
```

---

## backtest_metrics

```sql
id UUID PK
backtest_id UUID FK
sharpe_ratio NUMERIC
sortino_ratio NUMERIC
drawdown NUMERIC
win_rate NUMERIC
```

# 16.5 BENCHMARK DOMAIN

## benchmarks

Supported benchmark indices.

```sql
id UUID PK

symbol VARCHAR UNIQUE

name VARCHAR

asset_type VARCHAR

currency VARCHAR

description TEXT

created_at TIMESTAMP
```

Example

```text
SPY

QQQ

DIA

VT
```

---

## benchmark_results

Stores benchmark comparison results.

```sql
id UUID PK

backtest_id UUID FK

benchmark_id UUID FK

strategy_return NUMERIC

benchmark_return NUMERIC

alpha NUMERIC

beta NUMERIC

information_ratio NUMERIC

tracking_error NUMERIC

outperformed BOOLEAN

created_at TIMESTAMP
```

---

# 17. ANALYTICS DOMAIN

## portfolio_metrics

```sql
id UUID PK
portfolio_id UUID FK
sharpe_ratio NUMERIC
sortino_ratio NUMERIC
drawdown NUMERIC
```

---

## recommendation_metrics

```sql
id UUID PK
recommendation_id UUID FK
accuracy_score NUMERIC
```

---

## agent_metrics

```sql
id UUID PK
agent_id UUID FK
avg_score NUMERIC
success_rate NUMERIC
```

---

## retrieval_metrics

```sql
id UUID PK
analysis_id UUID FK
precision_score NUMERIC
recall_score NUMERIC
```

## portfolio_evaluations

```sql
id UUID PK

portfolio_id UUID FK

benchmark_symbol VARCHAR

portfolio_return NUMERIC

benchmark_return NUMERIC

alpha NUMERIC

beta NUMERIC

sharpe_ratio NUMERIC

sortino_ratio NUMERIC

maximum_drawdown NUMERIC

created_at TIMESTAMP
```

---

# 18. MODEL ROUTING DOMAIN

## model_providers

```sql
id UUID PK
name VARCHAR
provider VARCHAR
cost_per_1k_tokens NUMERIC
active BOOLEAN
```

---

## model_usage_logs

```sql
id UUID PK
provider_id UUID FK
analysis_id UUID FK
tokens_used INTEGER
cost NUMERIC
created_at TIMESTAMP
```

---

## cost_tracking

```sql
id UUID PK
provider_id UUID FK
daily_cost NUMERIC
monthly_cost NUMERIC
```

---

# 19. NOTIFICATION DOMAIN

## notifications

```sql
id UUID PK
user_id UUID FK
type VARCHAR
title VARCHAR
content TEXT
read_at TIMESTAMP
```

---

## websocket_connections

```sql
id UUID PK
user_id UUID FK
connection_id VARCHAR
connected_at TIMESTAMP
```

---

# 20. SYSTEM DOMAIN

## system_events

```sql
id UUID PK
event_type VARCHAR
payload JSONB
created_at TIMESTAMP
```

---

## feature_flags

```sql
id UUID PK
flag_name VARCHAR
enabled BOOLEAN
```

---

## service_health

```sql
id UUID PK
service_name VARCHAR
status VARCHAR
last_check TIMESTAMP
```

---

# 21. AUDIT DOMAIN

## audit_logs

```sql
id UUID PK
actor_id UUID FK
action VARCHAR
entity_type VARCHAR
entity_id UUID
metadata JSONB
created_at TIMESTAMP
```

---

# 22. Database Statistics

## Table Count

```text
57 Tables
```

---

## Domains

```text
18 Domains
```

---

## Estimated Relationships

```text
120+
```

---

## AI-Specific Tables

```text
agents

agent_runs

agent_results

agent_steps

agent_memories

prompt_templates

prompt_versions

analysis_sources

analysis_citations

model_usage_logs

cost_tracking
```

---

## RAG-Specific Tables

```text
documents

document_chunks

embedding_jobs

rag_collections

rag_documents

retrieval_logs
```

---

## Evaluation-Specific Tables

```text
investment_decisions

decision_outcomes

decision_reviews

backtests

backtest_positions

backtest_trades

backtest_metrics
```

---

# 23. Architecture Compatibility

This schema fully supports:

✅ FastAPI

✅ SQLAlchemy

✅ PostgreSQL

✅ Redis

✅ Qdrant

✅ LangGraph

✅ Multi-Agent Workflows

✅ RAG

✅ WebSocket

✅ OAuth

✅ 2FA

✅ Microservices

✅ AI Evaluation Framework

✅ 42 ft_transcendence Bonus Architecture

✅ Benchmark Evaluation

✅ Portfolio Attribution

---