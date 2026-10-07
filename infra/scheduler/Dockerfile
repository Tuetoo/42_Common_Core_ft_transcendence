FROM alpine:3.20

# postgresql16-client (pg_dump / pg_restore / psql)
# matches the postgres:16-alpine server -- pg_dump refuses to dump a
# server newer than itself, so bump both together. jq parses the Qdrant
# REST responses and writes manifest.json. rclone copies the object
# storage's objects through its S3 API (backup.sh, restore.sh).
RUN apk add --no-cache dcron curl tzdata jq postgresql16-client rclone

ENV TZ=America/New_York

# /backups and /avatar-data (and, for the `restore` service,
# /objectstore-data) are NOT created here on purpose -- they must come
# from compose mounts, and backup.sh refuses to run (or skips avatars)
# when they are missing, instead of silently backing up an empty
# directory inside the container.
RUN mkdir -p /scripts
COPY infra/scheduler/crontab /etc/crontabs/root
COPY infra/scheduler/entrypoint.sh /entrypoint.sh
COPY infra/scheduler/call-job.sh /scripts/call-job.sh
COPY infra/scheduler/logs-cleanup.sh /scripts/logs-cleanup.sh
COPY infra/scheduler/backup.sh /scripts/backup.sh
COPY infra/scheduler/restore.sh /scripts/restore.sh
COPY infra/scheduler/s3-remote.sh /scripts/s3-remote.sh
COPY infra/scheduler/heartbeat.sh /scripts/heartbeat.sh
RUN chmod +x /entrypoint.sh /scripts/call-job.sh \
    /scripts/logs-cleanup.sh /scripts/backup.sh /scripts/restore.sh \
    /scripts/heartbeat.sh

ENTRYPOINT ["/entrypoint.sh"]
