#!/usr/bin/env bash
# Deploy the latest main on the cloud host. Runs on that host: by the
# deploy workflow over SSH (.github/workflows/deploy.yml), or by hand:
#
#   bash scripts/deploy.sh
#
# Fast-forwards the checkout to origin/main, rebuilds the images, restarts
# the services whose image or configuration changed and waits until every
# service is healthy (exit 1 otherwise). Uses docker-compose.yaml only:
# the development override is never loaded on the host. .env.docker and
# the gateway certificate are not in git and are never touched.
# A restart of ai-service ends an analysis that is still running.
set -euo pipefail
cd "$(dirname "$0")/.."

export COMPOSE_FILE=docker-compose.yaml
compose=(docker compose --env-file .env.docker)

[ -f .env.docker ] || { echo "error: .env.docker is missing" >&2; exit 1; }
[ -f infra/gateway/certs/server.crt ] \
  || { echo "error: infra/gateway/certs/server.crt is missing" >&2; exit 1; }

git fetch --quiet origin main
git merge --ff-only --quiet origin/main
echo "deploying $(git log -1 --format='%h %s')"

"${compose[@]}" up -d --build --remove-orphans --wait --wait-timeout 300
docker image prune -f > /dev/null
"${compose[@]}" ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
