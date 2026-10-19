#!/bin/sh
# Healthcheck of the `objectstore` service.
#
# Sends a real, SigV4-signed S3 ListBuckets request (GET /) to the local S3
# port with the configured keys -- the same kind of request the services'
# boto3 client makes. Healthy only when SeaweedFS answers 200, which needs
# the S3 gateway, its filer, master and volume server to be up and the
# identity file to accept the keys. The keys are passed to curl on stdin
# (-K -), never on its command line.
#
# Environment: S3_ACCESS_KEY, S3_SECRET_KEY (required), S3_REGION
# (default us-east-1). The S3 port is 9000.

set -eu

: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is not set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is not set}"
REGION="${S3_REGION:-us-east-1}"

# curl config-file string escaping (backslash and double quote).
curl_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

printf 'user = "%s:%s"\n' "$(curl_escape "$S3_ACCESS_KEY")" "$(curl_escape "$S3_SECRET_KEY")" \
    | curl -K - -sS -f -o /dev/null --max-time 4 \
        --aws-sigv4 "aws:amz:${REGION}:s3" \
        "http://127.0.0.1:9000/"
