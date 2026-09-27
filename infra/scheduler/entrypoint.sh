#!/bin/sh
set -e

{
    echo "CORE_SERVICE_URL=${CORE_SERVICE_URL:-http://core-service:8000}"
    echo "AI_SERVICE_URL=${AI_SERVICE_URL:-http://ai-service:8000}"
} > /scripts/env.sh

touch /var/log/scheduler.log
crond -f -l 2