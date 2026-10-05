#!/usr/bin/env bash
# Recompute portfolio NAV snapshots from the ledger and official closes
# (core-service scripts.recompute_snapshots).
#
#   bash scripts/snapshots.sh                     # preview for PORTFOLIO_ID
#   bash scripts/snapshots.sh --confirm           # write the flagged rows
#   bash scripts/snapshots.sh --all --from 2026-09-01
#
# Without --portfolio/--all the test portfolio (PORTFOLIO_ID) is used.
# Refused inside the scheduler's busy windows unless FORCE=1.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

guard_busy_window "snapshot recompute"
target_given=0
for arg in "$@"; do
  case "$arg" in --portfolio|--portfolio=*|--all) target_given=1 ;; esac
done
if [ "$target_given" = "1" ]; then
  exec "${COMPOSE[@]}" exec -T core-service python -m scripts.recompute_snapshots "$@"
fi
exec "${COMPOSE[@]}" exec -T core-service python -m scripts.recompute_snapshots \
  --portfolio "$PORTFOLIO_ID" "$@"
