#!/bin/sh
# Runs one HTTP scheduled job: POSTs to the job's /internal/* route with
# the shared X-Internal-Token (every /internal/* route requires it), uses
# `curl -sS --fail-with-body` so a failure prints its reason (HTTP status,
# DNS, timeout) and the answer body, and exits non-zero on failure. On
# success it reports a heartbeat (heartbeat.sh). Output goes to the
# container's stdout (see crontab), where `vector` captures it.
# Usage: /scripts/call-job.sh <job>
#
# Per-job time limits (--max-time):
#   market-open      600s: one ai-service create call (up to 15s each)
#                    per held ticker of each auto_analyze_at_open
#                    portfolio, at most 10 at once.
#   market-close     600s: recomputes every portfolio snapshot inline.
#   tier-monitoring  900s: checks every open tier inline.
#   review-run      1800s: runs the whole review workflow inline.
#   rag-ingest      3600s: fetches filings and news for every tracked
#                    ticker plus the macro research report inline
#                    (slowest job).
#   purge-unverified 300s: deletes (anonymizes) accounts that never
#                    verified their email, one by one (auth-service).
#   notification-retention 300s: deletes old read notifications in
#                    batches (core-service).
#   deleted-user-sweep-core 900s, deleted-user-sweep-ai 900s: clean the
#                    data of deleted accounts, one user at a time.
. /scripts/env.sh
job="$1"
case "$job" in
    market-open)
        url="${CORE_SERVICE_URL}/internal/scheduler/market-open"
        max_time=600
        message="requesting analyses for auto_analyze_at_open portfolios"
        ;;
    market-close)
        url="${CORE_SERVICE_URL}/internal/scheduler/market-close"
        max_time=600
        message="recomputing portfolio NAV snapshots"
        ;;
    tier-monitoring)
        url="${AI_SERVICE_URL}/internal/scheduler/tier-monitoring"
        max_time=900
        message="checking recommendation tiers for triggers and expiry"
        ;;
    review-run)
        url="${AI_SERVICE_URL}/internal/review/run"
        max_time=1800
        message="triggering AI accuracy scoring, decision outcomes, and agent metrics rollup"
        ;;
    rag-ingest)
        url="${AI_SERVICE_URL}/internal/scheduler/rag-ingest"
        max_time=3600
        message="ingesting new SEC filings and news for tracked tickers, and the macro research report (spends LLM, NewsAPI and embedding quota)"
        ;;
    purge-unverified)
        url="${AUTH_SERVICE_URL}/internal/scheduler/purge-unverified"
        max_time=300
        message="deleting accounts that never verified their email"
        ;;
    notification-retention)
        url="${CORE_SERVICE_URL}/internal/scheduler/notification-retention"
        max_time=300
        message="deleting old read notifications"
        ;;
    deleted-user-sweep-core)
        url="${CORE_SERVICE_URL}/internal/scheduler/deleted-user-sweep"
        max_time=900
        message="deleting core data left by deleted accounts"
        ;;
    deleted-user-sweep-ai)
        url="${AI_SERVICE_URL}/internal/scheduler/deleted-user-sweep"
        max_time=900
        message="deleting ai data left by deleted accounts"
        ;;
    *)
        echo "[$(date)] call-job: unknown job '${job}'" >&2
        exit 2
        ;;
esac
echo "[$(date)] ${job}: ${message}"
if curl -sS --fail-with-body --max-time "$max_time" -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "X-Internal-Token: ${INTERNAL_API_TOKEN:-}"; then
    echo
    echo "[$(date)] ${job}: done"
    /scripts/heartbeat.sh "$job"
else
    rc=$?
    echo "[$(date)] ${job}: request FAILED (curl exit code $rc)" >&2
    exit "$rc"
fi
