#!/bin/sh
. /scripts/env.sh
echo "[$(date)] tier-monitoring: checking recommendation tiers for triggers and expiry"
curl -sf -X POST "${AI_SERVICE_URL}/internal/scheduler/tier-monitoring" \
    -H "Content-Type: application/json" \
    || echo "[$(date)] tier-monitoring: request FAILED"