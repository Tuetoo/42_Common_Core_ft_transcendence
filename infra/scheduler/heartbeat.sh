#!/bin/sh
# Report a scheduled job's success to core-service
# (POST /internal/scheduler/heartbeat/<job>), which exports it as
# alphamind_job_last_success_timestamp_seconds{scheduled_job} (the label
# "job" is Prometheus' scrape job) for the "job has not
# succeeded for too long" alerts (infra/prometheus/alert_rules.yml, group
# scheduled_jobs). Never fails the job itself: a heartbeat that cannot be
# sent is logged; the alert then shows the job as stale, which is the
# point (core-service or the network is not working).
# Usage: /scripts/heartbeat.sh <job>
. /scripts/env.sh
job="$1"
if curl -sS -f --max-time 10 -X POST "${CORE_SERVICE_URL}/internal/scheduler/heartbeat/${job}" \
    -H "X-Internal-Token: ${INTERNAL_API_TOKEN:-}" > /dev/null; then
    echo "[$(date)] ${job}: success reported"
else
    echo "[$(date)] ${job}: could not report success to core-service (curl exit code $?)" >&2
fi
exit 0
