#!/usr/bin/env bash
# Real-environment end-to-end checks against the running stack.
#
#   bash scripts/e2e.sh [all] [args]  # both checks: core e2e_check, then the tier
#                                    # chain, each with the same args (only options
#                                    # both accept: --verbose, --keep, --break-lock)
#   bash scripts/e2e.sh core [args]  # only scripts.e2e_check (args passed through,
#                                    # e.g. --only auth,portfolio --verbose)
#   bash scripts/e2e.sh tier [args]  # only scripts.e2e_tier_check
#
# A fresh admin token is made for the run. Refused inside the scheduler's
# busy windows (see lib.sh) unless FORCE=1.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

part="${1:-all}"
[ "$#" -gt 0 ] && shift
guard_busy_window "end-to-end run"

token=$(make_token "$ADMIN_USER_ID" admin 2)
[ -n "$token" ] || die "could not create an admin token (is auth-service up?)"

run_core() {
  "${COMPOSE[@]}" exec -T -e E2E_ADMIN_TOKEN="$token" core-service \
    python -m scripts.e2e_check "$@"
}
run_tier() {
  "${COMPOSE[@]}" exec -T -e E2E_ADMIN_TOKEN="$token" ai-service \
    python -m scripts.e2e_tier_check "$@"
}

case "$part" in
  core) run_core "$@" ;;
  tier) run_tier "$@" ;;
  all)
    status=0
    run_core "$@" || status=$?
    echo
    run_tier "$@" || status=$?
    exit "$status"
    ;;
  *) die "usage: bash scripts/e2e.sh [all|core|tier] [args]" ;;
esac
