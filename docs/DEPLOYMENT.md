# AlphaMind - Cloud deployment

The cloud deployment runs the same `docker-compose.yaml` stack as a
local machine, on one virtual machine. Nothing in the code differs
between the two: the differences live in the host's own `.env.docker`
and gateway certificate, which are never committed. The local setup is
the development environment.

| | Local | Cloud |
|---|---|---|
| Address | `https://localhost:8443` | `https://alphamind-research.duckdns.org:8443` |
| Compose files | `docker-compose.yaml` + `docker-compose.override.yml` (development stage, source bind-mounted) | `docker-compose.yaml` only (production stage) |
| `APP_ENV` | `development` | `production` (the services refuse to start without a strong `JWT_SECRET`, an `INTERNAL_API_TOKEN` and a `TOTP_ENCRYPTION_KEY`) |
| TLS certificate | self-signed (`make certs`) | Let's Encrypt, renewed automatically |
| Email | MailHog | MailHog (read through an SSH tunnel, section 6) |
| Deploy | `make up`, `scripts/rebuild.sh` | `scripts/deploy.sh`, run by the deploy workflow after every merge to `main` |

## 1. The host

| Item | Value |
|---|---|
| Provider | AWS, free account plan (credits; the account closes when they run out or after 6 months unless upgraded) |
| Region | `eu-north-1` (Stockholm) |
| Instance | EC2 `m7i-flex.large`: 2 vCPU, 8 GiB memory, 40 GiB gp3 disk, Ubuntu 24.04 LTS (x86_64), 2 GiB swap file |
| Address | Elastic IP `16.192.224.66`; DNS name `alphamind-research.duckdns.org` (DuckDNS, set by hand in the DuckDNS web page) |
| Inbound ports (security group) | 22 (SSH), 80 (Let's Encrypt HTTP challenge), 8443 (the gateway); 443 is open but unused |
| Software | Docker Engine with the compose plugin, git, make, certbot |
| Checkout | `/home/ubuntu/alphamind`, cloned from GitHub with a read-only deploy key (`~/.ssh/github_deploy`) |

The stack uses about 2 GB of memory when idle (measured locally, 17
containers), so 8 GiB leaves room for analyses and image builds. A
4 GiB machine with the swap file also runs it, at the cost of slower
builds.

## 2. One-time host setup

These steps were done once; repeat them only for a new host.

1. Launch the instance (table above) with a new key pair, allow SSH from
   your address and HTTP/HTTPS from anywhere, then add port 8443 from
   anywhere to its security group.
2. Allocate an Elastic IP, associate it with the instance and enter it
   as the DuckDNS address.
3. On the host:

   ```
   sudo apt-get update && sudo apt-get install -y git make certbot
   curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker ubuntu
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile \
     && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   echo 'export COMPOSE_FILE=docker-compose.yaml' >> ~/.bashrc
   ```

   `COMPOSE_FILE` makes `docker compose`, `make` and the scripts in
   `scripts/` skip the development override. `~/.bashrc` applies only to
   interactive shells, so it covers the commands typed after `ssh` logs
   you in, but not a command passed to `ssh` directly
   (`ssh <host> '<command>'`): export the variable in that same command,
   for example `ssh -i <key> ubuntu@<host> 'cd ~/alphamind && export
   COMPOSE_FILE=docker-compose.yaml && make health'`. `scripts/deploy.sh`
   exports it itself.
4. Create the read-only deploy key (`ssh-keygen -t ed25519 -f
   ~/.ssh/github_deploy`, an `~/.ssh/config` entry for github.com), add the
   public key under the repository's Settings -> Deploy keys without write
   access, and clone the repository to `~/alphamind`.
5. Certificate: `sudo certbot certonly --standalone -d
   alphamind-research.duckdns.org --agree-tos
   --register-unsafely-without-email`, then copy `fullchain.pem` and
   `privkey.pem` to `infra/gateway/certs/server.crt` and `server.key`. The
   deploy hook `/etc/letsencrypt/renewal-hooks/deploy/alphamind.sh` copies
   them again after every renewal and reloads nginx. certbot's systemd
   timer renews; the gateway uses host ports 8443 and 8000, so port 80
   stays free for the challenge. `sudo certbot renew --dry-run` tests it.
   The certificate names only this domain. The frontend reaches the API
   on the gateway's internal plain-HTTP listener
   (`BACKEND_API_URL=http://gateway:8080/api/v1`, on the `edge` Docker
   network, not published),
   so no certificate that names `gateway` is needed.
6. `.env.docker`: copy the local file
   (`scp -i <key> .env.docker ubuntu@<host>:/home/ubuntu/alphamind/`),
   `chmod 600` it, then set on the host:

   | Key | Cloud value |
   |---|---|
   | `OAUTH_REDIRECT_BASE_URL`, `FRONTEND_BASE_URL`, `BACKEND_CORS_ORIGINS` | `https://alphamind-research.duckdns.org:8443` |
   | `APP_ENV` | `production` |
   | `JWT_SECRET`, `INTERNAL_API_TOKEN` | new values from `openssl rand -hex 32` |
   | `TOTP_ENCRYPTION_KEY` | a new Fernet key (32 random bytes, URL-safe base64) |

   The cloud and the local stack share the LLM and data provider keys and
   so their quotas: do not run analysis batches on both at the same time.

## 3. Continuous deployment

```
 merge to main (commit X) -> "stack smoke CI" (build + start the whole stack)
                                 | success
                                 v
                           deploy workflow: wait until every other check of X
                                 | all succeeded or skipped
                                 v
                           SSH to the host, command = X -> scripts/deploy.sh
                                                             git merge --ff-only X
                                                             docker compose up -d --build --wait
```

- `.github/workflows/deploy.yml` runs when the stack smoke workflow
  succeeded for a push to `main`, or by hand (Actions -> deploy -> Run
  workflow). A failed smoke run deploys nothing; the host keeps running
  the previous version.
- Before it connects, the workflow checks that X is still the head of
  `main` (otherwise it stops: the newer commit's own run deploys that
  one). It then waits, up to 30 minutes, for every other workflow run of
  X triggered by the push (per-service CI, frontend CI), and for the
  newest push run on `main` of every other active workflow. It deploys
  only when all of them succeeded or were skipped; a failed or unfinished
  check fails the deploy run and nothing is deployed. A failed GitHub API
  call is retried twice.
- The second set covers earlier commits. The per-service workflows have
  path filters, so they run only for the services a commit touches. A
  workflow that has no run for X is judged by its newest run on `main`,
  from an earlier commit. So a failed check on `main`, from any earlier
  commit, blocks every deploy until a later run of that workflow passes
  or the failed run is re-run.
- The deploy is triggered only by the stack smoke run. If another check
  failed for a flaky reason and you re-run it until it passes, no deploy
  starts by itself: start one with Actions -> deploy -> Run workflow.
- The workflow sends X as the SSH command. `deploy.sh` reads it from
  `SSH_ORIGINAL_COMMAND`, accepts only a 40-character hex commit that is
  on `origin/main`, and fast-forwards to exactly X, so a newer commit
  whose checks are still running is never deployed by X's run. Run by
  hand on the host (no SSH command), it fast-forwards to `origin/main`.
- `scripts/deploy.sh` then rebuilds, restarts what
  changed and waits up to 5 minutes for every health check. It refuses to
  run without `.env.docker` or the certificate. It needs only bash 3.2
  (no `mapfile`), so it also runs with the bash that macOS ships.
  Compose restarts only
  services whose image or configuration changed; a restart of ai-service
  ends an analysis that is still running.
- The scheduler keeps the state it had before the deploy. The host keeps
  it stopped (the cloud and the local stack share the LLM and news
  quotas), so the host runs no scheduled jobs and makes no automatic
  backups (section 6). When it is not running, `deploy.sh` rebuilds its image but
  leaves it out of `docker compose up`, so it never starts, not even for
  a minute; `--wait` then waits for every other service. The script prints
  `scheduler was not running before the deploy: kept stopped`. To start
  it: `docker compose --env-file .env.docker up -d scheduler`. When it was
  running, it is restarted like any other service.
- The jobs that spend no LLM or news quota still matter on the host,
  because real accounts are deleted there: purge-unverified,
  notification-retention and the two deleted-user sweeps (they remove
  what a lost deletion event left behind). Run them by hand, for
  example once a week, while the scheduler stays stopped:

  ```bash
  for job in purge-unverified notification-retention \
      deleted-user-sweep-core deleted-user-sweep-ai; do
    docker compose --env-file .env.docker run --rm --no-deps scheduler \
      /scripts/call-job.sh "$job"
  done
  ```

  With arguments, the scheduler image runs that one command instead of
  cron (handbook chapter 26, sections 3.2 and 3.11).
- Quick check after a deploy, from any machine:
  `curl -sk https://alphamind-research.duckdns.org:8443/healthz`. It
  answers `{"status":"ok"}` only when the frontend runs and its server
  side reaches the API through the gateway; otherwise 503 (or 502 when
  the frontend is down).
- The workflow's SSH key may only run the deploy script; the commit it
  sends is only data for that script, never run as a command. Its line
  in the host's `~/.ssh/authorized_keys` is

  ```
  command="bash /home/ubuntu/alphamind/scripts/deploy.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... github-actions-deploy
  ```

- Repository secrets: `DEPLOY_HOST` (`alphamind-research.duckdns.org`),
  `DEPLOY_SSH_KEY` (the private half of that key) and
  `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan -t ed25519
  alphamind-research.duckdns.org`, so the job verifies the host).
- GitHub's runners connect from changing addresses, so port 22 is open to
  anywhere. Password login is off on the Ubuntu image; only keys work.

## 4. Moving data between local and cloud

The cloud starts with an empty database. To show the local data there,
copy a backup and restore it (procedures in
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)):

```
make backup                                  # local
scp -i <key> -r backups/<YYYY-MM-DD_HHMM> ubuntu@<host>:/home/ubuntu/alphamind/backups/
export COMPOSE_FILE=docker-compose.yaml            # on the host
make restore BACKUP=<YYYY-MM-DD_HHMM>              # on the host: dry run
make restore BACKUP=<YYYY-MM-DD_HHMM> CONFIRM=yes  # on the host
docker compose --env-file .env.docker stop scheduler  # on the host
```

`make restore ... CONFIRM=yes` ends with `docker compose up -d`, which
also starts the scheduler. Stop it again at once: otherwise its jobs
(market-open analyses, news ingestion) spend the LLM and news quotas
that the cloud shares with the local stack, and every later deploy keeps
it running.

After a restore, users sign in again (the cloud has its own
`JWT_SECRET`). Two-factor secrets written locally without a
`TOTP_ENCRYPTION_KEY` were encrypted with a key derived from the local
`JWT_SECRET`; the cloud cannot read them, so an admin resets that user's
2FA (`DELETE /api/v1/admin/users/{id}/2fa`) and the user sets it up again.

## 5. Switching between local and cloud

Both run at the same time and do not share data. "Switching" means
using the other address:

- Browser and Swagger: `https://localhost:8443` or
  `https://alphamind-research.duckdns.org:8443`.
- Scripts in `scripts/` run on the machine whose stack they check and
  reach it through `docker compose`, so they need no change. The one that
  calls the public address (`tiers.sh decline`) takes it from
  `GATEWAY_URL`: on the host, set
  `GATEWAY_URL=https://alphamind-research.duckdns.org:8443`.
- OAuth: register both callback URLs with each provider
  (`<base>/api/v1/auth/oauth/google/callback` and `.../github/callback`).
  The callback answers 303 to `<FRONTEND_BASE_URL>/oauth/complete` with
  the result in the URL fragment, so `FRONTEND_BASE_URL` must be the
  cloud URL (table above).
  Google's consent screen in testing mode only admits listed test users.
- Operator commands (`make health`, `make e2e`, `bash scripts/psql.sh`,
  logs) run on the host after `ssh -i <key> ubuntu@<host>` and
  `cd ~/alphamind`, exactly as locally. The host runs the production
  image stage, so the two operator scripts that write under `/app`
  (`measure_rag_recall --sweep --apply`, `regenerate_filing_summaries`)
  fail there with a permission error; run them on the local stack
  (handbook chapter 2, section 3.5b).

## 6. Operations on the host

| Task | Command |
|---|---|
| Log in | `ssh -i <key> ubuntu@alphamind-research.duckdns.org` |
| Health | `make health`; frontend: `curl -sk https://alphamind-research.duckdns.org:8443/healthz` |
| Deploy by hand | `bash scripts/deploy.sh` |
| Logs | `bash scripts/logs.sh <service>` or `logs/<service>/` |
| Mail (MailHog, Prometheus, Qdrant UIs listen on the host's 127.0.0.1 only) | from your machine: `ssh -i <key> -L 8025:127.0.0.1:8025 ubuntu@<host>`, then open `http://127.0.0.1:8025` |
| Organization sweep refused by its safety brake (`ORGANIZATION_SWEEP_MAX_MISSING`) | `docker compose --env-file .env.docker exec core-service python -m scripts.clean_organizations --id <org id>` (preview), then the same with `--confirm`; it skips any id auth still has (handbook chapter 26, section 6) |
| Certificate status | `sudo certbot certificates` |
| Free space | `df -h /` and `docker system df` |

The host keeps the scheduler stopped (section 3), so it makes no
automatic backups and runs no scheduled jobs. `make backup` does not work
there either, because it runs `backup.sh` inside the running scheduler
container. Backups are made on the local stack: nightly by its scheduler,
or by hand with `make backup`. The cloud's data is not backed up: copy
what you need to keep off the host before stopping the instance for
good. For the same reason the job-health alerts (`DailyJobStale`,
`TradingDayJobStale`) fire on the host; show backups, scheduled jobs and
alerting on the local stack.

## 7. Cost and shutdown

The instance, its disk and the Elastic IP use the free plan's credits
(about 78 USD a month in total; the remaining credits and days are on
the AWS Billing page). To stop spending, stop the instance (the disk and
Elastic IP still cost a little) or terminate it and release the Elastic
IP. On the free plan nothing is ever charged to the card: the account
closes when the credits run out, with 90 days to upgrade and keep the
data.
