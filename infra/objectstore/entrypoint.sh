#!/bin/sh
# Entrypoint of the `objectstore` service (SeaweedFS).
#
# It writes the S3 identity file from S3_ACCESS_KEY / S3_SECRET_KEY (the
# same keys every service uses, from .env.docker through compose
# interpolation) and then hands over to the image's own /entrypoint.sh,
# which fixes /data ownership, drops to the unprivileged `seaweed` user and
# starts `weed server ...` with the arguments given as the compose
# `command`.
#
# -s3.config is always added here, so the S3 gateway always runs with
# authentication: SeaweedFS treats a config file as "authentication on",
# and there is no anonymous identity in it, so an unsigned request or a
# wrong key gets 403 AccessDenied.
#
# The file is recreated on every start (a changed key in .env.docker takes
# effect after `docker compose up -d objectstore`) and is readable only by
# the seaweed user. The secret never appears on a command line.
#
# Environment:
#   S3_ACCESS_KEY, S3_SECRET_KEY   required
#   OBJECTSTORE_S3_CONFIG          where to write the file
#                                  (default /etc/seaweedfs/s3.json)
#   SEAWEEDFS_ENTRYPOINT           the image entrypoint to exec
#                                  (default /entrypoint.sh)

set -eu

die() { echo "objectstore-entrypoint: ERROR: $*" >&2; exit 1; }

: "${S3_ACCESS_KEY:?set S3_ACCESS_KEY (docker compose --env-file .env.docker)}"
: "${S3_SECRET_KEY:?set S3_SECRET_KEY (docker compose --env-file .env.docker)}"
CONFIG="${OBJECTSTORE_S3_CONFIG:-/etc/seaweedfs/s3.json}"
UPSTREAM="${SEAWEEDFS_ENTRYPOINT:-/entrypoint.sh}"

# A newline or a tab in a key would need JSON escapes that this small
# script does not produce; refuse it instead of writing a broken file.
nl='
'
tab="$(printf '\t')"
for value in "$S3_ACCESS_KEY" "$S3_SECRET_KEY"; do
    case "$value" in
        *"$nl"*|*"$tab"*) die "S3_ACCESS_KEY / S3_SECRET_KEY must not contain a newline or a tab" ;;
    esac
done

# JSON string escaping for the two characters that need it here.
json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

umask 077
mkdir -p "$(dirname "$CONFIG")"
tmp="$CONFIG.tmp.$$"
cat > "$tmp" <<EOF
{
  "identities": [
    {
      "name": "alphamind",
      "credentials": [
        {
          "accessKey": "$(json_escape "$S3_ACCESS_KEY")",
          "secretKey": "$(json_escape "$S3_SECRET_KEY")"
        }
      ],
      "actions": ["Admin", "Read", "List", "Tagging", "Write"]
    }
  ]
}
EOF
mv "$tmp" "$CONFIG"
# The image entrypoint re-executes itself as the `seaweed` user, which must
# be able to read the file.
if [ "$(id -u)" = "0" ] && id seaweed > /dev/null 2>&1; then
    chown seaweed:seaweed "$CONFIG"
fi
echo "objectstore-entrypoint: wrote the S3 identity file $CONFIG (1 identity)"

[ $# -gt 0 ] || die "no command given (expected: server -s3 ...)"
exec "$UPSTREAM" "$@" "-s3.config=$CONFIG"
