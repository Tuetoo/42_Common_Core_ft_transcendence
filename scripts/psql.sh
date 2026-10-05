#!/usr/bin/env bash
# psql inside the postgres container (the container's own user and database).
#
#   bash scripts/psql.sh                          # interactive session
#   bash scripts/psql.sh -c "SELECT count(*) FROM ai.analyses;"
#   bash scripts/psql.sh < query.sql
set -euo pipefail
. "$(dirname "$0")/lib.sh"

if [ "$#" -eq 0 ] && [ -t 0 ]; then
  exec "${COMPOSE[@]}" exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
fi
if [ "$#" -eq 0 ]; then
  psql_stdin
  exit
fi
# Arguments are passed to psql unchanged; the user and database names are
# read from the container first so no inner shell has to re-quote them.
pg_user=$("${COMPOSE[@]}" exec -T postgres printenv POSTGRES_USER)
pg_db=$("${COMPOSE[@]}" exec -T postgres printenv POSTGRES_DB)
exec "${COMPOSE[@]}" exec -T postgres psql -U "$pg_user" -d "$pg_db" "$@"
