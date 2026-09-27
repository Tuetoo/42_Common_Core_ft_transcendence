#!/bin/sh
. /scripts/env.sh
echo "[$(date)] review-run: triggering AI accuracy scoring, decision outcomes, and agent metrics rollup"
curl -sf -X POST "${AI_SERVICE_URL}/internal/review/run" \
    -H "Content-Type: application/json" \
    || echo "[$(date)] review-run: request FAILED"