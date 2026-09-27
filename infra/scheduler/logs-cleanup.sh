#!/bin/sh
set -eu

LOGS_DIR="${LOGS_DIR:-/logs}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

case "$RETENTION_DAYS" in
    ''|*[!0-9]*)
        echo "[$(date)] logs-cleanup: RETENTION_DAYS must be a whole number, got '$RETENTION_DAYS'" >&2
        exit 1
        ;;
esac

if [ ! -d "$LOGS_DIR" ]; then
    echo "[$(date)] logs-cleanup: $LOGS_DIR does not exist, nothing to do"
    exit 0
fi

deleted=$(find "$LOGS_DIR" -type f -name '*.log' -mtime +"$RETENTION_DAYS" -print -delete | wc -l)
find "$LOGS_DIR" -mindepth 1 -type d -empty -delete

echo "[$(date)] logs-cleanup: deleted $deleted log file(s) older than $RETENTION_DAYS days under $LOGS_DIR"