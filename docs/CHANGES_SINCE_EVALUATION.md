# Changes since the evaluation

AlphaMind was evaluated at 42 London in October 2026 as the team project
ft_transcendence. That version is tagged
[`v1.0-42-evaluated`](https://github.com/Tuetoo/42_Common_Core_ft_transcendence/tree/v1.0-42-evaluated),
and the README still describes it. This file lists how the current code
differs from that tag. The handbook (`docs/handbook/`) describes the
current code in full; the Gemini and Claude comparison and the caching
measurements are in [MODEL_COMPARISON.md](MODEL_COMPARISON.md).

## LLM providers

- **Anthropic Claude is fully supported.** `LLM_DEFAULT_PROVIDER` selects
  `google` (the code default), `anthropic` or `mistral`. `openai` is also
  accepted for a dormant adapter; it is not tested against the real API,
  and its errors are mapped like Anthropic's. CHEAP uses Haiku 5.5 and
  HIGHEST_ACCURACY uses Sonnet 5.5. Claude is the recommended provider
  (see MODEL_COMPARISON.md).
- **Embeddings stay on Gemini**, so `GOOGLE_API_KEY` is needed whichever
  provider runs the analyses.
- **Anthropic details.** Structured output falls back to a reduced
  schema, then to no schema, when the API calls a schema too complex. A
  used-up credit balance fails the analysis with `llm_credit_exhausted`
  and is not retried. A reply whose thinking used all output tokens
  before any text is retried once. The Anthropic and OpenAI clients make
  no retries of their own (`max_retries=0`); the router decides.
- **Timeouts.** A timed-out call is retried once with every provider
  (evaluated: Google only).
- **Prompt caching on Anthropic.** The system prompt and the ReAct
  scratchpad are cached by default: each ReAct turn after the first
  (except the last allowed turn) reads the previous turn's prompt from
  the cache. The text the model sees is unchanged. MODEL_COMPARISON.md,
  section 11, measures the caching methods.
- **Settings are checked at startup.** A typo in the provider, a tier,
  the effort or a thinking setting stops ai-service with a clear error.
  A blank value means the default; a blank effort sends no effort.
- **Owner scripts** in `services/ai-service/scripts/`:
  `probe_anthropic_schemas.py` (which schema level each agent needs) and
  `probe_anthropic_cache.py` (cache reads and writes per ReAct turn),
  both with the real key; `llm_usage_report.py` (read-only: Anthropic
  calls, tokens, cache use, wall time and cost per analysis and per
  group, from the ai-service logs).

## AI analysis pipeline

- **JSON.** A valid JSON object or list wrapped in extra text, or an
  object with trailing commas, is recovered. A Reflection reply with
  neither `checked_claims` nor `issues` counts as unparseable, not as a
  clean review.
- **SEC filings** are read from the SGML submission first (some 10-Ks
  could not be read before). An amendment filed before the original is
  ignored. `FILING_LLM_TIER` sets the tier of the filing summary and
  filing records calls at ingestion (evaluated: always
  HIGHEST_ACCURACY). The latest-quarter figures appear once in the
  debate evidence. A filing too large to summarize counts as a failed
  source, so the ticker is retried on the next run. A failed refresh of
  SEC's ticker map keeps the previous day's map.
- **Nightly ingestion** checks for an existing file name among system
  documents only (`GET /internal/documents/filenames?system_only=true`),
  so a user's upload with the same name does not stop the system copy.
- **Risk events.** The Risk agent's answer is not rejected because an
  event is worded differently from the tool output. Only events backed
  by a shown filing record or by the cited tool's output are listed as
  sourced; an event that cites a record takes the record's name, and
  duplicates are listed once. When none survive but legal records were
  shown, the events line is built from those records, open matters
  first.
- **Reflection** issues that claim a side ignored the other must quote
  an earlier argument of the other side. An unresolved issue does not
  lower the confidence (evaluated: minus 0.15); it stays in the note and
  in the stored reflections.
- **Confidence note.** `confidence_adjustment_note` states each step
  that lowers the confidence and by how much, the stored confidence
  against the Judge's own, and how many Bull, Bear, Judge or Reflection
  calls a backup model answered.
- **Judge.** If a later Judge pass fails after an earlier valid verdict,
  the analysis keeps that verdict instead of failing, with that verdict's
  own convergence and confidence. A provider timeout of the Judge gets
  the same delayed retries as a rate limit (within the Judge retry
  budget); the Reflection citation review gets one delayed retry. A
  figure rewrite of the Judge's answer that has no leaning or confidence
  is refused, and the first answer is kept. A confidence that is not a
  finite number counts as 0.
- **Tiers.** A low-confidence BUY closes the earlier unfilled BUY tiers
  of that ticker, like HOLD and SELL. Tier monitoring checks every daily
  bar since the recommendation was accepted, not only today's bar, so a
  day without a successful run is checked on the next run (the fill
  price is that day's bar). Position sizing accepts a whole-number
  window written as `14.0`, refuses `true`, and drops tiers whose price
  or quantity is not a finite number. The 50% single-holding limit is
  also applied when some holdings have no live price (measured against
  the priced holdings plus cash).
- **Archived reports.** The near-duplicate check before archiving an
  agent report compares only reports of the same owner (one user, or the
  system).
- **Portfolio backtest.** The stored initial capital is the first value
  of the strategy curve. A sell never sells more than the position
  holds. A backtest whose newest day has no benchmark close yet fails
  with a "run it again later" error.
- **Portfolio tool** shows each holding's weight of net value and of
  holdings value, and lists holdings above the 50% cap.
- **News.** When NewsAPI has no key or no quota, the tool result tells
  the agent not to call `search_news` again (and to use web search
  instead where the News agent has it). The Sentiment agent makes no
  extra LLM call to classify a sentiment label and stores no label.
- **Figure checks** read "13.0 percent" like "13.0%" and "13%" like
  "13.0%". A data agent's "$2.0 million" matches "$2,015,046.00" in its
  own evidence. A figure from an earlier note that matches fresh
  evidence within rounding is not flagged as recycled.
- **Market data.** A new instrument is stored without name, sector and
  exchange (the Yahoo metadata lookup is gone; nothing read those
  fields). The volume tool compares the first session in the data with
  its own open.
- **Experiment switch.** `ANALYSIS_EXPERIMENT_ISOLATION` (off by
  default) stops analyses from reading or writing earlier analyses'
  context, memory, archived notes and knowledge-graph facts.

## Platform and web app

- **Security.** The OAuth return page checks that sign-in started in the
  same browser tab (start and finish on the `FRONTEND_BASE_URL` origin),
  and shows one fixed error text (the reason is in the
  `oauth_callback_failed` log). The analytics CSV export reports a
  failure to the dashboard as a fixed code, so no text from the URL is
  shown.
  2FA codes: at most `RATE_LIMIT_2FA_FAILURES` (5) codes are checked per
  user within one hour (`RATE_LIMIT_2FA_WINDOW_SECONDS`, 3600 s); each
  attempt is counted before the code is checked, so parallel requests
  cannot get past the limit, and a correct code resets the count.
  A user may send at most `RATE_LIMIT_INVITATIONS_PER_USER` (20)
  invitations per hour across all their organizations.
  Invitation links survive a Google or GitHub sign-in and a registration
  (`next` is kept). Open invitations sent by a member who leaves, is
  removed or loses a manager role are deleted, and an invitation whose
  inviter is no longer a manager cannot be accepted. A password reset
  link stops working once the account's email changes. Completing a
  password reset marks the email as verified.
- **Rate limits** also cover the public API (`/api/v1/public`: 5
  requests per second per client address, burst 20, at the gateway) and
  decision reviews (`DECISION_REVIEW_RATE_LIMIT_PER_MINUTE` /
  `_PER_DAY`: 3 per minute and 20 per UTC day per user, 429 beyond).
- **Account deletion.** Deleting a user also deletes their sessions,
  the open invitations they sent and the invitations sent to them. An
  organization left with no member is deleted. core deletes the user's
  private documents, notes, portfolios, watchlists, API keys and
  notifications, and sends them no new notifications. ai deletes their
  analyses, recommendations, decisions, backtests, memories and archived
  reports; an analysis that finishes after its owner (or the analysis
  itself) was deleted archives nothing. `user.deleted` is also sent as a
  durable event. A nightly sweep in core (04:30 New York) and ai (04:40)
  repeats this for every deleted account that still owns data, so a lost
  event or a late write is cleaned the next night; the core sweep also
  cleans the content of organizations that no longer exist in auth, and
  leftover files of deleted users. When more than
  `ORGANIZATION_SWEEP_MAX_MISSING` organizations, or every referenced
  one (more than one), look missing, the sweep deletes nothing and
  fails; `python -m scripts.clean_organizations` cleans named ones. The
  nightly purge-unverified job also removes what older deleted accounts
  still had in auth (sessions, sent invitations, memberships, owned
  organizations), and deletes open invitations that expired more than
  `ORGANIZATION_INVITATION_EXPIRE_DAYS` (7) days ago.
  A deleted owner's organization passes to an active member first. The
  privacy page describes what deletion removes.
  Deleting an organization deletes its documents and notes in core;
  its shared portfolios become private to their owners (or are deleted
  when the owner's account was deleted).
- **Account settings.** `/me` returns only `notification_preferences`
  in `settings`; the unused `theme`, `preferred_currency` and
  `default_model` are ignored in a PATCH. A 2FA secret stored without
  encryption is refused (409) instead of read; an admin resets that
  user's 2FA.
- **Real time.** All WebSocket pages share one hook, and each tab keeps
  one shared live connection (`user:` channel) for the badge,
  notifications, analytics and friends; friends' online status is pushed
  live (the friends page also reloads every 60 s). The unread badge
  re-reads its count after each reconnect. An edit made while the Yjs
  editor saves, or a tab that joins during a save, is not lost.
- **Research assistant.** A streamed answer always ends with `done` or
  `error` within one time limit, which also covers the non-streamed
  fallback. An empty LLM answer is an error (`/rag/ask` answers 503, the
  stream ends with `empty_answer`). The question is embedded once for
  all requested categories; when embedding fails, keyword (BM25) search
  alone is used. In the web app, a stream that ends without `done` shows
  "cut off", and leaving the page cancels the answer.
- **Documents.** A duplicate file name answers 409. A stuck upload is
  reset under a row lock before it is ingested again. A document whose
  ingestion failed is retried by the nightly ingestion only for
  `FAILED_DOCUMENT_RETRY_DAYS` after its first upload. The documents
  page stops polling after 60 minutes without a status change (reload
  to resume).
- **Backtests** have a list page (`/backtests`, `GET /api/v1/backtests`)
  and a sidebar link.
- **Portfolios.** Sharing a portfolio with an organization needs an
  `organization_id` (422 without one) and a writer role in it (403
  otherwise). When a member leaves or is removed, their portfolios shared
  with that organization become private. The no-leverage cash check
  groups by the snapshot day (17:30 New York), not the UTC day. In
  `history.csv`, a holding adjustment is valued at the session close on
  or before its day, the same rule as the portfolio backtest (the cost
  basis only when no close exists). The web app's holdings list does
  not ask ai-service for each holding's latest recommendation
  (`latest_recommendation` is null there; the public API still fills
  it).
- **Analytics.** `GET /api/v1/analytics/agents` rows carry the agent
  name. The decisions list has a stable order for equal timestamps.
- **Nightly review** gives up on a recommendation or decision
  `REVIEW_GIVE_UP_AFTER_DAYS` (30) after its evaluation window ends.
- **Email** sending has a 10 s time limit, and notification emails are
  sent in the background.
- **Scheduler.** The nightly review runs at 00:20 and the backup at
  00:50 New York (evaluated: 02:00 and 02:30), so no job falls in the
  01:00-02:59 daylight-saving change hours. The nightly RAG ingestion
  runs at 03:05 New York (evaluated: 03:00). The two deleted-user sweeps
  are new jobs (04:30, 04:40). One job can be run by hand from the
  scheduler image (DEPLOYMENT.md). cron's notice about jobs skipped
  after the host slept appears in the scheduler logs.
- **Alerts.** `TradingDayJobStale` fires when a trading-day job missed
  one weekday (26 hours after a Monday-Thursday success, 76 hours after
  a Friday or Saturday success, 50 hours after a manual Sunday success)
  instead of after 100 hours. `DailyJobStale` also covers the two
  sweeps. Prometheus scrapes Qdrant, so `ServiceDown` covers it. The
  Grafana request-rate panels leave out `/metrics` and `/health`, and
  the latency panel also leaves out internal routes and RAG streams.
  The decision review route has a 300 s gateway read timeout.
- **Network.** The frontend container is on its own `edge` network with
  the gateway only; it cannot reach the databases or the services
  directly.
- **Builds.** Images and CI install with `uv sync --locked`, so a stale
  `uv.lock` fails the build; CI caches uv downloads. CI workflows have
  read-only permissions. The integration CI starts the stack with
  `APP_ENV=production`, so the production start-up checks run before a
  deploy.
- **Deploy.** The deploy also waits for the newest run on `main` of
  every other workflow, so a failing check on `main` blocks every deploy
  until it passes.
- **Phones.** Form fields use a 16 px font below 768 px, so iOS Safari
  does not zoom in.
- **Web app.** Signed-in pages show "Loading..." while their data loads.
  These pages therefore stream: a missing page answers HTTP 200 with the
  not-found page, and a server redirect happens in the browser (what
  the user sees is the same). "Mark all as read" shows an error when it
  fails. The `npm start` script is gone (the image runs the standalone
  `server.js`).
- **Scripts** read the admin, owner and portfolio ids from the
  environment or a git-ignored `.env.scripts`; there are no built-in
  ids. Without them, the admin is the oldest active admin and the owner
  is the owner of `PORTFOLIO_ID`; `PORTFOLIO_ID` itself is required.
  Tokens stay off the command line. The busy-window guard covers the
  new nightly window (00:15-04:55 New York). Several owner scripts
  exit non-zero on failure, and `scripts.e2e_check` has an opt-in `deletion`
  section that deletes a throwaway account and runs both sweeps.
- Next.js is pinned at 16.3.8 (evaluated: 16.3.3).

## Internal interfaces

- New internal routes: auth `POST /internal/users/deleted`,
  `POST /internal/organizations/missing` and
  `GET /internal/users/{id}/friends`; core and ai
  `POST /internal/scheduler/deleted-user-sweep`. The internal
  notification-settings answer carries `deleted`.
- An event without an `event_id` is no longer de-duplicated by its
  payload in core. The `document.uploaded` payload has no `is_system`,
  and the backtest event payload has no `status` (the event name says
  it).
- auth's start-up sync of the Redis user-state cache also removes keys
  of users that no longer exist.
- The Postgres init script no longer creates the `pgcrypto` extension
  (`gen_random_uuid()` is built in).
- Other changes are refactors and deletions of dead code with no change
  in behaviour (shared helpers for rate limits, cleanup steps and form
  messages; unused functions, fields and settings removed), plus unit
  tests for the behaviour above. There are no new database migrations.

## New settings

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ANTHROPIC_REQUEST_TIMEOUT_SECONDS` | 120 | Time limit of one Anthropic call. |
| `LLM_ANTHROPIC_EFFORT` | `low` | Effort sent to Claude; blank sends none. |
| `LLM_ANTHROPIC_THINKING_CHEAP` | `adaptive` | Thinking mode of the CHEAP model. |
| `LLM_ANTHROPIC_THINKING_HIGHEST_ACCURACY` | `between_tools` | Thinking mode of the HIGHEST_ACCURACY model. |
| `LLM_ANTHROPIC_THINKING_HEADROOM_TOKENS` | 2048 | Extra output room for adaptive thinking. |
| `LLM_ANTHROPIC_SEND_TEMPERATURE` | false | Send a temperature (Claude 5.5 rejects non-default values). |
| `LLM_ANTHROPIC_PROMPT_CACHING` | true | Prompt caching on Anthropic (system prompt and ReAct scratchpad). |
| `LLM_ANTHROPIC_CACHE_SCRATCHPAD` | true | With caching on, also cache the ReAct scratchpad; false caches only the system prompt. |
| `LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST` | false | Experiment switch: send the debate evidence once, as the first cached system block, shared by Bull, Bear, Judge and Reflection. |
| `ANALYSIS_EXPERIMENT_ISOLATION` | false | Experiment switch: analyses neither read nor write earlier analyses' context, memory, archived notes or knowledge-graph facts. |
| `FILING_LLM_TIER` | `highest_accuracy` | Tier of the filing calls at ingestion. |
| `FAILED_DOCUMENT_RETRY_DAYS` | 7 | Days after its first upload that a failed document is still retried nightly; 0 turns the retry off. |
| `REVIEW_GIVE_UP_AFTER_DAYS` | 30 | Days after its window that an unscored item stays in the nightly review. |
| `SMTP_TIMEOUT_SECONDS` | 10 | Time limit of one email send. |
| `ORGANIZATION_SWEEP_MAX_MISSING` | 5 | Most missing organizations the nightly core sweep cleans in one run; above it, it deletes nothing and fails. |
| `DECISION_REVIEW_RATE_LIMIT_PER_MINUTE` / `_PER_DAY` | 3 / 20 | Decision reviews a user may generate per minute / UTC day. |
| `RATE_LIMIT_INVITATIONS_PER_USER` | 20 | Invitations one user may send per hour (`RATE_LIMIT_INVITATIONS_WINDOW_SECONDS`), across all organizations. |

Changed defaults: the Anthropic models are `claude-haiku-5-5` and
`claude-sonnet-5-5`; `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` is 0.85
(evaluated: unset, which meant 0.90); `RATE_LIMIT_2FA_WINDOW_SECONDS` is
3600 (evaluated: 300). In `.env.example`, `SEC_EDGAR_USER_AGENT` is
empty (SEC access stays off until a real name and email are set) and
`LLM_DEFAULT_PROVIDER` lists `anthropic` and `openai`. The provider,
tier, effort and thinking settings accept only their listed values.
Removed: `RATE_LIMIT_2FA_LOCK_SECONDS` (the attempt count itself blocks
until the window ends), and the object storage script variables
`OBJECTSTORE_S3_CONFIG`, `SEAWEEDFS_ENTRYPOINT` and
`OBJECTSTORE_S3_PORT` (fixed values).

## Known limitations

- Judge confidence clusters near 0.55, and a Judge that sees its previous
  verdict mostly repeats it.
- Position sizing is first come, first served within a batch.
- Two stored filing records are wrong (an LLY currency hedge gain read
  as an unusual item, ORCL's restructuring plan understated); fixing them
  needs an extraction prompt change.
- The NewsAPI free tier is small and shared with the scheduled jobs.
- Tier monitoring does not check a session missed on the window's last
  day, because the next run expires the tier first. A tier retried
  after a failed commit is filled at the newer day's price.
- The organization sweep cannot tell a wrong auth environment when
  exactly one organization is referenced.
- The decision review rate limit, like the RAG one, also counts refused
  requests.
- The demo site works only while the AWS instance runs; Google sign-in
  there accepts only registered test users.
