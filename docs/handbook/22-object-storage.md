# 22. Object storage (SeaweedFS, S3 API)

> Research documents are files: PDFs, text files, Markdown, SEC filing
> texts. Files do not belong in PostgreSQL rows or in Qdrant payloads, so
> AlphaMind keeps the raw bytes in S3-compatible object storage. In the
> Docker stack that storage is SeaweedFS 4.47, running as one process
> (`weed server -s3`) that speaks the Amazon S3 API. core-service writes
> uploaded documents into the bucket `research-documents`; ai-service
> reads them back to index them for RAG. Both use the same small wrapper
> around `boto3` (`shared/storage.py`), so the same code would work
> against real AWS S3 or Cloudflare R2 with only configuration changes.
> This chapter explains the storage service, its security setup, the key
> layout, the life of a document, and backups.

---

## 1. The big picture

```
  browser / API client
        |  POST /api/v1/research/documents (multipart file)
        v
  core-service  ------------------------------------------+
   ResearchService.upload()                                |
     1. validate (type, size, category, content)          |
     2. duplicate check in core.documents                 |
     3. put_object  documents/<owner_id>/<hex>.<ext> ---->|---+
     4. INSERT core.documents (storage_url = key)         |   |
     5. COMMIT (get_db)                                   |   |
     6. background: XADD stream:document.uploaded ------->|---|--> Redis
                                                              |
                         S3 API, SigV4 signed, port 9000      v
                     +----------------------------------------------+
                     | objectstore  (chrislusf/seaweedfs:4.47)      |
                     | network name: objectstore, alias "minio"     |
                     |                                              |
                     |  S3 gateway :9000 (+ gRPC :19000)  <- Docker |
                     |  filer      (127.0.0.1 only)          network|
                     |  master     (127.0.0.1 only)                 |
                     |  volume     (127.0.0.1 only)                 |
                     |  data: volume objectstore_data -> /data      |
                     +----------------------------------------------+
                                ^                       ^
          get_object            |                       | S3 API (rclone copy)
  ai-service rag_ingestion_workflow                scheduler backup.sh 02:30 New York
  (after document.uploaded)                        -> objectstore/<bucket>/<key>
```

- **Who writes:** core-service (user uploads and system documents from
  the automated ingestion job). ai-service also writes the raw SEC filing
  texts under `raw_filings/` (section 3.5).
- **Who reads:** ai-service (RAG ingestion, and two filing scripts that
  read `raw_filings/`), core-service (document download and preview
  endpoints), and the operator tools.
- **Where the link is kept:** `core.documents.storage_url` holds the
  object **key** (not a URL).

---

## 2. Concepts you need

| Term | Plain explanation | In AlphaMind |
|---|---|---|
| Object storage | Storage for whole files ("objects") addressed by a name, without folders or partial updates. You put, get or delete a whole object. | The raw bytes of research documents. |
| S3 API | Amazon's HTTP API for object storage. Many products implement it. | SeaweedFS implements it, so `boto3` works unchanged. |
| Bucket | A top-level container for objects. | `research-documents` (`S3_BUCKET_DOCUMENTS`). |
| Key | The full name of an object inside a bucket. Slashes are just characters, but tools show them like folders. | `documents/<owner_id>/<hex>.pdf`. |
| Endpoint | The base URL of the S3 service. | `http://minio:9000` (`S3_ENDPOINT_URL`). |
| Access key / secret key | The S3 equivalent of a user name and password. | `S3_ACCESS_KEY` / `S3_SECRET_KEY` from `.env.docker`. |
| SigV4 | "Signature Version 4": every S3 request carries a signature computed from the request and the secret key. The secret itself is never sent. | Required by the SeaweedFS gateway here; unsigned requests get 403. |
| boto3 | The official AWS SDK for Python. It is synchronous. | Wrapped in `asyncio.to_thread` in `shared/storage.py`. |
| SeaweedFS | An open-source distributed file system with an S3 gateway. | The object store of this stack (service `objectstore`). |
| SeaweedFS master | Keeps track of volumes and where they are. | Inside the one `weed server` process, loopback only. |
| SeaweedFS volume server | Stores file contents inside large "volume" files. | Same process, loopback only. |
| SeaweedFS filer | Keeps the directory tree and file metadata (names, sizes). The S3 gateway uses it to map keys to file contents. | Same process, loopback only. |
| Network alias | An extra DNS name for a container on a Docker network. | `minio`, the host name in `S3_ENDPOINT_URL`. |

---

## 3. How it works in AlphaMind

### 3.1 The `objectstore` service

From `docker-compose.yaml`:

```
objectstore:
  image: chrislusf/seaweedfs:4.47
  entrypoint: ["/bin/sh", "/objectstore/entrypoint.sh"]
  command: [server, -s3, -s3.port=9000, -s3.ip.bind=0.0.0.0,
            -s3.port.iceberg=0, -s3.port.lance=0,
            -ip=127.0.0.1, -ip.bind=127.0.0.1]
  environment: S3_ACCESS_KEY, S3_SECRET_KEY (required), S3_REGION (default us-east-1)
  volumes: objectstore_data:/data, ./infra/objectstore:/objectstore:ro
  networks: default, aliases: [minio]
  healthcheck: sh /objectstore/healthcheck.sh  (every 5s, timeout 5s, 12 retries, start 30s)
```

What the flags mean:

- `server -s3`: run master, volume server, filer and the S3 gateway in
  one process.
- `-ip=127.0.0.1 -ip.bind=127.0.0.1`: master, volume server and filer
  listen only on the container's own loopback. They have no
  authentication of their own, so nothing else may reach them.
- `-s3.ip.bind=0.0.0.0 -s3.port=9000`: only the S3 gateway (port 9000,
  plus its gRPC port 19000) is reachable from the Docker network.
- `-s3.port.iceberg=0 -s3.port.lance=0`: two catalog endpoints this
  project does not use are turned off.
- The image's own entrypoint adds `-dir=/data -volume.max=0
  -master.volumeSizeLimitMB=1024`.
- No host port is published. Only containers on the Docker network can
  reach the storage.
- The tag is pinned. The `_large_disk` image variants use a different
  on-disk format; never switch one volume between the two.

The compose file uses `${S3_ACCESS_KEY:?...}` and `${S3_SECRET_KEY:?...}`,
so `docker compose` refuses to start without them. That is one reason
every compose command needs `--env-file .env.docker`.

core-service, ai-service and the scheduler wait for `objectstore` to be
`service_healthy`. The first start takes about 15-20 seconds.

### 3.2 Credentials: `infra/objectstore/entrypoint.sh`

SeaweedFS's S3 gateway runs with authentication when it is given an
identity file. The entrypoint:

1. requires `S3_ACCESS_KEY` and `S3_SECRET_KEY`, and refuses values with a
   newline or tab (they would need JSON escapes it does not produce);
2. with `umask 077`, writes `/etc/seaweedfs/s3.json` (path configurable
   with `OBJECTSTORE_S3_CONFIG`) through a temporary file and a rename.
   The file has one identity, `alphamind`, with those keys and the actions
   `Admin`, `Read`, `List`, `Tagging`, `Write`;
3. gives the file to the `seaweed` user (the image drops to that user);
4. `exec`s the image's own `/entrypoint.sh` (configurable with
   `SEAWEEDFS_ENTRYPOINT`) with the compose command plus
   `-s3.config=/etc/seaweedfs/s3.json`. The image entrypoint fixes the
   ownership of `/data`, drops to the unprivileged `seaweed` user and
   starts `weed server ...`. SeaweedFS treats the presence of a config
   file as "authentication on".

Consequences: there is no anonymous identity, so an unsigned request or
a wrong key gets `403 AccessDenied`. The secret never appears on a
command line (where `ps` could show it). The file is recreated at every
start, so a new key in `.env.docker` takes effect after
`docker compose --env-file .env.docker up -d objectstore` (and a restart
of the services that use the keys).

### 3.3 Healthcheck: a real signed ListBuckets

`infra/objectstore/healthcheck.sh` sends the same kind of request the
services' boto3 client sends:

```
printf 'user = "%s:%s"\n' "<access key>" "<secret key>" \
  | curl -K - -sS -f -o /dev/null --max-time 4 \
      --aws-sigv4 "aws:amz:${REGION}:s3" \
      "http://127.0.0.1:${PORT}/"
```

- `GET /` on the S3 port is the S3 `ListBuckets` call.
- `--aws-sigv4` makes curl sign the request with SigV4.
- The keys reach curl on stdin (`-K -`, a curl config read from standard
  input), never on its command line.
- `-f` makes curl fail on any HTTP error, so the check is healthy only
  when the gateway, filer, master and volume server all work **and** the
  identity file accepts the keys.

A simple "is port 9000 open?" check would report healthy while the
gateway still rejected every request.

### 3.4 The client: `shared/storage.py`

| Function | What it does |
|---|---|
| `_get_client(settings)` | one `boto3.client("s3", endpoint_url, keys, region_name, use_ssl)` per endpoint URL, cached in a dict (not `lru_cache` on `settings`: pydantic settings objects are not hashable) |
| `ensure_bucket(settings, bucket)` | `head_bucket`; on `404`/`NoSuchBucket`, `create_bucket`. Idempotent. Called in core-service's startup (`main.py` lifespan, after `ensure_schema`); a convenience for a bucket that exists only because `docker compose up` created the storage, the S3 counterpart of `CREATE SCHEMA IF NOT EXISTS` (a real production deployment would provision the bucket with its infrastructure tooling) |
| `upload_bytes(settings, bucket=, key=, content=, content_type=)` | `put_object` (with `ContentType` when known) |
| `download_bytes(settings, bucket=, key=)` | `get_object` and read the whole body |
| `delete_object(settings, bucket=, key=)` | `delete_object` |
| `check_storage_connection(settings)` | `list_buckets`; part of `/health` in core-service and ai-service (`"storage": true/false`) |
| `close_storage_clients()` | clears the cache at shutdown (boto3 holds no persistent connection; this keeps the shutdown symmetric with the Redis, HTTP and Qdrant clients) |

`boto3` is synchronous: a call blocks the thread until the network
answers. FastAPI runs on one event loop, so a blocking call would freeze
every other request. Each function therefore runs its boto3 call in a
worker thread with `asyncio.to_thread(...)`.

Why `boto3` and not a vendor library: the same code talks to SeaweedFS,
MinIO, AWS S3 or Cloudflare R2. Only `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`,
`S3_SECRET_KEY` (and `S3_REGION`/`S3_USE_SSL`) change; moving to another
S3-compatible store needs no application code change.

### 3.5 Key layout

All objects are in one bucket, `research-documents`. Verified key
patterns:

| Prefix | Full key | Written by | Database row |
|---|---|---|---|
| `documents/` | `documents/<owner_id>/<uuid4 hex><ext>` | core-service `ResearchService.upload` (`services/core-service/domains/research/service.py`) | yes, `core.documents.storage_url` |
| `documents/00000000-0000-0000-0000-0000000005ec/` | same pattern; this owner id is `SYSTEM_DOCUMENT_OWNER_ID` | core-service, for system documents sent by ai-service's ingestion jobs through `POST /internal/documents` and `POST /internal/documents/system` | yes |
| `raw_filings/` | `raw_filings/<TICKER>/<TICKER>_<form>_<filing date>.txt` and `...txt.amendment.txt` | ai-service `ai/ingestion/sec_edgar_filings.py` | no |

Notes:

- `<ext>` is the upload's extension, one of `.pdf`, `.txt`, `.md`
  (`ALLOWED_DOCUMENT_EXTENSIONS` in `services/core-service/config.py`).
- The `<uuid4 hex>` part makes the key unique and unguessable. The
  original file name is kept in `core.documents.filename`, not in the key.
- The owner id in the key is for humans browsing the bucket. Access
  control is enforced in PostgreSQL (`owner_id`, `organization_id`), not
  by the key.
- `raw_filings/` holds the original SEC filing text **before** it is
  summarised (plus `<file>.amendment.txt` when a 10-K/A was merged). Only
  the summary becomes a searchable document. The raw text has no
  database row and no event and is never indexed, so it does not
  duplicate the summary in search. It is read again in two places:
  1. to audit a summary (`bash scripts/objectstore.sh cat
     raw_filings/<T>/<file>`), for example to check a figure that looks
     invented without fetching the filing from SEC EDGAR again;
  2. as the input of `scripts/backfill_filing_records.py` (structured
     records into `ai.filing_records`) and
     `scripts/regenerate_filing_summaries.py` (re-summarise a stored
     filing), so these scripts do not have to fetch from SEC EDGAR again
     (they fall back to EDGAR when the object is missing).
  An archive failure is logged (`sec_edgar_filings.raw_archive_failed`)
  and does not block the summary, but that filing then has no local raw
  copy.

### 3.6 Life of a document

**Upload** (`POST /api/v1/research/documents`, or the internal routes for
system documents), in `ResearchService.upload`:

1. Validate: extension, size up to `MAX_UPLOAD_SIZE_BYTES` (20 MB),
   category in `ALLOWED_DOCUMENT_CATEGORIES`, and content (a PDF must
   start with `%PDF-`; text must be UTF-8 without NUL bytes). An optional
   `ticker` must pass `shared/tickers.normalize_ticker` (stripped,
   uppercased, 1 to 10 characters of `A-Z`, `0-9`, `.`, `-`), else 422.
   For an organization upload, the user must be allowed to write in that
   organization. Nothing is stored before all of this passes.
2. Duplicate check **before** touching storage: the same (tenant,
   ticker, category, filename) returns the existing document (201,
   `duplicate`) and uploads nothing. Exceptions:
   - an existing document in status `failed` is retried in place: its
     object is overwritten under the **same key**, the status reset to
     `uploaded`, and the event sent again;
   - an existing document whose non-final status has not changed for too
     long (`uploaded` for 10 minutes, `parsing`, `chunking` or `embedding`
     for 60 minutes, measured from `status_changed_at`) gets its
     `document.uploaded` event sent
     again (a repair path when ai-service never processed it or stopped
     in the middle) and logs `document_upload_republished`; the object is
     not uploaded again.
3. `upload_bytes` to `documents/<owner_id>/<hex><ext>`.
4. Insert the `core.documents` row with `storage_url = key`,
   `status = "uploaded"`, and the `document.uploaded` event as a row of
   `core.event_outbox` in the same transaction. If the insert fails, the object is deleted again
   (`_delete_object_quietly`). If a concurrent upload of the same document
   won the insert race, this request's object is deleted and the winner's
   row returned.
5. The request's session commits (see
   [20-postgresql.md](20-postgresql.md), "commit before the response").
6. A background task publishes the durable event `document.uploaded`
   with `document_id`, `owner_id`, `organization_id`, `storage_bucket`,
   `storage_key`, `document_type`, `category`, `ticker`, `content_date`,
   `filename` and `is_system`, and marks the outbox row published. If
   Redis is down, the outbox dispatcher publishes it later
   ([24-events.md](24-events.md), section 3.5).

**Ingestion** in ai-service (`ai/workflows/rag_ingestion_workflow.py`):
`download_bytes(settings, bucket=..., key=...)` (`shared/storage.py`)
reads the object, then parsing, chunking,
embedding and indexing follow. The status moves
`uploaded -> parsing -> chunking -> embedding -> ready` (or `failed`).
Each change is published as `document.status_changed`, both durably
(core-service persists it in `core.documents.status`) and on pub/sub
(live WebSocket updates). See [17-ai-rag.md](17-ai-rag.md) and
[21-qdrant.md](21-qdrant.md).

**Reading back**: `GET /api/v1/research/documents/{id}/content` calls
`ResearchService.download_document`, which checks read access
(`get_document`) and then reads the object with
`ResearchService.document_content`. `/preview` calls the same two
functions separately, so it can refuse a type it cannot show inline
before it downloads anything. `document_content` uses
`download_bytes(...)`, which reads the **whole object into memory**
(at most `MAX_UPLOAD_SIZE_BYTES`, 20 MB, per request); nothing is
streamed from object storage. `/content` returns the bytes as one
response; `/preview` sends the in-memory bytes in 64 KiB chunks with an
explicit `Content-Length`. There is no public file URL: `storage_url` is
an internal key, so the bytes are only reachable through these
authenticated endpoints.

**Files outside the bucket.** `scripts/regenerate_filing_summaries.py`
keeps its drafts on local disk (`/app/_regenerated/<file>.summary.txt`
in the ai-service container; with the development bind mount that is
`services/ai-service/_regenerated/` on the host), not in the bucket. Only
its `--apply` step creates the new filing document, under the normal
system-owner key. Delete the old document first with
`scripts.delete_documents --confirm`; a filing whose old document still
exists is skipped.

**Delete** (`DELETE /api/v1/research/documents/{id}`, owner only):
delete the row and write `document.deleted` to the outbox in the same
transaction. After the commit, one background task publishes the durable
event `document.deleted`, and another deletes the stored file
(`_delete_object_quietly`). ai-service then removes the document's Qdrant
points, BM25 entries and bookkeeping rows. The file goes last on purpose:
a failed commit leaves the row and its file intact, and a failed file
delete only logs `document_orphan_cleanup_failed` and leaves an orphan
object (no row points to it).

The event semantics (streams, consumer groups, retries) are explained in
[24-events.md](24-events.md).

### 3.7 Backups and restore

**Backup** (`infra/scheduler/backup.sh`, daily at 02:30 New York, 07:30
London): the scheduler copies every object of every bucket through the
S3 API with `rclone`, using the services' own endpoint and keys
(`S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`). The
rclone remote `objectstore:` is defined only by environment variables
(`RCLONE_CONFIG_OBJECTSTORE_*`, set by `infra/scheduler/s3-remote.sh`), so
the keys never appear on a command line. For each bucket:

```
rclone copy objectstore:<bucket> <backup>/objectstore/<bucket>
```

rclone checks the MD5 of every object it copies. The result is one plain
file per object at `objectstore/<bucket>/<key>`; an empty bucket becomes
an empty folder. Every object is listed in `SHA256SUMS`, and
`manifest.json` (format 3) records `objectstore: {engine: "seaweedfs",
method: "s3", endpoint, dir: "objectstore", buckets: [{name, objects,
bytes}]}`.

Why an S3 copy and not a copy of the data directory:

- every S3 `GET` returns one complete object, so each copied object is
  consistent even when a document is uploaded or deleted during the
  backup; a file copy of the live volume (filer database and volume
  files, written at different moments) can be torn;
- it does not depend on SeaweedFS's on-disk format or image version;
- the restore puts the objects back through the same API, into any
  S3-compatible store.

An object uploaded after its bucket was listed is in the next night's
backup. The backup keeps keys and contents, not S3 metadata: on restore
each object's `Content-Type` is set from its key's file extension (the
services never read it back).

**Restore** (`infra/scheduler/restore.sh`; **destructive**, dry run by
default) runs through the one-off `restore` compose service (profile
`tools`), not the scheduler. The `objectstore` service must be
**running**, and the services that read or write documents stopped. This
is checked in the restore's preflight, together with the other parts'
preconditions, before PostgreSQL or Qdrant are touched, so a stopped
object store stops the restore with nothing changed. For
each `objectstore/<bucket>/` folder of the backup it creates the bucket
if needed and runs `rclone sync --checksum`: missing or different objects
are uploaded, unchanged ones are skipped, and objects that are not in the
backup are deleted. Buckets that are not in the backup are left alone.
`restore.sh --verify` changes nothing and compares the live data with the
backup (for object storage: `rclone check` of every bucket in both
directions, by size and MD5); it exits 1 on any difference. The Makefile
targets:

```
make restore BACKUP=<YYYY-MM-DD_HHMM>              # dry run, checks only
make restore BACKUP=<YYYY-MM-DD_HHMM> CONFIRM=yes  # DESTRUCTIVE: stops the app services and scheduler, keeps objectstore running, restores, verifies, starts all
make restore-drill [BACKUP=<YYYY-MM-DD_HHMM>]      # restores into a separate, temporary compose project and verifies it
```

The restore drill (`bash scripts/backups.sh drill`) starts `postgres`,
`qdrant` and `objectstore` as the compose project `alphamind-drill` with
empty volumes and no published ports, runs `restore.sh --yes` and then
`restore.sh --verify` on the backup, prints how long it took, and removes
the project with its volumes, also when a step failed. It never touches
the live stack.

Older backups hold `objectstore-data.tar.gz` (format 2, a tarball of the
SeaweedFS data directory). `restore.sh` still extracts it into the
`objectstore_data` volume, which needs `objectstore` **stopped**
(`restore.sh` refuses while the S3 API answers; `make restore` stops or
starts `objectstore` according to the backup's format). A backup with
`minio-data.tar.gz` (MinIO era) cannot be read by SeaweedFS: a full
restore skips that part and `--only objectstore` refuses it. See
`../DISASTER_RECOVERY.md` sections 1, 4.3, 6 and 9.

### 3.8 Operator tools

- `make objectstore` / `bash scripts/objectstore.sh` runs Python inside
  core-service with the application's own client and keys:
  - `bash scripts/objectstore.sh` lists buckets with object count and
    total size;
  - `bash scripts/objectstore.sh ls [prefix]` lists up to 200 keys of the
    documents bucket (size, time, key);
  - `bash scripts/objectstore.sh cat <key>` prints one object: it runs
    Python inside core-service that downloads the object from
    `S3_BUCKET_DOCUMENTS` and writes it to stdout as UTF-8 text. Useful
    to compare a stored summary with its raw filing. It decodes as text,
    so it suits `.txt` and `.md` objects, not PDFs.
- `services/core-service/scripts/delete_documents.py` is an operator
  cleanup tool that deletes documents by id the same way the API does,
  without the owner check: the row delete and the outbox entry come from
  the same helper the API uses (`service.delete_document_row`), one
  transaction and commit per document, then the event is published with
  `outbox.publish_now` and the stored file is deleted; a failed file
  delete is reported as an orphan object. It previews by default; with
  `--confirm` it **deletes data**. Never use `--confirm` in a demo.

---

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `S3_ENDPOINT_URL` | `http://minio:9000` | S3 endpoint (the `minio` alias of `objectstore`) | `shared/config.py`, `.env.example` |
| `S3_ACCESS_KEY` | `minioadmin` in code; required by compose (`.env.example`: `alphamind-dev`) | access key, also written into SeaweedFS's identity file | `shared/config.py`, `docker-compose.yaml` |
| `S3_SECRET_KEY` | `minioadmin` in code; required by compose | secret key | `shared/config.py`, `docker-compose.yaml` |
| `S3_REGION` | `us-east-1` | region used in SigV4 signatures | `shared/config.py`, `docker-compose.yaml` |
| `S3_USE_SSL` | `False` | HTTPS to the endpoint | `shared/config.py` |
| `S3_BUCKET_DOCUMENTS` | `research-documents` | the documents bucket | `shared/config.py` |
| `MAX_UPLOAD_SIZE_BYTES` | `20 * 1024 * 1024` | upload size limit | `services/core-service/config.py` |
| `ALLOWED_DOCUMENT_EXTENSIONS` | `.pdf`, `.txt`, `.md` | accepted file types (a class constant, not read from the environment) | `services/core-service/config.py` |
| `OBJECTSTORE_S3_CONFIG` | `/etc/seaweedfs/s3.json` | where the identity file is written | `infra/objectstore/entrypoint.sh` |
| `SEAWEEDFS_ENTRYPOINT` | `/entrypoint.sh` | the image entrypoint the script hands over to | `infra/objectstore/entrypoint.sh` |
| `OBJECTSTORE_S3_PORT` | `9000` | port the healthcheck calls | `infra/objectstore/healthcheck.sh` |
| `OBJECTSTORE_DATA_DIR` | `/objectstore-data` | where `restore.sh` sees the volume (only for an old tarball backup; the `restore` service mounts it) | `infra/scheduler/restore.sh` |
| image | `chrislusf/seaweedfs:4.47` | pinned SeaweedFS release | `docker-compose.yaml` |

---

## 5. Design decisions and why

**SeaweedFS instead of MinIO.**
MinIO publishes no public images on Docker Hub or Quay, so a fresh
deployment cannot pull it. SeaweedFS has an official, pinned image, an
S3 gateway, and runs as a single process. The stack has no MinIO
service, tooling or volume.

**Network alias `minio`.**
Alternative: rename the endpoint everywhere. Reason: every
`S3_ENDPOINT_URL=http://minio:9000` (in `.env.docker` files, the code
default, the docs) works unchanged.

**Only the S3 gateway is reachable; everything else on loopback.**
The master, volume server and filer have no authentication. Binding them
to 127.0.0.1 inside the container means the only way in is the S3
gateway, which requires signed requests.

**Authentication always on, keys never on a command line.**
The identity file is written at every start with `umask 077`; the
healthcheck passes keys on stdin.

**A real signed request as healthcheck.**
It proves the whole chain works with the real keys, not just that a port
is open.

**Duplicate check before upload; cleanup of orphans.**
Without it, a duplicate upload would store a new orphan object and
re-publish `document.uploaded`, which makes ai-service re-index the old
document id with the new content. Duplicates are detected first, and
objects whose row could not be written are deleted.

**Events after commit.**
Publishing `document.uploaded` inside the request could reach ai-service
before the row is committed. It is a background task that runs after the
commit, and the event is also stored in `core.event_outbox` in the same
transaction, so a Redis outage at publish time does not lose it.

**S3 object copy for backups.**
Explained in section 3.7: each object is copied whole and MD5-checked
through the S3 API, so a backup taken while documents change is still
consistent per object, and it does not depend on SeaweedFS's on-disk
format.

---

## 6. When things go wrong

| Symptom | Likely cause | What the system does | How to diagnose |
|---|---|---|---|
| `docker compose` refuses to start: "set S3_ACCESS_KEY in .env.docker ..." | compose run without `--env-file .env.docker` or key missing | nothing starts | use `docker compose --env-file .env.docker ...` |
| `objectstore` stays unhealthy | wrong keys, first start still running, or SeaweedFS failed | core/ai-service and the scheduler wait | `docker compose --env-file .env.docker logs objectstore`; look for `objectstore-entrypoint: wrote the S3 identity file` |
| core/ai `/health` shows `"storage": false` | gateway down or keys rejected | 503 from `/health` | `bash scripts/objectstore.sh` (fails with AccessDenied if the keys differ) |
| Upload returns 201 but the document stays `uploaded` (or stops at `parsing`, `chunking`, `embedding`) | `document.uploaded` still waiting in `core.event_outbox` (Redis down), ai-service down, or ai-service stopped in the middle | the outbox dispatcher publishes it when Redis is back; after the stuck limit (10 minutes `uploaded`, 60 minutes in a processing status, from the last status change) a re-upload of the same file re-sends the event, and the next nightly ingestion submits a stuck system document again | `bash scripts/psql.sh -c "SELECT id, filename, status, status_changed_at FROM core.documents WHERE status NOT IN ('ready', 'failed') ORDER BY status_changed_at;"`; [24-events.md](24-events.md) |
| Document ends `failed` | parsing found no text (for example a scanned PDF), or embedding failed | status `failed`; a re-upload retries in place | ai-service log `rag_ingestion_failed` (success is `rag_ingestion_complete`); there is no OCR |
| Log `document_orphan_cleanup_failed` | object could not be deleted after a failed insert, or after a committed document delete | an orphan object remains (no row points to it) | `bash scripts/objectstore.sh ls documents/<owner_id>/` and compare with `core.documents` |
| `GET .../content` returns an error for an existing row | object missing (restored database without matching object storage) | 5xx | `bash scripts/objectstore.sh ls <key prefix>`; restore both parts from the same backup |
| Backup fails at "objectstore: copying bucket" | S3 API unreachable or keys rejected (the scheduler's `S3_*` settings) | run marked FAILED, old backups kept | scheduler log; `make backups`; `bash scripts/objectstore.sh` |
| `restore.sh --verify` reports objectstore differences | objects changed or added since the backup, or an incomplete restore | exit code 1, nothing changed | rerun `restore.sh --yes --only objectstore`, then `--verify` again |

---

## 7. Demo walkthrough

All steps are read-only.

1. **The service and its alias.**
   ```
   docker compose --env-file .env.docker ps objectstore
   ```
   Expected: `healthy`. Say: "This is SeaweedFS. Inside the Docker
   network it also answers to the name `minio`, the host name in
   `S3_ENDPOINT_URL`. It has no host port."

2. **Buckets with counts and size.**
   ```
   bash scripts/objectstore.sh
   ```
   Expected: `research-documents   <n> objects   <bytes> bytes`. Say: "This
   runs inside core-service with the application's own boto3 client and
   keys."

3. **Key layout.**
   ```
   bash scripts/objectstore.sh ls documents/
   bash scripts/objectstore.sh ls documents/00000000-0000-0000-0000-0000000005ec/
   bash scripts/objectstore.sh ls raw_filings/
   ```
   Say: "User uploads are under the owner's id, system documents under
   the fixed system owner id, and raw SEC filings are kept for audit and the filing scripts, never indexed."

4. **Link a database row to its object.**
   ```
   bash scripts/psql.sh -c "SELECT id, filename, category, ticker, status, storage_url FROM core.documents ORDER BY uploaded_at DESC LIMIT 5;"
   ```
   Copy a `storage_url` of a `.txt` or `.md` document, then:
   ```
   bash scripts/objectstore.sh cat <storage_url> | head -40
   ```
   Say: "`storage_url` is a key, not a URL. The only way for a user to get
   the bytes is the authenticated `/content` endpoint."

5. **Show that the gateway requires signatures.** From inside core-service
   (no keys sent):
   ```
   docker compose --env-file .env.docker exec -T core-service python -c "import urllib.request; urllib.request.urlopen('http://minio:9000/')"
   ```
   Expected: an `HTTPError 403`. Say: "Unsigned requests are refused. The
   healthcheck sends a real SigV4-signed ListBuckets with the keys."

6. **Show the health of the dependency.**
   ```
   docker compose --env-file .env.docker exec -T core-service python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
   ```
   Point at `"storage": true`.

7. **Backups.**
   ```
   make backups
   ls -la backups/<newest>/
   ```
   Point at the `objectstore/research-documents/` folder (one file per
   object) and its lines in `SHA256SUMS`. Say: "Every object is copied
   through the S3 API and MD5-checked, so the backup does not depend on
   SeaweedFS's on-disk format."

8. **Restore dry run (changes nothing).**
   ```
   make backup-check
   ```

---

## 8. Check your understanding

<details>
<summary>1. Why does `S3_ENDPOINT_URL` say `minio` when the store is SeaweedFS?</summary>

The `objectstore` (SeaweedFS) container has the network alias `minio`,
so that URL resolves to it and no setting has to change.
</details>

<details>
<summary>2. Which SeaweedFS components are reachable from other containers?</summary>

Only the S3 gateway (port 9000, and its gRPC port 19000). The master,
volume server and filer listen on the container's loopback.
</details>

<details>
<summary>3. Why does `shared/storage.py` use `asyncio.to_thread`?</summary>

boto3 is synchronous. Running its calls in a worker thread keeps the
FastAPI event loop free for other requests.
</details>

<details>
<summary>4. What is stored in `core.documents.storage_url`?</summary>

The object key, for example `documents/<owner_id>/<hex>.pdf`, not a URL.
</details>

<details>
<summary>5. Why is `document.uploaded` published in a background task?</summary>

Background tasks run after `get_db` committed. Publishing before that could
let ai-service process a document whose row was not committed yet.
</details>

<details>
<summary>6. Why are objects backed up through the S3 API and not as a copy of the volume?</summary>

Each S3 `GET` returns one complete object and rclone checks its MD5, so
every object in the backup is consistent even if documents change during
the backup; a file copy of the live data directory can be torn. The copy
also does not depend on SeaweedFS's on-disk format or version, and
`restore.sh --verify` can compare every object by size and MD5.
</details>

<details>
<summary>7. What is under `raw_filings/` and why is it not searchable?</summary>

The raw SEC filing text archived before summarisation. It is used to
audit summaries and as the input of `backfill_filing_records.py` and
`regenerate_filing_summaries.py`. It has no database row and no event, so
it is never indexed and does not duplicate the summary in search.
</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `docker-compose.yaml` (`objectstore`, `scheduler`, `restore`) | SeaweedFS service, alias, volumes, healthcheck; backup and restore mounts |
| `infra/objectstore/entrypoint.sh` | writes the S3 identity file, starts SeaweedFS |
| `infra/objectstore/healthcheck.sh` | SigV4-signed ListBuckets |
| `shared/config.py` | `S3_*` settings |
| `shared/storage.py` | boto3 client, `ensure_bucket`, upload/download/delete, health check |
| `services/core-service/main.py` | `ensure_bucket` at startup |
| `services/core-service/domains/research/service.py` | upload, duplicate handling, download, delete, `document.uploaded` / `document.deleted` |
| `services/core-service/api/internal.py` | system document routes used by ai-service jobs (`/internal/documents*`) |
| `services/ai-service/ai/ingestion/sec_edgar_filings.py` | writes `raw_filings/` objects |
| `services/ai-service/scripts/backfill_filing_records.py`, `regenerate_filing_summaries.py` | read `raw_filings/` objects (fallback: SEC EDGAR) |
| `services/core-service/scripts/delete_documents.py` | destructive cleanup tool |
| `infra/scheduler/backup.sh`, `infra/scheduler/restore.sh`, `infra/scheduler/s3-remote.sh` | S3 object-copy backup, restore (`rclone sync --checksum`) and `--verify`; the rclone remote |
| `scripts/objectstore.sh` | buckets, keys, print an object |

---

## 10. Related chapters

- [12-core-service.md](12-core-service.md): research documents API
- [17-ai-rag.md](17-ai-rag.md): what happens to the bytes after download
- [20-postgresql.md](20-postgresql.md): `core.documents` and commit ordering
- [21-qdrant.md](21-qdrant.md): where the chunks and vectors go
- [24-events.md](24-events.md): `document.uploaded`, `document.status_changed`, `document.deleted`
- [26-scheduler-jobs.md](26-scheduler-jobs.md): backup and ingestion jobs
- [29-security.md](29-security.md): document access rules
- [../ERD.md](../ERD.md) section 5, [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md) sections 4 and 9, [../API_SPEC.md](../API_SPEC.md)
