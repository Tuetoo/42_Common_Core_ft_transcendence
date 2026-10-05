#!/usr/bin/env bash
# Rebuild and restart services, then wait until they report healthy.
#
#   bash scripts/rebuild.sh ai-service
#   bash scripts/rebuild.sh auth-service core-service ai-service scheduler
#   RESTART_ONLY=1 bash scripts/rebuild.sh ai-service   # no image build: the
#                    # development override bind-mounts the source, so a
#                    # restart is enough for a code-only change
#
# Refused inside the scheduler's busy windows (see lib.sh) unless FORCE=1.
# Do not restart ai-service while an analysis is running: the analysis is
# lost with the process.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

[ "$#" -ge 1 ] || die "usage: bash scripts/rebuild.sh <service> [service ...]"
guard_busy_window "restart of $*"

if [ "${RESTART_ONLY:-0}" = "1" ]; then
  "${COMPOSE[@]}" restart "$@"
else
  "${COMPOSE[@]}" up -d --build "$@"
fi

echo "waiting for health checks (up to 3 minutes)..."
for _ in $(seq 1 36); do
  pending=""
  for service in "$@"; do
    # -a also lists a container that exited; one without a health check
    # is ready once it is running.
    state=$("${COMPOSE[@]}" ps -a --format '{{.State}}/{{.Health}}' "$service" 2>/dev/null || true)
    case "$state" in
      running/healthy|running/) ;;
      "") pending="$pending $service(no container)" ;;
      *) pending="$pending $service($state)" ;;
    esac
  done
  if [ -z "$pending" ]; then
    "${COMPOSE[@]}" ps "$@"
    exit 0
  fi
  sleep 5
done
"${COMPOSE[@]}" ps -a "$@"
die "not running and healthy yet:$pending"
