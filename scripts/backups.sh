#!/usr/bin/env bash
# Backups made by the scheduler's nightly backup.sh (./backups/<YYYY-MM-DD_HHMM>/).
#
#   bash scripts/backups.sh                 # list backups with their size
#   bash scripts/backups.sh now             # make one now (same as make backup)
#   bash scripts/backups.sh check [name]    # restore dry run of a backup
#                                           # (default: the newest); changes nothing
#   bash scripts/backups.sh drill [name]    # restore drill: restores the backup
#                                           # (default: the newest) into a separate,
#                                           # throwaway compose project and checks it
#                                           # with restore.sh --verify; the live stack
#                                           # is not touched
# A real restore is only done with: make restore BACKUP=<name> CONFIRM=yes
#
# The drill runs postgres, qdrant, objectstore and the restore tool as the
# compose project "alphamind-drill" (own volumes and network, no host
# ports, see infra/drill/docker-compose.drill.yml), restores everything
# with restore.sh --yes, runs restore.sh --verify (alembic revisions,
# table counts, Qdrant point counts, every object by size and MD5,
# avatars), prints the time it took and removes the drill project with
# its volumes, also when a step fails.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

cmd="${1:-list}"
# Backup folders are named YYYY-MM-DD_HHMM, so glob order is date order.
newest() {
  local last="" dir
  for dir in backups/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]; do
    [ -d "$dir" ] && last="${dir#backups/}"
  done
  echo "$last"
}

case "$cmd" in
  list)
    [ -d backups ] || die "no ./backups folder yet"
    for dir in backups/*/; do
      [ -d "$dir" ] || continue
      dir="${dir%/}"
      printf '%-18s %s\n' "${dir#backups/}" "$(du -sh "$dir" | cut -f1)"
    done
    ;;
  now)
    "${COMPOSE[@]}" exec scheduler /scripts/backup.sh
    ;;
  check)
    name="${2:-$(newest)}"
    [ -n "$name" ] || die "no backup found"
    echo "restore dry run of $name"
    # --build: the restore image is never rebuilt by `up --build`, so an
    # old image would run an old restore.sh.
    "${COMPOSE[@]}" --profile tools run --rm --build restore /scripts/restore.sh "/backups/$name"
    ;;
  drill)
    name="${2:-$(newest)}"
    [ -n "$name" ] || die "no backup found"
    [ -d "backups/$name" ] || die "no backup backups/$name"
    project=alphamind-drill
    live="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_ROOT")}"
    [ "$(printf '%s' "$live" | tr '[:upper:]' '[:lower:]')" != "$project" ] \
      || die "the live compose project is named $project; rename the folder or set COMPOSE_PROJECT_NAME"
    drill=(docker compose --env-file .env.docker -p "$project"
      -f docker-compose.yaml -f infra/drill/docker-compose.drill.yml --profile tools)
    remove_drill() {
      echo "removing the drill project $project and its volumes"
      "${drill[@]}" down -v --rmi local --remove-orphans > /dev/null 2>&1 || true
    }
    trap remove_drill EXIT
    started=$(date +%s)
    echo "restore drill of $name in the compose project $project"
    "${drill[@]}" up -d --wait postgres qdrant objectstore
    "${drill[@]}" run --rm --build restore /scripts/restore.sh --yes "/backups/$name"
    "${drill[@]}" run --rm restore /scripts/restore.sh --verify "/backups/$name"
    echo "restore drill of $name passed in $(( $(date +%s) - started )) s"
    ;;
  *) die "usage: bash scripts/backups.sh [list|now|check [name]|drill [name]]" ;;
esac
