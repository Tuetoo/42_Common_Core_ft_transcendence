#!/usr/bin/env bash
# One-screen health view of the running stack.
#
#   bash scripts/health.sh
#
# 1. container states (docker compose ps)
# 2. when each scheduled job last reported success (Prometheus gauge
#    alphamind_job_last_success_timestamp_seconds, fed by the scheduler's
#    heartbeat.sh through core-service); a job with no success yet is
#    labelled so (its gauge holds the time tracking began)
# 3. portfolios skipped by the last market-close run
# 4. alerts that are firing right now
# 5. NewsAPI requests in the last 24 h, as counted by ai-service
set -euo pipefail
. "$(dirname "$0")/lib.sh"

prom="${PROMETHEUS_LOCAL_URL:-http://127.0.0.1:9090}"

echo "== containers"
"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'

query() {
  curl -sS --get "$prom/api/v1/query" --data-urlencode "query=$1"
}

echo
echo "== scheduled jobs (last success)"
# Jobs with a real success are the fields of core:job_last_success. A job
# without one still has a gauge value: the time core-service began
# tracking it (core:job_tracked_since), so it is labelled "no success yet".
succeeded=$({ "${COMPOSE[@]}" exec -T redis redis-cli -n 0 --raw HKEYS \
  core:job_last_success 2>/dev/null || echo "?unreadable"; } | tr '\n' ' ')
query 'alphamind_job_last_success_timestamp_seconds' | SUCCEEDED="$succeeded" python3 -c '
import json, os, sys, time
from datetime import datetime, timezone
rows = json.load(sys.stdin)["data"]["result"]
names = os.environ.get("SUCCEEDED", "").split()
known = "?unreadable" not in names
if not rows:
    print("no value yet (core-service has not exported the job gauges)")


def job_name(row):
    # The job name is in "scheduled_job" ("job" is the Prometheus scrape job).
    return row["metric"].get("scheduled_job", "?")


for row in sorted(rows, key=job_name):
    ts = float(row["value"][1])
    age_h = (time.time() - ts) / 3600
    when = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if known and job_name(row) not in names:
        print(f"{job_name(row):20} no success yet (tracked since {when}, {age_h:.1f} h)")
    else:
        print(f"{job_name(row):20} {when}  ({age_h:.1f} h ago)")
if not known:
    print("(Redis not readable: a job without any success shows its tracking start)")
'

echo
echo "== market close: portfolios skipped in the last run"
query 'alphamind_market_close_portfolios_skipped' | python3 -c '
import json, sys
rows = json.load(sys.stdin)["data"]["result"]
print(rows[0]["value"][1] if rows else "no value yet")
'

echo
echo "== firing alerts"
curl -sS "$prom/api/v1/alerts" | python3 -c '
import json, sys
alerts = [a for a in json.load(sys.stdin)["data"]["alerts"] if a["state"] == "firing"]
if not alerts:
    print("none")
for a in alerts:
    labels = a["labels"]
    name = labels.get("alertname", "?")
    # A stale-job alert names the scheduled job in "scheduled_job";
    # "job" is only the Prometheus scrape job (e.g. core-service).
    where = (
        labels.get("scheduled_job")
        or labels.get("service")
        or labels.get("job")
        or labels.get("instance", "")
    )
    print(f"{name}  {where}")
'

echo
echo "== NewsAPI requests in the last 24 h (counted by ai-service)"
since=$(( $(date +%s) - 86400 ))
{ "${COMPOSE[@]}" exec -T redis redis-cli -n 0 --no-raw ZRANGEBYSCORE \
  ai:newsapi:requests "$since" +inf WITHSCORES 2>/dev/null || true; } | python3 -c '
import re, sys
from datetime import datetime
values = re.findall(r"\"([^\"]*)\"", sys.stdin.read())
scores = [float(v) for v in values[1::2]]
limit, batch = 100, 35
used = len(scores)
print(f"{used} of {limit} used, {limit - used} left (a 15-ticker batch needs about {batch})")
if scores:
    oldest = datetime.fromtimestamp(min(scores) + 86400).strftime("%a %H:%M")
    print(f"the oldest counted request leaves the window at {oldest} (local time)")
    if limit - used < batch:
        need = sorted(scores)[used - (limit - batch) - 1] + 86400
        ready = datetime.fromtimestamp(need).strftime("%a %H:%M")
        print(f"enough for one batch from about {ready} (local time)")
print("Counted from the requests this app made; the NewsAPI account page (updated hourly) is the provider view.")
'
