#!/usr/bin/env bash
# redis-cli inside the redis container.
#
#   bash scripts/redis-cli.sh                      # interactive session
#   bash scripts/redis-cli.sh INFO persistence
#   bash scripts/redis-cli.sh -n 2 PUBSUB CHANNELS 'events:*'   # pub/sub channels with a subscriber now
set -euo pipefail
. "$(dirname "$0")/lib.sh"

if [ "$#" -eq 0 ]; then
  exec "${COMPOSE[@]}" exec redis redis-cli
fi
exec "${COMPOSE[@]}" exec -T redis redis-cli "$@"
