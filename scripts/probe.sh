#!/usr/bin/env bash
# Measure a Gemini model's latency on Judge-sized requests
# (ai-service scripts.probe_model_latency). Each request counts against
# that model's daily quota per key.
#
#   bash scripts/probe.sh                          # HIGHEST primary model, 2 per key
#   bash scripts/probe.sh gemini-3.6-flash --per-key 1 --cap 90
#   bash scripts/probe.sh --health                 # every key and configured model
set -euo pipefail
. "$(dirname "$0")/lib.sh"

model_args=()
if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then
  model_args=(--model "$1")
  shift
fi
"${COMPOSE[@]}" exec -T ai-service python -m scripts.probe_model_latency ${model_args[@]+"${model_args[@]}"} "$@"
