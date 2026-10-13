#!/usr/bin/env bash
# Recommendation tiers of a portfolio.
#
#   bash scripts/tiers.sh                    # BUY cash held by open tiers, per
#                                            # analysis, exactly as the budget
#                                            # counts it (see analysis_cli)
#   bash scripts/tiers.sh all                # every tier of the portfolio, newest first
#   bash scripts/tiers.sh decline <analysis-uuid> [...]
#                                            # decline the analysis' recommendation
#                                            # (its pending tiers become declined)
#
# PORTFOLIO_ID selects the portfolio (default: the test portfolio). A
# decision must come from the portfolio owner, so `decline` uses a token
# of OWNER_USER_ID (default: the test portfolio's owner).
set -euo pipefail
. "$(dirname "$0")/lib.sh"

uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
[[ "$PORTFOLIO_ID" =~ $uuid_re ]] || die "PORTFOLIO_ID is not a UUID: $PORTFOLIO_ID"

cmd="${1:-open}"
[ "$#" -gt 0 ] && shift

case "$cmd" in
  open)
    "${COMPOSE[@]}" exec -T ai-service python -m scripts.analysis_cli committed "$PORTFOLIO_ID"
    ;;
  all)
    psql_stdin <<SQL
SELECT a.ticker, t.side, t.target_price, t.quantity, t.status,
       r.created_at, r.window_expires_at, a.id AS analysis_id
FROM ai.recommendation_tiers t
JOIN ai.recommendations r ON t.recommendation_id = r.id
JOIN ai.analyses a ON r.analysis_id = a.id
WHERE a.portfolio_id = '$PORTFOLIO_ID'
ORDER BY r.created_at DESC, a.ticker, t.target_price
LIMIT 100;
SQL
    ;;
  decline)
    [ "$#" -ge 1 ] || die "usage: bash scripts/tiers.sh decline <analysis-uuid> [...]"
    token=$(make_token "$OWNER_USER_ID" user 1)
    for analysis_id in "$@"; do
      [[ "$analysis_id" =~ $uuid_re ]] || die "not a UUID: $analysis_id"
      echo "== $analysis_id"
      curl -sk -X POST "$GATEWAY_URL/api/v1/analyses/$analysis_id/decisions" \
        -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
        -d "{\"action_taken\": \"declined\", \"portfolio_id\": \"$PORTFOLIO_ID\"}"
      echo
    done
    ;;
  *) die "usage: bash scripts/tiers.sh [open|all|decline <analysis-uuid> ...]" ;;
esac
