#!/usr/bin/env bash
# Print a short-lived access token.
#
#   bash scripts/token.sh admin              # the admin account (role admin)
#   bash scripts/token.sh owner              # the test portfolio owner (role user)
#   bash scripts/token.sh <user-uuid> [role] # any user; role defaults to user
#   HOURS=4 bash scripts/token.sh admin
#
# Typical use:  TOKEN=$(bash scripts/token.sh admin)
set -euo pipefail
. "$(dirname "$0")/lib.sh"

who="${1:-}"
[ -n "$who" ] || die "usage: bash scripts/token.sh admin|owner|<user-uuid> [role]"
case "$who" in
  admin) make_token "$ADMIN_USER_ID" admin "${HOURS:-2}" ;;
  owner) make_token "$OWNER_USER_ID" user "${HOURS:-2}" ;;
  *) make_token "$who" "${2:-user}" "${HOURS:-2}" ;;
esac
