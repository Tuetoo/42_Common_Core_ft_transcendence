#!/usr/bin/env bash
# Follow a service's log, or search the saved daily log files.
#
#   bash scripts/logs.sh ai-service                # follow, last 10 minutes first
#   SINCE=2h bash scripts/logs.sh core-service
#   bash scripts/logs.sh ai-service grep judge_node.delayed_retry
#                                                  # search today's ./logs file (New York date)
set -euo pipefail
. "$(dirname "$0")/lib.sh"

service="${1:-}"
[ -n "$service" ] || die "usage: bash scripts/logs.sh <service> [grep <pattern> [YYYY-MM-DD]]"

if [ "${2:-}" = "grep" ]; then
  pattern="${3:-}"
  [ -n "$pattern" ] || die "usage: bash scripts/logs.sh <service> grep <pattern> [YYYY-MM-DD]"
  day="${4:-$(TZ=America/New_York date +%Y-%m-%d)}"
  file="logs/$service/$day.log"
  [ -f "$file" ] || die "no log file $file"
  grep -- "$pattern" "$file"
  exit
fi
exec "${COMPOSE[@]}" logs -f --since "${SINCE:-10m}" "$service"
