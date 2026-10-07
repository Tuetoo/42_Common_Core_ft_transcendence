# Sourced by backup.sh and restore.sh, not run on its own.
#
# Defines the rclone remote `objectstore:` for the object storage's S3 API
# (SeaweedFS), with the same endpoint and keys the services use
# (S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION from
# .env.docker). rclone reads the remote from these environment variables,
# so the keys never appear on a command line or in a config file. rclone
# checks the MD5 of every object it copies against the object's ETag.

S3_ENDPOINT="${S3_ENDPOINT_URL:-http://objectstore:9000}"
S3_ENDPOINT="${S3_ENDPOINT%/}"
RCLONE_CONFIG_OBJECTSTORE_TYPE=s3
RCLONE_CONFIG_OBJECTSTORE_PROVIDER=SeaweedFS
RCLONE_CONFIG_OBJECTSTORE_ENV_AUTH=false
RCLONE_CONFIG_OBJECTSTORE_ENDPOINT="$S3_ENDPOINT"
RCLONE_CONFIG_OBJECTSTORE_REGION="${S3_REGION:-us-east-1}"
RCLONE_CONFIG_OBJECTSTORE_ACCESS_KEY_ID="${S3_ACCESS_KEY:-}"
RCLONE_CONFIG_OBJECTSTORE_SECRET_ACCESS_KEY="${S3_SECRET_KEY:-}"
export RCLONE_CONFIG_OBJECTSTORE_TYPE RCLONE_CONFIG_OBJECTSTORE_PROVIDER \
    RCLONE_CONFIG_OBJECTSTORE_ENV_AUTH RCLONE_CONFIG_OBJECTSTORE_ENDPOINT \
    RCLONE_CONFIG_OBJECTSTORE_REGION RCLONE_CONFIG_OBJECTSTORE_ACCESS_KEY_ID \
    RCLONE_CONFIG_OBJECTSTORE_SECRET_ACCESS_KEY

# rclone with errors only on stderr and retries for transient failures.
s3() {
    rclone --log-level ERROR --retries 3 --low-level-retries 10 "$@"
}

# Bucket names, one per line.
s3_buckets() {
    s3 lsf --dirs-only objectstore: | sed 's#/$##'
}

# True when the S3 API answers with the configured keys (one quick try).
s3_reachable() {
    rclone --log-level ERROR --retries 1 --low-level-retries 1 \
        --contimeout 5s --timeout 10s \
        lsf --dirs-only objectstore: > /dev/null 2>&1
}

# "<count> <bytes>" of the files below a local directory.
local_size() {
    rclone --log-level ERROR size --json "$1" | jq -r '"\(.count) \(.bytes)"'
}
