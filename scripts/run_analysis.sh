#!/usr/bin/env bash
# Real analysis runner for one or more tickers.
#
#   caffeinate -i bash scripts/run_analysis.sh AMD              # one ticker
#   caffeinate -i bash scripts/run_analysis.sh AAPL MSFT NVDA   # a batch
#   PORTFOLIO_ID=<uuid> caffeinate -i bash scripts/run_analysis.sh AMD
#
# Run it on the host (needs docker compose), wrapped in `caffeinate -i` so
# the Mac does not sleep mid-run. It always works from the repository root
# (lib.sh) and writes runs/run_<timestamp>/. Tickers run one after
# another (ai-service's analysis queue is serial anyway); one ticker
# failing or timing out never stops the rest.
#
# Analyses are created through ai-service's internal endpoints with a
# small stdlib client run inside the container (scripts/analysis_cli.py),
# as system analyses bound to PORTFOLIO_ID: for a BUY/SELL verdict the
# position sizing runs and PENDING_ACCEPTANCE tiers are written; they do
# not execute unless accepted.
#
# Output: runs/run_<timestamp>/ with, per ticker,
#   <T>_result.json       everything from GET /analyses/{id}: agent runs and
#                         outputs, debate, reflections, recommendation
#   <T>_agent_reports.txt self-archived reports (not part of result.json)
#   <T>_tiers.txt         recommendation tiers (not part of result.json)
#   <T>_filings.txt       this ticker's filing documents (id/status/url)
#   <T>_ai-service.log    the ai-service log window of this run
# and _summary.txt (analysis id, final status, elapsed time per ticker).
# runs/ is gitignored.

set -uo pipefail  # not `set -e`: one ticker failing must not stop the batch

if [ "$#" -lt 1 ]; then
  echo "Usage: bash scripts/run_analysis.sh TICKER [TICKER ...]" >&2
  exit 1
fi
TICKERS=("$@")
# lib.sh: repository root as cwd, COMPOSE, PORTFOLIO_ID default, psql_stdin.
. "$(dirname "$0")/lib.sh"

AI_CLI=("${COMPOSE[@]}" exec -T ai-service python -m scripts.analysis_cli)
POLL_INTERVAL_SECONDS=5
# Wide on purpose: a ticker whose newest filing is not summarized yet is
# summarized inline first (up to 300 s x 5 attempts, see
# ai/knowledge/filing_summary.py _FILING_SUMMARY_TIMEOUT_SECONDS).
MAX_WAIT_SECONDS=3600

# One query into one output file (stdout and errors).
psql_to() {
  local sql="$1" out="$2"
  psql_stdin <<< "$sql" > "$out" 2>&1
}

OUTDIR="runs/run_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"
SUMMARY_FILE="$OUTDIR/_summary.txt"
{
  echo "Run started $(date)"
  echo "Tickers: ${TICKERS[*]}"
  echo "Portfolio-bound: $PORTFOLIO_ID"
  echo ""
} > "$SUMMARY_FILE"
echo "Output directory: $OUTDIR"

for ticker in "${TICKERS[@]}"; do
  echo "=================================================="
  echo "=== Starting $ticker at $(date) ==="
  START_TIME_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  RESPONSE=$("${AI_CLI[@]}" create "$ticker" --portfolio "$PORTFOLIO_ID")
  ANALYSIS_ID=$(echo "$RESPONSE" | python3 -c \
    "import sys, json
try:
    print(json.load(sys.stdin)['data']['id'])
except Exception:
    pass" 2>/dev/null)

  if [ -z "$ANALYSIS_ID" ]; then
    echo "  FAILED to create analysis for $ticker: $RESPONSE"
    echo "$ticker: FAILED TO CREATE -- $RESPONSE" >> "$SUMMARY_FILE"
    echo "$RESPONSE" > "$OUTDIR/${ticker}_create_ERROR.txt"
    continue
  fi
  echo "  $ticker -> analysis_id=$ANALYSIS_ID"

  ELAPSED=0
  STATUS="pending"
  while [ "$ELAPSED" -lt "$MAX_WAIT_SECONDS" ]; do
    sleep "$POLL_INTERVAL_SECONDS"
    ELAPSED=$((ELAPSED + POLL_INTERVAL_SECONDS))
    STATUS=$("${AI_CLI[@]}" status "$ANALYSIS_ID" 2>/dev/null | python3 -c \
      "import sys, json
try:
    print(json.load(sys.stdin)['data']['status'])
except Exception:
    print('unknown')" 2>/dev/null)
    echo "  [$ticker] elapsed=${ELAPSED}s status=$STATUS"
    if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
      break
    fi
  done
  if [ "$STATUS" != "completed" ] && [ "$STATUS" != "failed" ]; then
    echo "  [$ticker] TIMED OUT after ${MAX_WAIT_SECONDS}s, last status=$STATUS"
  fi

  # The status turns completed/failed BEFORE the post-verdict
  # writes (archived notes, KG facts, procedural memory) finish; wait up
  # to 2 minutes for the queue job, or the export misses the last note
  # and the next ticker starts while they still run.
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    for _ in $(seq 1 24); do
      if "${COMPOSE[@]}" logs ai-service --since "$START_TIME_UTC" 2>/dev/null \
          | grep "analysis_queue_job_finished" | grep -q "$ANALYSIS_ID"; then
        break
      fi
      sleep 5
    done
  fi

  "${AI_CLI[@]}" get "$ANALYSIS_ID" 2>/dev/null | python3 -m json.tool \
    > "$OUTDIR/${ticker}_result.json" 2>/dev/null
  psql_to "SELECT agent_role, ticker, category, content FROM ai.agent_reports
           WHERE analysis_id = '$ANALYSIS_ID' ORDER BY agent_role;" \
    "$OUTDIR/${ticker}_agent_reports.txt"
  psql_to "SELECT rt.side, rt.target_price, rt.quantity, rt.status, r.window_expires_at
           FROM ai.recommendation_tiers rt
           JOIN ai.recommendations r ON r.id = rt.recommendation_id
           WHERE r.analysis_id = '$ANALYSIS_ID'
           ORDER BY rt.target_price DESC;" \
    "$OUTDIR/${ticker}_tiers.txt"
  psql_to "SELECT id, filename, status, storage_url, uploaded_at
           FROM core.documents
           WHERE ticker = '$ticker' AND category = 'filing'
           ORDER BY uploaded_at DESC;" \
    "$OUTDIR/${ticker}_filings.txt"

  # `--until` has one-second resolution and drops lines
  # logged later in the same second; wait first.
  sleep 5
  END_TIME_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  "${COMPOSE[@]}" logs ai-service --since "$START_TIME_UTC" --until "$END_TIME_UTC" \
    > "$OUTDIR/${ticker}_ai-service.log" 2>&1

  echo "=== Finished $ticker at $(date), final status=$STATUS ==="
  echo "$ticker: analysis_id=$ANALYSIS_ID status=$STATUS elapsed=${ELAPSED}s" >> "$SUMMARY_FILE"
done

echo "=================================================="
echo "ALL DONE. Results in: $OUTDIR/"
cat "$SUMMARY_FILE"
