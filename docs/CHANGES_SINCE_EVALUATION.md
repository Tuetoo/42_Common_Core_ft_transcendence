# Changes since the evaluation

AlphaMind was evaluated at 42 London in October 2026 as the team project
ft_transcendence. That version is tagged
[`v1.0-42-evaluated`](https://github.com/Tuetoo/42_Common_Core_ft_transcendence/tree/v1.0-42-evaluated),
and the README still describes it. This file lists how the current code
differs from that tag. The handbook (`docs/handbook/`) describes the
current code in full; the Gemini and Claude comparison is in
[MODEL_COMPARISON.md](MODEL_COMPARISON.md).

## LLM providers

- **Anthropic Claude is fully supported.** `LLM_DEFAULT_PROVIDER` selects
  `google` (the code default), `anthropic` or `mistral` (`openai` is also
  accepted for a dormant adapter, whose errors are mapped like
  Anthropic's). CHEAP uses Haiku 5.5 and
  HIGHEST_ACCURACY uses Sonnet 5.5. Claude is the recommended provider
  (see MODEL_COMPARISON.md).
- **Embeddings stay on Gemini**, so `GOOGLE_API_KEY` is needed whichever
  provider runs the analyses.
- **Anthropic details.** Structured output falls back to a reduced
  schema, then to no schema, when the API calls a schema too complex.
  The system prompt is cached (about 16% cheaper in the batch runs). A used-up credit
  balance fails the analysis with `llm_credit_exhausted` and is not
  retried. A timed-out call is retried once, as for Google.
- **Settings are checked at startup.** A typo in the provider, tier,
  effort or thinking settings stops ai-service with a clear error; a
  blank value means the default.
- `services/ai-service/scripts/probe_anthropic_schemas.py` checks which
  schema level each agent needs with the real key.

## AI analysis pipeline

- **JSON.** A valid JSON object wrapped in extra text, or with trailing
  commas, is recovered. A Reflection reply without its required fields
  counts as unparseable, not as a clean review.
- **SEC filings** are read from the SGML submission first (some 10-Ks
  could not be read before). `FILING_LLM_TIER` sets the tier of the
  filing calls at ingestion. The latest-quarter figures reach the debate
  once.
- **Risk events.** The Risk agent's answer is not rejected because an
  event is worded differently from the tool output. Only events backed
  by a shown record or by the cited tool's output are listed as sourced.
  When none survive but legal records were shown, the events line is
  built from those records.
- **Reflection** issues that claim a side ignored the other must quote
  the ignored argument. An unresolved issue no longer lowers the
  confidence; it stays in the note and in the stored reflections.
- **Confidence note.** `confidence_adjustment_note` explains every step
  that lowers the confidence, and names a backup model when one
  answered.
- **Judge.** If a later Judge pass fails after an earlier valid verdict,
  the analysis keeps that verdict instead of failing.
- **Tiers.** A low-confidence BUY closes the earlier unfilled BUY tiers
  of that ticker, as HOLD and SELL already did.
- **Portfolio tool** shows each holding's weight and lists holdings
  above the 50% cap.
- **News.** When NewsAPI has no quota or no key, the agent is told not
  to search again. A Sentiment agent whose searches all failed stores no
  label instead of "neutral".
- **Figure checks** read "13.0 percent" like "13.0%", and a data agent's
  "$2.0 million" matches "$2,015,046.00" in its own evidence.

## Platform and web app

- **Security.** The OAuth return page checks that sign-in started in the
  same browser tab (start and finish on the `FRONTEND_BASE_URL` origin).
  Invitation links survive a Google or GitHub sign-in; invitations from
  a removed admin stop working. A password reset link stops working
  after an admin changes the email.
- **Rate limits** also cover the public API (`/api/v1/public`) and
  decision reviews (3 per minute and 20 per day per user).
- **Account deletion.** Deleting a user now also removes their sessions
  and invitations. An organization left with no member is deleted. core
  deletes the user's private documents, notes, portfolios, watchlists,
  API keys and notifications, and sends them no new notifications. ai
  deletes their analyses, recommendations, decisions, backtests,
  memories and archived reports. A nightly sweep in core (04:30 New
  York) and ai (04:40) repeats this for every deleted account that still
  owns data, so a lost event or a late write is cleaned the next night;
  the core sweep also cleans the content of organizations that no
  longer exist in auth, and leftover files of deleted users; when too
  many organizations look missing it stops, and
  `python -m scripts.clean_organizations` cleans named ones. The nightly
  purge-unverified job also removes what older deleted accounts still
  had in auth (sessions, invitations, memberships, owned organizations).
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
  live. The Yjs editor no longer loses an edit when a
  tab joins during a save.
- **Research assistant.** A streamed answer always ends with `done` or
  `error` within one time limit. A duplicate file name answers 409.
- **Documents.** A stuck upload is reset under a row lock before it is
  ingested again. A document whose ingestion failed is retried by the
  nightly ingestion only for `FAILED_DOCUMENT_RETRY_DAYS` after its first
  upload. The documents page stops polling after 60 minutes without a
  status change (reload to resume).
- **Backtests** have a list page (`/backtests`, `GET /api/v1/backtests`).
- **Recommendations.** The 50% single-holding limit works when some
  prices are missing; the nightly review gives up on items that can
  never be scored.
- **Email** sending has a 10 s time limit.
- **Scheduler.** The nightly RAG ingestion runs at 03:05 New York. One
  job can be run by hand from the scheduler image (DEPLOYMENT.md).
- **Phones.** Form fields use a 16 px font below 768 px, so iOS Safari
  does not zoom in.
- **Scripts** read the admin, owner and portfolio ids from the
  environment or a git-ignored `.env.scripts`.
- Next.js is pinned at 16.3.8.

## New settings

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ANTHROPIC_REQUEST_TIMEOUT_SECONDS` | 120 | Time limit of one Anthropic call. |
| `LLM_ANTHROPIC_EFFORT` | `low` | Effort sent to Claude; blank sends none. |
| `LLM_ANTHROPIC_THINKING_CHEAP` | `adaptive` | Thinking mode of the CHEAP model. |
| `LLM_ANTHROPIC_THINKING_HIGHEST_ACCURACY` | `between_tools` | Thinking mode of the HIGHEST_ACCURACY model. |
| `LLM_ANTHROPIC_THINKING_HEADROOM_TOKENS` | 2048 | Extra output room for adaptive thinking. |
| `LLM_ANTHROPIC_SEND_TEMPERATURE` | false | Send a temperature (Claude 5.5 rejects non-default values). |
| `LLM_ANTHROPIC_PROMPT_CACHING` | true | Cache the system prompt. |
| `FILING_LLM_TIER` | `highest_accuracy` | Tier of the filing calls at ingestion. |
| `FAILED_DOCUMENT_RETRY_DAYS` | 7 | Days after its first upload that a failed document is still retried nightly; 0 turns the retry off. |
| `REVIEW_GIVE_UP_AFTER_DAYS` | 30 | Days after its window that an unscored item stays in the nightly review. |
| `SMTP_TIMEOUT_SECONDS` | 10 | Time limit of one email send. |
| `ORGANIZATION_SWEEP_MAX_MISSING` | 5 | Most missing organizations the nightly core sweep cleans in one run; above it, it deletes nothing and fails. |
| `DECISION_REVIEW_RATE_LIMIT_PER_MINUTE` / `_PER_DAY` | 3 / 20 | Decision reviews a user may generate per minute / UTC day. |

Changed defaults: the Anthropic models are `claude-haiku-5-5` and
`claude-sonnet-5-5`; `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` is 0.85.

## Known limitations

- Judge confidence clusters near 0.55, and a Judge that sees its previous
  verdict mostly repeats it.
- Position sizing is first come, first served within a batch.
- Two stored filing records are wrong (an LLY currency hedge gain read
  as an unusual item, ORCL's restructuring plan understated); fixing them
  needs an extraction prompt change.
- The NewsAPI free tier is small and shared with the scheduled jobs.
- The demo site works only while the AWS instance runs; Google sign-in
  there accepts only registered test users.
