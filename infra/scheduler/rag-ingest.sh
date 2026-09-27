#!/bin/sh
. /scripts/env.sh
echo "[$(date)] rag-ingest: checking tracked tickers for new SEC filings"
curl -sf -X POST "${AI_SERVICE_URL}/internal/scheduler/rag-ingest" \
    -H "Content-Type: application/json" \
    || echo "[$(date)] rag-ingest: request FAILED"