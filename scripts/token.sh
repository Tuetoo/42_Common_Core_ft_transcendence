#!/usr/bin/env bash
# Print a short-lived access token.
#
#   bash scripts/token.sh admin              # ADMIN_USER_ID (role admin)
#   bash scripts/token.sh owner              # OWNER_USER_ID (role user)
#   bash scripts/token.sh <user-uuid> [role] # any user; role defaults to user
#   HOURS=4 bash scripts/token.sh admin
#
# Typical use:  TOKEN=$(bash scripts/token.sh admin)
set -euo pipefail
. "$(dirname "$0")/lib.sh"

who="${1:-}"
[ -n "$who" ] || die "usage: bash scripts/token.sh admin|owner|<user-uuid> [role]"
case "$who" in
  admin) require_admin_id; make_token "$ADMIN_USER_ID" admin "${HOURS:-2}" ;;
  owner) require_owner_id; make_token "$OWNER_USER_ID" user "${HOURS:-2}" ;;
  *) make_token "$who" "${2:-user}" "${HOURS:-2}" ;;
esac
