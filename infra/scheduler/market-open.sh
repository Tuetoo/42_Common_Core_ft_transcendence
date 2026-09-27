#!/bin/sh
. /scripts/env.sh
echo "[$(date)] market-open: requesting analyses for auto_analyze_at_open portfolios"
curl -sf -X POST "${CORE_SERVICE_URL}/internal/scheduler/market-open" \
    -H "Content-Type: application/json" \
    || echo "[$(date)] market-open: request FAILED"