# Disaster recovery

This document explains how AlphaMind's data is backed up, how to check
that a backup is good, and how to restore it -- from a single service up
to a full rebuild on a new machine.

Scripts: `infra/scheduler/backup.sh` and `infra/scheduler/restore.sh`
(both inside the `scheduler` image, at `/scripts/`, with
`infra/scheduler/s3-remote.sh`, which points `rclone` at the object
storage's S3 API). Run either with `--help` for the full option list.
`scripts/backups.sh` (on the host) lists backups, checks one and runs the
restore drill.

## 1. What is backed up

| Data | Where it lives | How it is backed up | File in the backup |
|------|----------------|---------------------|--------------------|
| PostgreSQL: all three schemas (`auth`, `core`, `ai`) -- users, organizations, portfolios, transactions, research notes, analyses, recommendations, agent memory, ... | `postgres_data` volume | `pg_dump -Fc` of the whole database (consistent snapshot, the database stays online) | `postgres.dump` |
| Qdrant: every collection (RAG vectors, e.g. `research_documents_gemini`) | `qdrant_data` volume | Qdrant REST API: create a snapshot per collection, download it, delete it from Qdrant | `qdrant/<collection>.snapshot` |
| Object storage (SeaweedFS, service `objectstore`): every bucket, in practice `research-documents` (uploaded research files and archived raw SEC filings) | `objectstore_data` volume | every object copied through the S3 API with `rclone copy`, with the services' own endpoint and keys (`S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`); rclone checks each object's MD5 | `objectstore/<bucket>/<key>`: one plain file per object, an empty folder for an empty bucket |
| auth-service avatars (uploaded profile pictures) | `avatar_data` volume | `tar -czf` of the volume, mounted read-only | `avatars.tar.gz` |
| Checksums | -- | `sha256sum` of every file above | `SHA256SUMS` |
| Manifest | -- | time, sizes and checksums of the top-level and Qdrant files, Qdrant collections with their exact point counts, object count and bytes per bucket, and the **Alembic revision of each schema** | `manifest.json` |

Object storage is SeaweedFS (section 9). It is copied object by object
through its S3 API, not as a copy of its data directory: every S3 `GET`
returns one complete object and rclone checks its MD5, so each copied
object is consistent even when a document is uploaded or deleted during
the backup, while a file copy of the live volume (the filer database and
the volume files, written at different moments) can be torn. The copy does
not depend on SeaweedFS's on-disk format or image version, and the restore
puts the objects back through the same API. An object uploaded after its
bucket was listed is in the next night's backup. The backup keeps keys and
contents, not S3 metadata: on restore each object's `Content-Type` is set
from its key's file extension (the services never read it back).

`manifest.json` has `"format": 3`. A backup without an `objectstore/`
folder has no object storage part: a restore skips that part with a
warning (or stops, with `--only objectstore`).

The layout of one backup:

```
backups/2026-10-05_0230/
  postgres.dump
  qdrant/research_documents_gemini.snapshot
  objectstore/research-documents/documents/<user-id>/<hex>.pdf
  objectstore/research-documents/raw_filings/<TICKER>/<filing>.txt
  avatars.tar.gz
  SHA256SUMS            (every file above, objects included)
  manifest.json
```

## 2. Where, when, how long

- **Location:** `./backups/<YYYY-MM-DD_HHMM>/` in the repository folder on
  the host (mounted at `/backups` in the scheduler). `./backups/` is
  gitignored.
- **Schedule:** every day at **02:30 America/New_York** (scheduler
  crontab), between the review job (02:00) and RAG ingestion (03:00).
  Backups run only while the scheduler runs. The cloud host keeps it
  stopped, so the cloud makes no backups; backups are made on the local
  stack (`docs/DEPLOYMENT.md` section 6).
- **By hand:** `make backup`, which runs
  `docker compose --env-file .env.docker exec scheduler /scripts/backup.sh`
  (so the scheduler must be running).
  Add `--dry-run` to see what it would do without doing anything.
- **Retention:** backups older than `BACKUP_RETENTION_DAYS` days (default
  **7**, set in `.env.docker`) are deleted -- but only after the new
  backup finished successfully. A failed run never deletes anything.
  The newest 3 complete backups are always kept, whatever their age (so a
  host that was asleep for longer than the retention window still has
  backups after the next run), and `.incomplete-*` folders older than one
  day (left by a killed run) are deleted. A successful run reports to core-service
  (`/scripts/heartbeat.sh backup`); Prometheus alerts (`DailyJobStale`)
  when no backup has succeeded for 26 hours.
- **Failure handling:** each run is built in `./backups/.incomplete-<stamp>`
  and renamed to `./backups/<stamp>` only when every step succeeded. A
  failed run is kept as `./backups/<stamp>-FAILED`, the script exits with
  code 1 and logs `backup: ERROR: ...` (see `docker compose logs
  scheduler` or `./logs/scheduler/<date>.log`). Only one backup runs at a
  time.

**Important:** the backups are on the same disk as the live data. They
protect against a broken database, a bad migration, deleted data or a
lost Docker volume -- not against losing the whole machine. Copy
`./backups/` to another place (external disk, cloud storage) regularly,
and keep `.env.docker` and a copy of it somewhere safe as well (it is
**not** in the backups, see section 7). The backups contain personal
data and password hashes: store them as carefully as the database.

## 3. How to verify a backup

Every backup can be checked without restoring it:

```sh
cd backups/2026-09-29_0230
sha256sum -c SHA256SUMS            # every file, every object: OK
cat manifest.json                  # time, sizes, alembic revisions, buckets
```

`restore.sh` without `--yes` does the same checks plus "can pg_restore
read the dump" and prints what it would restore, without changing
anything:

```sh
docker compose --env-file .env.docker exec scheduler \
    /scripts/restore.sh 2026-09-29_0230
make backup-check BACKUP=2026-09-29_0230   # the same through the restore image
```

A backup is good when: the directory name has no `-FAILED` suffix, the
checksums pass, `manifest.json` lists every Qdrant collection you expect,
and the Alembic revisions are the ones your code expects (see
`alembic heads` in each service). Only a restore proves that it can be
restored: run the restore drill (section 6).

## 4. How to restore

`restore.sh` is **dry-run by default**. It only changes data with
`--yes`. Restore always replaces the current data of the chosen part with
the backup's data; anything written after the backup is lost for that
part.

Before it changes anything, a real restore runs a **preflight** of every
chosen part: PostgreSQL and Qdrant answer; for an `objectstore/` backup
the object storage's S3 API answers (objectstore running); the avatars
directory is mounted writable. If one check fails, the restore stops with "nothing was
changed", so a part that cannot run stops the restore before anything
changes. A part that fails while it runs (for example a Qdrant upload
error or an object storage sync error) leaves the parts before it
restored; fix the cause and run the same restore again (it is
repeatable, because each part replaces its data).

After a real restore, `restore.sh --verify <backup>` compares the running
data services with the backup without changing anything, and exits 1 on
any difference:

| Part | What `--verify` checks |
|------|------------------------|
| postgres | each schema's Alembic revision equals the manifest's; each schema has as many tables as the dump holds table data for; prints the row counts of `auth.users`, `core.portfolios` and `ai.analyses` |
| qdrant | every collection of the manifest exists, with exactly the point count the manifest recorded (the backup records it only when the count was the same before and after the snapshot) |
| objectstore | `rclone check` of every bucket against its `objectstore/<bucket>/` folder: the same keys, sizes and MD5 in both directions |
| avatars | the volume's files equal the files in `avatars.tar.gz` |

Run it before the services start again (they migrate the schemas
forward, which changes the Alembic revision). `make restore ...
CONFIRM=yes` runs it automatically and stops before `up` when it fails.

**First, rebuild the `restore` image.** The `restore` service belongs to
the compose profile `tools`, so `docker compose up -d --build` does NOT
rebuild it. Its image can therefore hold an old `restore.sh` that does
not understand current backups (for example one without `rclone`,
which cannot restore the `objectstore/` folders). `make restore`,
`make backup-check` and `make restore-drill` build it themselves
(`--build`); before running `restore.sh` through `docker compose run`
by hand on an existing machine, run:

```sh
docker compose --env-file .env.docker --profile tools build restore
```

On a new machine (section 4.4) the image is built on first use, so this
step is not needed there.

### 4.1 Only the database (PostgreSQL)

For example after a bad migration or deleted rows.

```sh
# 1. stop the services that write to the database
docker compose --env-file .env.docker stop auth-service core-service ai-service scheduler
# 2. check, then restore
docker compose --env-file .env.docker --profile tools run --rm restore \
    /scripts/restore.sh --only postgres /backups/2026-09-29_0230
docker compose --env-file .env.docker --profile tools run --rm restore \
    /scripts/restore.sh --yes --only postgres /backups/2026-09-29_0230
# 3. start everything again
docker compose --env-file .env.docker up -d
```

Check with `/scripts/restore.sh --verify --only postgres
/backups/2026-09-29_0230` before step 3.

`restore.sh` drops the `auth`, `core` and `ai` schemas with CASCADE and
recreates them from the dump, all in one transaction: if anything fails,
nothing changes. CASCADE matters: `pg_restore --clean` alone drops each
schema without CASCADE, so a database holding a table newer than the
backup -- exactly the "after a bad migration" case -- could not be
restored. On their next start the services run `alembic upgrade head`
(see section 8), so a backup from an older code version is migrated
forward automatically -- except a backup whose Alembic revision is not
in the current chains (the baselines `f1160*`), which cannot be
restored (section 8).

### 4.2 Only Qdrant

For example when the vector collection is broken or was deleted.

```sh
docker compose --env-file .env.docker --profile tools run --rm restore \
    /scripts/restore.sh --yes --only qdrant /backups/2026-09-29_0230
docker compose --env-file .env.docker restart ai-service   # rebuilds its BM25 index from Qdrant
```

Each `qdrant/<collection>.snapshot` is uploaded with
`POST /collections/<collection>/snapshots/upload?priority=snapshot`,
which creates or replaces that collection.

### 4.3 Only object storage (documents) or avatars

Objects are restored through the S3 API, so `objectstore` must be
**running**; the services that read or write documents are stopped:

```sh
docker compose --env-file .env.docker stop core-service ai-service scheduler
docker compose --env-file .env.docker up -d --wait objectstore
docker compose --env-file .env.docker --profile tools run --rm restore \
    /scripts/restore.sh --yes --only objectstore /backups/2026-09-29_0230
docker compose --env-file .env.docker --profile tools run --rm restore \
    /scripts/restore.sh --verify --only objectstore /backups/2026-09-29_0230
docker compose --env-file .env.docker up -d
```

For each `objectstore/<bucket>/` folder `restore.sh` creates the bucket if
needed and makes it identical to the folder (`rclone sync --checksum`):
missing or different objects are uploaded, unchanged ones are skipped,
and objects that are not in the backup (uploaded after it) are deleted.
Buckets that are not in the backup are left alone. A document whose
object is restored but whose database row is not (or the reverse) is a
mismatch between parts: restore PostgreSQL from the same backup.

For avatars use `--only avatars` and stop `auth-service` instead. The
`restore` service mounts `avatar_data` read-write; the scheduler mounts it
read-only, so avatars cannot be restored from inside the scheduler.

### 4.4 Full disaster: new machine, fresh clone

Needed: a copy of `./backups/<stamp>/` and your saved `.env.docker`.

1. Install Docker (with the compose plugin) and `make`, then clone the
   repository and `cd` into it.
2. Put your saved `.env.docker` in the repository root. If it is lost,
   create it from the example env file (`.env.example`) and set new
   secrets (`JWT_SECRET`, `INTERNAL_API_TOKEN`, `POSTGRES_PASSWORD`,
   `S3_ACCESS_KEY`/`S3_SECRET_KEY`, `GRAFANA_ADMIN_PASSWORD`, the LLM and
   data-provider API keys). A new `JWT_SECRET` logs every user out. Stored
   2FA secrets are encrypted with `TOTP_ENCRYPTION_KEY` (or, when that is
   empty, a key derived from `JWT_SECRET`): with new values, users who had
   2FA cannot pass the 2FA check and need it reset.
   `POSTGRES_USER`/`POSTGRES_DB` must match the old ones only for
   convenience; the dump is restored with `--no-owner`.
3. Generate the TLS certificate: `make certs` (self-signed, stored in
   `infra/gateway/certs/`, never backed up -- it is simply regenerated;
   browsers will ask to trust the new one).
4. Copy the backup directory to `./backups/<stamp>/`.
5. Start **only** the data services, so no application creates tables
   before the restore:
   ```sh
   docker compose --env-file .env.docker up -d postgres redis qdrant objectstore
   ```
6. Restore everything (dry run first, then with `--yes`, then verify):
   ```sh
   docker compose --env-file .env.docker --profile tools run --rm restore \
       /scripts/restore.sh /backups/<stamp>
   docker compose --env-file .env.docker --profile tools run --rm restore \
       /scripts/restore.sh --yes /backups/<stamp>
   docker compose --env-file .env.docker --profile tools run --rm restore \
       /scripts/restore.sh --verify /backups/<stamp>
   ```
   (`make restore BACKUP=<stamp> CONFIRM=yes` does the last two steps and
   starts the stack.)
7. Start the whole stack: `make up` (or
   `docker compose --env-file .env.docker up -d`).
8. Check: `https://localhost:8443/status` shows every service and
   dependency as up; log in with an existing account; open a portfolio
   and a research document; run one analysis.

## 5. RPO and RTO

- **RPO (how much data can be lost): up to 24 hours.** Backups are daily;
  anything written after the last successful backup (02:30 ET) is lost
  in a full restore. Run `make backup` before risky work (migrations,
  bulk deletes, upgrades) to bring this close to zero for that event.
- **RTO (how long a restore takes): about 30 minutes** for a full rebuild
  on a new machine with the current data size (image builds take most of
  it; the restore itself takes a few minutes). A single-part restore
  (4.1 to 4.3) takes a few minutes. The restore drill (section 6) prints
  the time of the restore and verification, without the application
  image builds.

These targets fit a development / school deployment. A production
deployment would add off-site copies, point-in-time recovery for
PostgreSQL (WAL archiving) and replication.

## 6. Restore drill (monthly)

An untested backup is not a backup. Once a month (and after any change
to the backup scripts or to the storage layout):

```sh
make restore-drill                          # the newest backup
make restore-drill BACKUP=2026-09-29_0230   # a given one
```

(= `bash scripts/backups.sh drill [<stamp>]`). The drill runs next to the
live stack without touching it:

1. It starts `postgres`, `qdrant` and `objectstore` as a separate compose
   project, `alphamind-drill`, from the same `docker-compose.yaml` and
   `.env.docker` plus `infra/drill/docker-compose.drill.yml`. The project
   has its own empty volumes and network; none of its containers publishes
   a host port (qdrant's `127.0.0.1:6333` is removed by the drill
   override), so nothing conflicts with the live stack. The live `vector`
   writes only its own project's container logs to `./logs`, so the drill
   leaves no lines there.
2. It builds that project's `restore` image and runs
   `restore.sh --yes` on the backup (all parts), then
   `restore.sh --verify` (section 4): Alembic revisions, table counts,
   Qdrant point counts, every object by key, size and MD5, avatars.
3. It prints `restore drill of <stamp> passed in <N> s`, or stops at the
   first failing step.
4. It removes the drill project with its volumes and its `restore` image
   (`docker compose -p alphamind-drill down -v --rmi local`), also when a
   step failed.

Record the backup date, the time the drill took, the `--verify` output
and anything that did not work. Compare the row counts it prints with the
live system (`bash scripts/psql.sh`) if they matter for the drill. A
drill does not start the application services: to also try a login or
an analysis on restored data, do a full rebuild on a spare machine
(section 4.4).

## 7. What is NOT backed up

| Data | Why |
|------|-----|
| Redis (cache, rate limits, durable event streams, `auth:user_state:*`, job heartbeats `core:job_last_success`, market-close skip count `core:market_close_skipped`) | Persisted only in its own `redis_data` volume (AOF on), not in the backup: caches refill, rate-limit windows expire, auth-service rewrites every `auth:user_state:*` key (no TTL) at startup and deletes the keys of users that are not in the restored database, and the heartbeat hash and skip count refill on the next job runs. core-service's durable events that had not reached Redis are kept in Postgres (`core.event_outbox`, which is backed up) and the outbox dispatcher publishes them again. |
| Analysis queue (ai-service process memory, `domains/analysis/queue.py`) | Lost on restart by design: analyses still queued or running are failed at the next start and must be re-run. |
| Logs (`./logs/`, container logs) | Operational only; kept 14 days by `logs-cleanup.sh`. |
| Prometheus / Alertmanager / Grafana data | Metrics history is not business data. Dashboards, alert rules and datasources are provisioned from the repository (`infra/`). Grafana users other than the admin would have to be recreated. |
| TLS certificates (`infra/gateway/certs/`) | Regenerated with `make certs`. |
| `.env.docker` (secrets, API keys) | Secrets must not sit next to the data they protect. Keep your own safe copy (password manager or an encrypted file). |
| Docker images | Rebuilt from the repository. |

## 8. How the database schema is brought up to date

Each service runs `database.ensure_schema` at startup, in every
environment. Alembic alone manages the schema:

- an empty schema: `alembic upgrade head` -- the baseline migration
  (`f1160b000001` auth, `f1160c000001` core, `f1160a000001` ai) creates
  every table;
- a schema at a known revision: `alembic upgrade head`;
- a schema at a revision Alembic does not know (for example from a backup
  made with an older migration chain, before the baselines above): Alembic
  stops with "Can't locate revision" and the service does not start. Such
  backups cannot be restored with this version;
- a schema with tables but no Alembic revision: refused (its state is
  unknown).

So a backup whose schemas carry a revision of the current chains is
migrated forward on the next start. The three services
keep separate version tables (`auth.alembic_version`,
`core.alembic_version`, `ai.alembic_version`) and can start at the same
time.

## 9. Object storage: SeaweedFS

Object storage is SeaweedFS (`chrislusf/seaweedfs`, compose service
`objectstore`, volume `objectstore_data`). MinIO is not used because its
public images are not available on Docker Hub or Quay, so a fresh
deployment cannot pull it. The application only speaks S3 through boto3,
and the service has the network alias `minio`, so `S3_ENDPOINT_URL=
http://minio:9000` and the keys in `.env.docker` work unchanged. The stack
has no MinIO service, volume or copy tooling.

### 9.1 SeaweedFS notes

- One process runs the SeaweedFS master, volume server, filer and S3
  gateway (`weed server -s3`). Only the S3 port (9000) and the S3
  gateway's gRPC port (19000) listen on the Docker network; nothing is
  published on the host. The master, volume server and filer listen on
  the container's loopback only.
- The only S3 identity is `S3_ACCESS_KEY` / `S3_SECRET_KEY`, written to
  `/etc/seaweedfs/s3.json` at every start by
  `infra/objectstore/entrypoint.sh`; changing the keys needs
  `docker compose --env-file .env.docker up -d objectstore` (and a
  restart of the services that use them). Unsigned requests get 403.
- The healthcheck (`infra/objectstore/healthcheck.sh`) makes a signed
  S3 ListBuckets request with those keys.
- The backup and restore scripts reach SeaweedFS only through its S3
  API, so a backup can be restored into any SeaweedFS version (or any
  other S3 server). Never switch the same volume between
  the normal and the `_large_disk` image variants (different on-disk
  format).

