# Shared helpers for the scripts in this folder. Source it, do not run it.
# Every script runs from the repository root, whatever the caller's
# current directory is, and reaches the stack through docker compose with
# the same --env-file as the Makefile. The scripts never read .env.docker
# themselves: values such as the database user come from the containers.
# Written for the bash 3.2 that ships with macOS.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

COMPOSE=(docker compose --env-file .env.docker)

# Public gateway address used by scripts that call the public API (the
# same address as OAUTH_REDIRECT_BASE_URL in .env.docker).
GATEWAY_URL="${GATEWAY_URL:-https://localhost:8443}"
GATEWAY_URL="${GATEWAY_URL%/}"

# Default ids of the development data (override with environment variables).
ADMIN_USER_ID="${ADMIN_USER_ID:-551b898b-e87c-4872-8bca-70333bc9d00b}"
OWNER_USER_ID="${OWNER_USER_ID:-5c7a7207-b4a1-4db8-8550-aae19d0a3d65}"
PORTFOLIO_ID="${PORTFOLIO_ID:-d72855ea-2776-41c3-bc77-ce1a4c1435d5}"

die() {
  echo "error: $*" >&2
  exit 1
}

# Run SQL with psql inside the postgres container, as that container's own
# POSTGRES_USER on POSTGRES_DB. SQL is read from stdin.
psql_stdin() {
  "${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'
}

# Print a 2-hour access token for USER_ID with ROLE (admin or user).
make_token() {
  local user_id="$1" role="$2" hours="${3:-2}"
  "${COMPOSE[@]}" exec -T auth-service python -c \
    "from datetime import timedelta; from security import _create_token; print(_create_token('$user_id', timedelta(hours=$hours), 'access', {'role': '$role'}))"
}

# Busy windows of the scheduler, in New York time (the crontab's zone):
#   09:00-10:30  market-open (09:30) starts analyses for some portfolios
#   16:55-17:50  tier monitoring (17:00) and the market-close snapshot (17:30)
# A restart of ai-service or core-service, or an end-to-end run, inside
# them can break a job. guard_busy_window refuses unless FORCE=1.
#   01:55-04:30  the nightly jobs: review-run (02:00, up to 30 minutes),
#                backup (02:30), rag-ingest (03:00, up to 60 minutes),
#                logs-cleanup (04:00), purge-unverified (04:10) and
#                notification-retention (04:20)
# A restart there aborts the running nightly job, which then reports no
# success until the next night (DailyJobStale after 26 hours).
# guard_busy_window only warns for this window: it is the morning in
# Europe, and the job can be run again by hand
# (docker compose exec scheduler /scripts/call-job.sh <job>).
in_busy_window() {
  local now
  now=$(TZ=America/New_York date +%H%M)
  now=$((10#$now))
  if [ "$now" -ge 900 ] && [ "$now" -lt 1030 ]; then
    echo "market-open (09:30 New York)"
    return 0
  fi
  if [ "$now" -ge 1655 ] && [ "$now" -lt 1750 ]; then
    echo "tier monitoring / market close (17:00-17:30 New York)"
    return 0
  fi
  return 1
}

in_nightly_window() {
  local now
  now=$(TZ=America/New_York date +%H%M)
  now=$((10#$now))
  [ "$now" -ge 155 ] && [ "$now" -lt 430 ]
}

guard_busy_window() {
  local what="$1" window
  if window=$(in_busy_window); then
    if [ "${FORCE:-0}" != "1" ]; then
      die "$what refused: the scheduler is busy now ($window). Wait, or run again with FORCE=1."
    fi
    echo "warning: $what inside the busy window ($window), FORCE=1 given" >&2
  fi
  if in_nightly_window; then
    echo "warning: $what during the nightly jobs (01:55-04:30 New York: review-run, backup, rag-ingest, logs-cleanup, purge-unverified, notification-retention); a job it interrupts must be run again by hand" >&2
  fi
}
