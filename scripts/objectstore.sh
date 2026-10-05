#!/usr/bin/env bash
# Look at the S3 object storage (SeaweedFS) with the application's own
# client and keys, from inside core-service.
#
#   bash scripts/objectstore.sh                  # buckets: object count and size
#   bash scripts/objectstore.sh ls [prefix]      # keys in the documents bucket (first 200)
#   bash scripts/objectstore.sh cat <key>        # print one object
set -euo pipefail
. "$(dirname "$0")/lib.sh"

cmd="${1:-buckets}"
case "$cmd" in
  buckets)
    "${COMPOSE[@]}" exec -T core-service python - <<'PY'
from config import settings
from shared.storage import _get_client

client = _get_client(settings)
for bucket in client.list_buckets().get("Buckets", []):
    name = bucket["Name"]
    count = size = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=name):
        for obj in page.get("Contents", []):
            count += 1
            size += obj["Size"]
    print(f"{name:30} {count:6} objects {size:14,} bytes")
PY
    ;;
  ls)
    "${COMPOSE[@]}" exec -T -e PREFIX="${2:-}" core-service python - <<'PY'
import os

from config import settings
from shared.storage import _get_client

client = _get_client(settings)
response = client.list_objects_v2(
    Bucket=settings.S3_BUCKET_DOCUMENTS, Prefix=os.environ["PREFIX"], MaxKeys=200,
)
for obj in response.get("Contents", []):
    print(f"{obj['Size']:12,}  {obj['LastModified']:%Y-%m-%d %H:%M}  {obj['Key']}")
if response.get("IsTruncated"):
    print("... more keys (narrow the prefix)")
PY
    ;;
  cat)
    [ -n "${2:-}" ] || die "usage: bash scripts/objectstore.sh cat <key>"
    "${COMPOSE[@]}" exec -T -e KEY="$2" core-service python - <<'PY'
import asyncio
import os
import sys

from config import settings
from shared.storage import download_bytes

content = asyncio.run(
    download_bytes(settings, bucket=settings.S3_BUCKET_DOCUMENTS, key=os.environ["KEY"])
)
sys.stdout.write(content.decode("utf-8"))
PY
    ;;
  *) die "usage: bash scripts/objectstore.sh [buckets|ls [prefix]|cat <key>]" ;;
esac
