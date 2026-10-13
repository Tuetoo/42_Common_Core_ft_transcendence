# 32. CI/CD: from a pull request to the cloud host

> Every change reaches users through the same path. A developer pushes a
> branch and opens a pull request; GitHub Actions lints, type-checks,
> tests and builds the parts the change touches (continuous
> integration). After the merge, the whole stack is built and started
> once more on a fresh GitHub machine; only if that succeeds, and every
> other workflow run of the same commit succeeded, does a second workflow log in to
> the cloud host over SSH and run `scripts/deploy.sh`, which updates the
> code to exactly that commit, rebuilds the images and
> waits until every service is healthy again (continuous deployment; a
> stopped scheduler stays stopped).
> The host is one AWS virtual machine running the same
> `docker-compose.yaml` as a laptop. Nothing in the application code
> knows whether it runs locally or in the cloud: the differences live in
> files that are never committed. The host's one-time setup is recorded
> in [../DEPLOYMENT.md](../DEPLOYMENT.md); this chapter explains the
> whole chain, from the basic ideas to the commands.

---

## 1. The big picture

```
 developer laptop                     GitHub                                  AWS (eu-north-1)
 ----------------                     ------                                  ----------------
 branch feature/x
   | git push
   v
 pull request  ----------------->  CI on the pull request
 (feature/x -> main)               (only the workflows whose paths changed)
                                     auth-service CI    flake8, mypy, tests, image build
                                     core-service CI    flake8, mypy, tests, image build
                                     ai-service CI      flake8, mypy, tests, image build
                                     stack smoke CI     config checks (infra, compose, scripts)
                                     frontend CI        lint, type check, production build
                                   reviewer reads the diff and the checks
   merge  ------------------------>  main moves forward
                                         |
                                         v
                                   stack smoke CI on main
                                     config checks + build and start the
                                     whole stack, probe the gateway and
                                     the frontend (/login, /healthz)
                                         | success (push to main)
                                         v
                                   deploy workflow
                                     wait for every other check of the
                                     commit (all succeeded or skipped)
                                         |
                                         v
                                     SSH, command = the checked sha
                                     (key that may only run the
                                     deploy script)  ------------------------------------>  EC2 host
                                                                                        scripts/deploy.sh
                                                                                          git merge --ff-only <checked sha>
                                                                                          docker compose up -d --build --wait
                                                                                          (17 containers; a stopped
                                                                                           scheduler stays stopped)
                                                                                              |
 browser  <------------------ https://alphamind-research.duckdns.org:8443 -------------------- gateway
```

Three ideas explain almost everything:

1. **Checks run where nobody's laptop matters.** CI runs on a clean
   GitHub machine every time, so "it works on my machine" is not enough
   to merge: the code must pass on a machine that has nothing but the
   repository.
2. **The same stack everywhere.** Local, CI and cloud all start
   `docker-compose.yaml`. CI and the cloud use only that file; a laptop
   also loads `docker-compose.override.yml` for development
   conveniences.
3. **Configuration outside git.** Secrets, public URLs, the TLS
   certificate and `APP_ENV` differ per machine, so they live in
   `.env.docker` and `infra/gateway/certs/` on each machine and are
   never committed.

---

## 2. Concepts you need

### 2.1 The ideas

| Term | Meaning | In AlphaMind |
|---|---|---|
| **Continuous integration (CI)** | Every proposed change is merged often and checked automatically (build, lint, tests) before it joins the shared branch. | The five `*-ci.yml` workflows on pull requests and pushes to `main`. |
| **Continuous delivery** | Every change that passed CI is ready to be released by one action. | `main` is always deployable; the deploy can also be started by hand. |
| **Continuous deployment (CD)** | Every change that passed the checks is released automatically, with no manual step. | `deploy.yml` runs by itself after a successful stack smoke on `main`, once every other workflow run of the same commit has succeeded or was skipped. Per-service checks run only for the services a commit touches (section 3.4). |
| **Pipeline** | The ordered chain of automated steps from a change to a running release. | Section 1. |
| **Gate** | A step that must succeed before the next may run. | Pull-request checks before the merge; stack smoke and every other check of the commit before the deploy. |
| **Artifact** | What a build produces and the next step uses. | The Docker images, built again on the host from the same commit. |
| **Environment** | One place where the system runs, with its own configuration. | Local (development), CI (throwaway) and cloud (production). |
| **Idempotent** | Running it twice gives the same result as once. | `deploy.sh` on an up-to-date host changes nothing and only re-checks health. |
| **Rollback** | Going back to the previous working version. | Revert the bad commit on `main`; the pipeline deploys the revert (section 6). |

### 2.2 Git and GitHub

| Term | Meaning |
|---|---|
| **Branch** | A named line of commits. `main` is the shared, deployable branch; work happens on `feature/...` branches. |
| **Pull request (PR)** | A request to merge one branch into another, with its diff, discussion and check results on one page. |
| **Merge** | Joining a branch into another. GitHub's merge button creates a merge commit on `main`. |
| **Fast-forward** | Moving a branch forward to a later commit when it has no commits of its own; no merge commit is needed. `git merge --ff-only` refuses anything else. |
| **Status check** | A pass/fail result a workflow reports on a commit or PR (the green tick or red cross). |
| **Branch protection / ruleset** | Repository rules for a branch, for example "no deletion" or "checks must pass before merging". |

### 2.3 GitHub Actions

| Term | Meaning |
|---|---|
| **Workflow** | A YAML file in `.github/workflows/` that describes automated work. |
| **Event (trigger)** | What starts a workflow: `pull_request`, `push`, `workflow_run` (another workflow finished), `workflow_dispatch` (a person clicked "Run workflow"). |
| **Path filter** | `paths:` under an event: the workflow runs only when the change touches one of these paths. |
| **Job** | A group of steps that runs on one machine. Jobs of a workflow run in parallel unless one `needs` another. |
| **Step** | One command (`run:`) or one reusable action (`uses:`) inside a job. |
| **Runner** | The machine that runs a job. `ubuntu-latest` is a fresh GitHub-hosted Linux VM with Docker, thrown away afterwards. |
| **Action** | A reusable step published by someone else, for example `actions/checkout@v4` (clone the repository) or `astral-sh/setup-uv@v3` (install uv). |
| **Secret** | A value stored encrypted in the repository settings, given to a job as `${{ secrets.NAME }}` and masked in logs. |
| **Concurrency group** | At most one run of the group at a time; a new run waits (or cancels the old one). |
| **`if:`** | A condition on a job or step; when false it is skipped (grey, not red). |

### 2.4 Containers and the host

| Term | Meaning |
|---|---|
| **Image** | A packaged file system plus a start command, built from a `Dockerfile`. |
| **Build stage** | One `FROM` section of a multi-stage `Dockerfile`. The services have a `development` stage (tools, source mounted) and a `production` stage (non-root user, code copied in). |
| **Compose** | `docker compose` starts many containers from one YAML file with their networks and volumes. |
| **Health check** | A command Docker runs inside a container to decide "healthy" or "unhealthy". `docker compose up --wait` waits for it. |
| **Virtual machine (EC2 instance)** | A rented computer in a cloud data centre. |
| **Elastic IP** | A fixed public IPv4 address that stays when the instance stops and starts. |
| **Security group** | AWS's firewall for an instance: which ports accept traffic from which addresses. |
| **DNS name** | A human-readable name that resolves to an IP address (`alphamind-research.duckdns.org` -> `16.192.224.66`). |
| **TLS certificate** | Proves to browsers that a server really owns a name and encrypts the connection (HTTPS). Let's Encrypt issues free ones valid 90 days. |

### 2.5 SSH and keys

| Term | Meaning |
|---|---|
| **SSH** | An encrypted remote shell. `ssh -i key user@host` logs in with a private key. |
| **Key pair** | A private key (secret, stays with its owner) and a public key (shareable). The server stores public keys; whoever proves possession of the matching private key may log in. |
| **`authorized_keys`** | The server-side list of public keys allowed to log in as a user. |
| **Forced command** | A `command="..."` prefix on an `authorized_keys` line: whatever the client asks, the server runs only that command for this key. |
| **`known_hosts`** | The client-side list of server public keys. It stops a client from talking to an impostor server. |
| **Deploy key** | A key that GitHub accepts for one repository only, read-only unless write access is ticked. |

---

## 3. How it works in AlphaMind

### 3.1 Branches and the pull-request flow

| Branch | Holds |
|---|---|
| `main` | The shared, deployable state. |
| `feature/backend` | `shared/`, auth-service, core-service, operator scripts, compose files, Makefile, backend CI, platform docs. |
| `feature/ai` | ai-service, the AI handbook chapters, ai-service CI. |
| `feature/tests` | `tests/`. |
| `feature/frontend` | `frontend/` (the Frontend Lead). |
| `feature/deploy` | The deploy workflow, `scripts/deploy.sh`, `docs/DEPLOYMENT.md`. |
| `infra/` | Committed by the team member who owns it. |

A change goes through these steps:

1. Update your branch from `main`: `git checkout main && git pull`, then
   `git checkout -b feature/x` (or `git merge origin/main` into an
   existing feature branch).
2. Commit in small, reviewable pieces; `git push -u origin feature/x`.
3. Open a pull request into `main`. The workflows whose paths changed
   start automatically and report on the PR.
4. Read the diff and the checks. Fix and push again; the checks rerun on
   every push.
5. Merge. `main` moves forward and the post-merge pipeline starts
   (sections 3.3 and 3.4).

Before running anything locally, always know which branch you are on:
`git branch --show-current`.

### 3.2 Continuous integration on pull requests and pushes to `main`

Each service has its own workflow, so a change to core-service does not
wait for the ai-service suite. The `paths:` list of each workflow is
"everything this suite reads": the service, `shared/`, its tests, the
shared test helpers, the Makefile, the dependency files, the workflow
itself, and any repository file a test inspects.

| Workflow | Runs when a PR or a push to `main` touches | Steps |
|---|---|---|
| `auth-service-ci.yml` | `services/auth-service/**`, `shared/**`, `tests/auth-service/**`, `tests/alembic_contract.py`, `tests/auth_tokens.py`, `Makefile`, `pyproject.toml`, `uv.lock`, the workflow | checkout; install uv 0.8.17; `uv sync --frozen`; flake8 of the service, `shared/` and its tests; `make mypy-shared mypy-auth`; `make test-auth`; `docker build ... --target production` |
| `core-service-ci.yml` | the same for core, plus `infra/scheduler/**`, `infra/gateway/nginx.conf` and `infra/prometheus/alert_rules.yml` (its tests read them) | the same with `mypy-core`, `test-core` |
| `ai-service-ci.yml` | the same for ai, plus `infra/scheduler/**`, `infra/vector/**`, `scripts/**`, `.env.example`, `.gitignore`, `.dockerignore` and the auth and core Dockerfiles | the same with `mypy-ai`, `test-ai` |
| `frontend-ci.yml` ("frontend CI") | `frontend/**`, the workflow (and pushes to `main` that touch them) | in `frontend/` with Node 24: `npm ci`; `npm run lint`; `npx next typegen && npx tsc --noEmit` (route types, then the type check); `npm run build` |
| `integration-ci.yml` ("stack smoke CI"), job `config-checks` | `infra/**`, `scripts/**`, both compose files, `.dockerignore`, `frontend/Dockerfile`, `Makefile`, `.env.example`, the workflow (and every push to `main`) | flake8 of `infra/`; the status page's CSP hashes (`csp_hashes.py --check`); `docker compose config -q` for the stack and the restore-drill project; `bash -n` / `sh -n` of every shell script; `make promtool-test` (alert rules and their unit tests) |

What each step protects against:

- **`uv sync --frozen`** installs exactly the versions in `uv.lock` and
  fails if `pyproject.toml` and the lock disagree, so CI never tests
  with different library versions than the images use.
- **flake8** catches style errors, unused imports and undefined names
  (chapter [31](31-testing-quality.md) section 3.11).
- **mypy `--strict`** catches type errors: a wrong argument type, a
  possibly `None` value used as a string, a missing return.
- **The tests** run against a real PostgreSQL and Redis that
  `testcontainers` starts inside the runner's Docker
  (chapter [31](31-testing-quality.md)), so database behaviour is
  tested for real, not mocked.
- **The image build** (`--target production`) proves the `Dockerfile`
  still builds, so a merge cannot break the deployment's build.

A workflow that does not run for a PR (its paths were not touched) shows
no check at all; that is expected, not a failure.

### 3.3 After the merge: stack smoke on `main`

`integration-ci.yml` also runs on every push to `main`, which is what a
merge is. Its `stack-smoke` job runs only for pushes
(`if: github.event_name == 'push'`):

1. Make a throwaway `.env.docker` from `.env.example` and add random
   `GRAFANA_ADMIN_PASSWORD`, `INTERNAL_API_TOKEN` and `JWT_SECRET`.
2. `make certs`: a self-signed certificate for the gateway.
3. `docker compose --env-file .env.docker -f docker-compose.yaml up -d
   --build`: the whole stack, production image stages, no development
   override (the explicit `-f` skips it).
4. Wait up to 5 minutes until `/health/auth`, `/health/core`,
   `/health/ai` and the frontend's `/healthz` answer through the gateway. On timeout it prints the
   container states and the last 100 log lines of every service, then
   fails.
5. Gateway probes: `http://localhost:8000/status` -> 301 (redirect to
   HTTPS), `https://localhost:8443/status` -> 200, `/internal/x` -> 404
   and `/metrics` -> 404 (both closed to the outside), `/login` -> 200
   (the frontend serves its pages) and `/healthz` -> 200 (the frontend's
   server side reaches the API through the gateway's internal listener).
6. Always (also after a failure): `docker compose down -v`.

This is the last gate. Unit tests prove each service alone; the smoke
run proves that the containers start together from this exact commit:
migrations apply on empty databases, health checks pass, the gateway
routes. That is the same thing the deploy will do on the host, so its
success is the condition for starting the deploy workflow, which then
also waits for the other checks of the commit (section 3.4).

### 3.4 The deploy workflow (`.github/workflows/deploy.yml`)

```yaml
on:
  workflow_run:
    workflows: ["stack smoke CI"]
    types: [completed]
    branches: [main]
  workflow_dispatch:
```

- `workflow_run` starts it whenever a "stack smoke CI" run on `main`
  completes, successful or not. `workflow_dispatch` adds the "Run
  workflow" button (Actions -> deploy) for a manual deploy.
- A `workflow_run` workflow always runs the version of `deploy.yml` on
  the default branch, so a pull request cannot change how deployment
  works before it is merged.

```yaml
concurrency:
  group: deploy
  cancel-in-progress: false
```

At most one deploy at a time. Two quick merges produce two runs; the
second waits for the first instead of both changing the host at once.

```yaml
if: >-
  github.event_name == 'workflow_dispatch' ||
  (github.event.workflow_run.conclusion == 'success' &&
   github.event.workflow_run.event == 'push')
```

Deploy only when a person asked for it, or when the smoke run succeeded
and was started by a push to `main`. A failed smoke run leaves the job
skipped (grey), and the host keeps the previous version.

The commit is `SHA`: the smoke run's `head_sha`, or `github.sha` for a
manual run. The job has two steps.

1. **Wait for every check of the commit.** If `SHA` is no longer the
   head of `main`, the job ends without deploying (a newer commit's run
   deploys it). Otherwise it lists every workflow run of that commit
   that was started by a push, except `deploy` itself (per-service CI,
   frontend CI), every 20 seconds for up to 30 minutes. When all have
   finished, it continues only if each one succeeded or was skipped; one
   failure, or a check still running after 30 minutes, fails the job and
   nothing is deployed. Each GitHub API call is tried up to 3 times, 15
   seconds apart.

   What the gate checks, exactly: every workflow run of the deployed
   commit must succeed (or be skipped). It does not look at earlier
   commits. The per-service workflows have path filters, so they run
   only for the services a commit touches. Example: commit W changes
   ai-service and its ai-service CI fails, so W is not deployed. The next
   commit X changes only `docs/`; X has no ai-service CI run, its own runs
   succeed, and X is deployed. The host fast-forwards to X, which
   contains W's code. So a red check on `main` must be fixed (or the
   commit reverted) before anything else is merged.
2. **Deploy over SSH.** It writes the private key from `DEPLOY_SSH_KEY`
   to a file readable only by the runner's user, writes
   `DEPLOY_KNOWN_HOSTS` to `~/.ssh/known_hosts` and runs

```
ssh -i ~/.ssh/deploy_key -o BatchMode=yes -o ServerAliveInterval=30 "ubuntu@$DEPLOY_HOST" "$SHA"
```

The only command sent is the commit sha. It is data, not a command: the
host's forced command runs `scripts/deploy.sh` whatever the client asks,
and the script reads the sha from `SSH_ORIGINAL_COMMAND` and validates it
(sections 3.5 and 3.7).
`BatchMode=yes` makes ssh fail at once instead of waiting for a
password prompt nobody can answer; `ServerAliveInterval=30` keeps the
connection alive during a long image build. The job's output is the
deploy script's output, so the Actions log shows the deployed commit,
the build and the final container table.

### 3.5 The deploy script (`scripts/deploy.sh`)

It runs on the host, from the deploy workflow or by hand
(`bash scripts/deploy.sh` in `~/alphamind`).

| Line | Why |
|---|---|
| `set -euo pipefail` | Stop at the first failing command, treat unset variables as errors, and let a failure inside a pipe count. A half-done deploy must not report success. |
| `cd "$(dirname "$0")/.."` | Run from the repository root wherever it is called from. |
| `export COMPOSE_FILE=docker-compose.yaml` | Use only the production compose file, never the development override, even if the shell profile was not loaded (the forced SSH command starts a non-interactive shell). |
| `[ -f .env.docker ]`, `[ -f infra/gateway/certs/server.crt ]` | Refuse to start without the host's configuration and certificate, with a clear message instead of a half-started stack. |
| `target=origin/main`, then `SSH_ORIGINAL_COMMAND` | From the deploy workflow, sshd passes the sha the client sent in `SSH_ORIGINAL_COMMAND`. Only `^[0-9a-f]{40}$` is accepted, anything else exits 1; the sha becomes the target. By hand there is no SSH command, and the target stays `origin/main`. |
| `git fetch --quiet origin main` | Download the latest `main` from GitHub with the read-only deploy key. |
| `git merge-base --is-ancestor <target> origin/main` | Refuse a commit that is not on `main` ("is not a commit on origin/main"). |
| `git merge --ff-only --quiet <target>` | Move the checkout forward to exactly the checked commit, never to a newer `main` whose checks may still run. `--ff-only` refuses if the host has local commits or edited tracked files, so a hand-made change on the host is never silently merged or overwritten (section 6). |
| `echo "deploying ..."` | Put the commit hash and title in the log. |
| `ps -q --status running scheduler` | Remember whether the scheduler runs before the deploy. The cloud host keeps it stopped on purpose (its cron jobs spend the LLM and news quotas the cloud shares with the local stack), and a deploy must not change that. |
| `docker compose --env-file .env.docker up -d --build --remove-orphans --wait --wait-timeout 300` | Rebuild images whose inputs changed, recreate only containers whose image or configuration changed, remove containers of services deleted from the compose file, then wait up to 5 minutes until every container is running and healthy. Any failure makes the script, and so the workflow, fail. When the scheduler was running, this runs for every service. |
| `build scheduler`, then the same `up` with the service list from `config --services` without `scheduler` | When the scheduler was not running: its image is still rebuilt (a later `up -d scheduler` runs the new code), but only the other services are started, so no cron job can fire during the `--wait` window. The list is read with a `while read` loop, not `mapfile`, so the script also runs under the bash 3.2 that macOS ships (its test runs it there). |
| `stop scheduler` and the message "scheduler was not running before the deploy: kept stopped" | A guard in case a future dependency starts it anyway; the message says how to start it. |
| `docker image prune -f` | Delete image layers no container uses, so repeated builds do not fill the 40 GiB disk. |
| `docker compose ... ps` | Show the final state in the log. |

Because compose recreates only what changed, a documentation-only merge
rebuilds nothing and restarts nothing; a change to ai-service restarts
ai-service (and an analysis that was running in it is lost, so avoid
merging during a demo or a batch).

### 3.6 The host

| Path or setting | What it is | In git? |
|---|---|---|
| `/home/ubuntu/alphamind` | The checkout of `main`. | yes (the code) |
| `/home/ubuntu/alphamind/.env.docker` | Production settings: public URLs, `APP_ENV=production`, the host's own `JWT_SECRET`, `INTERNAL_API_TOKEN`, `TOTP_ENCRYPTION_KEY`, provider keys. Mode 600. | no (`.gitignore`) |
| `/home/ubuntu/alphamind/infra/gateway/certs/server.crt`, `server.key` | The Let's Encrypt certificate copied where the gateway reads it. | no (`.gitignore`) |
| `/etc/letsencrypt/` | certbot's account, certificates and renewal settings. | no |
| `/etc/letsencrypt/renewal-hooks/deploy/alphamind.sh` | After a renewal: copy the new certificate and reload nginx. | no |
| `~/.bashrc`: `export COMPOSE_FILE=docker-compose.yaml` | Interactive commands (`make health`, `scripts/*.sh`) skip the development override too. | no |
| `/swapfile` (2 GiB) | Safety margin for memory peaks during builds. | no |
| Docker volumes (`postgres_data`, `qdrant_data`, ...) | The cloud's data. A deploy never touches them. | no |

The security group allows 22 (SSH, from anywhere because GitHub's
runners have changing addresses; only keys work), 80 (Let's Encrypt's
check), 8443 (the gateway) and 443 (open, unused). Developer tools
(MailHog, Prometheus, Qdrant, Alertmanager) listen on the host's
`127.0.0.1` only and are reached through an SSH tunnel
([../DEPLOYMENT.md](../DEPLOYMENT.md) section 6).

### 3.7 Every key and secret in the chain

| Key | Private part lives in | Public part lives in | Lets | Cannot |
|---|---|---|---|---|
| `alphamind-key.pem` (EC2 key pair) | the owner's Mac (`~/Downloads`) | the host's `authorized_keys` | the owner log in as `ubuntu` | - (full shell; keep it safe) |
| `github_deploy` (read-only deploy key) | the host (`~/.ssh/github_deploy`) | GitHub repository -> Settings -> Deploy keys | the host `git fetch` the repository | push, read other repositories |
| `gha_deploy` (deploy workflow key) | GitHub secret `DEPLOY_SSH_KEY` only (deleted from the host after setup) | the host's `authorized_keys`, behind `command="bash /home/ubuntu/alphamind/scripts/deploy.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty` | GitHub run the deploy script; the sha it sends is only data that the script validates | open a shell, run any other command, forward ports |
| host key (`/etc/ssh/ssh_host_ed25519_key`) | the host | GitHub secret `DEPLOY_KNOWN_HOSTS` | the runner verify it reached the real host | - |

Other GitHub secrets: `DEPLOY_HOST` = `alphamind-research.duckdns.org`.

If the deploy key leaks, the worst an attacker can do is deploy a commit
that is already on `main` again. To replace it: generate a new pair on the host,
replace its `authorized_keys` line and the `DEPLOY_SSH_KEY` secret.

### 3.8 The certificate

- Issued by Let's Encrypt for `alphamind-research.duckdns.org` with
  `certbot certonly --standalone`: certbot briefly runs its own web
  server on port 80, Let's Encrypt fetches a token from it over the
  internet, and that proves the name points to this machine.
- The gateway listens on host ports 8443 and 8000 (the same mapping as
  locally), so port 80 stays free for renewals while the stack runs.
- The certificate names only `alphamind-research.duckdns.org`. The
  frontend's server side therefore does not call `https://gateway`; it
  uses the gateway's internal plain-HTTP listener
  (`BACKEND_API_URL=http://gateway:8080/api/v1`, Docker network only), so
  no certificate has to name `gateway`
  ([10](10-gateway.md) section 3.3).
- certbot's systemd timer checks twice a day and renews 30 days before
  expiry; the deploy hook copies the new files and runs `nginx -s
  reload` in the gateway container. `sudo certbot renew --dry-run`
  tests the whole chain.

### 3.9 Local and cloud side by side

| | Local | Cloud |
|---|---|---|
| Start | `make up` | the deploy workflow, or `bash scripts/deploy.sh` on the host |
| Compose files | `docker-compose.yaml` + override | `docker-compose.yaml` only |
| Image stage | `development`, source bind-mounted | `production`, code copied into the image, non-root user |
| Code change takes effect | `docker compose restart <service>` (or `scripts/rebuild.sh`) | merge to `main` |
| `APP_ENV` | `development` (weak secrets allowed, with a warning) | `production` (a weak or missing secret stops the service) |
| Address | `https://localhost:8443` (self-signed) | `https://alphamind-research.duckdns.org:8443` (Let's Encrypt) |
| Data | your local volumes | the host's volumes; replaced by restoring a local backup when wanted |
| Scheduler | running: the cron jobs and the nightly backup | stopped: no scheduled job, no automatic backup |

The two never share data and can run at the same time. They do share the
LLM and data-provider keys, so their quotas: do not run analysis batches
on both at once.

The cloud host keeps the scheduler stopped, so it makes no backups and
runs no scheduled jobs; its stale-job alerts fire (chapter
[26](26-scheduler-jobs.md) section 3.11). Backups are made on the local
stack, nightly or by hand with `make backup` there. To show local data
on the cloud, copy a local backup to the host's `backups/` and restore
it there ([../DEPLOYMENT.md](../DEPLOYMENT.md) section 4).
`make restore BACKUP=<name> CONFIRM=yes` ends with `docker compose up -d`,
which also starts the scheduler, so stop it again right after the
restore:

```
make restore BACKUP=<YYYY-MM-DD_HHMM> CONFIRM=yes
docker compose --env-file .env.docker stop scheduler
```

Later deploys keep the scheduler in the state they find.

---

## 4. Configuration

| Setting | Where | Value | Meaning |
|---|---|---|---|
| `DEPLOY_HOST` | GitHub secret | `alphamind-research.duckdns.org` | where the deploy job connects |
| `DEPLOY_SSH_KEY` | GitHub secret | private key | the restricted deploy key |
| `DEPLOY_KNOWN_HOSTS` | GitHub secret | `alphamind-research.duckdns.org ssh-ed25519 AAAA...` | the host's public key |
| `COMPOSE_FILE` | host `~/.bashrc`, `scripts/deploy.sh` | `docker-compose.yaml` | skip the development override |
| `APP_ENV` | host `.env.docker` | `production` | strict secret checks (chapter [29](29-security.md)) |
| `OAUTH_REDIRECT_BASE_URL`, `FRONTEND_BASE_URL`, `BACKEND_CORS_ORIGINS` | host `.env.docker` | `https://alphamind-research.duckdns.org:8443` | public address in OAuth callbacks, email links and CORS |
| `JWT_SECRET`, `INTERNAL_API_TOKEN` | host `.env.docker` | 64 hex characters each | signing and internal-call secrets, different from local |
| `TOTP_ENCRYPTION_KEY` | host `.env.docker` | Fernet key | 2FA secret encryption, required in production |
| `--wait-timeout` | `scripts/deploy.sh` | 300 s | how long a deploy waits for health |
| uv version | every CI workflow | 0.8.17 | the same uv everywhere |
| Stack smoke health wait | `integration-ci.yml` | 60 x 5 s | how long CI waits for the stack |

---

## 5. Design decisions and why

- **One virtual machine running Docker Compose, not Kubernetes or a
  managed container service.** The stack already runs as one compose
  project; moving it unchanged keeps local and cloud identical, needs no
  new tooling, and fits a student budget. The price: no automatic
  failover, and a deploy restarts changed services in place (a few
  seconds of downtime for that service).
- **Build images on the host, not in a registry.** CI already proves the
  images build (each service's `docker build` step and the smoke run);
  building again on the host avoids a container registry, its storage
  quota and registry credentials on the host. The price: a deploy takes
  a few minutes of host CPU, and the host's images are not
  byte-identical to the ones CI built (same commit and lock file, so the
  same code and versions).
- **Deploy only after stack smoke succeeded on `main` and every other
  workflow run of the same commit succeeded.** The smoke run does on a fresh
  machine exactly what the host will do. If it fails, deploying would
  most likely break the cloud too. The other checks (tests, lint, type
  checks) run in parallel workflows, so the deploy waits for them, and
  it deploys exactly the commit it checked. The gate does not look at
  earlier commits, and per-service checks run only for the services a
  commit touches, so a red check on `main` is fixed before the next
  merge (section 3.4).
- **Pull over SSH with a forced command, not push files.** The host
  pulls from GitHub with a read-only key, and GitHub may only trigger
  that pull. Neither side holds a key that can do more than its job.
- **`git merge --ff-only`, not `git reset --hard`.** A hand edit on the
  host makes the deploy fail loudly instead of disappearing.
- **Keep port 8443.** The gateway, `nginx.conf`'s redirect and every
  script already use it; using 443 would need configuration changes for
  a cosmetic gain.
- **Configuration outside git.** Secrets in a repository are copied to
  every clone forever. `.env.docker` and the certificate live only on
  the machine that uses them.
- **`APP_ENV=production` in the cloud.** The services refuse to start
  with a weak or missing `JWT_SECRET`, `INTERNAL_API_TOKEN` or
  `TOTP_ENCRYPTION_KEY`, so a misconfigured host fails safe instead of
  running on development defaults on the internet.
- **AWS with DuckDNS.** Google Cloud's signup refused the owner's card;
  AWS's free account plan worked and cannot charge the card. DuckDNS is
  free, instant and on the Public Suffix List, so Let's Encrypt and
  Google OAuth accept it; a personal domain was kept for other use.
- **No automatic data migration.** The cloud starts empty; data moves
  only through an explicit backup and restore, so a deploy can never
  overwrite data.

---

## 6. When things go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| The deploy job is grey (skipped) after a merge | stack smoke failed, or was not a push to `main` | open the stack smoke run and fix its failure; or deploy by hand (Actions -> deploy -> Run workflow) if you are sure |
| The deploy job is red at "Wait for every check of the commit" | another check of the commit failed, or did not finish in 30 minutes | fix the failing check; a re-run that turns green does not start a deploy by itself, so then use Actions -> deploy -> Run workflow |
| The deploy job ends with "skip: ... is not the head of main" | a newer commit reached `main` first | nothing to do: the newer commit's run deploys it |
| Code whose check failed on an earlier commit is live | a later commit that does not touch that service passed its own checks and was deployed; the gate checks only the deployed commit's runs | fix the failing check (or revert the commit) on `main`; the next deploy carries the fix |
| `error: the SSH command must be a 40-hex commit sha` / `is not a commit on origin/main` | the client sent something other than a commit of `main` | check the workflow's `SHA`; by hand, run the script without a command |
| `Host key verification failed` | `DEPLOY_KNOWN_HOSTS` does not match the host (new instance, or a typo) | on the host: `echo "alphamind-research.duckdns.org $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"`, update the secret |
| `Permission denied (publickey)` | `DEPLOY_SSH_KEY` incomplete (missing the BEGIN/END lines) or its public key not in `authorized_keys` | recreate the key pair (section 3.7) |
| `Connection timed out` on port 22 | security group allows SSH only from one address | set the SSH rule's source to `0.0.0.0/0` |
| `error: .env.docker is missing` / `server.crt is missing` | new checkout or a deleted file | restore them ([../DEPLOYMENT.md](../DEPLOYMENT.md) section 2) |
| `fatal: Not possible to fast-forward, aborting.` | someone committed or edited a tracked file on the host | on the host: `git status`; keep the change in a branch and PR it, then `git checkout -- <file>` (or `git reset --hard origin/main` if the change is not wanted) and deploy again |
| `container ... is unhealthy` / `--wait` timeout | a service fails to start in production (often a missing or weak secret, or a failed migration) | on the host: `docker compose --env-file .env.docker logs --tail 100 <service>`; fix the configuration or revert the commit |
| `no space left on device` during the build | old images and build cache | `docker system df`, then `docker builder prune -f` and `docker image prune -af` |
| The browser warns about the certificate | renewal failed, or the copy hook did not run | `sudo certbot certificates`, `sudo certbot renew --dry-run`, check the hook file |
| The site does not open but the host is healthy | DNS (DuckDNS) or the security group | `dig +short alphamind-research.duckdns.org` must print the Elastic IP; port 8443 must be open |
| A bad change is live | it passed every check but is wrong | revert it on `main` (GitHub's "Revert" button on the merged PR, then merge the revert PR); the pipeline deploys the previous code |
| The CI checks are red only on GitHub | a file the tests read is missing in the repository (it is ignored or not committed), or the runner differs from your machine | read the failing step's last lines; reproduce with the same `make` target locally on a clean clone |

---

## 7. Demo walkthrough

1. **Show the pipeline definition.** Open `.github/workflows/` and explain
   the five CI workflows and `deploy.yml` (sections 3.2-3.4).
2. **Make a harmless change.** On a new branch, edit one sentence in a
   handbook chapter, push and open a pull request. Show that only the
   workflows whose paths changed run (a doc change triggers none of the
   service workflows).
3. **Show a real check.** Open a recent PR that touched a service: the
   checks list, a green run, and its steps (flake8, mypy, tests, image
   build).
4. **Merge and follow the pipeline.** In Actions, show "stack smoke CI"
   on `main` starting, then "deploy" starting after it; open the deploy
   log: the first step lists the other checks of the commit until all
   have passed, the second shows the commit line and the container
   table.
5. **Show the result.** Open
   `https://alphamind-research.duckdns.org:8443/` (the frontend's login
   page), `/status` and a Swagger page (`/docs/core/`); show the valid
   certificate in the browser's address bar.
6. **Show the host.** `ssh -i ~/Downloads/alphamind-key.pem
   ubuntu@alphamind-research.duckdns.org`, then `cd ~/alphamind && git
   log --oneline -1` (the deployed commit) and `make health`.
7. **Explain safety.** The restricted deploy key (`authorized_keys`
   line), configuration outside git, `APP_ENV=production`, and
   revert-based rollback.

---

## 8. Check your understanding

1. What is the difference between continuous delivery and continuous
   deployment, and which one does AlphaMind use?
   *Delivery: every passing change is ready to release by one action.
   Deployment: it is released automatically. AlphaMind deploys
   automatically after stack smoke and every other workflow run of the
   commit succeeded, and keeps a manual button.*
2. Why does a pull request that changes only `docs/` show no service
   checks?
   *The service workflows have path filters; `docs/` is in none of
   them.*
3. Why is the deploy gated on stack smoke and not only on the unit
   tests?
   *Unit tests check each service alone; stack smoke starts the whole
   stack from that commit the same way the host will.*
4. A deploy fails with "Not possible to fast-forward". What happened,
   and why is failing better than overwriting?
   *Someone changed tracked files or committed on the host. Failing
   keeps that change visible instead of silently losing it.*
5. What can an attacker do with a leaked `DEPLOY_SSH_KEY`?
   *Only run `scripts/deploy.sh`, which deploys a commit that is
   already on `main` (the commit the workflow checked, or `origin/main`
   when no sha is sent).*
6. Why are `.env.docker` and the certificate not in git?
   *They hold secrets and per-machine values; anything committed is
   copied to every clone forever.*
7. How do you roll back a bad release?
   *Revert the merge on `main` through a PR; the pipeline deploys the
   previous code. The data volumes are not touched.*
8. Why does the cloud refuse to start with a weak `JWT_SECRET` while the
   laptop only warns?
   *`APP_ENV=production` turns the warning into an error, so a public
   server never runs with development secrets.*
9. Which data does a deploy change?
   *None: it replaces containers, not volumes. Data moves only through
   backup and restore.*

---

## 9. Code map

| File | What it holds |
|---|---|
| `.github/workflows/auth-service-ci.yml`, `core-service-ci.yml`, `ai-service-ci.yml` | Pull-request CI per service. |
| `.github/workflows/frontend-ci.yml` | Lint, type check and build of `frontend/`. |
| `.github/workflows/integration-ci.yml` | Config checks on PRs; config checks and the full stack smoke (with `/login` and `/healthz`) on `main`. |
| `.github/workflows/deploy.yml` | Continuous deployment over SSH: the wait for every workflow run of the commit, then the sha sent to the host. |
| `scripts/deploy.sh` | The deploy on the host. |
| `docker-compose.yaml` | The stack, used everywhere. |
| `docker-compose.override.yml` | Local development only. |
| `services/*/Dockerfile` | `development` and `production` stages. |
| `Makefile` | `mypy-*`, `test-*`, `promtool-test`, `certs`, `health` targets the workflows and the host use. |
| `.env.example` | Every setting, without secrets; the template for each machine's `.env.docker`. |
| `shared/config.py`, `services/auth-service/config.py` | The production checks on `JWT_SECRET`, `INTERNAL_API_TOKEN`, `TOTP_ENCRYPTION_KEY`. |
| `infra/gateway/nginx.conf`, `infra/gateway/gen-certs.sh` | The gateway, its port 8443 and the local self-signed certificate. |
| [../DEPLOYMENT.md](../DEPLOYMENT.md) | The host's setup, data moves, operations, cost. |

---

## 10. Related chapters

- [01-architecture.md](01-architecture.md): the containers the pipeline
  builds and starts.
- [02-operations.md](02-operations.md): the operator commands, which work
  the same on the host.
- [10-gateway.md](10-gateway.md): TLS, ports and the status page the
  smoke run probes.
- [29-security.md](29-security.md): secrets, `APP_ENV` checks and
  exposure.
- [31-testing-quality.md](31-testing-quality.md): the test suites,
  flake8 and mypy that CI runs.
- [../DEPLOYMENT.md](../DEPLOYMENT.md) and
  [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md): the host and moving
  data with backups.
