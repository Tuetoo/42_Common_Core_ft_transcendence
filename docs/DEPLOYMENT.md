# AlphaMind - Cloud deployment

The cloud deployment runs the same `docker-compose.yaml` stack as a
local machine, on one virtual machine. Nothing in the code differs
between the two: the differences live in the host's own `.env.docker`
and gateway certificate, which are never committed. The local setup is
unchanged and stays the development environment.

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

   `COMPOSE_FILE` makes every `docker compose` command and every script in
   `scripts/` skip the development override.
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
 merge to main -> "stack smoke CI" (build + start the whole stack)
                       | success
                       v
                 deploy workflow -> SSH to the host -> scripts/deploy.sh
                                                         git merge --ff-only origin/main
                                                         docker compose up -d --build --wait
```

- `.github/workflows/deploy.yml` runs when the stack smoke workflow
  succeeded for a push to `main`, or by hand (Actions -> deploy -> Run
  workflow). A failed smoke run deploys nothing; the host keeps running
  the previous version.
- `scripts/deploy.sh` fast-forwards the checkout, rebuilds, restarts what
  changed and waits up to 5 minutes for every health check. It refuses to
  run without `.env.docker` or the certificate. Compose restarts only
  services whose image or configuration changed; a restart of ai-service
  ends an analysis that is still running.
- The workflow's SSH key may only run the deploy script: its line in the
  host's `~/.ssh/authorized_keys` is

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
make restore BACKUP=<YYYY-MM-DD_HHMM>              # on the host: dry run
make restore BACKUP=<YYYY-MM-DD_HHMM> CONFIRM=yes  # on the host
```

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
  `cd ~/alphamind`, exactly as locally.

## 6. Operations on the host

| Task | Command |
|---|---|
| Log in | `ssh -i <key> ubuntu@alphamind-research.duckdns.org` |
| Health | `make health` |
| Deploy by hand | `bash scripts/deploy.sh` |
| Logs | `bash scripts/logs.sh <service>` or `logs/<service>/` |
| Mail (MailHog, Prometheus, Qdrant UIs listen on the host's 127.0.0.1 only) | from your machine: `ssh -i <key> -L 8025:127.0.0.1:8025 ubuntu@<host>`, then open `http://127.0.0.1:8025` |
| Certificate status | `sudo certbot certificates` |
| Free space | `df -h /` and `docker system df` |

Backups run nightly on the host as they do locally (`backups/` on the
host's disk). They protect against mistakes, not against losing the
machine: copy one off the host before stopping the instance for good.

## 7. Cost and shutdown

The instance, its disk and the Elastic IP use the free plan's credits
(about 78 USD a month in total; the remaining credits and days are on
the AWS Billing page). To stop spending, stop the instance (the disk and
Elastic IP still cost a little) or terminate it and release the Elastic
IP. On the free plan nothing is ever charged to the card: the account
closes when the credits run out, with 90 days to upgrade and keep the
data.
