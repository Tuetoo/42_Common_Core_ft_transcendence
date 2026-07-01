# AlphaMind
## AI-Powered Investment Research Platform
### ft_transcendence Final Project Proposal

---

# Executive Summary

AlphaMind is an AI-powered investment research and portfolio intelligence platform built as our ft_transcendence final project.

Unlike traditional stock analysis tools that rely on static indicators or a single AI response, AlphaMind combines:

- Multi-Agent AI
- Retrieval-Augmented Generation (RAG)
- Portfolio Analytics
- Investment Research Management
- Backtesting
- Real-Time Collaboration
- Enterprise Monitoring

into one unified system.

The project is designed with two goals:

1. Achieve a top evaluation score in ft_transcendence (target: 26–31 points).
2. Build a platform that remains genuinely useful after the project is completed.

This is not intended to be a "school-only" project. The architecture is designed so it can continue evolving into a real-world investment research platform.

---

# The Problem

Retail investors today face several challenges:

- Information is scattered across many sources.
- Research reports are difficult to organize.
- News, filings, and market data are overwhelming.
- Most AI tools provide unstructured answers.
- Very few systems track whether previous recommendations were actually correct.

Current AI assistants can answer questions, but they usually:

- lack long-term memory,
- lack structured investment reasoning,
- do not compare opposing viewpoints,
- do not evaluate their own performance.

---

# Our Vision

AlphaMind acts as an AI investment research analyst.

Instead of asking:

> "Should I buy NVIDIA?"

and receiving one AI-generated answer,

the system performs a structured analysis:

1. Gather information.
2. Retrieve relevant documents.
3. Analyze multiple dimensions.
4. Debate bullish and bearish viewpoints.
5. Evaluate evidence.
6. Produce a recommendation.
7. Track future outcomes.

The result is a more transparent and explainable decision-making process.

---

# Core Features

## Portfolio Management

Users can:

- Create portfolios
- Track holdings
- Record transactions
- Create watchlists
- Analyze allocations
- Monitor risk exposure

---

## Research Workspace

Users can:

- Upload research documents
- Upload company filings
- Organize notes
- Tag research
- Search knowledge bases

Supported formats:

- PDF
- Markdown
- Text

---

## RAG Knowledge System

AlphaMind includes a Retrieval-Augmented Generation pipeline.

Documents are:

```text
Upload

↓

Chunking

↓

Embedding

↓

Vector Storage

↓

Retrieval

↓

AI Analysis
```

This allows the AI to answer questions using actual documents rather than relying only on model memory.

---

## Multi-Agent AI

Instead of a single LLM, AlphaMind uses multiple specialized agents.

Examples:

### Market Agent

Analyzes price behavior and technical indicators.

### News Agent

Analyzes financial news.

### Sentiment Agent

Measures public sentiment.

### Filing Agent

Reviews company filings.

### Risk Agent

Identifies investment risks.

### Fundamental Agent

Evaluates company quality.

---

## AI Debate System

One of the most interesting features.

Two agents independently generate:

### Bull Case

Why the investment may succeed.

### Bear Case

Why the investment may fail.

A Judge Agent then evaluates both sides.

This creates explainable investment reasoning.

---

## Reflection Layer

Before producing a final recommendation:

The system performs a self-review process.

Checks include:

- Source quality
- Evidence sufficiency
- Risk coverage
- Confidence scoring

This helps reduce hallucinations.

---

## Recommendation Engine

Final outputs:

```text
BUY

HOLD

SELL
```

with:

- confidence score
- evidence
- sources
- supporting arguments

---

## Backtesting Engine

Recommendations can be evaluated historically.

Metrics include:

- Return
- Sharpe Ratio
- Drawdown
- Win Rate

This allows us to measure whether the system actually works.

---

## Decision Tracking

Every recommendation is stored.

Later we compare:

```text
Recommendation

↓

User Action

↓

Market Outcome
```

This creates a feedback loop that most AI systems do not have.

---

# Technical Architecture

## Frontend

Technology:

```text
Next.js
TypeScript
TailwindCSS
```

Responsibilities:

- Dashboard
- Portfolio UI
- Research UI
- Analytics UI
- Real-time updates

---

## Backend

Technology:

```text
FastAPI
Python
```

Responsibilities:

- REST APIs
- Authentication
- AI orchestration
- Portfolio management
- Analytics

---

## Databases

### PostgreSQL

Stores:

- Users
- Portfolios
- Transactions
- Organizations
- Research metadata
- Agent outputs

### Redis

Stores:

- Cache
- Sessions
- Rate limiting
- Temporary state

### Qdrant

Stores:

- Vector embeddings
- Semantic search data
- RAG knowledge

---

## AI Layer

Technology:

```text
LangGraph
LangChain
OpenAI
Qdrant
```

Responsibilities:

- Agent orchestration
- Workflow management
- State management
- AI reasoning

---

# High-Level Architecture

```text
Frontend (Next.js)

          │

          ▼

     API Gateway

          │

 ┌────────┼────────┐

 ▼        ▼        ▼

Auth   Portfolio   Research

Service Service    Service

          │

          ▼

      AI Service

          │

          ▼

   Analytics Service

          │

 ┌────────┼────────┐

 ▼        ▼        ▼

Postgres Redis   Qdrant
```

---

# Multi-Agent Architecture

```text
User

↓

Supervisor Agent

↓

Market Agent
News Agent
Sentiment Agent
Filing Agent

↓

Technical Agent
Fundamental Agent
Risk Agent
Macro Agent

↓

Bull Agent
Bear Agent

↓

Judge Agent

↓

Reflection Agent

↓

Recommendation
```

---

# Why This Project Is Interesting

Most ft_transcendence projects are:

- chat systems,
- games,
- social platforms.

AlphaMind combines:

### Modern Software Engineering

- Microservices
- Monitoring
- CI/CD
- Docker

### Modern AI Engineering

- RAG
- Multi-Agent Systems
- Reflection
- Evaluation

### Real Product Value

The platform can continue being useful after graduation.

---

# Technology Stack

## Frontend

```text
Next.js
TypeScript
TailwindCSS
```

## Backend

```text
FastAPI
Python
SQLAlchemy
```

## Databases

```text
PostgreSQL
Redis
Qdrant
```

## AI

```text
LangGraph
LangChain
OpenAI
```

## Infrastructure

```text
Docker
Prometheus
Grafana
GitHub Actions
```

---

# Project Scope

Estimated size:

```text
50+ Database Tables

100+ API Endpoints

15+ Frontend Screens

19 AI Agents

6 Core Services
```

---

# Team Structure

We plan to build AlphaMind with 3 developers.

---

## Developer A
### Platform & Infrastructure Lead

Responsibilities:

- Authentication
- OAuth
- 2FA
- Database
- Organizations
- Docker
- CI/CD
- Monitoring

Estimated contribution:

```text
30%
```

---

## Developer B
### AI & Data Lead

Responsibilities:

- RAG
- LangGraph
- Agents
- Recommendation Engine
- Backtesting
- Analytics

Estimated contribution:

```text
35%
```

---

## Developer C
### Frontend & UX Lead

Responsibilities:

- Dashboard
- Portfolio UI
- Research UI
- Analytics UI
- WebSocket Integration

Estimated contribution:

```text
35%
```

---

# Development Timeline

## Week 1

Foundation

- Repository
- Docker
- Database
- FastAPI
- Next.js

---

## Week 2

Authentication

- Login
- OAuth
- 2FA
- Organizations

---

## Week 3

Portfolio + Research

- Portfolio system
- Research workspace
- File upload
- Semantic search

---

## Week 4

Multi-Agent AI

- Agent orchestration
- Debate system
- Recommendation engine

---

## Week 5

Evaluation System

- Backtesting
- Analytics
- Decision tracking

---

## Week 6

Hardening

- Testing
- Monitoring
- Performance
- Evaluation preparation

---

# Evaluation Strategy

We are not targeting the minimum passing score.

Goal:

```text
26+ Points
```

Stretch Goal:

```text
28–31 Points
```

---

# Main Evaluation Strengths

## Security

- OAuth
- JWT
- 2FA
- RBAC

---

## Backend

- FastAPI
- REST APIs
- WebSockets

---

## Database

- PostgreSQL
- Complex relational design

---

## Search

- Semantic Search
- Vector Search

---

## Infrastructure

- Docker
- Monitoring
- CI/CD

---

## AI Features

- RAG
- Multi-Agent System
- Reflection
- Debate Architecture

---

# Why Join This Project?

This project provides hands-on experience with:

### Backend Engineering

- FastAPI
- PostgreSQL
- Redis

### Frontend Engineering

- Next.js
- TypeScript

### AI Engineering

- LangGraph
- RAG
- Multi-Agent Systems

### System Design

- Microservices
- Monitoring
- Scalable Architecture

### Real Product Development

Unlike many student projects, AlphaMind is designed to remain useful after completion.

Team members will gain experience with technologies and patterns commonly used in modern AI startups and production systems.

---

# Success Criteria

By the end of the project we aim to deliver:

- A fully functional investment research platform
- A production-quality architecture
- A strong ft_transcendence evaluation result
- A system we can continue developing after graduation

---

# AlphaMind

### Build an AI that does not just give answers —
### it explains, evaluates, and learns from investment decisions.
