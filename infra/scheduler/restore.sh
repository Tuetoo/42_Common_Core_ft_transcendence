#!/bin/sh
# Restores one backup directory
# written by backup.sh, or checks a restore. See docs/DISASTER_RECOVERY.md
# for the full procedure (which services to stop first, a full rebuild
# from a fresh clone, and the monthly restore drill).
#
# DRY RUN BY DEFAULT: without --yes it only verifies the backup
# (checksums, dump readable) and prints what it would do.
#
# Usage:
#   restore.sh [--yes] [--only postgres|qdrant|objectstore|avatars] <backup dir>
#   restore.sh --verify [--only ...] <backup dir>
#     <backup dir>  /backups/2026-09-29_0230 or just 2026-09-29_0230
#     --only X      restore (or verify) a single part (repeatable);
#                   default: all parts present in the backup
#     --yes         really restore (destructive: replaces current data)
#     --verify      change nothing; compare the running data services with
#                   the backup after a restore and exit 1 on any
#                   difference (see "Verify" below)
#
# What each part does:
#   postgres  in ONE transaction on the running database: the auth, core
#             and ai schemas are dropped (CASCADE, so objects newer than
#             the backup go too) and recreated from the dump's SQL
#             Any error changes nothing. Stop auth-service,
#             core-service and ai-service first so nothing holds locks or
#             writes in between.
#   qdrant    for each qdrant/<collection>.snapshot: upload + recover it
#             through the REST API (POST /collections/<name>/snapshots/
#             upload?priority=snapshot), replacing that collection.
#   objectstore
#             for each objectstore/<bucket>/ folder: creates the bucket
#             if needed and makes it identical to the folder through the
#             S3 API (rclone sync --checksum: missing or different objects
#             are uploaded, objects that are not in the backup are
#             deleted). Needs the objectstore service RUNNING; stop
#             core-service and ai-service first. Buckets that are not in
#             the backup are left alone. Content-Type is set from each
#             key's file extension (the services never read it back).
#             Older backups hold a tarball of the SeaweedFS data
#             directory (objectstore-data.tar.gz) instead: it is extracted
#             into OBJECTSTORE_DATA_DIR (default /objectstore-data), which
#             needs that volume mounted READ-WRITE (the `restore` compose
#             service, profile "tools") and the objectstore service
#             STOPPED. Backups from the MinIO era hold MinIO's files
#             (minio-data.tar.gz), which SeaweedFS cannot read: this part
#             is then skipped (refused with --only objectstore).
#   avatars   empties auth-service's avatar_data volume (AVATAR_DATA_DIR,
#             default /avatar-data, mounted read-write) and extracts
#             avatars.tar.gz into it, when the backup has one. Stop
#             auth-service first.
#
# Verify (--verify), for each part:
#   postgres  every schema's alembic revision equals the manifest's, and
#             each schema has as many tables as the dump holds table data
#             for; prints the row counts of auth.users, core.portfolios
#             and ai.analyses
#   qdrant    every collection of the manifest exists; its point count
#             equals the manifest's when the backup recorded one
#   objectstore
#             rclone check of every bucket against its folder (same keys,
#             sizes and MD5 in both directions)
#   avatars   the volume's files equal the archive's
#
# Typical use (see the Makefile targets `restore` and `restore-drill`):
#   docker compose --env-file .env.docker stop auth-service core-service ai-service scheduler
#   docker compose --env-file .env.docker up -d --wait objectstore
#   docker compose --env-file .env.docker --profile tools run --rm restore \
#       /scripts/restore.sh --yes /backups/2026-09-29_0230
#   docker compose --env-file .env.docker --profile tools run --rm restore \
#       /scripts/restore.sh --verify /backups/2026-09-29_0230
#   docker compose --env-file .env.docker up -d

set -eu

if [ -f /scripts/env.sh ]; then
    # shellcheck disable=SC1091
    . /scripts/env.sh
fi
# shellcheck source=infra/scheduler/s3-remote.sh
. "$(dirname "$0")/s3-remote.sh"

usage() {
    sed -n '2,/^set -eu/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

CONFIRM=0
VERIFY=0
ONLY=""
TARGET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) CONFIRM=1 ;;
        --verify) VERIFY=1 ;;
        --only)
            [ $# -ge 2 ] || { echo "restore: --only needs a value" >&2; exit 2; }
            case "$2" in
                postgres|qdrant|objectstore|avatars) ONLY="$ONLY $2" ;;
                *) echo "restore: --only must be postgres, qdrant, objectstore or avatars" >&2; exit 2 ;;
            esac
            shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "restore: unknown option '$1'" >&2; exit 2 ;;
        *)
            [ -z "$TARGET" ] || { echo "restore: only one backup directory" >&2; exit 2; }
            TARGET="$1" ;;
    esac
    shift
done
[ -n "$TARGET" ] || { usage >&2; exit 2; }
if [ "$CONFIRM" -eq 1 ] && [ "$VERIFY" -eq 1 ]; then
    echo "restore: --yes and --verify are separate runs (restore first, then verify)" >&2
    exit 2
fi
EXPLICIT_ONLY=0
[ -z "$ONLY" ] || EXPLICIT_ONLY=1
[ -n "$ONLY" ] || ONLY="postgres qdrant objectstore avatars"

BACKUP_ROOT="${BACKUP_DIR:-/backups}"
case "$TARGET" in
    /*) SRC="$TARGET" ;;
    *) SRC="$BACKUP_ROOT/$TARGET" ;;
esac
SRC="${SRC%/}"
# SeaweedFS data directory (tarball backups only).
OBJECTSTORE_DATA_DIR="${OBJECTSTORE_DATA_DIR:-/objectstore-data}"
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

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] restore: $*"; }
die() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] restore: ERROR: $*" >&2; exit 1; }
wants() { case " $ONLY " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

qdrant() {
    if [ -n "${QDRANT_API_KEY:-}" ]; then
        curl -sS -f --max-time 1800 -H "api-key: ${QDRANT_API_KEY}" "$@"
    else
        curl -sS -f --max-time 1800 "$@"
    fi
}

point_count() {
    qdrant -X POST -H 'Content-Type: application/json' -d '{"exact": true}' \
        "$QDRANT_URL/collections/$1/points/count" 2>/dev/null \
        | jq -r '.result.count // empty' 2>/dev/null || true
}

FAILURES=0
mismatch() {
    log "FAIL: $*"
    FAILURES=$((FAILURES + 1))
}

# --verify: compare the running data services with the backup.
verify_backup() {
    manifest="$SRC/manifest.json"
    log "VERIFY: comparing the running data services with $SRC (nothing is changed): $ONLY"
    if wants postgres; then
        toc="$(pg_restore -l "$SRC/postgres.dump")"
        for schema in auth core ai; do
            expected="$(jq -r --arg s "$schema" '.postgres.alembic_versions[$s] // ""' "$manifest")"
            rev="$(psql -X -At --no-password -c "SELECT version_num FROM \"$schema\".alembic_version LIMIT 1" 2>/dev/null || true)"
            if [ "$rev" = "$expected" ]; then
                log "postgres: schema $schema alembic revision ${rev:-<none>} matches"
            else
                mismatch "postgres: schema $schema is at alembic revision '${rev:-<none>}', the backup at '${expected:-<none>}'"
            fi
            dumped="$(printf '%s\n' "$toc" | grep -c "^[0-9]*; [0-9]* [0-9]* TABLE DATA $schema " || true)"
            live="$(psql -X -At --no-password -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = '$schema' AND table_type = 'BASE TABLE'" 2>/dev/null || echo "?")"
            if [ "$live" = "$dumped" ]; then
                log "postgres: schema $schema has the backup's $dumped tables"
            else
                mismatch "postgres: schema $schema has $live tables, the backup $dumped"
            fi
        done
        for table in auth.users core.portfolios ai.analyses; do
            rows="$(psql -X -At --no-password -c "SELECT count(*) FROM $table" 2>/dev/null || echo "not readable")"
            log "postgres: $table rows: $rows"
        done
    fi
    if wants qdrant; then
        for collection in $(jq -r '.qdrant.collections[]' "$manifest"); do
            points="$(point_count "$collection")"
            expected="$(jq -r --arg c "$collection" '.qdrant.points[$c] // empty' "$manifest")"
            if [ -z "$points" ]; then
                mismatch "qdrant: collection '$collection' is missing or not readable"
            elif [ -z "$expected" ]; then
                log "qdrant: collection '$collection' has $points points (the backup recorded no count)"
            elif [ "$points" = "$expected" ]; then
                log "qdrant: collection '$collection' has the backup's $points points"
            else
                mismatch "qdrant: collection '$collection' has $points points, the backup $expected"
            fi
        done
    fi
    if wants objectstore && [ "$OBJECTSTORE_KIND" = s3 ]; then
        for dir in $BUCKET_DIRS; do
            bucket="$(basename "$dir")"
            if s3 check "$dir" "objectstore:$bucket"; then
                log "objectstore: bucket '$bucket' matches the backup ($(find "$dir" -type f | wc -l | tr -d ' ') objects)"
            else
                mismatch "objectstore: bucket '$bucket' differs from the backup (see the rclone lines above)"
            fi
        done
    fi
    if wants objectstore && [ "$OBJECTSTORE_KIND" = tar ]; then
        log "objectstore: a tarball backup cannot be compared object by object, skipped"
    fi
    if wants avatars; then
        unpacked="$(mktemp -d /tmp/restore-avatars.XXXXXX)"
        tar -xzf "$SRC/avatars.tar.gz" -C "$unpacked" || mismatch "avatars: avatars.tar.gz cannot be extracted"
        if diff -r "$unpacked" "$AVATAR_DATA_DIR" > /dev/null 2>&1; then
            log "avatars: $AVATAR_DATA_DIR matches the backup"
        else
            mismatch "avatars: $AVATAR_DATA_DIR differs from the backup (or is not mounted)"
        fi
        rm -rf "$unpacked"
    fi
    if [ "$FAILURES" -eq 0 ]; then
        log "VERIFY OK: the running data matches $SRC"
        return 0
    fi
    log "VERIFY FAILED: $FAILURES difference(s) from $SRC"
    return 1
}

# --- verify the backup (always, also in dry-run) ---------------------------
[ -d "$SRC" ] || die "backup directory $SRC does not exist"
case "$SRC" in
    *-FAILED|*/.incomplete-*) die "$SRC is a failed/incomplete backup; pick a complete one" ;;
esac
[ -f "$SRC/manifest.json" ] || die "$SRC/manifest.json missing: not a complete backup"
[ -f "$SRC/SHA256SUMS" ] || die "$SRC/SHA256SUMS missing: not a complete backup"
log "verifying checksums in $SRC"
( cd "$SRC" && sha256sum -c SHA256SUMS ) > /dev/null || die "checksum mismatch in $SRC (run: sha256sum -c SHA256SUMS there)"
log "checksums OK"
if command -v jq > /dev/null 2>&1; then
    log "backup taken at $(jq -r '.created_at' "$SRC/manifest.json"), alembic versions $(jq -c '.postgres.alembic_versions' "$SRC/manifest.json")"
fi

if wants postgres; then
    [ -f "$SRC/postgres.dump" ] || die "postgres.dump missing"
    pg_restore -l "$SRC/postgres.dump" > /dev/null || die "postgres.dump is not readable by pg_restore"
fi
# How the object storage was backed up: "s3" (objectstore/<bucket>/
# folders), "tar" (a tarball of the SeaweedFS data directory) or, from
# the MinIO era, MinIO's files, which must never be extracted into the
# SeaweedFS volume.
OBJECTSTORE_KIND=""
if [ -d "$SRC/objectstore" ]; then
    OBJECTSTORE_KIND=s3
elif [ -f "$SRC/objectstore-data.tar.gz" ]; then
    OBJECTSTORE_KIND=tar
fi
if wants objectstore && [ -z "$OBJECTSTORE_KIND" ]; then
    if [ -f "$SRC/minio-data.tar.gz" ]; then
        msg="this backup is from before the move to SeaweedFS: it has MinIO's minio-data.tar.gz, which SeaweedFS cannot read (its documents are in SeaweedFS and in newer backups)"
    else
        msg="this backup has no object storage part (objectstore/ or objectstore-data.tar.gz)"
    fi
    [ "$EXPLICIT_ONLY" -eq 0 ] || die "$msg"
    log "WARNING: $msg -- the object storage part is skipped"
    ONLY="$(printf '%s' "$ONLY" | sed 's/ *objectstore//')"
fi
if wants avatars && [ ! -f "$SRC/avatars.tar.gz" ]; then
    [ "$EXPLICIT_ONLY" -eq 0 ] || die "avatars.tar.gz missing in this backup"
    log "this backup has no avatars.tar.gz; avatars are skipped"
    ONLY="$(printf '%s' "$ONLY" | sed 's/ *avatars//')"
fi
SNAPSHOTS=""
if wants qdrant && [ -d "$SRC/qdrant" ]; then
    SNAPSHOTS="$(find "$SRC/qdrant" -maxdepth 1 -type f -name '*.snapshot' | sort)"
fi

# Bucket folders of an S3 backup, one per line.
BUCKET_DIRS=""
if wants objectstore && [ "$OBJECTSTORE_KIND" = s3 ]; then
    BUCKET_DIRS="$(find "$SRC/objectstore" -mindepth 1 -maxdepth 1 -type d | sort)"
fi

if [ "$VERIFY" -eq 1 ]; then
    verify_backup
    exit $?
fi

if [ "$CONFIRM" -ne 1 ]; then
    log "DRY RUN (add --yes to restore). Would restore: $ONLY"
    wants postgres && log "would run, in ONE transaction on $PGDATABASE (host $PGHOST:$PGPORT): DROP SCHEMA auth, core, ai CASCADE, then the SQL of $SRC/postgres.dump (pg_restore --no-owner -f -)"
    if wants qdrant; then
        if [ -n "$SNAPSHOTS" ]; then
            for f in $SNAPSHOTS; do
                log "would upload+recover $(basename "$f" .snapshot) from $f to $QDRANT_URL"
            done
        else
            log "no Qdrant snapshots in this backup"
        fi
    fi
    if wants objectstore && [ "$OBJECTSTORE_KIND" = s3 ]; then
        if [ -z "$BUCKET_DIRS" ]; then
            log "no buckets in this backup"
        fi
        for dir in $BUCKET_DIRS; do
            log "would make bucket $(basename "$dir") at $S3_ENDPOINT identical to $dir ($(find "$dir" -type f | wc -l | tr -d ' ') objects; objects not in the backup are deleted)"
        done
        if s3_reachable; then
            log "object storage at $S3_ENDPOINT answers"
        else
            log "WARNING: object storage at $S3_ENDPOINT does not answer; start it before the real restore"
        fi
    fi
    wants objectstore && [ "$OBJECTSTORE_KIND" = tar ] && log "would empty $OBJECTSTORE_DATA_DIR and extract $SRC/objectstore-data.tar.gz into it (objectstore must be stopped)"
    wants avatars && log "would empty $AVATAR_DATA_DIR and extract $SRC/avatars.tar.gz into it (auth-service must be stopped)"
    exit 0
fi

# --- preflight: every precondition of every part, before anything is replaced
# A part that cannot run must stop the restore here, not after Postgres or
# Qdrant were already replaced (that would leave a mix of backup and live
# data).
writable_dir() {
    [ -d "$2" ] || die "$2 does not exist (mount the $1 volume there); nothing was changed"
    probe="$2/.restore-write-test.$$"
    if ! ( : > "$probe" ) 2>/dev/null; then
        die "$2 is read-only: run the $1 part through the 'restore' compose service (profile tools); nothing was changed"
    fi
    rm -f "$probe"
}
log "preflight: checking every part before anything is replaced"
if wants postgres; then
    psql -X -At --no-password -c 'SELECT 1' > /dev/null 2>&1 \
        || die "postgres at $PGHOST:$PGPORT does not answer; nothing was changed"
fi
if wants qdrant && [ -n "$SNAPSHOTS" ]; then
    qdrant "$QDRANT_URL/collections" > /dev/null \
        || die "qdrant at $QDRANT_URL does not answer; nothing was changed"
fi
if wants objectstore && [ "$OBJECTSTORE_KIND" = s3 ]; then
    s3_reachable || die "object storage at $S3_ENDPOINT does not answer: start it (docker compose up -d --wait objectstore) and run again; nothing was changed"
fi
if wants objectstore && [ "$OBJECTSTORE_KIND" = tar ]; then
    ! s3_reachable || die "object storage at $S3_ENDPOINT is running: stop it (docker compose stop objectstore) before its data directory is replaced; nothing was changed"
    writable_dir objectstore "$OBJECTSTORE_DATA_DIR"
fi
if wants avatars; then
    writable_dir avatars "$AVATAR_DATA_DIR"
fi
log "preflight: OK"

# --- (1) Postgres -------------------------------------------------------------
if wants postgres; then
    log "postgres: restoring into '$PGDATABASE' on $PGHOST:$PGPORT (drops the auth, core and ai schemas and recreates them from the dump)"
    # `pg_restore --clean` would drop each schema WITHOUT CASCADE and fail
    # on a live database holding objects newer than the backup (e.g. a
    # table from a later migration). Instead the dump is converted to SQL
    # first (a broken dump stops here, nothing touched), then ONE psql
    # transaction drops the three schemas with CASCADE and runs that SQL;
    # any error rolls everything back.
    restore_sql="$(umask 077 && mktemp /tmp/restore-postgres.XXXXXX)"
    trap 'rm -f "$restore_sql"' EXIT
    pg_restore --no-owner --no-password -f "$restore_sql" "$SRC/postgres.dump" \
        || die "pg_restore could not read the dump (nothing was changed)"
    { printf 'DROP SCHEMA IF EXISTS auth, core, ai CASCADE;\n'; cat "$restore_sql"; } \
        | psql -X -q --no-password -v ON_ERROR_STOP=1 --single-transaction \
            -d "$PGDATABASE" > /dev/null \
        || die "postgres restore failed (nothing was changed: single transaction)"
    rm -f "$restore_sql"
    for schema in auth core ai; do
        rev="$(psql -X -At --no-password -c "SELECT version_num FROM \"$schema\".alembic_version LIMIT 1" 2>/dev/null || true)"
        log "postgres: schema $schema alembic revision ${rev:-<none>}"
    done
    log "postgres: done (services run 'alembic upgrade head' on their next start if the code is newer)"
fi

# --- (2) Qdrant ---------------------------------------------------------------
if wants qdrant; then
    if [ -z "$SNAPSHOTS" ]; then
        log "qdrant: no snapshots in this backup, skipped"
    fi
    for f in $SNAPSHOTS; do
        collection="$(basename "$f" .snapshot)"
        log "qdrant: recovering collection '$collection' from $(basename "$f")"
        qdrant -X POST \
            "$QDRANT_URL/collections/$collection/snapshots/upload?priority=snapshot&wait=true" \
            -F "snapshot=@$f" > /dev/null || die "Qdrant recovery of '$collection' failed"
    done
    [ -z "$SNAPSHOTS" ] || log "qdrant: done"
fi

# --- (3) object storage / (4) avatars: replace a mounted volume's files ----
# $1 label, $2 target directory, $3 archive
restore_volume() {
    log "$1: emptying $2 and extracting $(basename "$3")"
    find "$2" -mindepth 1 -maxdepth 1 -exec rm -rf {} \; || die "could not empty $2"
    tar -xzf "$3" -C "$2" || die "extracting $(basename "$3") failed"
    log "$1: done"
}
if wants objectstore && [ "$OBJECTSTORE_KIND" = s3 ]; then
    for dir in $BUCKET_DIRS; do
        bucket="$(basename "$dir")"
        log "objectstore: restoring bucket '$bucket'"
        s3 mkdir "objectstore:$bucket" || die "could not create bucket '$bucket'"
        s3 sync --checksum "$dir" "objectstore:$bucket" \
            || die "restore of bucket '$bucket' failed"
    done
    log "objectstore: done"
fi
if wants objectstore && [ "$OBJECTSTORE_KIND" = tar ]; then
    restore_volume objectstore "$OBJECTSTORE_DATA_DIR" "$SRC/objectstore-data.tar.gz"
    log "objectstore: start the objectstore service again"
fi
if wants avatars; then
    restore_volume avatars "$AVATAR_DATA_DIR" "$SRC/avatars.tar.gz"
fi

log "restore of $SRC complete: $ONLY"
