# AlphaMind v2.3 Enterprise
# MODULE_MAPPING.md

Version: 2.3

---

# 1. Purpose

This document maps:

```text
AlphaMind Features

↓

Technical Components

↓

42 Transcendence Modules

↓

Expected Evaluation Points
```

Its purpose is to ensure:

- No duplicated work
- Maximum point coverage
- Clear ownership
- Easier evaluation preparation

---

# 2. Point Strategy

## Minimum Goal

```text
21 Points
```

Pass safely.

---

## Recommended Goal

```text
24~26 Points
```

Strong project.

---

## Stretch Goal

```text
28~31 Points
```

Top-tier evaluation.

---

# 3. Architecture Overview

```text
Frontend (Next.js)

↓

REST API Layer

↓

Workflow API Layer

↓

Microservices

├── Auth Service
├── Portfolio Service
├── Research Service
├── AI Service
├── Analytics Service

↓

LangGraph Workflow Engine

↓

PostgreSQL

Redis

Qdrant
```

The REST API layer manages business resources.

The Workflow API layer orchestrates asynchronous AI analysis using LangGraph.

---

# 4. Mandatory Part Mapping

---

# Module M1
# Web Framework

Target Points:

```text
2
```

Implementation:

```text
FastAPI
```

---

Responsibilities

```text
REST APIs

Workflow APIs

Dependency Injection

Pydantic Validation

Swagger Documentation
```

---

Endpoints

```text
120+
```

---

Complexity

```text
Medium
```

---

Evaluator Demo

```text
Swagger

OpenAPI

Automatic Docs
```

---

# Module M2
# Frontend Framework

Target Points:

```text
2
```

Implementation:

```text
Next.js
```

---

Responsibilities

```text
Dashboard

Portfolio UI

Research UI

AI UI

Backtest UI
```

---

Pages

```text
Dashboard

Portfolio

Research

Analysis

Organizations

Settings
```

---

Expected Screens

```text
15+
```

---

# Module M3
# Database

Target Points:

```text
2
```

Implementation

```text
PostgreSQL
```

---

Tables

```text
56+
```

---

Domains

```text
17
```

---

Features

```text
Relations

Constraints

Indexes

Transactions
```

---

# Module M4
# ORM

Target Points

```text
1
```

Implementation

```text
SQLAlchemy 2.0
```

---

Features

```text
Typed Models

Alembic Migrations

Async ORM
```

---

# Module M5
# User Management

Target Points

```text
2
```

Implementation

```text
JWT

RBAC

Profile System
```

---

Features

```text
Register

Login

Logout

Profile

Permissions
```

---

# Module M6
# OAuth

Target Points

```text
1
```

Implementation

```text
Google OAuth

GitHub OAuth
```

---

Flow

```text
OAuth Login

↓

Callback

↓

User Creation

↓

JWT
```

---

# Module M7
# Two Factor Authentication

Target Points

```text
1
```

Implementation

```text
TOTP
```

---

Applications

```text
Google Authenticator

Authy
```

---

# Module M8
# REST & Workflow APIs

Target Points

```text
2
```

Implementation

```text
FastAPI

REST API Layer

Workflow API Layer
```

---

Coverage

```text
Auth

Portfolio

Research

Analysis

Analytics

Workflow
```

---

Endpoints

```text
REST APIs

120+

Workflow APIs

10+
```

---

# Module M9
# Real-Time

Target Points

```text
2
```

Implementation

```text
WebSocket
```

---

Channels

```text
Portfolio

Organization

Analysis

Notifications
```

---

Events

```text
Workflow Started

Analysis Progress

Agent Started

Agent Finished

Workflow Completed

Document Indexed

Notification Created
```
---

# Module M10
# File Upload

Target Points

```text
1
```

Implementation

```text
Research Document Upload
```

---

Supported

```text
PDF

TXT

Markdown
```

---

Pipeline

```text
Upload

↓

Chunk

↓

Embed

↓

Store
```

---

# Module M11
# Search

Target Points

```text
1
```

Implementation

```text
PostgreSQL Full Text

Qdrant Vector Search
```

---

Features

```text
Research Search

Semantic Search

RAG Search
```

---

# Module M12
# Monitoring

Target Points

```text
2
```

Implementation

```text
Prometheus

Grafana

Loki
```

---

Metrics

```text
CPU

Memory

Latency

AI Cost

Agent Runtime
```

---

# Module M13
# Microservices

Target Points

```text
2
```

Implementation

```text
Auth Service

Portfolio Service

Research Service

AI Service

Analytics Service
```

---

Communication

```text
REST

Redis Queue
```

---

Containers

```text
10+
```

---

# Module M14
# Docker

Target Points

```text
1
```

Implementation

```text
Docker Compose
```

---

Services

```text
Frontend

Backend

Postgres

Redis

Qdrant
```

---

# Module M15
# CI/CD

Target Points

```text
1
```

Implementation

```text
GitHub Actions
```

---

Stages

```text
Lint

Test

Build

Deploy
```

---

# Engineering Standards

Implementation

```text
uv

Python 3.13

Pydantic v2

Ruff

Pytest
```

---

Purpose

```text
Unified Dependency Management

Typed Data Models

Consistent Code Style

Reliable Testing
```

---

Evaluation Impact

```text
Improves Maintainability

Improves Team Collaboration

Supports Production-quality Engineering
```

# 5. AlphaMind Core AI Modules

These are not directly required by ft_transcendence but dramatically improve evaluation quality.

---

# AI Module A1
# Multi-Agent System

Implementation

```text
LangGraph
```

---

Agents

```text
19 Agents
```

---

Includes

```text
Supervisor

Market

News

Sentiment

Risk

Bull

Bear

Judge

Reflection
```

---

Evaluation Impact

```text
Very High
```

---

Execution Model

```text
Workflow API

LangGraph

AsyncIO

Parallel Execution
```

---

# AI Module A2
# RAG

Implementation

```text
Qdrant
```

---

Pipeline

```text
Chunk

Embed

Retrieve

Re-rank

Cite
```

---

Features

```text
Document QA

Research Search

Source Citation
```

---

# AI Module A3
# Debate Engine

Implementation

```text
Bull Agent

Bear Agent

Judge Agent
```

---

Purpose

```text
Explainability
```

---

Evaluator Effect

```text
Excellent Demo Feature
```

---

# AI Module A4
# Reflection Engine

Implementation

```text
Reflection Agent
```

---

Purpose

```text
Reduce Hallucinations
```

---

Checks

```text
Source Quality

Logic

Confidence
```

---

# AI Module A5
# Portfolio Intelligence

Implementation

```text
Portfolio Evaluation Agent
```

---

Capabilities

```text
Diversification

Sector Exposure

Risk Analysis
```

---

# AI Module A6
# Decision Tracking

Implementation

```text
Decision Database
```

---

Purpose

```text
Track Recommendation Quality
```

---

Example

```text
AI says BUY

User buys

30 days later

Evaluate result
```

---

# AI Module A7
# Weekly Evaluation Engine

Implementation

```text
Backtest Agent
```

---

Purpose

```text
Measure AI Performance
```

---

Outputs

```text
Strategy Return

Benchmark Return (SPY)

Alpha

Beta

Sharpe Ratio

Sortino Ratio

Maximum Drawdown

Win Rate
```

---

# 6. Bonus Modules

---

# Bonus B1
# Organization Workspace

Features

```text
Shared Research

Shared Portfolio

Team Collaboration
```

---

Tables

```text
organizations

members

roles
```

---

# Bonus B2
# Audit System

Features

```text
Track All Critical Actions
```

---

Benefits

```text
Compliance

Debugging
```

---

# Bonus B3
# Prompt Management

Features

```text
Prompt Versioning

Prompt Evaluation
```

---

Used By

```text
AI Team
```

---

# Bonus B4
# AI Cost Tracking

Features

```text
Model Usage

Token Usage

Cost Monitoring
```

---

Useful For

```text
Production Deployment
```

---

# Bonus B5
# Feature Flags

Features

```text
Enable

Disable

Experimental Features
```

---

# 7. Point Estimation

## Conservative

Modules

```text
Web Framework

Frontend

Database

ORM

User Management

OAuth

2FA

REST API

Real-Time

File Upload

Search

Monitoring

Docker

CI/CD
```

---

Estimated

```text
22~24 Points
```

---

## Recommended

Add

```text
Microservices
```

---

Estimated

```text
24~26 Points
```

---

## Stretch

Add

```text
Advanced Monitoring

AI Analytics

Enterprise Deployment
```

---

Estimated

```text
28~31 Points
```

---

# 8. Team Ownership Mapping

---

## Developer A
## Platform Lead

Owns

```text
Auth Service

User System

Organizations

CI/CD

Docker
```

---

Expected Contribution

```text
30%
```

---

## Developer B
## AI Lead

Owns

Owns

```text
Workflow Engine

LangGraph

Agents

RAG

Recommendation Engine

Benchmark Engine

Backtesting
```

---

Expected Contribution

```text
35%
```

---

## Developer C
## Fullstack Lead

Owns

```text
Next.js

Dashboard

Portfolio UI

Analytics UI

WebSocket
```

---

Expected Contribution

```text
35%
```

---

# 9. Evaluation Demo Checklist

Evaluator should see:

---

## Authentication

```text
Register

Login

OAuth

2FA
```

---

## Portfolio

```text
Create Portfolio

Add Holdings

Track Performance
```

---

## Research

```text
Upload PDF

Search Documents

RAG Retrieval
```

---

## AI

```text
Run Workflow

Watch Agents Execute

See Debate

View Workflow Timeline

Receive Recommendation
```

---

## Analytics

```text
Workflow Dashboard

Accuracy Dashboard

Decision Tracking

Backtesting

Benchmark Comparison
```

---

## Infrastructure

```text
Swagger

Grafana

Docker

CI/CD
```

---

# 10. Final Expected Score

## Safe Pass

```text
22+
```

---

## Strong Evaluation

```text
26+
```

---

## Outstanding Project

```text
28~31+
```

---

# AlphaMind Target

```text
Primary Goal

26+ Points

Stretch Goal

30+ Points

Workflow-driven AI Platform

Enterprise-grade Architecture
```

while remaining:

```text
Technically Realistic

6 Week Timeline

3 Developers

1200~1800 Engineering Hours
```

---

# END OF MODULE_MAPPING.md
# Version 2.3 Enterprise