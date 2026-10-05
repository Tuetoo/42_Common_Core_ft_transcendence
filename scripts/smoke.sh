#!/usr/bin/env bash
# Smoke check of the features an analysis batch does not exercise
# (ai-service scripts.smoke_check). No LLM call unless --with-llm.
#
#   bash scripts/smoke.sh
#   bash scripts/smoke.sh --only db redis qdrant
#   bash scripts/smoke.sh --with-llm
set -euo pipefail
. "$(dirname "$0")/lib.sh"

"${COMPOSE[@]}" exec -T ai-service python -m scripts.smoke_check "$@"
