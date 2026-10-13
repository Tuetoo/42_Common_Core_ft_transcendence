#!/usr/bin/env bash
# Deploy main on the cloud host. Runs on that host: by the deploy workflow
# over SSH (.github/workflows/deploy.yml), or by hand:
#
#   bash scripts/deploy.sh
#
# The workflow sends the commit it checked as the SSH command. The host's
# authorized_keys entry forces this script, so sshd passes that text in
# SSH_ORIGINAL_COMMAND: it must be a 40-hex commit on origin/main, and the
# checkout is fast-forwarded to exactly that commit (never to a newer one
# whose checks may still be running). Without it (by hand), the checkout is
# fast-forwarded to origin/main. Then the script rebuilds the images, restarts
# the services whose image or configuration changed and waits until every
# service is healthy (exit 1 otherwise). Uses docker-compose.yaml only:
# the development override is never loaded on the host. .env.docker and
# the gateway certificate are not in git and are never touched.
# A restart of ai-service ends an analysis that is still running.
# The scheduler keeps its state: if it was not running before the deploy
# (the cloud host keeps it stopped so the shared LLM and news quotas are
# not spent twice), it is left out of `up` and never starts; its image is
# still rebuilt, so a later `up -d scheduler` runs the new code.
set -euo pipefail
cd "$(dirname "$0")/.."

export COMPOSE_FILE=docker-compose.yaml
compose=(docker compose --env-file .env.docker)

[ -f .env.docker ] || { echo "error: .env.docker is missing" >&2; exit 1; }
[ -f infra/gateway/certs/server.crt ] \
  || { echo "error: infra/gateway/certs/server.crt is missing" >&2; exit 1; }

target=origin/main
sha=${SSH_ORIGINAL_COMMAND:-}
if [ -n "$sha" ]; then
  [[ $sha =~ ^[0-9a-f]{40}$ ]] \
    || { echo "error: the SSH command must be a 40-hex commit sha" >&2; exit 1; }
  target=$sha
fi

git fetch --quiet origin main
git merge-base --is-ancestor "$target" origin/main 2>/dev/null \
  || { echo "error: $target is not a commit on origin/main" >&2; exit 1; }
git merge --ff-only --quiet "$target"
echo "deploying $(git log -1 --format='%h %s')"

up=(up -d --build --remove-orphans --wait --wait-timeout 300)
if [ -n "$("${compose[@]}" ps -q --status running scheduler)" ]; then
  "${compose[@]}" "${up[@]}"
else
  # A read loop, not mapfile: the script also runs (in tests) under the
  # bash 3.2 that macOS ships.
  services=()
  while IFS= read -r service; do
    [ -n "$service" ] && services+=("$service")
  done < <("${compose[@]}" config --services | grep -vx scheduler || true)
  [ "${#services[@]}" -gt 0 ] \
    || { echo "error: could not list the compose services" >&2; exit 1; }
  "${compose[@]}" build scheduler
  "${compose[@]}" "${up[@]}" "${services[@]}"
  # Only if something started it as a dependency; nothing does today.
  "${compose[@]}" stop scheduler
  echo "scheduler was not running before the deploy: kept stopped" \
    "(start it with: docker compose --env-file .env.docker up -d scheduler)"
fi
docker image prune -f > /dev/null
"${compose[@]}" ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
