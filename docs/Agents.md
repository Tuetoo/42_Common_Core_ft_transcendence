# AlphaMind v2.3 Enterprise
# AGENTS.md

Version: 2.3

---

# 1. Overview

AlphaMind is built around a Multi-Agent Architecture.

Instead of asking a single LLM to perform all reasoning, AlphaMind decomposes investment research into specialized AI agents.

Each agent owns one responsibility.

Benefits:

```text
Higher Accuracy

Better Explainability

Parallel Processing

Lower Hallucination Rate

Independent Evaluation

Future Extensibility
```

---

# 2. Agent Architecture

```text
                         User Request

                               │

                               ▼

                    Supervisor Agent

                               │

        ┌──────────────────────┼──────────────────────┐

        ▼                      ▼                      ▼

    Data Layer           Analysis Layer          Debate Layer

        │                      │                      │

        ▼                      ▼                      ▼

 Market Agent         Technical Agent         Bull Agent

 News Agent           Fundamental Agent       Bear Agent

 Filing Agent         Risk Agent

 Sentiment Agent      Macro Agent

        │                      │

        └──────────────┬───────┘

                       ▼

                  Judge Agent

                       ▼

               Reflection Agent

                       ▼

                Recommendation

                       ▼

                     User
```

---

# 3. LangGraph Workflow

```text
START

↓

Supervisor Agent

↓

Parallel Data Collection

├── Market Agent
├── News Agent
├── Sentiment Agent
├── Filing Agent

↓

Parallel Analysis

├── Technical Agent
├── Fundamental Agent
├── Risk Agent
├── Macro Agent

↓

Debate Layer

├── Bull Agent
├── Bear Agent

↓

Judge Agent

↓

Reflection Agent

↓

Recommendation Agent

↓

Backtest Agent

↓

Benchmark Evaluation

↓

Portfolio Evaluation

↓

END
```

The workflow is executed asynchronously by LangGraph.

Independent agents execute concurrently using asyncio whenever possible.

The frontend monitors workflow progress through Workflow APIs and WebSocket events.
---

# 4. Agent Execution Model

## Async Execution

Agents execute concurrently whenever possible.

Example:

```python
await asyncio.gather(
    market_agent(),
    news_agent(),
    sentiment_agent(),
    filing_agent()
)
```

---

## Agent Lifecycle

```text
Queued

↓

Running

↓

Completed

↓

Stored

↓

Evaluated
```

---

# 5. Agent Base Contract

Every agent implements the same asynchronous interface.

```python
class BaseAgent:

    async def run(
        self,
        state: AnalysisState,
    ) -> AnalysisState:
        ...
```

---

## Shared State

All agents communicate exclusively through a shared **Pydantic v2** state model.

Example

```python
class AnalysisState(BaseModel):

    ticker: str

    market: MarketAnalysis | None

    news: NewsAnalysis | None

    sentiment: SentimentAnalysis | None

    technical: TechnicalAnalysis | None

    recommendation: Recommendation | None
```

No agent should exchange anonymous dictionaries directly.

---

Every agent returns a typed Pydantic model.

Example

```python
class AgentResult(BaseModel):

    score: float

    confidence: float

    summary: str

    sources: list[str]
```


```text
Concrete locations (see PROJECT_STRUCTURE.md):

    BaseAgent        -> ai/agents/base.py
    AnalysisState     -> ai/graph/state.py (the LangGraph shared state
                          object every node reads/writes)
    AgentResult        -> ai/schemas/outputs.py
    Final analysis     -> ai/schemas/analysis.py (AnalysisRead) — the
      entity/status                run/status entity; distinct from AgentResult,
                                    which is one agent's individual output
    Recommendation      -> ai/schemas/recommendation.py
    What other domains  -> ai/schemas/refs.py (AnalysisRef,
      may reference          RecommendationRef) — domains/notification and
                              domains/portfolio embed these; they never
                              import ai/graph/state.py or ai/schemas/outputs.py
                              directly (see ALIGNMENT_README.md §5 rule 5:
                              dependencies only point downward).
```

---

# 6. Supervisor Agent

## Purpose

Orchestrates the entire workflow.

---

## Responsibilities

### Task Routing

Determines which agents should execute.

---

### State Management

Maintains LangGraph state.

---

### Failure Handling

Retries failed agents.

---

### Dependency Resolution

Ensures correct execution order.

---

## Input

```json
{
  "ticker":"NVDA"
}
```

---

## Output

```json
{
  "analysis_id":"uuid"
}
```

### Workflow Management

Creates and manages LangGraph workflows.

---

### Workflow Streaming

Publishes workflow progress through WebSocket events.

---

### Parallel Scheduling

Schedules independent agents concurrently using asyncio.

---

# 7. Market Agent

## Purpose

Analyze price action and market behavior.

---

## Data Sources

```text
Historical Prices

Volume Data

Technical Indicators
```

---

## Metrics

```text
RSI

MACD

EMA

SMA

ATR

Volume Trend
```

---

## Output Example

```json
{
  "score":82,
  "trend":"bullish",
  "summary":"Strong momentum"
}
```

---

# 8. News Agent

## Purpose

Analyze recent financial news.

---

## Sources

```text
Reuters

Bloomberg

Yahoo Finance

Financial Times
```

---

## Tasks

```text
Summarization

Sentiment Extraction

Event Detection

Impact Scoring
```

---

## Output

```json
{
  "score":74,
  "summary":"Positive earnings coverage"
}
```

---

# 9. Sentiment Agent

## Purpose

Measure crowd sentiment.

---

## Sources

```text
Reddit

X

Stocktwits
```

---

## Metrics

```text
Bullish %

Bearish %

Trend Momentum
```

---

## Output

```json
{
  "score":69,
  "summary":"Retail sentiment improving"
}
```

---

# 10. Filing Agent

## Purpose

Analyze SEC filings.

---

## Sources

```text
10-K

10-Q

8-K

Earnings Calls
```

---

## Tasks

```text
Risk Detection

Growth Detection

Management Commentary

Financial Trends
```

---

## Output

```json
{
  "score":81,
  "summary":"Revenue growth remains strong"
}
```

---

# 11. Technical Agent

## Purpose

Convert market signals into investment signals.

---

## Inputs

```text
Market Agent

Historical Data
```

---

## Responsibilities

```text
Trend Classification

Momentum Analysis

Support/Resistance

Signal Generation
```

---

## Output

```json
{
  "score":78,
  "signal":"BUY"
}
```

---

# 12. Fundamental Agent

## Purpose

Evaluate company quality.

---

## Inputs

```text
Financial Statements

Filing Analysis
```

---

## Metrics

```text
Revenue Growth

EPS Growth

Margins

Debt

Cash Flow
```

---

## Output

```json
{
  "score":85,
  "summary":"Strong fundamentals"
}
```

---

# 13. Risk Agent

## Purpose

Evaluate downside risk.

---

## Metrics

```text
Volatility

Drawdown

Beta

Sector Risk

Concentration Risk
```

---

## Output

```json
{
  "score":35,
  "risk":"moderate"
}
```

---

# 14. Macro Agent

## Purpose

Analyze macroeconomic environment.

---

## Sources

```text
Interest Rates

Inflation

GDP

Unemployment
```

---

## Output

```json
{
  "score":65,
  "summary":"Macro environment neutral"
}
```

---

# 15. Bull Agent

## Purpose

Generate strongest bullish argument.

---

## Question

```text
Why should we invest?
```

---

## Responsibilities

```text
Growth Drivers

Catalysts

Competitive Advantages

Future Potential
```

---

## Output

```json
{
  "argument":"AI demand supports future growth"
}
```

---

# 16. Bear Agent

## Purpose

Generate strongest bearish argument.

---

## Question

```text
Why should we avoid this investment?
```

---

## Responsibilities

```text
Valuation Risks

Competition

Execution Risk

Macroeconomic Risks
```

---

## Output

```json
{
  "argument":"Valuation significantly above sector average"
}
```

---

# 17. Judge Agent

## Purpose

Evaluate Bull vs Bear debate.

---

## Inputs

```text
Bull Argument

Bear Argument

Supporting Evidence
```

---

## Responsibilities

```text
Weight Evidence

Determine Winner

Calculate Confidence
```

---

## Output

```json
{
  "winner":"bull",
  "confidence":82
}
```

---

# 18. Reflection Agent

## Purpose

Final quality control layer.

---

## Questions

### Check 1

Are sources sufficient?

---

### Check 2

Are risks acknowledged?

---

### Check 3

Do agents disagree?

---

### Check 4

Is evidence recent?

---

## Output

```json
{
  "verified":true,
  "issues":[]
}
```

---

# 19. Recommendation Agent

## Purpose

Generate final recommendation.

---

## Inputs

```text
Technical Score

Fundamental Score

News Score

Sentiment Score

Risk Score

Macro Score

Judge Result
```

## Output

```json
{
  "recommendation":"BUY",
  "confidence":0.91,
  "investment_horizon":"LONG_TERM",
  "risk_level":"MEDIUM"
}
```

---

## Formula

```text
Recommendation Score

=

0.20 Technical

+

0.25 Fundamental

+

0.15 News

+

0.15 Sentiment

+

0.10 Macro

-

0.15 Risk
```

---

## Thresholds

### BUY

```text
Score > 75
```

---

### HOLD

```text
50 <= Score <= 75
```

---

### SELL

```text
Score < 50
```

---

# 20. RAG Retrieval Agent

## Purpose

Retrieve supporting context.

---

## Sources

```text
Research Notes

SEC Filings

Uploaded PDFs

Previous Analyses
```

---

## Retrieval Pipeline

```text
Query

↓

Embedding

↓

Qdrant Search

↓

Metadata Filtering

↓

Re-ranking

↓

Top-K Results
```

---

## Output

```json
{
  "documents":[]
}
```

---

# 21. Citation Agent

## Purpose

Generate evidence references.

---

## Responsibilities

```text
Document Citations

Page References

Source Validation
```

---

## Output

```json
{
  "citations":[]
}
```

---

# 22. Portfolio Evaluation Agent

## Purpose

Analyze portfolio quality.

---

## Metrics

```text
Diversification

Risk Exposure

Sector Allocation

Drawdown
```

---

## Output

```json
{
  "portfolio_score":81
}
```

## Additional Metrics

```text
Benchmark Comparison

Portfolio Attribution

Risk-adjusted Performance
```

---

# 23. Decision Review Agent

## Purpose

Evaluate historical decisions.

---

## Inputs

```text
Decision

Strategy Return

Benchmark Return

Alpha

Investment Thesis
```

---

## Output

```json
{
  "correct":true,
  "alpha":4.8,
  "outperformed":true,
  "lesson":"Investment thesis validated"
}
```

---

# 24. Backtest Agent

## Purpose

Execute historical simulations and evaluate AlphaMind investment recommendations.

---

## Responsibilities

```text
Trade Generation

Portfolio Simulation

Performance Calculation

Benchmark Comparison

Portfolio Attribution
```

---

## Default Benchmark

```text
SPY (S&P500 ETF)
```

Optional

```text
QQQ

DIA
```

---

## Performance Metrics

```text
Strategy Return

Benchmark Return

Alpha

Beta

Sharpe Ratio

Sortino Ratio

Maximum Drawdown

Win Rate
```

---

## Output

```json
{
  "strategy_return":18.4,
  "benchmark_return":13.2,
  "alpha":5.2,
  "beta":1.01,
  "sharpe_ratio":1.82,
  "outperformed":true
}
```
---

## Output

```json
{
  "return":42.8,
  "sharpe":1.8
}
```

---

# 25. Memory Agent

## Purpose

Manage long-term AI memory.

---

## Storage

```text
Qdrant

Redis
```

---

## Memory Types

### Episodic

Past decisions.

---

### Semantic

Financial knowledge.

---

### Analytical

Past recommendations.

---

# 26. Agent Evaluation Framework

Every agent is measured.

---

## Metrics

### Accuracy

```text
Correct Outputs
/
Total Outputs
```

---

### Latency

```text
Average Response Time
```

---

### Reliability

```text
Successful Runs
/
Total Runs
```

---

### Contribution

```text
Impact On Final Recommendation
```

### Benchmark Performance

```text
Average Alpha

Average Excess Return

Benchmark Outperformance Rate
```

---

# 27. Agent Registry

Stored in database.

---

Example:

```json
{
  "name":"Market Agent",
  "version":"1.0.0",
  "active":true
}
```

---

# 28. Future Agents (v3)

Not required for AlphaMind v2.

---

## Strategy Agent

Generate investment strategies.

---

## ETF Agent

Analyze ETFs.

---

## Crypto Agent

Analyze cryptocurrencies.

---

## Earnings Agent

Analyze earnings reports.

---

## Autonomous Research Agent

Continuously monitor companies.

---

## Alert Agent

Generate proactive alerts.

---

# 29. Agent Communication Protocol

Agents never directly communicate.

All communication goes through:

```text
LangGraph State
```

---

Example

```text
Market Agent

↓

Update State

↓

Fundamental Agent

↓

Read State
```

---

Benefits

```text
Loose Coupling

Traceability

Replayability

Testing
```

Agents never invoke other agents directly.

All execution is orchestrated by LangGraph through a shared workflow state.

Workflow progress is exposed externally through the Workflow API layer while agent communication remains internal to the orchestration engine.

---

# 30. Final Agent Architecture

# 30. Final Agent Architecture

```text
                    Workflow API

                         │

                         ▼

                 Supervisor Agent

                         │

        ┌────────────────┼────────────────┐

        ▼                ▼                ▼

   Data Agents     Analysis Agents   Debate Agents

        │                │                │

        └────────────────┴────────────────┘

                         ▼

                 Reflection Agent

                         ▼

               Recommendation Agent

                         ▼

                  Backtest Agent

                         ▼

              Benchmark Evaluation

                         ▼

          Portfolio Evaluation Agent

                         ▼

                     REST API
```

---

# Agent Statistics

## Total Agents

```text
19
```

---

## Parallel Agents

```text
Market

News

Sentiment

Filing

Technical

Fundamental

Risk

Macro
```

---

## Evaluation Agents

```text
Judge

Reflection

Decision Review
```

---

## Knowledge Agents

```text
RAG Retrieval

Citation

Memory
```

---

## Future Compatibility

✅ LangGraph

✅ FastAPI

✅ AsyncIO

✅ PostgreSQL

✅ Redis

✅ Qdrant

✅ Multi-Agent

✅ RAG

✅ Reflection

✅ Debate Architecture

✅ Workflow API

✅ Pydantic v2

✅ Benchmark Evaluation

✅ Portfolio Attribution

---

# END OF AGENTS.md
# Version 2.3 Enterprise