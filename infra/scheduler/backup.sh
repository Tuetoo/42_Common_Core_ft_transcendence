#!/bin/sh
# Daily backup of every stateful
# service AlphaMind cannot rebuild on its own. See docs/DISASTER_RECOVERY.md.
#
# Writes one directory per run:
#   /backups/<YYYY-MM-DD_HHMM>/            (host: ./backups/...)
#     postgres.dump            pg_dump -Fc of the whole database
#                              (auth, core and ai schemas)
#     qdrant/<collection>.snapshot
#                              one Qdrant snapshot per collection (created
#                              through the REST API, downloaded, then
#                              deleted from Qdrant again)
#     objectstore/<bucket>/<key>
#                              every object of every bucket, copied through
#                              the object storage's S3 API (rclone, see
#                              s3-remote.sh); an empty bucket is an empty
#                              folder. Each object is one plain file whose
#                              path below the bucket folder is its key.
#     avatars.tar.gz           auth-service's uploaded avatars (the
#                              avatar_data volume, read-only), when mounted
#     SHA256SUMS               `sha256sum -c SHA256SUMS` verifies the files
#     manifest.json            sizes and checksums of the top-level and
#                              Qdrant files, Qdrant collections with their
#                              point counts, object count and bytes per
#                              bucket, and the alembic revision of each
#                              schema
# then deletes backup directories older than BACKUP_RETENTION_DAYS (7).
#
# Why the S3 API and not a copy of the SeaweedFS data directory: every
# S3 GET returns one complete object, and rclone checks its MD5, so each
# copied object is consistent even while documents are uploaded or
# deleted. A file copy of the live volume (filer database plus volume
# files written at different moments) can be torn. The copy does not
# depend on SeaweedFS's on-disk format, and restore.sh puts the objects
# back through the same API. An object uploaded after its bucket was
# listed is in the next backup.
#
# Safety rules:
# - The run is built in /backups/.incomplete-<stamp> and renamed to
#   /backups/<stamp> only when every step succeeded. A failed run is kept
#   as /backups/<stamp>-FAILED for inspection and the script exits 1.
# - Old backups are pruned ONLY after today's backup succeeded.
# - One run at a time (lock directory /backups/.lock).
#
# Usage:
#   backup.sh [--dry-run]
# Cron runs it daily (infra/scheduler/crontab); by hand:
#   docker compose --env-file .env.docker exec scheduler /scripts/backup.sh
#   docker compose --env-file .env.docker exec scheduler /scripts/backup.sh --dry-run
#
# Environment (from .env.docker through the scheduler's env_file):
#   POSTGRES_HOST/PORT/DB/USER/PASSWORD, QDRANT_URL (or QDRANT_HOST/PORT),
#   QDRANT_API_KEY (optional), BACKUP_RETENTION_DAYS (default 7),
#   S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION (the
#   services' own object storage settings), BACKUP_DIR (default /backups),
#   AVATAR_DATA_DIR (default /avatar-data; skipped when not mounted).

set -eu

if [ -f /scripts/env.sh ]; then
    # shellcheck disable=SC1091
    . /scripts/env.sh
fi
# shellcheck source=infra/scheduler/s3-remote.sh
. "$(dirname "$0")/s3-remote.sh"

DRY_RUN=0
usage() {
    sed -n '2,/^set -eu/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "backup: unknown argument '$arg' (use --dry-run or --help)" >&2; exit 2 ;;
    esac
done

BACKUP_ROOT="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
AVATAR_DATA_DIR="${AVATAR_DATA_DIR:-/avatar-data}"
QDRANT_URL="${QDRANT_URL:-http://${QDRANT_HOST:-qdrant}:${QDRANT_PORT:-6333}}"
QDRANT_URL="${QDRANT_URL%/}"
PGHOST="${POSTGRES_HOST:-postgres}"
PGPORT="${POSTGRES_PORT:-5432}"
PGDATABASE="${POSTGRES_DB:-alphamind}"
PGUSER="${POSTGRES_USER:-postgres}"
export PGHOST PGPORT PGDATABASE PGUSER
if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    PGPASSWORD="$POSTGRES_PASSWORD"
    export PGPASSWORD
fi
SCHEMAS="auth core ai"

STAMP="$(date +%Y-%m-%d_%H%M)"
FINAL_DIR="$BACKUP_ROOT/$STAMP"
WORK_DIR="$BACKUP_ROOT/.incomplete-$STAMP"
LOCK_DIR="$BACKUP_ROOT/.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] backup: $*"; }
err() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] backup: ERROR: $*" >&2; }
die() { err "$*"; exit 1; }

case "$RETENTION_DAYS" in
    ''|*[!0-9]*) die "BACKUP_RETENTION_DAYS must be a whole number, got '$RETENTION_DAYS'" ;;
esac
[ "$RETENTION_DAYS" -ge 1 ] || die "BACKUP_RETENTION_DAYS must be at least 1"

# Qdrant REST call; adds the api-key header only when one is configured.
qdrant() {
    if [ -n "${QDRANT_API_KEY:-}" ]; then
        curl -sS -f --max-time 900 -H "api-key: ${QDRANT_API_KEY}" "$@"
    else
        curl -sS -f --max-time 900 "$@"
    fi
}

# Exact number of points in a collection (empty when it cannot be read).
point_count() {
    qdrant -X POST -H 'Content-Type: application/json' -d '{"exact": true}' \
        "$QDRANT_URL/collections/$1/points/count" 2>/dev/null \
        | jq -r '.result.count // empty' 2>/dev/null || true
}

# Backup directories older than the retention window (only names that
# look like a backup run, never anything else under $BACKUP_ROOT).
expired_backups() {
    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
        -name '20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]*' \
        -mmin +"$((RETENTION_DAYS * 1440))"
}

if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY RUN -- nothing is written, dumped or deleted"
    log "would create $WORK_DIR and rename it to $FINAL_DIR on success"
    log "would run: pg_dump -Fc -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f postgres.dump (schemas: $SCHEMAS)"
    log "would read alembic_version from schemas: $SCHEMAS"
    log "would list collections at $QDRANT_URL/collections, then for each: POST .../snapshots?wait=true, download it to qdrant/<collection>.snapshot, DELETE it from Qdrant"
    log "would copy every object of every bucket at $S3_ENDPOINT into objectstore/<bucket>/ (rclone, S3 API)"
    if [ -d "$AVATAR_DATA_DIR" ]; then
        log "would archive $AVATAR_DATA_DIR into avatars.tar.gz"
    else
        log "would skip avatars ($AVATAR_DATA_DIR is not mounted)"
    fi
    log "would write SHA256SUMS and manifest.json"
    log "would then delete backups older than $RETENTION_DAYS day(s) under $BACKUP_ROOT:"
    if [ -d "$BACKUP_ROOT" ]; then
        expired="$(expired_backups)"
        if [ -n "$expired" ]; then
            printf '%s\n' "$expired" | sed 's/^/    /'
        else
            echo "    (none)"
        fi
    else
        echo "    ($BACKUP_ROOT does not exist)"
    fi
    for tool in pg_dump psql curl jq tar sha256sum rclone; do
        command -v "$tool" >/dev/null 2>&1 || log "note: '$tool' is not installed here"
    done
    exit 0
fi

for tool in pg_dump pg_restore psql curl jq tar sha256sum rclone; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool '$tool' is not installed"
done
[ -d "$BACKUP_ROOT" ] || die "$BACKUP_ROOT does not exist (mount ./backups:/backups)"
[ -w "$BACKUP_ROOT" ] || die "$BACKUP_ROOT is not writable"

# --- lock -------------------------------------------------------------------
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    other="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        die "another backup is running (pid $other); not starting a second one"
    fi
    log "removing stale lock (pid '${other:-?}' is not running)"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" || die "cannot create lock $LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"

SUCCESS=0
QDRANT_PENDING=""   # "collection snapshot" of a snapshot still on Qdrant
cleanup() {
    status=$?
    if [ -n "$QDRANT_PENDING" ]; then
        # shellcheck disable=SC2086
        set -- $QDRANT_PENDING
        qdrant -X DELETE "$QDRANT_URL/collections/$1/snapshots/$2" >/dev/null 2>&1 || \
            err "could not delete snapshot $2 of collection $1 from Qdrant; delete it by hand"
    fi
    if [ "$SUCCESS" -ne 1 ] && [ -d "$WORK_DIR" ]; then
        mv "$WORK_DIR" "$FINAL_DIR-FAILED" 2>/dev/null || true
        err "backup FAILED; partial files kept in $FINAL_DIR-FAILED; older backups were NOT pruned"
    fi
    rm -rf "$LOCK_DIR"
    if [ "$SUCCESS" -ne 1 ] && [ "$status" -eq 0 ]; then
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

[ ! -e "$FINAL_DIR" ] || die "$FINAL_DIR already exists (two runs in the same minute?)"
mkdir -p "$WORK_DIR/qdrant"
log "starting backup into $FINAL_DIR"

# --- (a) Postgres -----------------------------------------------------------
log "postgres: dumping database '$PGDATABASE' on $PGHOST:$PGPORT"
pg_dump -Fc --no-password -f "$WORK_DIR/postgres.dump" \
    || die "pg_dump failed"
# Reading the archive's table of contents proves it is a complete,
# readable custom-format dump.
pg_restore -l "$WORK_DIR/postgres.dump" > /dev/null \
    || die "pg_restore -l could not read the dump"
PG_SERVER_VERSION="$(psql -X -At --no-password -c 'SHOW server_version' 2>/dev/null || echo unknown)"

ALEMBIC_JSON="{}"
for schema in $SCHEMAS; do
    rev="$(psql -X -At --no-password -c "SELECT version_num FROM \"$schema\".alembic_version LIMIT 1" 2>/dev/null || true)"
    if [ -z "$rev" ]; then
        log "postgres: WARNING schema '$schema' has no alembic_version row"
        ALEMBIC_JSON="$(printf '%s' "$ALEMBIC_JSON" | jq -c --arg s "$schema" '. + {($s): null}')"
    else
        ALEMBIC_JSON="$(printf '%s' "$ALEMBIC_JSON" | jq -c --arg s "$schema" --arg r "$rev" '. + {($s): $r}')"
    fi
done
log "postgres: done ($(wc -c < "$WORK_DIR/postgres.dump") bytes), alembic $ALEMBIC_JSON"

# --- (b) Qdrant -------------------------------------------------------------
log "qdrant: listing collections at $QDRANT_URL"
collections_json="$(qdrant "$QDRANT_URL/collections")" || die "cannot list Qdrant collections"
COLLECTIONS="$(printf '%s' "$collections_json" | jq -r '.result.collections[].name')" \
    || die "unexpected Qdrant /collections response"
QDRANT_POINTS="{}"
for collection in $COLLECTIONS; do
    log "qdrant: snapshotting collection '$collection'"
    points_before="$(point_count "$collection")"
    snapshot_json="$(qdrant -X POST "$QDRANT_URL/collections/$collection/snapshots?wait=true")" \
        || die "snapshot of collection '$collection' failed"
    snapshot="$(printf '%s' "$snapshot_json" | jq -r '.result.name // empty')"
    [ -n "$snapshot" ] || die "Qdrant returned no snapshot name for '$collection'"
    QDRANT_PENDING="$collection $snapshot"
    qdrant -o "$WORK_DIR/qdrant/$collection.snapshot" \
        "$QDRANT_URL/collections/$collection/snapshots/$snapshot" \
        || die "download of snapshot '$snapshot' ($collection) failed"
    qdrant -X DELETE "$QDRANT_URL/collections/$collection/snapshots/$snapshot" > /dev/null \
        || die "could not delete snapshot '$snapshot' of '$collection' from Qdrant"
    QDRANT_PENDING=""
    points_after="$(point_count "$collection")"
    # The point count goes into the manifest only when no write happened
    # around the snapshot, so restore.sh --verify can compare it exactly.
    if [ -n "$points_before" ] && [ "$points_before" = "$points_after" ]; then
        QDRANT_POINTS="$(printf '%s' "$QDRANT_POINTS" | jq -c --arg c "$collection" --argjson n "$points_after" '. + {($c): $n}')"
    else
        QDRANT_POINTS="$(printf '%s' "$QDRANT_POINTS" | jq -c --arg c "$collection" '. + {($c): null}')"
    fi
    log "qdrant: '$collection' done ($(wc -c < "$WORK_DIR/qdrant/$collection.snapshot") bytes)"
done
[ -n "$COLLECTIONS" ] || log "qdrant: no collections to back up"

# --- (c) object storage (SeaweedFS, through its S3 API) --------------------
log "objectstore: listing buckets at $S3_ENDPOINT"
BUCKETS="$(s3_buckets)" || die "cannot list the buckets at $S3_ENDPOINT (check S3_ENDPOINT_URL and the S3 keys)"
mkdir -p "$WORK_DIR/objectstore"
BUCKETS_JSON="[]"
for bucket in $BUCKETS; do
    target="$WORK_DIR/objectstore/$bucket"
    mkdir -p "$target"
    log "objectstore: copying bucket '$bucket'"
    s3 copy "objectstore:$bucket" "$target" \
        || die "copy of bucket '$bucket' failed"
    size="$(local_size "$target")" || die "cannot measure the copy of '$bucket'"
    objects="${size% *}"
    bytes="${size#* }"
    BUCKETS_JSON="$(printf '%s' "$BUCKETS_JSON" | jq -c --arg b "$bucket" --argjson n "$objects" --argjson s "$bytes" '. + [{name: $b, objects: $n, bytes: $s}]')"
    log "objectstore: '$bucket' done ($objects objects, $bytes bytes)"
done
[ -n "$BUCKETS" ] || log "objectstore: no buckets to back up"

# --- (c2) auth-service avatars (optional volume) -----------------------------
AVATARS_FILE=""
if [ -d "$AVATAR_DATA_DIR" ]; then
    log "avatars: archiving $AVATAR_DATA_DIR"
    tar -czf "$WORK_DIR/avatars.tar.gz" -C "$AVATAR_DATA_DIR" . \
        || die "tar of $AVATAR_DATA_DIR failed"
    AVATARS_FILE="avatars.tar.gz"
    log "avatars: done ($(wc -c < "$WORK_DIR/avatars.tar.gz") bytes)"
else
    log "avatars: $AVATAR_DATA_DIR is not mounted, skipped"
fi

# --- (d) checksums + manifest ----------------------------------------------
(
    cd "$WORK_DIR"
    find . -type f ! -name SHA256SUMS ! -name manifest.json | sed 's#^\./##' | LC_ALL=C sort \
        | while IFS= read -r f; do sha256sum "$f"; done > SHA256SUMS
) || die "could not compute checksums"

files_json="$(
    cd "$WORK_DIR"
    while read -r sum path; do
        case "$path" in objectstore/*) continue ;; esac
        size="$(wc -c < "$path" | tr -d ' ')"
        jq -n -c --arg p "$path" --arg s "$sum" --argjson b "$size" \
            '{path: $p, bytes: $b, sha256: $s}'
    done < SHA256SUMS | jq -s -c '.'
)" || die "could not build the file list"

# Manifest format 3: objects copied through the S3 API (format 2 had a
# tarball of the SeaweedFS data directory, format 1 a MinIO tarball).
jq -n \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg stamp "$STAMP" \
    --arg db "$PGDATABASE" \
    --arg pgv "$PG_SERVER_VERSION" \
    --arg qdrant_url "$QDRANT_URL" \
    --arg s3_endpoint "$S3_ENDPOINT" \
    --arg avatars_file "$AVATARS_FILE" \
    --argjson retention "$RETENTION_DAYS" \
    --argjson alembic "$ALEMBIC_JSON" \
    --argjson files "$files_json" \
    --argjson points "$QDRANT_POINTS" \
    --argjson buckets "$BUCKETS_JSON" \
    --arg collections "$COLLECTIONS" \
    '{
        format: 3,
        created_at: $created_at,
        name: $stamp,
        retention_days: $retention,
        postgres: {database: $db, server_version: $pgv, file: "postgres.dump",
                   alembic_versions: $alembic},
        qdrant: {url: $qdrant_url,
                 collections: ($collections | split("\n") | map(select(length > 0))),
                 points: $points},
        objectstore: {engine: "seaweedfs", method: "s3", endpoint: $s3_endpoint,
                      dir: "objectstore", buckets: $buckets},
        avatars: (if $avatars_file == "" then null else {file: $avatars_file} end),
        files: $files,
        total_bytes: (($files | map(.bytes) | add // 0)
                      + ($buckets | map(.bytes) | add // 0))
     }' > "$WORK_DIR/manifest.json" || die "could not write manifest.json"

mv "$WORK_DIR" "$FINAL_DIR" || die "could not rename $WORK_DIR to $FINAL_DIR"
SUCCESS=1
log "backup complete: $FINAL_DIR ($(du -sh "$FINAL_DIR" | cut -f1))"

# --- retention (only after a successful backup) ------------------------------
# The newest $KEEP_MIN complete backups are kept whatever their age, so a
# long pause (machine asleep, scheduler down) cannot leave just one backup
# after the next run.
KEEP_MIN=3
keep="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name '20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]' | sort | tail -n "$KEEP_MIN")"
pruned=0
for old in $(expired_backups); do
    [ "$old" = "$FINAL_DIR" ] && continue
    case "
$keep
" in *"
$old
"*) continue ;; esac
    log "retention: deleting $old (older than $RETENTION_DAYS days)"
    rm -rf "$old" || err "retention: could not delete $old"
    pruned=$((pruned + 1))
done
log "retention: deleted $pruned backup(s) older than $RETENTION_DAYS days (the newest $KEEP_MIN are always kept)"
# A run killed outright (SIGKILL: the container recreated mid-backup) skips
# the EXIT trap and leaves .incomplete-<stamp> behind, holding a full
# database dump. Removed after a day.
for stale in $(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
        -name '.incomplete-*' -mmin +1440); do
    log "retention: deleting leftover $stale"
    rm -rf "$stale" || err "retention: could not delete $stale"
done
# Report success (alerted on when missing for > 26 h).
/scripts/heartbeat.sh backup || true
