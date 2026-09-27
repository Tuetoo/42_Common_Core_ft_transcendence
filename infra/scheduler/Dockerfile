FROM alpine:3.20

RUN apk add --no-cache dcron curl tzdata

ENV TZ=America/New_York

RUN mkdir -p /scripts
COPY infra/scheduler/crontab /etc/crontabs/root
COPY infra/scheduler/entrypoint.sh /entrypoint.sh
COPY infra/scheduler/market-open.sh /scripts/market-open.sh
COPY infra/scheduler/market-close.sh /scripts/market-close.sh
COPY infra/scheduler/review-run.sh /scripts/review-run.sh
COPY infra/scheduler/rag-ingest.sh /scripts/rag-ingest.sh
COPY infra/scheduler/tier-monitoring.sh /scripts/tier-monitoring.sh
COPY infra/scheduler/logs-cleanup.sh /scripts/logs-cleanup.sh
RUN chmod +x /entrypoint.sh /scripts/market-open.sh /scripts/market-close.sh \
    /scripts/review-run.sh /scripts/rag-ingest.sh /scripts/tier-monitoring.sh \
    /scripts/logs-cleanup.sh

ENTRYPOINT ["/entrypoint.sh"]