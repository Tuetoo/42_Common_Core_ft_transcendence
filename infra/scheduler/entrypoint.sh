#!/bin/sh
set -e

# `crond` does not hand its own launch environment to the jobs it
# spawns, so persist what the job scripts need to a file they source.
#
# The scheduler gets env_file .env.docker, so this file carries
# INTERNAL_API_TOKEN (job scripts), the POSTGRES_* credentials, the Qdrant
# settings, the S3_* object storage settings (backup.sh copies the objects
# through the S3 API) and BACKUP_RETENTION_DAYS (backup.sh / restore.sh).
# Values are single-quoted (quotes inside a
# value are escaped) so any character survives, and the file is only
# readable by root because it holds secrets.

ENV_FILE=/scripts/env.sh

# Prints `export NAME='value'` for NAME when it is set in the environment.
emit() {
    name="$1"
    eval "isset=\${$name+x}"
    [ -n "$isset" ] || return 0
    eval "value=\${$name}"
    escaped=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    printf "export %s='%s'\n" "$name" "$escaped"
}

umask 077
{
    echo "export CORE_SERVICE_URL='${CORE_SERVICE_URL:-http://core-service:8000}'"
    echo "export AI_SERVICE_URL='${AI_SERVICE_URL:-http://ai-service:8000}'"
    echo "export AUTH_SERVICE_URL='${AUTH_SERVICE_URL:-http://auth-service:8000}'"
    for name in INTERNAL_API_TOKEN \
        POSTGRES_HOST POSTGRES_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
        QDRANT_URL QDRANT_HOST QDRANT_PORT QDRANT_API_KEY \
        S3_ENDPOINT_URL S3_ACCESS_KEY S3_SECRET_KEY S3_REGION \
        AVATAR_DATA_DIR \
        BACKUP_DIR BACKUP_RETENTION_DAYS TZ; do
        emit "$name"
    done
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

if [ -z "${INTERNAL_API_TOKEN:-}" ]; then
    echo "[$(date)] scheduler: WARNING INTERNAL_API_TOKEN is empty; jobs only work while the services run with APP_ENV=development/test" >&2
fi

# With arguments, run that one command (for example
# `docker compose run --rm --no-deps scheduler /scripts/call-job.sh
# deleted-user-sweep-core`) instead of cron, so a single job can be run by
# hand where the scheduler stays stopped (the cloud host).
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Job output is written to /proc/1/fd/1 and
# /proc/1/fd/2 (see crontab), i.e. the stdout/stderr of this script, which
# stays PID 1, so `docker compose logs scheduler` and the `vector`
# collector see every job line.
#
# crond is NOT exec'd: dcron calls setpgid() on startup, which fails with
# "Operation not permitted" when it is PID 1 (a session leader) and makes
# it exit, so the container restart-looped. It runs as a child instead,
# and this script forwards SIGTERM/SIGINT so `docker compose stop` still
# stops it cleanly instead of waiting for the kill timeout.
#
# -l 5 (notice): dcron logs to stderr with -f, so its "time disparity"
# notice reaches `docker compose logs scheduler`. dcron runs missed jobs
# late after a clock jump of up to 60 minutes (the host slept) and skips
# them, logging only that notice, after a longer jump.
crond -f -l 5 &
CROND_PID=$!
trap 'kill -TERM "$CROND_PID" 2>/dev/null; wait "$CROND_PID"; exit 0' TERM INT
wait "$CROND_PID"
