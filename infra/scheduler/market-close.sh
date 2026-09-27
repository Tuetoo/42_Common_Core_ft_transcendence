#!/bin/sh
. /scripts/env.sh
echo "[$(date)] market-close: recomputing portfolio NAV snapshots"
curl -sf -X POST "${CORE_SERVICE_URL}/internal/scheduler/market-close" \
    -H "Content-Type: application/json" \
    || echo "[$(date)] market-close: request FAILED"