# Model comparison: Google Gemini vs Anthropic Claude in AlphaMind

This document compares the two LLM providers that ran AlphaMind's analysis pipeline in nine batch runs between 12 and 19 October 2026. Section 11 then measures Claude's prompt caching methods in six further 5-ticker runs on 20 October. It covers the models and settings, speed, errors, tokens, cost and the quality of the recommendations. Every number comes from the stored run files (logs, `result.json`, `_summary.txt`), from `services/ai-service/scripts/llm_usage_report.py` run on those logs, or from the written reviews of the runs. Where a number is a reviewer's judgement and not a script count, the text says so.

**Short answer.** Claude (Haiku 5.5 for the cheap tier, Sonnet 5.5 for the highest-accuracy tier) is the recommended default provider. In the fair head-to-head run it won on all 19 tickers. It made 135 research tool calls against Gemini's 7, made fewer factual errors, and finished the batch faster (1,270 s against 1,780 s). It had 0 provider errors, against 7 timeouts and 25 HTTP 503s for Gemini. It costs about $0.18 to $0.20 per analysis. Section 11 measures Claude's prompt caching: the default (system prompt and ReAct scratchpad cached) is estimated to cost about 12% less than no caching, and sending the debate evidence once (an experiment switch, off by default) saved a further 17% of the Sonnet cost per call in one 5-ticker run. Neither model's confidence number is well calibrated, and that is a prompt problem, not a model problem.

---

## 1. The runs

Each batch analyses the same list of US tickers one after another for the same portfolio. G1 has 16 tickers; all other runs add ORCL, COST and LLY, for 19.

| Label | Run folder | Provider | Code version | NewsAPI | Judge sees previous verdict? |
|---|---|---|---|---|---|
| G1 | `run_20261012_224551` | Gemini | old code (before the review fixes) | on | yes (earlier Gemini runs) |
| C1 | `run_20261018_162614` | Claude | before fixes | on | yes (G1's verdicts) |
| C2 | `run_20261018_192257` | Claude | batch-1 fixes; first ingestion of ORCL/COST/LLY filings | on | yes (C1) |
| G2 | `run_20261018_212454` | Gemini | batch 1+2 | on until the quota ran out at COST | yes (C2, a Claude run) |
| C3 | `run_20261018_220503` | Claude | batch 1+2 | quota exhausted (429 at the first call) | yes (C2) |
| G3 | `run_20261019_075127` | Gemini | batch 1-3 | key unset (web search only) | **no** (temporary Judge patch) |
| C4 | `run_20261019_083834` | Claude | batch 1-3, same as G3 | key unset | **no** (same patch) |
| C5 | `run_20261019_121556` | Claude | batch 1-4 | key unset | yes (C4) |
| C6 | `run_20261019_144819` | Claude | release candidate (batch 1-4 + simplification) | key on, 429 at the first call | yes (C5) |

**G3 and C4 are the fair head-to-head.** They use the same code, the same news conditions and the same Judge patch, and ran less than an hour apart on the same market data.

## 2. How the numbers were computed

- **Wall time per ticker** is the `elapsed=` value in `_summary.txt`. The batch script polls every 5 s, so values are multiples of 5. It stops at "completed" and does not include about 30 s of archiving and export that follow each analysis (C6 review E6). **Batch span** is the time from the first to the last log line across all ticker logs of the run.
- **Judge passes** = `debate_debug.judge_node.enter` events. **Reflection retry rounds** = `debate_debug.route_after_reflection` with `route = retry_agents`. **Correction turns** = `react_loop.finish_answer_validation_failed` (an agent's answer rejected by a code check and sent back once).
- **Errors** are counted from named log events (`google.request_timed_out`, `google.server_error_failing_over`, `google.quota_exceeded_failing_over`, `llm_router.falling_back_to_alternate_model`, `llm_router.skipping_known_exhausted_model`, `newsapi.request_failed`) and from the `HTTP Response: POST https://api.anthropic.com/v1/messages` lines for Claude.
- **Final verdict model (Gemini):** the model of the last highest-tier `google.completion_succeeded` between the last `judge_node.enter` and `judge_node.exit` of each analysis.
- **Tokens** are summed from `anthropic.completion_succeeded` and `google.completion_succeeded`. Only events whose `analysis_id` matches the ticker's id in `_summary.txt` are counted (no event fell outside).
- **Research tool calls** = entries in each agent's stored scratchpad (`result.json`) that have an action and are not marked `prefetched` (that is, calls the model chose itself, beyond what the code fetches first).
- **Confidence** = the `confidence` of the last `judge.deciding_factor` event (the Judge's value). The stored value is in `result.json`.
- Quality verdicts (winner per ticker, factual errors) come from the manual reviews: J1-J6 for C4, H1-H6 for G3, F1-F6 for C5, E1-E6 for C6.

## 3. Models and settings

Source: `services/ai-service/config.py`, `ai/llm/router.py`, `ai/llm/anthropic_provider.py`, `ai/llm/google_provider.py`.

| | Anthropic | Google | Mistral (not run here) |
|---|---|---|---|
| CHEAP tier | `claude-haiku-5-5` | `gemini-3.5-flash-lite` | `mistral-small-2603` |
| CHEAP backup | none | `gemini-3.1-flash-lite` | `ministral-8b-2512` |
| HIGHEST_ACCURACY tier | `claude-sonnet-5-5` | `gemini-3.8-flash` | `mistral-medium-latest` |
| HIGHEST backup | none | `gemini-3.6-flash` | `mistral-large-2512` |
| Thinking | Haiku `adaptive` (+2,048 tokens of headroom); Sonnet `between_tools`; effort `low` on both | 3.8-flash thinking off (budget 0); 3.6-flash and the lite models `MINIMAL`; a lite answer that hits MAX_TOKENS from runaway thinking is retried with a 1,024-token budget | - |
| Prompt caching | on (`cache_control: ephemeral`, 5-minute cache). Runs C2-C6 cached the system prompt only; the code now also caches the ReAct scratchpad by default (section 11) | not used | - |
| Structured output | JSON schema sent as `output_config.format` (`json_schema`); falls back to a reduced schema, then to plain JSON, if the API calls the schema too complex | `response_mime_type=application/json` with `response_json_schema` | - |
| Timeout | 120 s per request; SDK retries off (`max_retries=0`), retries are done by the router | 30 s on 3.8-flash, 60 s on the 3.6 backup; a timeout puts the model in a 300 s cooldown, a 503 in a 20-300 s cooldown | - |
| Pacing | none | min. 5 s (cheap) and 13.5 s (highest) between calls per key; at most 2 concurrent lite calls | 15 s / 22 s |

- **Embeddings:** both providers use Google `gemini-embedding-2` (1,536 dimensions). So a Google key is required even when Claude runs the analysis.
- **Backup models exist only inside one provider.** The router has no cross-provider fallback. Anthropic has no backup model configured.
- **Pipeline steps by tier:**
  - CHEAP: the nine data agents (market, technical, fundamental, filing, news, sentiment, macro, risk, portfolio) through the ReAct loop, news and search relevance classification, query expansion, knowledge-graph extraction, digests, user sentiment and RAG answers (`RAG_ASK_LLM_TIER`).
  - HIGHEST_ACCURACY: Bull and Bear, Judge, Reflection, position sizing (with a CHEAP retry if it fails) and filing summaries (`FILING_LLM_TIER`).
- **Provider choice:** `LLM_DEFAULT_PROVIDER` in `config.py:58` defaults to `google` (allowed values `google`, `mistral`, `anthropic` and `openai`; a blank value also means `google`, and any other value stops ai-service at startup). The Claude runs set it to `anthropic` through the environment.

## 4. Speed

| Run | Tickers | Sum of elapsed (s) | Median per ticker (s) | Max per ticker (s) | Batch span (min) | Judge passes | Reflection retry rounds | Correction turns |
|---|---|---|---|---|---|---|---|---|
| G1 | 16 | 1,580 | 90 | 150 (MSFT) | 32.0 | 17 | 1 | 4 |
| C1 | 19 | 1,270 | 65 | 100 (INTC, UNH) | 31.4 | 32 | 13 | 75 |
| C2 | 19 | 1,680 | 65 | 245 (LLY) | 39.2 | 29 | 10 | 63 |
| G2 | 19 | 1,610 | 80 | 105 (META) | 34.3 | 19 | 0 | 6 |
| C3 | 19 | 1,450 | 75 | 130 (LLY) | 34.2 | 40 | 21 | 48 |
| **G3** | 19 | **1,780** | **90** | 135 (COST) | **37.1** | 21 | 2 | 11 |
| **C4** | 19 | **1,270** | **65** | 140 (GOOGL) | **32.1** | 28 | 9 | 46 |
| C5 | 19 | 1,235 | 60 | 110 (AMD) | 31.2 | 29 | 10 | 50 |
| C6 | 19 | 1,385 | 75 | 110 (AAPL) | 32.6 | 31 | 12 | 44 |

- C2's slow ORCL, COST and LLY (185-245 s) include the one-time ingestion of their SEC filings.
- **Claude is faster although it does more work.** In the head-to-head, C4 needed 28 Judge passes and 46 correction turns against G3's 21 and 11, and was still 510 s faster. Per ticker (J6 §8, from log events), C4's research phase took 30.9 s against G3's 57.9 s. Gemini's lite calls are paced about 3 s apart and hit 429 key failovers, and its debate phase lost time to timeouts.
- Claude needs more correction turns because its agents write longer answers with more figures, which the code checks then reject more often. Gemini's agents make almost no tool calls of their own, so there is less to check.

## 5. Errors and reliability

### Gemini

| Event | G1 | G2 | G3 |
|---|---|---|---|
| Timeouts at 30 s (3.8-flash) | 3 | 1 | **7** |
| HTTP 503 "high demand", failed over to the next key | 16 | 4 | **25** |
| ... gave up after all attempts | 5 | 1 | 3 |
| HTTP 429 quota (lite, per minute), failed over | 10 (+1 gave up) | 0 | 5 |
| Calls skipped because a model was in cooldown | 34 | 10 | **64** |
| Falls back to the backup model | 42 | 12 | **71** |
| Judge waited 20 s and retried (`judge_node.delayed_retry`) | - | 0 | 3 |
| HIGHEST calls served by 3.8 / by the 3.6 backup | 31 / 40 | 67 / 12 | **17 / 68** |
| **Final Judge verdicts served by the backup model** | **10 / 16** | 3 / 19 | **15 / 19** |
| Judge JSON parsed leniently (extra text around it) | 3 | 1 | 4 |
| Other unparseable responses (all recovered by retry) | 1 (news relevance) | 1 (news relevance) | 0 |
| Thinking runaway (MAX_TOKENS), retried | 0 | 0 | 1 |
| Bear node failure, repaired by retry | 1 | 0 | 0 |

- In G3, only INTC, JPM, LLY and COST got a Judge on the primary 3.8-flash. When 3.8 did answer, it took a median of 15.0 s (H6 §2.1), close to the 30 s limit. One timeout put the model in a 300 s cooldown for all keys.
- At the time of G3, no recommendation told the user that a backup model had written it. Batch 4 added that note; it was only verified on Claude runs, where no switch happened (F6).

### Claude

| | C1 | C2 | C3 | C4 | C5 | C6 |
|---|---|---|---|---|---|---|
| Anthropic requests | 890 | 947 | 878 | 824 | 820 | 822 |
| HTTP 200 OK | 890 | 947 | 878 | 824 | 820 | 822 |
| 429 / 529 overloaded / timeouts / retries | 0 | 0 | 0 | 0 | 0 | 0 |
| Fallbacks to a backup model | 0 (none configured) | 0 | 0 | 0 | 0 | 0 |
| Unparseable or repaired responses | 7: position sizing 4, ReAct 1, search relevance 1, KG 1 | 3: ReAct 1, Judge 1 (re-asked, recovered), wrapped JSON 1 | 3: trailing comma 2, wrapped JSON 1 | 1 wrapped JSON | 0 | 1 wrapped JSON |

The lowest remaining rate-limit headroom seen in C4 and C6 was about 995 of 1,000 requests per minute (J6, E6), so the batches used under 3% of the limits.

### NewsAPI (not a model error, but it changed the runs)

| Run | NewsAPI state | HTTP 429 | Tickers on web-search fallback |
|---|---|---|---|
| G1, C1, C2 | working | 0 | 0 |
| G2 | quota ran out at COST | 1 | 2 |
| C3 | quota already used up | 1 (first call, then 1-hour cooldown) | 19 |
| G3, C4, C5 | key unset | 0 | 19 |
| C6 | quota already used up | 1 (first call) | 19 |

With the quota exhausted (C3, C6), the News agent still tried `search_news` 14 times in C6 and 11 news reports were flagged as unverified (E6). With no key (G3, C4), the unverified-source flag can never fire, so news quality signals are not comparable between those runs and C3/C6.

## 6. Tokens

Prompt tokens include cached tokens. For Claude, "output" is the visible completion; thinking tokens are listed separately (the adapter logs `completion_tokens` net of thinking, in `anthropic_provider.py::_log_success`). Gemini logs no cache fields.

| Run | Model (tier) | Calls | Prompt | Output | Thinking | Cache read (share) | Cache write |
|---|---|---|---|---|---|---|---|
| G3 | 3.5-flash-lite (CHEAP) | 440 | 1.73M | 120k | 10.2k | - | - |
| G3 | 3.6-flash (HIGHEST backup) | 68 | 0.44M | 28k | 0 | - | - |
| G3 | 3.8-flash (HIGHEST) | 17 | 0.11M | 8k | 0 | - | - |
| C4 | Haiku 5.5 (CHEAP) | 711 | 6.09M | 310k | 46.7k | 1.33M (22%) | 81k |
| C4 | Sonnet 5.5 (HIGHEST) | 113 | 1.24M | 88k | 0 | 0.31M (25%) | 11k |

**Per analysis, all runs:**

| Run | Calls CHEAP / HIGHEST | Prompt tokens | Output + thinking tokens |
|---|---|---|---|
| G1 | 24.7 / 4.4 (+0.1 lite backup) | 118k | 9.4k |
| G2 | 26.8 / 4.2 | 119k | 10.3k |
| G3 | 23.2 / 4.5 | 120k | 8.7k |
| C1 | 40.0 / 6.8 | 354k | 26.5k |
| C2 | 41.9 / 7.9 | 527k (filing ingestion included) | 29.5k |
| C3 | 37.7 / 8.5 | 420k | 24.7k |
| C4 | 37.4 / 5.9 | 386k | 23.4k |
| C5 | 37.1 / 6.1 | 388k | 23.1k |
| C6 | 36.6 / 6.6 | 401k | 23.2k |

- Claude uses about **3.2 times the prompt tokens and 2.7 times the output** of Gemini for the same 19 tickers. The reasons are its own tool calls, longer agent summaries, and more correction turns and debate rounds.
- Caching was off in C1 and on from C2. Since then about 21-22% of Haiku and 25% of Sonnet prompt tokens are read from cache. In these runs only the system prompt was cached; the growing ReAct scratchpad in the user message was not (V2). The code now also caches the scratchpad prefix (each ReAct turn reads the previous turn's prompt); section 11 measures it.
- Sonnet, with thinking set to `between_tools`, logged 0 thinking tokens in every run. All logged thinking is Haiku's (`adaptive`).

## 7. Cost

**Anthropic.** The logs contain token counts only, no cost field, and the repository configures no prices. Cost is therefore computed with these list prices per million tokens: Haiku 5.5 $0.10 input / $0.50 output, Sonnet 5.5 $2 / $10; cache read at 0.1 times the input price; cache write (5-minute) at 1.25 times; thinking tokens billed as output. Uncached input is the logged `uncached_prompt_tokens` (C1 had no cache, so its `prompt_tokens` is all uncached).

| Run | Total | Per analysis (mean) | Median per ticker | Most expensive ticker | Without caching |
|---|---|---|---|---|---|
| C1 | $4.44 | $0.234 | $0.219 | INTC $0.49 | $4.44 (no cache) |
| C2 | $7.65 | $0.403 | $0.202 | ORCL $1.68 | $8.42 |
| C3 | $4.77 | $0.251 | $0.232 | LLY $0.40 | $5.69 |
| **C4** | **$3.48** | **$0.183** | $0.170 | GOOGL $0.41 | $4.14 |
| C5 | $3.53 | $0.186 | $0.175 | LLY $0.34 | $4.21 |
| C6 | $3.80 | $0.200 | $0.209 | AAPL $0.30 | $4.54 |

- **C2 includes the one-time ingestion of the ORCL, COST and LLY filings** (filing summaries run on Sonnet): those three tickers cost $4.53 together, the other 16 cost $3.12.
- **Sonnet is most of the bill:** in C4 it is about $2.81 of $3.48 (81%). By node, the Judge ($0.88), Reflection ($0.72), Bear ($0.60) and Bull ($0.59) are the largest items (J6 §2). So cost moves with the number of Judge and Reflection passes.
- Caching the system prompt saves 16% when C4's own tokens are priced without the cache discount (C4: $4.14 without, $3.48 with). This is the method of runs C2-C6, measured over the 19-ticker batches. Section 11 measures it against real runs without caching: there the saving is about 10% per call, because runs also differ in debate rounds and calls. The current default also caches the ReAct scratchpad (section 11).
- The J6 review gives $3.44 for C4 because it priced Sonnet cache reads at 0.05 times. This document uses 0.1 times throughout.

**Gemini.** Neither the code nor the logs contain Gemini prices, so the cost is not computed here. Gemini used about 120k prompt and 9-10k output tokens per analysis, about a third of Claude's volume.

## 8. Quality

### 8.1 Head-to-head, G3 vs C4 (same code and conditions)

Winners are the reviewers' verdicts (J1-J5 for C4, H1-H5 for G3). "J" is the Judge's confidence; stored = Judge in both runs.

| Ticker | C4 Claude | G3 Gemini | Winner and main reason |
|---|---|---|---|
| AAPL | HOLD 0.55 | BUY 0.85, sized with 100% of cash | Claude: weighs valuation, rates and growth; Gemini never mentions valuation |
| AMD | HOLD 0.55 | BUY 0.75 | Claude: Gemini ignores the price breakdown and makes an unsupported market-share claim |
| AMZN | HOLD 0.55 | BUY 0.75 | Claude: Gemini reads guidance that implies lower profit as "stable" |
| BA | HOLD 0.60 | HOLD 0.65 | Claude (narrow): spots the $9.6B one-off sale gain |
| DIS | HOLD 0.50 | BUY 0.75 | Claude: finds the layoff news; Gemini has a wrong funding statement and double-counts charges |
| GOOGL | HOLD 0.55 | BUY 0.75 | Claude (narrow): both sides weighed; Gemini lists only positives |
| INTC | HOLD 0.55 | HOLD 0.65 | Claude: uses the fab explosion, Terafab and the $20B equity raise |
| JPM | HOLD 0.55 | BUY 0.76 | Claude on calibration; Gemini had the fuller Q3 news this time |
| KO | HOLD 0.60 | BUY 0.75 | Claude: weighs RSI 75.9, price above the upper Bollinger band, 28x P/E |
| META | HOLD 0.55 | HOLD 0.70 | Claude: earnings two days away, and only Claude gives the date |
| MSFT | HOLD 0.62 | HOLD 0.85 | Claude: keeps quarter and full year apart; Gemini puts the full-year $6.5B OpenAI gain into one quarter |
| NVDA | BUY 0.62, tiers | BUY 0.85 | Claude: treats the $105B guarantees as a risk; Gemini treats them as an offset |
| QCOM | HOLD 0.55 | HOLD 0.70 | Claude (narrow): 8 own tool calls against 0 |
| TSLA | HOLD 0.60 | HOLD 0.65 | Claude: more research; but its reasoning reads bearish and never weighs SELL |
| UNH | BUY 0.60, tiers | BUY 0.80 | Claude: uses the raised guidance (at least $20.50); Gemini presents the old range as latest |
| XOM | HOLD 0.55 | BUY 0.80 | Claude: weighs RSI 72.9 and the oil-price outlook; one small error ($1.2B should be $2.38B) |
| ORCL | HOLD 0.55 | BUY 0.75 | Claude: a second search finds the Project Jupiter force-majeure notice; Gemini's BUY does not follow from its own debate |
| COST | HOLD 0.60 | HOLD 0.70 | Claude (narrow): uses September sales and 10-K tariff-refund and LIFO items |
| LLY | BUY 0.60, no tiers (cash used up) | BUY 0.75 | Claude: finds the EU CHMP opinion and weighs the stretched price |

**Result: Claude 19, Gemini 0** (5 of the 19 are narrow).

### 8.2 Research depth (script counts unless noted)

| | C4 | G3 |
|---|---|---|
| Own tool calls by agents | **135** (65 web searches, 22 `search_news`, 19 prior-analysis searches, 18 price-history, 11 filing searches) | **7** (all failed sentiment `search_news`) |
| Own tool calls in other runs | C1 109, C2 124, C3 145, C5 125, C6 134 | G1 2, G2 10 |
| News items found per ticker (J6) | 9.2 | 4.5 |
| Agent summary text per ticker (J6) | 13.9k chars | 8.5k chars |
| Reasonings that weigh the 5.10% 10-year yield (J6) | 13 / 19 | 1 / 19 |
| Judge answers to opposing points that lower its confidence (J6) | 28 / 49 | 6 / 38 |

Gemini's agents summarise what the code fetched first and almost never search further. Claude's agents run follow-up searches, which is how they found the Jupiter notice (ORCL), the fab explosion (INTC) and the layoffs (DIS).

### 8.3 Factual errors (reviewer counts)

- **G3:** model-specific MEDIUM or worse errors in the debate or Judge text on at least 7 tickers: AMD, AMZN, DIS, MSFT, NVDA, UNH and ORCL (H2-H5). The ORCL BUY was rated HIGH because it does not follow from the debate.
- **C4:** two MEDIUM-LOW errors that reached the analysis (QCOM: a P/E argument; XOM: $1.2B instead of $2.38B in charges) (J4).
- **Shared by both models**, because they come from stored filing records: the ORCL restructuring plan read as $2.1B (about $2.8B is right) and an incorrect $651.2M FX record for LLY.
- In later Claude runs some new Judge arithmetic slips appeared (C5: AMZN, ORCL; C6: INTC, ORCL funding statements), all MEDIUM, none changing the action (F1-F5, E1-E5).

### 8.4 Reflection behaviour

| | C4 | G3 |
|---|---|---|
| Reflection passes (first + retry) | 28 (19 + 9) | 21 (19 + 2) |
| First passes with no kept issue | 12 / 19 | 17 / 19 |
| Kept issues | 12 (8 valid, 2 borderline, 2 wrong or mis-targeted) | 2 |
| Data agents re-run on Reflection's request | 2 (GOOGL, ORCL) | 0 |

Claude's Reflection (Sonnet) is a much stricter reviewer. Before the fixes, this cost Claude confidence: a 0.15 penalty on every ticker whose last Reflection pass was unverified. Tickers with stored confidence below the Judge's: C1 8, C2 5, C3 14, C4 0, C5 5, C6 0; all Gemini runs 0. Batch 3 stopped penalties for argument-quality issues (C4 0), C5 showed that the label Reflection chose still decided the penalty (5), and the release candidate turned Reflection findings into notes only (C6 0).

### 8.5 Confidence calibration (Judge values, script counts)

| Run | Anchored? | Actions B / H / S | Mean | Median | SD | Range | Most used values |
|---|---|---|---|---|---|---|---|
| G1 (16) | yes | 11 / 3 / 2 | 0.759 | 0.75 | 0.071 | 0.65-0.90 | 0.70 ×6 |
| C1 | to G1 | 8 / 9 / 2 | 0.648 | 0.62 | 0.087 | 0.50-0.85 | 0.62 ×4 |
| C2 | to C1 | 7 / 10 / 2 | 0.619 | 0.60 | 0.084 | 0.45-0.80 | 0.60 ×5 |
| G2 | to C2 | 7 / 10 / 2 | 0.619 | 0.60 | 0.084 | 0.45-0.80 | = C2 |
| C3 | to C2 | 6 / 11 / 2 | 0.605 | 0.58 | 0.080 | 0.45-0.80 | 0.55 ×4 |
| **G3** | **no** | **12 / 7 / 0** | **0.748** | 0.75 | 0.062 | 0.65-0.85 | 0.75 ×7 |
| **C4** | **no** | **3 / 16 / 0** | **0.571** | 0.55 | **0.032** | 0.50-0.62 | 0.55 ×10 |
| C5 | to C4 | 3 / 16 / 0 | 0.569 | 0.55 | 0.031 | 0.50-0.62 | 0.55 ×10 |
| C6 | to C5 | 3 / 16 / 0 | 0.567 | 0.55 | 0.030 | 0.50-0.62 | 0.55 ×11 |

- **Independent Gemini clusters at BUY around 0.75; independent Claude clusters at HOLD around 0.55.** In G3 vs C4 the actions agree on 10/19 tickers. All 9 disagreements are Claude HOLD against Gemini BUY. The two confidence series barely correlate (r = 0.13, J6).
- Neither number separates strong from weak cases. In C4, MSFT (a HOLD forced by the 50% position cap) and NVDA (BUY on +106% revenue) both get 0.62.
- The cause is the prompt, not the model: the Judge prompt defines confidence only as `"confidence": 0.0-1.0` (`ai/agents/judge.py:136`), so each model falls back to its own habit.
- Claude's caution is mostly well-founded in direction (valuation, negative free cash flow, overbought technicals, the 5.10% yield and the Fed hike), but it treats HOLD as "not buying": on TSLA the case reads bearish, yet SELL is never weighed (J6 §9).

### 8.6 Anchoring

When the Judge sees the previous verdict, it mostly repeats it. The prompt asks for an explanation whenever confidence moves by 0.05 or more (`judge.py:86-88`, threshold `CONFIDENCE_CHANGE_TO_EXPLAIN` at `:253`), which makes staying put the easy choice.

| Pair | Same action | Same confidence |
|---|---|---|
| C1's "previous verdict" vs G1's verdict | 16 / 16 (identical) | 16 / 16 |
| G2 vs C2 (G2 saw C2) | 19 / 19 | 18 / 19 |
| C5 vs C4 (C5 saw C4) | 19 / 19 | 18 / 19 |
| C6 vs C5 (C6 saw C5) | 19 / 19 | 17 / 19 |
| G3 vs G1 (both Gemini, G3 independent) | 13 / 16 | 2 / 16 |

- **C1-C3 carried Gemini's G1 verdicts.** Each Claude run trimmed the inherited confidence a little (MSFT 0.90 → 0.85 → 0.80 → 0.80). Once the anchor was removed in C4, AAPL, KO and GOOGL (BUY) and BA and TSLA (SELL) all became HOLD. These five calls were G1's.
- **Control:** ORCL, COST and LLY were not in G1, so C1 had no anchor for them. C1 gave HOLD 0.50, HOLD 0.58 and BUY 0.60; independent C4 gave HOLD 0.55, HOLD 0.60 and BUY 0.60. So C4 shows Claude's own stable view.
- **G2 copied Claude.** Its apparent agreement with Claude was anchoring; independent G3 went back to G1's level (mean 0.748 vs 0.759).

### 8.7 The Judge independence experiment

For G3 and C4 only, a temporary patch hid the previous verdict, prediction history and earlier recommendations from the Judge. The logs confirm it: `previous_leaning` and `previous_confidence` are null in every `judge.deciding_factor` event of G3 and C4, and set on all 19 tickers in C5 and C6. The patch was reverted after C4. The experiment showed that each model's independent "style" is stable across code versions, and that most of the spread of confidence in C1-C3 was inherited, not judged.

## 9. Caveats

1. **Different code per run.** Only G3 and C4 share code and conditions. Other comparisons mix model effects with fix batches 1-4 and the release simplification.
2. **NewsAPI availability differs** (section 5). G3, C4 and C5 had no key; C3 and C6 had an exhausted quota, which degrades news more than no key.
3. **The Judge patch was temporary** and only in G3 and C4. All other runs are anchored, so their confidence values are not independent judgements.
4. **G3's Gemini was degraded.** 68 of 85 highest-tier calls and 15 of 19 final Judges ran on the backup 3.6-flash because 3.8-flash timed out and returned 503s. The reviewers note that the four tickers judged on 3.8 were just as overconfident, but the comparison is not perfectly equal on model strength.
5. **Same-day data.** All runs from 18 and 19 October used the same Friday market close. C4 ran after G3 and could read research notes that G3 wrote that morning.
6. **Batch order affects tiers.** Position sizing gives the first BUY in batch order most of the cash (G3: AAPL 100%; C4: NVDA 80%), so tier comparisons say little about the models.
7. **Gemini cost is not computed**, and Mistral was not run in any batch.

## 10. Conclusion

**Default provider: Anthropic** (Haiku 5.5 cheap, Sonnet 5.5 highest).

- **Quality:** it won all 19 tickers in the fair head-to-head. It researches (135 vs 7 own tool calls), makes fewer and smaller factual errors, and its Judge weighs both sides.
- **Reliability:** 5,181 Anthropic calls across six runs, every one `200 OK`, no retries and no fallbacks. Gemini had timeouts, 503s and 429s in every run, and in G3 most final verdicts came from the backup model.
- **Speed:** the fastest batches are all Claude (C5 1,235 s, C4 and C1 1,270 s, against 1,580-1,780 s for Gemini).
- **Cost:** about $3.5-3.8 per 19-ticker batch, $0.18-0.20 per analysis, with the system prompt cached (runs C4-C6). The current default also caches the ReAct scratchpad, which adds about 2-3 points of saving (section 11).

To use it, set `LLM_DEFAULT_PROVIDER=anthropic`. The built-in default (`config.py:58`) is still `google`, and the README, which is kept as it was at the evaluation, also names Google Gemini as the default; changing the default is a small follow-up. A Google key is still needed for embeddings.

**Where Gemini and Mistral remain useful:**
- As the alternative provider when Anthropic is unavailable or its credit runs out (the adapter raises a clear error that suggests switching `LLM_DEFAULT_PROVIDER`). The switch is manual; there is no automatic cross-provider fallback.
- For cost-sensitive or high-volume use. Gemini sends about a third of the tokens, at the price of much shallower research and overconfident BUYs.
- Mistral is wired in with its own backup models and pacing but was not evaluated in these runs.

**Open limitations (not solved by choosing a model):**
1. Judge confidence has no defined scale (`judge.py:136`); Claude sits near 0.55 and Gemini near 0.75 regardless of evidence strength.
2. Anchoring: the Judge copies the previous verdict (19/19 actions in C5 and C6) because of the 0.05 "explain the change" rule (`judge.py:86-88`).
3. Position sizing is first come, first served (`position_sizing.py:259`): the first BUY in batch order takes most of the cash.
4. Occasional Judge arithmetic errors on funding statements, and stale filing records (ORCL restructuring, LLY FX) that both models repeat.
5. No backup model is configured for Anthropic, so a Claude outage stops analyses until the provider is switched.
6. The NewsAPI free tier (100 requests a day) runs out quickly; with an exhausted quota, News wastes turns on `search_news`.

## 11. Anthropic prompt caching: measured

Section 7 measured caching of the system prompt only. This section compares Claude with itself: the same five tickers, analysed with four caching methods, to find which method saves money without losing quality, and when caching is not worth it. It does not compare providers.

### 11.1 Set-up

- **Tickers:** AAPL, NVDA, JPM, XOM and UNH, one batch per group, on 20 October 2026 (same market data in every group).
- **Groups** (settings in `.env.docker`, ai-service recreated before each group):

| Group | `LLM_ANTHROPIC_PROMPT_CACHING` | `LLM_ANTHROPIC_CACHE_SCRATCHPAD` | `LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST` | What is cached |
|---|---|---|---|---|
| A | false | - | - | nothing |
| B | true | false | false | the system prompt (the behaviour of runs C2-C6) |
| C | true | true | false | B + the ReAct scratchpad (the current default) |
| D | true | true | true | C + the debate evidence, sent once as the first system block and shared by Bull, Bear, Judge and Reflection |

- **Isolation.** `ANALYSIS_EXPERIMENT_ISOLATION=true` and `EPISODIC_CONTEXT_LIMIT=0` (the runs set both; the switch now also stops the episodic memory read, so the second setting is no longer needed): no analysis read or wrote memory, knowledge-graph facts, archived notes, the previous verdict or other analyses' tier cash. Every log has 21-25 `experiment_isolation.skipped` events. The BUY tiers of each group were declined before the next group, and every group started with 0.00 committed BUY cash. Groups were 6 minutes apart, so the 5-minute cache of one group could not serve the next.
- **Repeats.** C was run twice (C1, C2) to measure the noise between two identical runs. D was run twice: D1 with a design error (section 11.4), D2 after the fix.
- **Cost** is computed by `services/ai-service/scripts/llm_usage_report.py` from the `anthropic.completion_succeeded` and `anthropic.stream_completed` log lines, with the list prices of section 7. Every analysis completed; no run logged an error.

### 11.2 Results

| Group | Total, 5 analyses | Haiku per call | Haiku input price per M tokens | Sonnet per call | Sonnet input per call | Sonnet output per call (tokens) | Sonnet share of cost |
|---|---|---|---|---|---|---|---|
| A | $1.002 | $0.00090 | $0.100 | $0.0254 | $0.0180 | 746 | 86% |
| B | $0.904 | $0.00081 | $0.079 | $0.0227 | $0.0147 | 798 | 85% |
| C1 | $0.917 | $0.00062 | $0.057 | $0.0214 | $0.0139 | 752 | 89% |
| C2 | $0.798 | $0.00062 | $0.063 | $0.0221 | $0.0142 | 788 | 89% |
| D1 (broken) | $0.564 | $0.00061 | $0.058 | $0.0142 | $0.0105 | 371 | 83% |
| D2 | $0.680 | $0.00061 | $0.061 | $0.0183 | $0.0118 | 654 | 86% |

"Input price per M tokens" is the input cost divided by all prompt tokens (uncached + cache write + cache read); without caching it is the list price ($0.10 Haiku, $2 Sonnet).

Quality (script counts over the 5 analyses):

| Group | Verdicts (action/confidence) | Arguments | Citations per argument | Arguments saying the evidence is empty | Reflection issues | Ungrounded-figure warnings (Bull, Bear, Judge) |
|---|---|---|---|---|---|---|
| A | AAPL hold/0.60, NVDA buy/0.60, JPM hold/0.55, XOM hold/0.55, UNH buy/0.58 | 14 | 5.6 | 0 | 7 | 0 |
| B | AAPL hold/0.55, NVDA buy/0.62, JPM hold/0.55, XOM hold/0.55, UNH hold/0.50 | 13 | 5.4 | 0 | 6 | 3 |
| C1 | AAPL hold/0.55, NVDA buy/0.62, JPM hold/0.55, XOM hold/0.55, UNH hold/0.50 | 16 | 5.7 | 0 | 8 | 3 |
| C2 | AAPL hold/0.55, NVDA buy/0.62, JPM hold/0.55, XOM hold/0.55, UNH buy/0.58 | 12 | 6.2 | 0 | 2 | 4 |
| D1 (broken) | AAPL hold/0.20, NVDA hold/0.40, JPM hold/0.55, XOM hold/0.50, UNH hold/0.50 | 18 | 0.7 | 16 | 0 | - |
| D2 | AAPL hold/0.55, NVDA buy/0.62, JPM hold/0.55, XOM hold/0.50, UNH buy/0.60 | 14 | 5.7 | 0 | 7 | 0 |

Wall time for the five analyses: A 329 s, B 341 s, C1 397 s, C2 329 s, D2 322 s. Caching made no measurable difference to speed at this scale.

### 11.3 Findings

1. **Run-to-run noise is larger than most caching effects.** C1 and C2 have identical settings, yet their totals differ by 13% ($0.917 and $0.798), because the number of debate rounds and Sonnet calls changes from run to run (38 against 32 Sonnet calls). UNH also changed from hold to buy between C1 and C2. A single 5-ticker total cannot show a 10% saving. **Compare cost per call by model**, not batch totals.
2. **Caching never changed quality in A, B and C.** These groups send the model exactly the same text; only the billing differs. Their verdicts, citations and warnings vary no more than C1 against C2 do.
3. **System-prompt caching (B) saves about 10%** (per call: Sonnet -11%, Haiku -10%). Sonnet input per call falls from $0.0180 to $0.0147. The Sonnet system prompts are the same for every analysis, so within one batch the first analysis writes them and the later ones read them (7.5 reads per write).
4. **Scratchpad caching (C) works well on Haiku and adds little to the total.** 96% of Haiku prompt tokens go through the cache (cache reads and cache writes together; only 4% are sent uncached), and the Haiku input price falls by 37-43%, so Haiku cost per call falls 31% against A (24% against B). But Haiku is only 11-15% of the bill, so the total gains only about 2-3 points over B. It changes no text, so it stays on by default.
5. **Shared evidence first (D2) is the only method that cuts the Sonnet bill clearly.** Sonnet cost per call is $0.0183 against $0.0221 in C2 (-17%), and input per call is $0.0118 against $0.0142. Its quality in D2 matched C2: the same actions on all five tickers, 5.7 citations per argument, no evidence-free argument and no ungrounded-figure warning. Sonnet output per call was also lower (654 against 788 tokens); with one run this may be noise, so the saving that is certain is the input part, about 10% of the total.
6. **D's saving is limited by its own cache writes.** The evidence block now sits in front of each role's system prompt, so each role's prompt must be written again for every new evidence block (1.25 reads per write on Sonnet, against 6.6-8.4 in B and C). Each debate round with new evidence is written at 1.25 times the price.

### 11.4 What went wrong in D1, and the fix

In D1 the evidence moved to the first system block, but its heading ("Research agent evidence:") stayed in the user message with nothing after it. The models read the heading literally: 16 of 18 arguments said that the evidence block was empty and refused to argue. The Judge gave HOLD at 0.20-0.55 with no figures. The batch looked 44% cheaper than A (39% cheaper than C1) only because Sonnet wrote half as much (371 output tokens per call against 752 in C1).

Two lessons:
- **Moving text changes what the model reads**, even when every word is kept: a heading followed by nothing means "no evidence" to the model. The fix adds two fixed phrases, only when the switch is on: the label "Evidence for this analysis:" before the moved block, and a pointer to it where the evidence was. D2 shows the fix works.
- **No check stopped an evidence-free debate.** Reflection marked the D1 rounds as verified (the arguments made no claims, so nothing was unsupported), and the Judge still produced a recommendation. In normal runs the evidence is always in the message, so this cannot happen there, but a cost experiment must check quality, not only cost.

### 11.5 When caching is worth it, and when it is not

**Prices.** A cache write costs 1.25 times the input price and a cache read 0.1 times (the price used in this document; Anthropic lists 0.05 for Sonnet 5.5, which makes Sonnet reads cheaper still). A cached prefix pays for itself if it is read at least once before it expires (5 minutes): one read saves 0.9 times, more than the 0.25 times extra paid on the write. Prefixes shorter than the model's minimum (512 tokens for the 5.5 models) are not cached at all.

**Worth it when:**
- the same long prefix is sent again within 5 minutes: a ReAct agent's earlier turns (C), the same system prompt for many analyses in one batch (B), or the same evidence for several roles (D);
- the cached part is a large share of the prompt, and the model it applies to is a large share of the bill.

**Not worth it, or worth little, when:**
- **calls are far apart.** A single analysis run alone, or a schedule with more than 5 minutes between calls, writes caches that expire unread. Then caching costs up to 25% more input for those prefixes;
- **the prompt changes at the start.** Caching is a prefix match: anything that changes early (a new evidence block in front of the role's prompt in D) forces a new write of everything after it;
- **the cached model is a small share of the cost.** Scratchpad caching cut Haiku input by about 40% but moved the total by only a few percent, because Sonnet is 85-89% of the bill;
- **output dominates.** Caching lowers only input. Sonnet output is 29-36% of the Sonnet bill and is not affected (a lower output is a warning sign, as D1 showed, unless the quality checks are unchanged);
- **the batch is small and noisy.** At 5 tickers, effects under about 15% cannot be told apart from run-to-run variation without per-call measurements.

### 11.6 Recommendation

- Keep `LLM_ANTHROPIC_PROMPT_CACHING=true` and `LLM_ANTHROPIC_CACHE_SCRATCHPAD=true` (the defaults). They never change the text. Their saving against no caching is estimated at about 12%, mostly on input: about 10% from the system prompt (finding 3) plus 2-3 points from the scratchpad (finding 4). This is an estimate from the per-call results; the two measured C totals were 8% and 20% below A, a range that is inside the run-to-run noise (finding 1).
- `LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST` saved a further 17% of the Sonnet cost per call in D2 with no visible quality loss, but it changes where the evidence appears and adds two phrases, and it was measured on one 5-ticker run. It stays off by default. Before turning it on, run a full 19-ticker batch with it and compare the quality checks of section 8 with C6.
