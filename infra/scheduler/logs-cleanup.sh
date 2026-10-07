#!/bin/sh
# Retention for the per-service daily log files the `vector`
# service writes (./logs/<service>/<YYYY-MM-DD>.log on the host, mounted
# at /logs here). Deletes *.log files not modified for more than
# RETENTION_DAYS (default 14) days, then removes directories left empty.
#
# Runs daily from the scheduler's crontab; can also be run by hand:
#   docker compose exec scheduler /scripts/logs-cleanup.sh
# or on the host from the repo root:
#   LOGS_DIR=./logs sh infra/scheduler/logs-cleanup.sh
#
# POSIX sh + BusyBox find (Alpine): no GNU-only flags. `-mtime +N`
# matches files whose age, in whole days, is greater than N -- i.e. at
# least N+1 full days old -- so today's file and the previous 14 days'
# files are always kept. Vector closes idle files after 60s, so an old
# file is never still being written when it is deleted.

set -eu

LOGS_DIR="${LOGS_DIR:-/logs}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

# The heartbeat script exists only in the scheduler container; a host run
# skips it.
heartbeat() {
    script=/scripts/heartbeat.sh
    if [ -f "$script" ]; then
        /scripts/heartbeat.sh logs-cleanup
    else
        echo "[$(date)] logs-cleanup: $script not found, success not reported"
    fi
}

case "$RETENTION_DAYS" in
    ''|*[!0-9]*)
        echo "[$(date)] logs-cleanup: RETENTION_DAYS must be a whole number, got '$RETENTION_DAYS'" >&2
        exit 1
        ;;
esac

if [ ! -d "$LOGS_DIR" ]; then
    echo "[$(date)] logs-cleanup: $LOGS_DIR does not exist, nothing to do"
    heartbeat
    exit 0
fi

deleted=$(find "$LOGS_DIR" -type f -name '*.log' -mtime +"$RETENTION_DAYS" -print -delete | wc -l)
# Empty per-service directories (e.g. a one-off container that has not
# logged for two weeks). -mindepth 1 keeps $LOGS_DIR itself, which is a
# bind-mount point.
find "$LOGS_DIR" -mindepth 1 -type d -empty -delete

echo "[$(date)] logs-cleanup: deleted $deleted log file(s) older than $RETENTION_DAYS days under $LOGS_DIR"
heartbeat
