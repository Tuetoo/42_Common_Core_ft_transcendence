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

die() {
  echo "error: $*" >&2
  exit 1
}

UUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

# Ids of the accounts and portfolio the scripts act on: ADMIN_USER_ID,
# OWNER_USER_ID and PORTFOLIO_ID. Set them in the environment or as
# VAR=value lines in .env.scripts at the repository root (git-ignored;
# the environment wins). When unset, require_admin_id takes the oldest
# active admin and require_owner_id the owner of PORTFOLIO_ID;
# PORTFOLIO_ID has no fallback.
if [ -f .env.scripts ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    case "$key" in
      ADMIN_USER_ID|OWNER_USER_ID|PORTFOLIO_ID)
        [ -n "${!key:-}" ] || printf -v "$key" '%s' "${value%$'\r'}"
        ;;
    esac
  done < .env.scripts
fi

# Run SQL with psql inside the postgres container, as that container's own
# POSTGRES_USER on POSTGRES_DB. SQL is read from stdin.
psql_stdin() {
  "${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'
}

# Print the unaligned, header-less result of the SQL in $1.
psql_value() {
  "${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -At' <<< "$1"
}

require_admin_id() {
  if [ -z "${ADMIN_USER_ID:-}" ]; then
    ADMIN_USER_ID=$(psql_value "SELECT id FROM auth.users WHERE system_role = 'admin' AND is_active AND deleted_at IS NULL ORDER BY created_at LIMIT 1;") \
      || die "could not look up the admin account (is postgres up?); set ADMIN_USER_ID in the environment or in .env.scripts"
    [ -n "$ADMIN_USER_ID" ] \
      || die "no active admin account exists; give an account the admin role (scripts.set_system_role) or set ADMIN_USER_ID in the environment or in .env.scripts"
  fi
  [[ "$ADMIN_USER_ID" =~ $UUID_RE ]] || die "ADMIN_USER_ID is not a UUID: $ADMIN_USER_ID"
}

require_portfolio_id() {
  [ -n "${PORTFOLIO_ID:-}" ] \
    || die "PORTFOLIO_ID is not set; set it to the id of your test portfolio in the environment or in .env.scripts"
  [[ "$PORTFOLIO_ID" =~ $UUID_RE ]] || die "PORTFOLIO_ID is not a UUID: $PORTFOLIO_ID"
}

require_owner_id() {
  if [ -z "${OWNER_USER_ID:-}" ]; then
    require_portfolio_id
    OWNER_USER_ID=$(psql_value "SELECT owner_id FROM core.portfolios WHERE id = '$PORTFOLIO_ID';") \
      || die "could not look up the owner of $PORTFOLIO_ID (is postgres up?); set OWNER_USER_ID in the environment or in .env.scripts"
    [ -n "$OWNER_USER_ID" ] \
      || die "portfolio $PORTFOLIO_ID does not exist; set PORTFOLIO_ID or OWNER_USER_ID in the environment or in .env.scripts"
  fi
  [[ "$OWNER_USER_ID" =~ $UUID_RE ]] || die "OWNER_USER_ID is not a UUID: $OWNER_USER_ID"
}

# Print an access token for USER_ID with ROLE (admin or user), valid for
# HOURS (default 2).
make_token() {
  "${COMPOSE[@]}" exec -T auth-service python -c \
    "import sys; from datetime import timedelta; from security import _create_token; print(_create_token(sys.argv[1], timedelta(hours=float(sys.argv[3])), 'access', {'role': sys.argv[2]}))" \
    "$1" "$2" "${3:-2}"
}

# Busy windows of the scheduler, in New York time (the crontab's zone):
#   09:00-10:30  market-open (09:30) starts analyses for some portfolios
#   16:55-17:50  tier monitoring (17:00) and the market-close snapshot (17:30)
# A restart of ai-service or core-service, or an end-to-end run, inside
# them can break a job. guard_busy_window refuses unless FORCE=1.
#   01:55-04:55  the nightly jobs: review-run (02:00, up to 30 minutes),
#                backup (02:30), rag-ingest (03:05, up to 60 minutes),
#                logs-cleanup (04:00), purge-unverified (04:10),
#                notification-retention (04:20) and the deleted-user
#                sweeps (core 04:30, ai 04:40)
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
  [ "$now" -ge 155 ] && [ "$now" -lt 455 ]
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
    echo "warning: $what during the nightly jobs (01:55-04:55 New York: review-run, backup, rag-ingest, logs-cleanup, purge-unverified, notification-retention, deleted-user sweeps); a job it interrupts must be run again by hand" >&2
  fi
}
