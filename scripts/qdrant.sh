#!/usr/bin/env bash
# Look at Qdrant through its REST API (published on 127.0.0.1:6333 only).
#
#   bash scripts/qdrant.sh                 # collections with point counts
#   bash scripts/qdrant.sh info <name>     # one collection's full description
#   bash scripts/qdrant.sh count <name>    # exact number of points
# The web dashboard is at http://127.0.0.1:6333/dashboard
set -euo pipefail
. "$(dirname "$0")/lib.sh"

base="${QDRANT_LOCAL_URL:-http://127.0.0.1:6333}"
cmd="${1:-list}"

count_points() {
  curl -sS -X POST "$base/collections/$1/points/count" \
    -H "Content-Type: application/json" -d '{"exact": true}' |
    python3 -c "import sys, json; print(json.load(sys.stdin)['result']['count'])"
}

case "$cmd" in
  list)
    names=$(curl -sS "$base/collections" |
      python3 -c "import sys, json; print('\n'.join(c['name'] for c in json.load(sys.stdin)['result']['collections']))")
    [ -n "$names" ] || { echo "no collections"; exit 0; }
    for name in $names; do
      printf '%-40s %s points\n' "$name" "$(count_points "$name")"
    done
    ;;
  info)
    [ -n "${2:-}" ] || die "usage: bash scripts/qdrant.sh info <collection>"
    curl -sS "$base/collections/$2" | python3 -m json.tool
    ;;
  count)
    [ -n "${2:-}" ] || die "usage: bash scripts/qdrant.sh count <collection>"
    count_points "$2"
    ;;
  *) die "usage: bash scripts/qdrant.sh [list|info <name>|count <name>]" ;;
esac
